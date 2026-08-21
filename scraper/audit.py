"""Coverage audit: catch careers pages the per-company feed doesn't cover.

Two checks, both read-only (they only record findings in companies.audit_note):

1. sibling boards — the company site links to an ATS board (Greenhouse, Lever,
   Ashby, Rippling, BambooHR, ...) whose slug is NOT in the registered feed.
   Tenstorrent's separate `tenstorrentuniversity` Greenhouse board is the
   canonical case; Radiant/Mesh/Boom were slug collisions caught the same way.
2. student pages — site links whose text/path says university / students /
   early careers / internships; their ATS links are checked the same way.

Usage: python -m scraper.audit [--company NAME]
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

from . import adapters, db

KW = re.compile(r"univers|student|campus|early[- ]?career|early[- ]?talent|emerging|intern|co-?op|new[- ]?grad", re.I)
ATS_HOST = re.compile(
    r"(boards\.greenhouse\.io|job-boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|"
    r"apply\.workable\.com|\.recruitee\.com|smartrecruiters\.com|myworkdayjobs\.com|"
    r"bamboohr\.com|jobs\.gem\.com|ats\.rippling\.com|pinpointhq\.com)", re.I)
SLUG = re.compile(
    r"(?:greenhouse\.io/(?:embed/job_app\?for=)?|lever\.co/|ashbyhq\.com/|workable\.com/|"
    r"smartrecruiters\.com/|gem\.com/|rippling\.com/)([A-Za-z0-9_.-]+)|https?://([a-z0-9-]+)\.(?:recruitee|bamboohr)\.com", re.I)
NOISE = {"embed", "careers", "jobs", "legal", "recruiting", "developers", "product", "blog", "customers", "about", "technology", "company", "cart", "solutions", "team", "updates", "roadmap", "safety", "research", "applications", "privacy-policy", "terms-of-use", "accessibility", "sitemap", "reviews", "book-online", "products"}


def _get(url: str) -> str:
    try:
        r = httpx.get(url, headers=adapters.UA, timeout=15, follow_redirects=True)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _links(html: str, base: str):
    for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        yield urljoin(base, m.group(1)), text


def audit_company(c) -> list[str]:
    site = c["website"] or ""
    if not site:
        return []
    site = site if site.startswith("http") else f"https://{site}"
    host = urlparse(site).netloc.replace("www.", "")
    feed = (c["feed_url"] or "").lower().replace("%20", " ")
    pages = [site]
    if c["careers_url"] and not ATS_HOST.search(c["careers_url"]):
        pages.append(c["careers_url"])
    findings, seen = [], set()
    i = 0
    while i < len(pages) and i < 8:
        url = pages[i]
        i += 1
        for href, text in _links(_get(url), url):
            if href in seen:
                continue
            seen.add(href)
            if ATS_HOST.search(href):
                m = SLUG.search(href)
                slug = next((g for g in m.groups() if g), None) if m else None
                if not slug or slug.lower() in NOISE:
                    continue
                if slug.lower().replace("%20", " ") in feed:
                    continue
                findings.append(f"{slug} ({text[:40]}) via {url}")
            elif (KW.search(text) or KW.search(urlparse(href).path)) and \
                    urlparse(href).netloc.replace("www.", "").endswith(host):
                pages.append(href)
    return findings


def main_inline(conn, company: str | None = None) -> int:
    """Run the audit against an open connection; returns number flagged."""
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN audit_note TEXT")
    except Exception:
        pass
    q = "SELECT * FROM companies WHERE discovery_status<>'dead' AND website IS NOT NULL AND website<>''"
    params: tuple = ()
    if company:
        q += " AND name LIKE ?"
        params = (f"%{company}%",)
    rows = conn.execute(q, params).fetchall()
    flagged = 0
    with ThreadPoolExecutor(16) as ex:
        for c, findings in zip(rows, ex.map(audit_company, rows)):
            note = "; ".join(dict.fromkeys(findings)) if findings else None
            if note:
                flagged += 1
                print(f"  ? {c['name']}: {note[:200]}")
            conn.execute("UPDATE companies SET audit_note=? WHERE id=?", (note, c["id"]))
    conn.commit()
    print(f"  audit: {flagged} companies link to boards not in their feed")
    return flagged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company")
    args = ap.parse_args()
    conn = db.connect()
    main_inline(conn, args.company)
    return 0


if __name__ == "__main__":
    sys.exit(main())
