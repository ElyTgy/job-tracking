"""Ingest a live Tesla careers pull done through the user's own Chrome.

tesla.com sits behind Akamai bot management that 403s every automated fetch
(Playwright, curl, a cloud routine) but lets a real, human-owned Chrome session
through. So Tesla is pulled interactively: Claude drives the user's Chrome via
the Claude-in-Chrome extension, reads the careers app's state JSON, and hands
the diff to this module, which applies the same bookkeeping as
run_check.check_company (classify + tag new postings, refresh survivors,
count misses / close vanished ones, record a company-scoped run).

Protocol (see docs/tesla_pull_routine.md for the full prompt):

    python -m scraper.tesla_pull due [--hours 40]
        -> exit 0 if the last pull is older than 40h (so a daily schedule runs
           every other day and catches up after missed days), else exit 1.

    python -m scraper.tesla_pull known
        -> comma-separated req ids the DB already tracks. Paste these INTO the
           page JS so it returns only the diff (Chrome JS output truncates at
           ~1KB, so never dump the whole list out of the page).

    python -m scraper.tesla_pull ingest pull.json [--dry-run]
        pull.json = {"gone": ["<req id>", ...],
                     "new":  [{"key": "<req id>", "title": "...", "dept": "...",
                               "loc": "<City, California>"}, ...]}  (loc optional; defaults to Palo Alto)
        gone = ids from `known` that are no longer live on the page
        new  = live Palo Alto intern listings whose id was not in `known`
        Everything tracked that is neither new nor gone is treated as still live.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from scraper import classify, db

COMPANY_ID = 69
LOCATION = "Palo Alto, California"
JOB_URL = "https://www.tesla.com/careers/search/job/{key}"
AGG_DEPT = "via SimplifyJobs list"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_id(url_key: str) -> str:
    """'https://www.tesla.com/careers/search/job/281097' -> '281097'."""
    tail = url_key.rstrip("/").rsplit("/", 1)[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return tail if tail.isdigit() else digits[-6:]


def known_ids(conn) -> tuple[set[str], set[str]]:
    """(numeric-keyed open req ids, req ids covered by aggregator URL-keyed rows)."""
    numeric = {
        r["posting_key"]
        for r in conn.execute(
            "SELECT posting_key FROM postings WHERE company_id=? AND closed=0 "
            "AND posting_key NOT LIKE 'http%'",
            (COMPANY_ID,),
        ).fetchall()
    }
    agg = set()
    for r in conn.execute(
        "SELECT posting_key FROM postings WHERE company_id=? AND closed=0 "
        "AND posting_key LIKE 'http%'",
        (COMPANY_ID,),
    ).fetchall():
        t = _tail_id(r["posting_key"])
        if t:
            agg.add(t)
    return numeric, agg


def cmd_due(args) -> int:
    """Exit 0 (prints 'due') if Tesla hasn't been pulled in --hours; else exit 1.
    Lets a daily schedule behave as every-other-day and self-heal after missed days,
    exactly like run_check's 40h guard."""
    conn = db.connect()
    row = conn.execute("SELECT last_checked FROM companies WHERE id=?", (COMPANY_ID,)).fetchone()
    last = row["last_checked"] if row else None
    if last:
        age_h = (datetime.now(timezone.utc)
                 - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                 ).total_seconds() / 3600
        if age_h < args.hours:
            print(f"not due: last pull {age_h:.1f}h ago (< {args.hours}h)")
            return 1
        print(f"due: last pull {age_h:.1f}h ago")
    else:
        print("due: never pulled")
    return 0


def cmd_known(_args) -> int:
    conn = db.connect()
    numeric, agg = known_ids(conn)
    print(",".join(sorted(numeric | agg, key=int)))
    return 0


