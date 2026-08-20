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

from . import adapters, classify, db


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
    postings = fetcher(company["feed_url"] or company["careers_url"])
    internships = [p for p in postings if classify.is_internship(p["title"], cfg)]

    new_count = 0
    seen_keys = set()
    for p in internships:
        if p["posting_key"] in seen_keys:
            continue
        seen_keys.add(p["posting_key"])
        tag, hits = classify.tag_posting(p["title"], p["department"], cfg)
        existing = conn.execute(
            "SELECT id FROM postings WHERE company_id=? AND posting_key=?",
            (company["id"], p["posting_key"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE postings SET last_seen=?, closed=0, tag=?, tag_hits=? WHERE id=?",
                (run_started, tag, hits, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO postings (company_id, posting_key, title, url, location,
                   department, posted_date, tag, tag_hits, first_seen, last_seen, is_new)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
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
                    run_started,
                    run_started,
                ),
            )
            new_count += 1

    # Anything for this company not seen this run has disappeared from the feed.
    closed = conn.execute(
        "UPDATE postings SET closed=1 WHERE company_id=? AND closed=0 AND last_seen<?",
        (company["id"], run_started),
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
