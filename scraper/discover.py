"""Careers-page / ATS discovery.

Two modes:
  probe  — automatic, free: guess ATS slugs from the company name and hit each
           public API; also sniff the company site's /careers page for ATS links.
  set    — record a manually/agent-found result:
           python -m scraper.discover set "Company" --ats greenhouse --feed URL [--careers URL]

Probe covers most tech companies; the remainder get discovery_status='needs_manual'
and are resolved by web-search agents feeding `set`.
"""
import argparse
import re
import sys

import httpx

from . import adapters, db

UA = adapters.UA


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    joined = base.replace(" ", "")
    hyphen = base.replace(" ", "-")
    first = base.split()[0] if base.split() else joined
    return list(dict.fromkeys([joined, hyphen, first]))


def api_url(ats: str, slug: str) -> str:
    return {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        "recruitee": f"https://{slug}.recruitee.com/api/offers/",
    }[ats]


def board_url(ats: str, slug: str) -> str:
    return {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
        "workable": f"https://apply.workable.com/{slug}/",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{slug}",
        "recruitee": f"https://{slug}.recruitee.com/",
    }[ats]


def feed_has_jobs(ats: str, feed: str) -> bool:
    try:
        return len(adapters.FETCHERS[ats](feed)) > 0
    except Exception:
        return False


ATS_LINK_PATTERNS = [
    ("greenhouse", r"boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"job-boards\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9%_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([A-Za-z0-9-]+)\.recruitee\.com"),
]


def sniff_site(website: str):
    """Fetch the site's careers-ish pages and look for embedded ATS links.
    Returns (ats, slug, page_url) or (None, None, careers_page_or_None)."""
    if not website:
        return None, None, None
    origin = website if website.startswith("http") else f"https://{website}"
    origin = origin.rstrip("/")
    for path in ("/careers", "/jobs", "/careers/", "/company/careers", "/join-us", ""):
        url = origin + path
        try:
            r = httpx.get(url, headers=UA, timeout=15, follow_redirects=True)
            if r.status_code >= 400:
                continue
        except Exception:
            continue
        final = str(r.url)
        # careers page may itself already be a hosted board or a workday tenant
        wd = re.search(r"https://([a-z0-9]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", final + " " + r.text)
        if wd:
            tenant, wdn, site = wd.groups()
            feed = f"https://{tenant}.{wdn}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            return "workday", feed, final
        for ats, pat in ATS_LINK_PATTERNS:
            m = re.search(pat, final + " " + r.text)
            if m:
                return ats, m.group(1), final
        if path:  # reached a real careers page but no known ATS
            return None, None, final
    return None, None, None


def probe_company(conn, company) -> str:
    name = company["name"]
    # 1) sniff the company website for an ATS embed (most precise)
    ats, slug_or_feed, page = sniff_site(company["website"])
    if ats == "workday":
        if feed_has_jobs("workday", slug_or_feed):
            conn.execute(
                "UPDATE companies SET ats_type='workday', feed_url=?, careers_url=?, "
                "discovery_status='ok' WHERE id=?",
                (slug_or_feed, page, company["id"]),
            )
            return "workday"
    elif ats:
        feed = api_url(ats, slug_or_feed)
        if feed_has_jobs(ats, feed):
            conn.execute(
                "UPDATE companies SET ats_type=?, feed_url=?, careers_url=?, "
                "discovery_status='ok' WHERE id=?",
                (ats, feed, page or board_url(ats, slug_or_feed), company["id"]),
            )
            return ats

    # 2) blind slug guessing against each public API
    for slug in slug_candidates(name):
        for ats in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"):
            feed = api_url(ats, slug)
            if feed_has_jobs(ats, feed):
                conn.execute(
                    "UPDATE companies SET ats_type=?, feed_url=?, careers_url=?, "
                    "discovery_status='ok' WHERE id=?",
                    (ats, feed, board_url(ats, slug), company["id"]),
                )
                return ats

    # 3) careers page exists but custom/JS — try the HTML adapter on it
    if page:
        try:
            jobs = adapters.fetch_html(page)
        except Exception:
            jobs = []
        conn.execute(
            "UPDATE companies SET ats_type='html', feed_url=?, careers_url=?, "
            "discovery_status=? WHERE id=?",
            (page, page, "ok" if jobs is not None else "needs_manual", company["id"]),
        )
        return "html"

    conn.execute(
        "UPDATE companies SET discovery_status='needs_manual' WHERE id=?", (company["id"],)
    )
    return "needs_manual"


def cmd_probe(args):
    conn = db.connect()
    q = "SELECT * FROM companies WHERE discovery_status IN ('pending','needs_manual')"
    if args.company:
        q = "SELECT * FROM companies WHERE name LIKE ?"
        rows = conn.execute(q, (f"%{args.company}%",)).fetchall()
    else:
        rows = conn.execute(q).fetchall()
    counts: dict[str, int] = {}
    for c in rows:
        result = probe_company(conn, c)
        conn.commit()
        counts[result] = counts.get(result, 0) + 1
        print(f"  {c['name']}: {result}")
    print(f"\nProbe summary: {counts}")


def cmd_set(args):
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM companies WHERE name LIKE ?", (f"%{args.company}%",)
    ).fetchone()
    if not row:
        # allow registering a brand-new company on the fly
        norm = re.sub(r"[^a-z0-9]", "", args.company.lower())
        conn.execute(
            "INSERT INTO companies (name, normalized_name, sources) VALUES (?,?, 'manual')",
            (args.company, norm),
        )
        row = conn.execute(
            "SELECT * FROM companies WHERE normalized_name=?", (norm,)
        ).fetchone()
    ats = args.ats
    feed = args.feed
    if ats != "html" and not feed_has_jobs(ats, feed):
        print(f"WARNING: feed returned no jobs for {row['name']} ({feed})", file=sys.stderr)
        if not args.allow_empty:
            return 1
    conn.execute(
        "UPDATE companies SET ats_type=?, feed_url=?, careers_url=COALESCE(?, careers_url), "
        "discovery_status='ok' WHERE id=?",
        (ats, feed, args.careers, row["id"]),
    )
    conn.commit()
    print(f"set {row['name']}: {ats} -> {feed}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--company")
    p.set_defaults(fn=cmd_probe)
    s = sub.add_parser("set")
    s.add_argument("company")
    s.add_argument("--ats", required=True, choices=list(adapters.FETCHERS))
    s.add_argument("--feed", required=True)
    s.add_argument("--careers")
    s.add_argument("--allow-empty", action="store_true")
    s.set_defaults(fn=cmd_set)
    args = ap.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