def cmd_ingest(args) -> int:
    with open(args.file) as f:
        pull = json.load(f)
    gone = {str(k).strip() for k in pull.get("gone", []) if str(k).strip()}
    new_rows = []
    for row in pull.get("new", []):
        key = str(row.get("key") or row.get("id") or "").strip()
        title = " ".join(str(row.get("title") or row.get("t") or "").split())
        dept = str(row.get("dept") or row.get("department") or "").strip()
        loc = str(row.get("loc") or row.get("location") or LOCATION).strip()
        if key and title:
            new_rows.append((key, title, dept, loc))

    now = _now()
    conn = db.connect()
    cfg = classify.load_config()
    numeric, agg = known_ids(conn)
    live = (numeric - gone) | {k for k, _, _, _ in new_rows}

    plan = {"run_at": now, "known": len(numeric), "gone": sorted(gone),
            "new": len(new_rows), "live_after": len(live)}
    if args.dry_run:
        print("DRY RUN — nothing written")
        print(json.dumps(plan, indent=2))
        for key, title, dept, loc in new_rows:
            ok = classify.is_internship(title, cfg) and not classify.is_degree_excluded(title, cfg)
            tag, hits = classify.tag_posting(title, dept, cfg)
            print(f"  {'+' if ok else 'x'} {key}  [{tag if ok else 'dropped by classify'}]  {title}  ({dept}; {loc})")
        return 0

    cur = conn.execute("INSERT INTO runs (started, scope) VALUES (?, 'company')", (now,))
    run_id = cur.lastrowid

    inserted, refreshed_new, skipped_agg, skipped_classify = 0, 0, 0, []
    for key, title, dept, loc in new_rows:
        if not classify.is_internship(title, cfg) or classify.is_degree_excluded(title, cfg):
            skipped_classify.append(title)
            continue
        tag, hits = classify.tag_posting(title, dept, cfg)
        loc_ok = 1 if classify.location_ok(loc, cfg) else 0
        existing = conn.execute(
            "SELECT id FROM postings WHERE company_id=? AND posting_key=?",
            (COMPANY_ID, key),
        ).fetchone()
        if existing:  # previously closed and reposted, or `known` was stale
            conn.execute(
                "UPDATE postings SET last_seen=?, closed=0, misses=0, tag=?, tag_hits=?, loc_ok=?, location=? WHERE id=?",
                (now, tag, hits, loc_ok, loc, existing["id"]),
            )
            refreshed_new += 1
        elif key in agg:
            skipped_agg += 1
        else:
            conn.execute(
                """INSERT INTO postings (company_id, posting_key, title, url, location,
                   department, posted_date, tag, tag_hits, loc_ok, first_seen, last_seen, is_new)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (COMPANY_ID, key, title, JOB_URL.format(key=key),
                 loc, dept, "", tag, hits, loc_ok, now, now),
            )
            inserted += 1

    # Survivors: everything tracked that the page did not report as gone.
    refreshed = 0
    for r in conn.execute(
        "SELECT id, posting_key FROM postings WHERE company_id=? AND closed=0 "
        "AND posting_key NOT LIKE 'http%' AND last_seen<?",
        (COMPANY_ID, now),
    ).fetchall():
        if r["posting_key"] in live:
            conn.execute(
                "UPDATE postings SET last_seen=?, closed=0, misses=0 WHERE id=?",
                (now, r["id"]),
            )
            refreshed += 1

    # Same miss/close bookkeeping as check_company (aggregator rows exempt).
    conn.execute(
        "UPDATE postings SET misses=misses+1 WHERE company_id=? AND closed=0 AND pinned=0 "
        "AND department IS NOT ? AND last_seen<?",
        (COMPANY_ID, AGG_DEPT, now),
    )
    closed = conn.execute(
        "UPDATE postings SET closed=1 WHERE company_id=? AND closed=0 AND pinned=0 "
        "AND department IS NOT ? AND misses>=2",
        (COMPANY_ID, AGG_DEPT),
    ).rowcount
    at_one_miss = conn.execute(
        "SELECT COUNT(*) AS n FROM postings WHERE company_id=? AND closed=0 AND pinned=0 "
        "AND department IS NOT ? AND misses>0",
        (COMPANY_ID, AGG_DEPT),
    ).fetchone()["n"]

    conn.execute(
        "UPDATE companies SET last_checked=?, last_check_status='ok' WHERE id=?",
        (now, COMPANY_ID),
    )
    conn.execute(
        "UPDATE runs SET finished=?, companies_checked=1, companies_failed=0, "
        "new_postings=?, closed_postings=?, notes=? WHERE id=?",
        (_now(), inserted, closed,
         "Tesla live pull via user's Chrome (Akamai blocks automation)", run_id),
    )
    conn.commit()

    print(f"run {run_id} @ {now}")
    print(f"inserted {inserted} new, refreshed {refreshed + refreshed_new} existing, "
          f"skipped {skipped_agg} already tracked via aggregator")
    print(f"closed now: {closed}; at 1 miss (close next pull if still gone): {at_one_miss}")
    if skipped_classify:
        print("dropped by classify filters:")
        for t in skipped_classify:
            print("  -", t)
    new_by_tag = conn.execute(
        "SELECT tag, COUNT(*) n FROM postings WHERE company_id=? AND first_seen=? GROUP BY tag",
        (COMPANY_ID, now),
    ).fetchall()
    print("new by tag:", {r["tag"]: r["n"] for r in new_by_tag})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scraper.tesla_pull", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("due", help="exit 0 if a pull is due (last pull older than --hours)")
    d.add_argument("--hours", type=float, default=40)
    sub.add_parser("known", help="print tracked req ids to paste into the page JS")
    p = sub.add_parser("ingest", help="apply a pull.json diff to the DB")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args(argv)
    return {"due": cmd_due, "known": cmd_known, "ingest": cmd_ingest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
