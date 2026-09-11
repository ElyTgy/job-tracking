"""Whole-page change watcher for programmes that have no job feed (ats_type='watch').

Some programmes (e.g. the CERN openlab summer student programme) announce openings
by editing a plain web page rather than posting to a careers board. A 'watch'
company's feed_url is that page. On every check the page is fetched, reduced to
its visible text (scripts/head stripped, one block-level element per line), and
compared against the newest snapshot in watch_snapshots. The first fetch just
records the baseline; any later change stores a new snapshot together with a
unified diff against the previous one, and files a synthetic pinned posting so
the change reaches the board and the email digest through the normal pipeline
(tag='relevant' puts it at the top of the digest; pinned so no feed logic can
ever auto-close it).

run_check routes watch companies here before its ATS-fetcher path, so none of
the classify/close bookkeeping applies to them.

CLI (snapshots and diffs live in the DB, not in files):
    python -m scraper.watch list                      # watched pages + snapshot counts
    python -m scraper.watch diff [--company NAME] [-n N]   # show the latest N diffs
"""
import argparse
import difflib
import hashlib
import html as html_lib
import re
import sys

from . import adapters, db

# Words in the ADDED diff lines that suggest applications opened; used only to
# make the posting title louder, never to suppress a notification -- every
# change is reported regardless.
_OPEN_RE = re.compile(
    r"apply now|applications?\s+(?:are|is)?\s*(?:now\s+)?open\b"
    r"|call for applications[^.]{0,80}\bopen\b|registration[^.]{0,40}\bopen\b",
    re.I,
)


def page_text(raw: str) -> str:
    """Visible text of an HTML page, one block-level element per line.

    Deliberately dumb (regex, no parser dependency): good enough for diffing as
    long as it is deterministic for the same page bytes. Verified stable across
    repeated fetches of openlab.cern before this shipped.
    """
    s = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?is)<head[^>]*>.*?</head>", " ", s)
    s = re.sub(r"(?is)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    lines = (re.sub(r"\s+", " ", l).strip() for l in s.split("\n"))
    return "\n".join(l for l in lines if l)


def check(conn, company, run_started: str) -> dict:
    """Fetch the watched page, snapshot on change, file a posting for the diff.

    Returns the same {new, closed, total} dict as run_check.check_company.
    """
    url = (company["feed_url"] or company["careers_url"]).strip()
    text = page_text(adapters._request("GET", url).text)
    digest = hashlib.sha256(text.encode()).hexdigest()

    prev = conn.execute(
        "SELECT * FROM watch_snapshots WHERE company_id=? ORDER BY id DESC LIMIT 1",
        (company["id"],),
    ).fetchone()

    new_count = 0
    if prev is None or prev["hash"] != digest:
        diff = None
        if prev is not None:
            diff = "\n".join(
                difflib.unified_diff(
                    prev["content"].split("\n"), text.split("\n"),
                    fromfile=f"page @ {prev['fetched_at']}",
                    tofile=f"page @ {run_started}",
                    lineterm="",
                )
            )
        conn.execute(
            "INSERT INTO watch_snapshots (company_id, fetched_at, hash, content, diff)"
            " VALUES (?,?,?,?,?)",
            (company["id"], run_started, digest, text, diff),
        )
        if prev is not None:
            added = "\n".join(
                l[1:] for l in diff.split("\n")
                if l.startswith("+") and not l.startswith("+++")
            )
            first_added = next((l for l in added.split("\n") if l.strip()), "")
            if _OPEN_RE.search(added):
                title = "APPLICATIONS MAY BE OPEN — watched page changed"
            elif first_added:
                title = f"Page changed: “{first_added[:110]}”"
            else:
                title = "Page changed (content removed)"
            # One posting per page version: the hash keys it, so the same change
            # can never be filed or emailed twice, while every distinct change is.
            conn.execute(
                """INSERT OR IGNORE INTO postings
                   (company_id, posting_key, title, url, location, department,
                    posted_date, tag, tag_hits, loc_ok, pinned, first_seen, last_seen, is_new)
                   VALUES (?,?,?,?,?,?,?,?,?,1,1,?,?,1)""",
                (
                    company["id"], f"watch:{digest[:16]}", title, url, "",
                    "page watch", run_started[:10], "relevant", "page-watch",
                    run_started, run_started,
                ),
            )
            new_count = 1

    conn.execute(
        "UPDATE companies SET last_checked=?, last_check_status='ok' WHERE id=?",
        (run_started, company["id"]),
    )
    return {"new": new_count, "closed": 0, "total": new_count}


# ------------------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="watched pages and their snapshot counts")
    d = sub.add_parser("diff", help="show the most recent stored diffs")
    d.add_argument("--company", help="filter by company name substring")
    d.add_argument("-n", type=int, default=1, help="how many diffs back (default 1)")
    args = ap.parse_args()

    conn = db.connect()
    if args.cmd == "list":
        rows = conn.execute(
            """SELECT c.name, c.feed_url, c.last_checked, COUNT(s.id) AS snaps,
                      MAX(s.fetched_at) AS latest
               FROM companies c LEFT JOIN watch_snapshots s ON s.company_id=c.id
               WHERE c.ats_type='watch' GROUP BY c.id ORDER BY c.name"""
        ).fetchall()
        if not rows:
            print("No watched pages (ats_type='watch').")
        for r in rows:
            print(f"{r['name']}: {r['snaps']} snapshot(s), latest {r['latest'] or 'never'}"
                  f"\n  {r['feed_url']}")
        return 0

    q = """SELECT c.name, s.fetched_at, s.diff FROM watch_snapshots s
           JOIN companies c ON c.id=s.company_id WHERE s.diff IS NOT NULL"""
    params: tuple = ()
    if args.company:
        q += " AND c.name LIKE ?"
        params = (f"%{args.company}%",)
    rows = conn.execute(q + " ORDER BY s.id DESC LIMIT ?", (*params, args.n)).fetchall()
    if not rows:
        print("No diffs recorded yet (only baselines).")
    for r in rows:
        print(f"===== {r['name']} @ {r['fetched_at']} =====\n{r['diff']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
