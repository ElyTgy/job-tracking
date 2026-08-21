"""The recurring job: fetch every company's feed, keep internship postings,
tag them, diff against the DB, and record a run log.

Usage:
    python -m scraper.run_check [--company NAME] [--force]

--force skips the 40-hour spacing guard (the launchd job runs daily; this guard
is what turns "daily" into "every other day" while catching up after sleep).
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import adapters, aggregators, audit, classify, db, export_control


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recently_ran(conn, hours: float = 40.0) -> bool:
    row = conn.execute(
        "SELECT started FROM runs WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return False
    last = datetime.strptime(row["started"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return datetime.now(timezone.utc) - last < timedelta(hours=hours)


def check_company(conn, company, cfg, run_started: str) -> dict:
    fetcher = adapters.FETCHERS[company["ats_type"]]
    # A company may have several boards on the same ATS (e.g. a separate
    # university/early-careers board): feed_url holds them joined by " | ".
    postings = []
    for feed in (company["feed_url"] or company["careers_url"]).split(" | "):
        postings.extend(fetcher(feed.strip()))
    internships = [
        p
        for p in postings
        if classify.is_internship(p["title"], cfg)
        and not classify.is_degree_excluded(p["title"], cfg)
    ]

    new_count = 0
    seen_keys = set()
    needs_body = []   # postings whose feed carried no description; fetched below
    for p in internships:
        if p["posting_key"] in seen_keys:
            continue
        seen_keys.add(p["posting_key"])
        tag, hits = classify.tag_posting(p["title"], p["department"], cfg)
        loc_ok = 1 if classify.location_ok(p["location"], cfg) else 0
        # Eligibility gates (ITAR / US-person / visa sponsorship) live in the
        # description, which several ATS feeds hand us for free in this same call.
        body = export_control.to_text(p.get("description") or "")
        gates = export_control.classify(body) if len(body) > 200 else None
        existing = conn.execute(
            "SELECT id FROM postings WHERE company_id=? AND posting_key=?",
            (company["id"], p["posting_key"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE postings SET last_seen=?, closed=0, misses=0, tag=?, tag_hits=?, loc_ok=? WHERE id=?",
                (run_started, tag, hits, loc_ok, existing["id"]),
            )
            if gates:
                conn.execute(
                    "UPDATE postings SET export_status=?, export_regime=?, visa_sponsorship=?,"
                    " export_evidence=? WHERE id=?", (*gates, existing["id"]))
            elif not conn.execute("SELECT export_status FROM postings WHERE id=?",
                                  (existing["id"],)).fetchone()["export_status"]:
                needs_body.append((existing["id"], p["url"]))
        else:
            conn.execute(
                """INSERT INTO postings (company_id, posting_key, title, url, location,
                   department, posted_date, tag, tag_hits, loc_ok, first_seen, last_seen, is_new)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    company["id"],
                    p["posting_key"],
                    p["title"],
                    p["url"],
                    p["location"],
                    p["department"],
                    p["posted_date"],
                    tag,
                    hits,
                    loc_ok,
                    run_started,
                    run_started,
                ),
            )
            new_count += 1
            pid = conn.execute(
                "SELECT id FROM postings WHERE company_id=? AND posting_key=?",
                (company["id"], p["posting_key"])).fetchone()["id"]
            if gates:
                conn.execute(
                    "UPDATE postings SET export_status=?, export_regime=?, visa_sponsorship=?,"
                    " export_evidence=? WHERE id=?", (*gates, pid))
            else:
                needs_body.append((pid, p["url"]))

    # Feeds that ship no description need one page fetch per posting. Only ever
    # done for postings with no stored verdict, so this stays cheap after run one.
    if needs_body:
        export_control.enrich_missing(conn, needs_body)

    # Anything for this company not seen this run has disappeared from the feed.
    # Boards are flaky (pagination hiccups, rate limits), so a posting is only
    # closed after it has been missing on two consecutive runs -- and never when
    # the feed came back completely empty, which almost always means the scrape
    # itself failed rather than every job being pulled.
    closed = 0
    if postings:
        conn.execute(
            "UPDATE postings SET misses=misses+1 WHERE company_id=? AND closed=0 AND pinned=0 "
            "AND department IS NOT 'via SimplifyJobs list' AND last_seen<?",
            (company["id"], run_started),
        )
        closed = conn.execute(
            "UPDATE postings SET closed=1 WHERE company_id=? AND closed=0 AND pinned=0 "
            "AND department IS NOT 'via SimplifyJobs list' AND misses>=2",
            (company["id"],),
        ).rowcount
    conn.execute(
        "UPDATE companies SET last_checked=?, last_check_status='ok' WHERE id=?",
        (run_started, company["id"]),
    )
    return {"new": new_count, "closed": closed, "total": len(seen_keys)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", help="check just this one company (by name substring)")
    ap.add_argument("--force", action="store_true", help="ignore the 40h spacing guard")
    args = ap.parse_args()

    conn = db.connect()
    if not args.force and not args.company and recently_ran(conn):
        print("Last run was <40h ago; skipping (use --force to override).")
        return 0

    cfg = classify.load_config()
    q = "SELECT * FROM companies WHERE discovery_status='ok' AND ats_type IS NOT NULL"
    params: tuple = ()
    if args.company:
        q += " AND name LIKE ?"
        params = (f"%{args.company}%",)
    companies = conn.execute(q, params).fetchall()
    if not companies:
        print("No companies ready to check (discovery_status='ok').")
        return 0

    run_started = _now()
    # NEW badge always means "appeared in the latest completed run".
    if not args.company:
        conn.execute("UPDATE postings SET is_new=0")
    cur = conn.execute("INSERT INTO runs (started) VALUES (?)", (run_started,))
    run_id = cur.lastrowid
    conn.commit()

    checked = failed = new_total = closed_total = 0
    failures = []
    for c in companies:
        try:
            r = check_company(conn, c, cfg, run_started)
            new_total += r["new"]
            closed_total += r["closed"]
            checked += 1
            print(f"  ok    {c['name']}: {r['total']} internships ({r['new']} new)")
        except Exception as e:  # noqa: BLE001 — one bad feed must not kill the run
            failed += 1
            msg = f"{type(e).__name__}: {e}"[:200]
            failures.append(f"{c['name']}: {msg}")
            conn.execute(
                "UPDATE companies SET last_checked=?, last_check_status=? WHERE id=?",
                (run_started, f"error:{msg}", c["id"]),
            )
            print(f"  FAIL  {c['name']}: {msg}", file=sys.stderr)
        conn.commit()

    if not args.company:
        agg = aggregators.run(conn, cfg, run_started)
        new_total += agg["new"]
        print(f"  aggregators: {agg['matched']} rows matched tracked companies ({agg['new']} new)")
        try:
            audit.main_inline(conn)
        except Exception as e:  # noqa: BLE001
            print(f"  audit failed: {e}", file=sys.stderr)

    conn.execute(
        "UPDATE runs SET finished=?, companies_checked=?, companies_failed=?, "
        "new_postings=?, closed_postings=?, notes=? WHERE id=?",
        (_now(), checked, failed, new_total, closed_total, "; ".join(failures), run_id),
    )
    conn.commit()
    print(
        f"\nRun done: {checked} checked, {failed} failed, "
        f"{new_total} new postings, {closed_total} closed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
