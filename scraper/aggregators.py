"""Safety-net pass: cross-reference crowd-sourced internship lists against the
companies we track. Catches employers that block scrapers (Tesla), post only
on LinkedIn, or use an ATS we can't read.

Source: SimplifyJobs / PittCSC "Summer Internships" GitHub list (updated daily).
Only rows whose company matches one of OUR tracked companies are kept.
"""
import html as html_lib
import re

from . import adapters, classify
from .ingest import normalize

SIMPLIFY_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md",
]

ALIASES = {  # list name -> our company name, where they differ
    "commaai": "comma.ai",
    "appliedintuition": "Applied Intuition",
    "amazon": "Amazon",
    "googledeepmind": "Google DeepMind",
}


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()


def parse_simplify(md: str):
    """Yield {company, title, location, url} for every row in the README tables."""
    last_company = None
    for tr in re.findall(r"<tr>(.*?)</tr>", md, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 4:
            continue
        company = _clean(tds[0]).replace("🔥", "").strip()
        if company in ("", "↳"):
            company = last_company
        else:
            last_company = company
        if not company:
            continue
        title = _clean(tds[1])
        location = ", ".join(x for x in (_clean(p) for p in re.split(r"<br\s*/?>", tds[2])) if x)
        links = re.findall(r'href="([^"]+)"', tds[3])
        apply_url = next((u for u in links if "simplify.jobs/p/" not in u), links[0] if links else None)
        if not apply_url:
            continue
        apply_url = re.sub(r"[?&](utm_source|ref)=[^&]*", "", apply_url).rstrip("?&")
        yield {"company": company, "title": title, "location": location, "url": apply_url}


def run(conn, cfg, run_started: str) -> dict:
    by_norm = {r["normalized_name"]: r for r in conn.execute(
        "SELECT id, name, normalized_name FROM companies WHERE discovery_status != 'dead'")}
    by_name = {r["name"]: r for r in by_norm.values()}
    matched = new = 0
    for src in SIMPLIFY_URLS:
        try:
            md = adapters._request("GET", src).text
        except Exception as e:  # noqa: BLE001 — optional source; never break the run
            print(f"  aggregator {src.split('/')[-3]}: {type(e).__name__}")
            continue
        for row in parse_simplify(md):
            norm = normalize(row["company"])
            comp = by_norm.get(norm) or by_name.get(ALIASES.get(norm, ""))
            if not comp:
                continue
            # 🎓 in the list = "advanced degree required" — same as our PhD/MS exclusion
            if "🎓" in row["title"] or classify.is_degree_excluded(row["title"], cfg):
                continue
            matched += 1
            # our own feed already has this job (same title, still open)? then skip
            dup = conn.execute(
                """SELECT 1 FROM postings WHERE company_id=? AND closed=0 AND posting_key!=?
                   AND department!='via SimplifyJobs list' AND lower(trim(title))=lower(trim(?))""",
                (comp["id"], row["url"], row["title"]),
            ).fetchone()
            if dup:
                continue
            tag, hits = classify.tag_posting(row["title"], "", cfg)
            loc_ok = 1 if classify.location_ok(row["location"], cfg) else 0
            key = row["url"]
            existing = conn.execute(
                "SELECT id FROM postings WHERE company_id=? AND posting_key=?", (comp["id"], key)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE postings SET last_seen=?, closed=0, tag=?, tag_hits=?, loc_ok=? WHERE id=?",
                    (run_started, tag, hits, loc_ok, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO postings (company_id, posting_key, title, url, location, department,
                       posted_date, tag, tag_hits, loc_ok, first_seen, last_seen, is_new)
                       VALUES (?,?,?,?,?,?,'',?,?,?,?,?,1)""",
                    (comp["id"], key, row["title"], row["url"], row["location"],
                     "via SimplifyJobs list", tag, hits, loc_ok, run_started, run_started),
                )
                new += 1
    # rows that dropped off the list (or now duplicate a feed posting) close here
    conn.execute(
        "UPDATE postings SET closed=1 WHERE department='via SimplifyJobs list' AND closed=0 AND last_seen<?",
        (run_started,),
    )
    conn.commit()
    return {"matched": matched, "new": new}
