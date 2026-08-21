"""Fetch job-description text for postings and flag US export-control / citizenship gates.

    python -m scraper.export_control fetch          # US postings (default)
    python -m scraper.export_control fetch --all    # every open posting
    python -m scraper.export_control classify       # re-classify cached text, no network
    python -m scraper.export_control report

Why this exists: ITAR / EAR / "U.S. Person" requirements are never in the job title,
only in the description body. For a Canadian citizen without US permanent residency
these are hard blockers, so they change which postings are worth applying to.

Descriptions come from the ATS feed when it carries them (greenhouse ?content=true,
ashby descriptionPlain, jibe description) and otherwise from the posting page, via
JSON-LD JobPosting when present and visible text as a fallback.
"""
import argparse
import concurrent.futures as cf
import html as H
import json
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .db import connect

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
TIMEOUT = 40.0

DETAILS_SCHEMA = """
CREATE TABLE IF NOT EXISTS posting_details (
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    fetched_at TEXT NOT NULL,
    source TEXT,              -- feed:greenhouse | page:jsonld | page:text | error:<msg>
    body TEXT,                -- plain-text description
    export_status TEXT,       -- us-person-required | export-license-possible | export-mentioned | clear | unknown
    export_regime TEXT,       -- itar | ear | itar+ear | none
    visa_sponsorship TEXT,    -- no-sponsorship | unstated
    export_evidence TEXT      -- the sentence(s) that triggered the status
);
"""

# --------------------------------------------------------------------- text utils
_TAG = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_WS = re.compile(r"\s+")


def to_text(markup: str) -> str:
    if not markup:
        return ""
    s = markup
    # Greenhouse (and others) ship HTML that is itself entity-encoded, so a single
    # strip-then-unescape leaves visible "<li>" in the text. Strip, unescape, strip.
    for _ in range(2):
        s = _TAG.sub(" ", s)
        s = re.sub(r"(?i)<(br|/p|/li|/div|/tr|/h[1-6])[^>]*>", ". ", s)
        s = re.sub(r"<[^>]+>", " ", s)
        s = H.unescape(s)
    return _WS.sub(" ", s).strip()


def jsonld_description(page_html: str) -> str:
    """Pull JobPosting.description out of any ld+json block on the page."""
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            if node.get("@type") in ("JobPosting", ["JobPosting"]) and node.get("description"):
                return to_text(node["description"])
            graph = node.get("@graph")
            if isinstance(graph, list):
                for g in graph:
                    if isinstance(g, dict) and g.get("@type") == "JobPosting" and g.get("description"):
                        return to_text(g["description"])
    return ""


# --------------------------------------------------------------------- classifier
# Boilerplate that mentions citizenship WITHOUT restricting it — must be removed
# before the restriction patterns run, or every EEO statement reads as a gate.
EEO_SENTENCE = re.compile(
    r"(?i)[^.!?]*\b(without regard to|regardless of|equal (employment )?opportunity|"
    r"protected veteran|affirmative action|does not discriminate|all qualified applicants"
    r"|e-?verify|reasonable accommodation|considered for employment)\b[^.!?]*[.!?]")

# HARD gate — needs "U.S. person" status (citizen / green card / refugee / asylee).
# A Canadian citizen on a student visa cannot satisfy these, full stop.
HARD = [
    ("us-person-required", re.compile(
        r"(?i)\bU\.?S\.?\s*person\b[^.]{0,80}\b(status )?(is )?(required|require)"
        r"|must (either )?be (a |an )?[\"“]?U\.?S\.?\s*person"
        r"|\brequires? U\.?S\.?\s*person\b"
        r"|only U\.?S\.?\s*persons")),
    ("us-citizen-required", re.compile(
        r"(?i)(must be|applicants? (for )?[^.]{0,40}must be|required to be|restricted to|open only to)"
        r"[^.]{0,80}\b(U\.?S\.?|United States)\s*(citizen|national)"
        r"|\b(U\.?S\.?|United States)\s*citizenship\s+(is\s+)?(required|mandatory)"
        r"|\bMUST be U\.?S\.?\s*citizens?")),
]

# Government clearance / screening. Deliberately NOT folded into the US-person tier:
# a Canadian controlled-goods screening is attainable for a Canadian citizen, whereas
# a US clearance is not. Which one it is depends on where the posting is.
CLEARANCE = [
    ("clearance-required", re.compile(
        r"(?i)\b(active\s+)?(security|DoD|government)\s+(clearance|screening)\b"
        r"|\bTS/SCI\b|\btop secret\b"
        r"|controlled goods (program|registration)"
        r"|ability to obtain[^.]{0,50}\b(security )?clearance\b"
        r"|\bsecurity clearance[^.]{0,40}required")),
]

# SOFT gate — export rules apply, but satisfying them is a licensing question rather
# than a US-person test. Canada sits in the most-favoured EAR country group, so these
# are usually workable for a Canadian; ITAR ones in this tier still merit caution.
SOFT = [
    ("export-license-possible", re.compile(
        r"(?i)may require an export licen[cs]e"
        r"|whether to apply for a[^.]{0,40}licen[cs]e"
        r"|contingent upon[^.]{0,120}export[- ]control"
        r"|subject to[^.]{0,60}export[- ]control"
        r"|access to[^.]{0,60}export[- ]control")),
    ("export-mentioned", re.compile(
        r"(?i)\bITAR\b|\bExport Administration Regulations\b|\bEAR\b"
        r"|export[-\s]?control(led|s)?\b|export (regulations|licen[cs]e)")),
]

ITAR_RE = re.compile(r"(?i)\bITAR\b|International Traffic in Arms|22 C\.?F\.?R")
EAR_RE = re.compile(r"(?i)\bEAR\b|Export Administration Regulations|15 C\.?F\.?R|Country Group")
NO_VISA_RE = re.compile(
    r"(?i)(not|unable to|do(es)? not) (be )?(eligible for |offer |provide |sponsor)[^.]{0,40}"
    r"(visa|immigration) sponsor|no visa sponsorship|without (the need for )?visa sponsorship"
    r"|not eligible for (visa |immigration )?sponsorship")


# A sentence break is a .!? followed by space -- but NOT the periods inside "U.S.",
# "e.g." or "No." , which would otherwise cut every piece of evidence down to "U.S".
_BOUNDARY = re.compile(r"(?<![A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\bNo)[.!?]\s+")


def sentence_around(text: str, idx: int, span: int = 1) -> str:
    """The sentence containing idx, plus `span` sentences after it for context."""
    bounds = [m.end() for m in _BOUNDARY.finditer(text)]
    start = 0
    for b in bounds:
        if b <= idx:
            start = b
        else:
            break
    after = [b for b in bounds if b > idx]
    end = after[min(span, len(after)) - 1] if after else len(text)
    return " ".join(text[start:end].split())[:400]


def classify(body: str):
    """-> (status, regime, visa, evidence).

    status: us-person-required | clearance-required | export-license-possible
            | export-mentioned | clear | unknown
    regime: itar | ear | itar+ear | none      (which export rule the text names)
    visa:   no-sponsorship | unstated
    """
    if not body or len(body) < 120:
        return "unknown", "none", "unstated", ""
    clean = EEO_SENTENCE.sub(" ", body)
    itar, ear = bool(ITAR_RE.search(clean)), bool(EAR_RE.search(clean))
    regime = "itar+ear" if itar and ear else "itar" if itar else "ear" if ear else "none"
    visa = "no-sponsorship" if NO_VISA_RE.search(clean) else "unstated"

    evid = []
    for name, rx in HARD:
        m = rx.search(clean)
        if m:
            evid.append(sentence_around(clean, m.start()))
    if evid:
        return "us-person-required", regime, visa, " | ".join(dict.fromkeys(evid))[:600]
    for name, rx in CLEARANCE:
        m = rx.search(clean)
        if m:
            return "clearance-required", regime, visa, sentence_around(clean, m.start())[:600]
    for name, rx in SOFT:
        m = rx.search(clean)
        if m:
            return name, regime, visa, sentence_around(clean, m.start())[:600]
    return "clear", regime, visa, ""


# --------------------------------------------------------------------- fetching
_locks = defaultdict(threading.Lock)
_last = defaultdict(float)


def polite_get(url: str, **kw):
    host = urlparse(url).netloc
    with _locks[host]:
        gap = time.time() - _last[host]
        if gap < 0.4:
            time.sleep(0.4 - gap)
        _last[host] = time.time()
    return httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=True, **kw)


def bulk_descriptions(ats: str, feed_url: str):
    """{posting_key: text} for feeds that ship descriptions, else {}."""
    try:
        if ats == "greenhouse":
            sep = "&" if "?" in feed_url else "?"
            data = polite_get(f"{feed_url}{sep}content=true").json()
            return {str(j["id"]): to_text(j.get("content", "")) for j in data.get("jobs", [])}
        if ats == "ashby":
            data = polite_get(feed_url).json()
            return {str(j["id"]): (j.get("descriptionPlain") or to_text(j.get("descriptionHtml", "")))
                    for j in data.get("jobs", [])}
        if ats == "workable":
            sep = "&" if "?" in feed_url else "?"
            data = polite_get(f"{feed_url}{sep}details=true").json()
            return {str(j.get("shortcode") or j.get("url")): to_text(
                " ".join(filter(None, [j.get("description", ""), j.get("requirements", "")])))
                for j in data.get("jobs", [])}
        if ats == "jibe":
            base = feed_url.split("?")[0]
            out, page = {}, 1
            while page <= 40:
                data = polite_get(f"{base}?page={page}").json()
                batch = data.get("jobs", [])
                for wrap in batch:
                    j = wrap.get("data", wrap)
                    key = str(j.get("req_id") or j.get("slug") or j.get("apply_url"))
                    out[key] = to_text(" ".join(filter(None, [
                        j.get("description", ""), j.get("qualifications", ""), j.get("responsibilities", "")])))
                if not batch or len(out) >= data.get("totalCount", 0):
                    break
                page += 1
            return out
    except Exception:
        return {}
    return {}


def workday_json(url: str) -> str:
    """Workday posting pages have a JSON twin under /wday/cxs/."""
    m = re.match(r"(https://[^/]+)/(?:[a-z-]{2,5}/)?([^/]+)/job/(.+)", url)
    if not m:
        return ""
    origin, site, rest = m.groups()
    tenant = urlparse(origin).netloc.split(".")[0]
    try:
        r = polite_get(f"{origin}/wday/cxs/{tenant}/{site}/job/{rest}",
                       headers={**UA, "Accept": "application/json"})
        return to_text((r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))
    except Exception:
        return ""


def fetch_one(url: str):
    """-> (source, text)."""
    if "myworkdayjobs.com" in url:
        t = workday_json(url)
        if len(t) > 200:
            return "page:workday", t
    try:
        r = polite_get(url)
    except Exception as e:
        return f"error:{type(e).__name__}", ""
    if r.status_code >= 400:
        return f"error:http{r.status_code}", ""
    t = jsonld_description(r.text)
    if len(t) > 200:
        return "page:jsonld", t
    t2 = to_text(r.text)
    return ("page:text", t2) if len(t2) > 200 else ("error:empty", t2)


def enrich_missing(conn, items, max_workers: int = 6):
    """items = [(posting_id, url)]. Fetch each description once, cache the body in
    posting_details and store the verdict on the posting row. Used by run_check for
    feeds that carry no description."""
    items = [(pid, url) for pid, url in items if url]
    if not items:
        return 0
    conn.execute(DETAILS_SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_one, url): pid for pid, url in items}
        for fut in cf.as_completed(futs):
            pid = futs[fut]
            try:
                _, body = fut.result()
            except Exception:
                body = ""
            status, regime, visa, evid = classify(body)
            store_verdict(conn, pid, now, "page", body, status, regime, visa, evid)
            done += 1
    return done


def store_verdict(conn, pid, now, source, body, status, regime, visa, evid):
    """Write the body to the cache table and the verdict onto the posting row."""
    conn.execute(
        "INSERT INTO posting_details (posting_id, fetched_at, source, body, export_status,"
        " export_regime, visa_sponsorship, export_evidence) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(posting_id) DO UPDATE SET fetched_at=excluded.fetched_at,"
        " source=excluded.source, body=excluded.body, export_status=excluded.export_status,"
        " export_regime=excluded.export_regime, visa_sponsorship=excluded.visa_sponsorship,"
        " export_evidence=excluded.export_evidence",
        (pid, now, source, body, status, regime, visa, evid))
    conn.execute(
        "UPDATE postings SET export_status=?, export_regime=?, visa_sponsorship=?,"
        " export_evidence=? WHERE id=?", (status, regime, visa, evid, pid))


def cmd_backfill(args):
    """Copy verdicts from the posting_details cache onto postings, then fill the rest."""
    conn = connect()
    moved = conn.execute(
        "UPDATE postings SET export_status=(SELECT d.export_status FROM posting_details d"
        " WHERE d.posting_id=postings.id),"
        " export_regime=(SELECT d.export_regime FROM posting_details d WHERE d.posting_id=postings.id),"
        " visa_sponsorship=(SELECT d.visa_sponsorship FROM posting_details d WHERE d.posting_id=postings.id),"
        " export_evidence=(SELECT d.export_evidence FROM posting_details d WHERE d.posting_id=postings.id)"
        " WHERE EXISTS (SELECT 1 FROM posting_details d WHERE d.posting_id=postings.id)").rowcount
    try:
        conn.commit()
    except Exception:
        pass
    print(f"copied {moved} cached verdicts onto postings")
    todo = [(r["id"], r["url"]) for r in conn.execute(
        "SELECT id, url FROM postings WHERE closed=0 AND export_status IS NULL AND url IS NOT NULL")]
    print(f"{len(todo)} open postings still unread; fetching")
    n = enrich_missing(conn, todo)
    try:
        conn.commit()
    except Exception:
        pass
    from collections import Counter as C
    print(f"fetched {n}")
    print(dict(C(r["export_status"] for r in conn.execute(
        "SELECT export_status FROM postings WHERE closed=0"))))


# --------------------------------------------------------------------- driver
def target_postings(conn, us_only=True):
    rows = [dict(r) for r in conn.execute(
        "SELECT p.id, p.posting_key, p.title, p.url, p.location, c.name AS company,"
        "       c.ats_type, c.feed_url "
        "FROM postings p JOIN companies c ON c.id=p.company_id WHERE p.closed=0")]
    if not us_only:
        return rows
    from .analyze import geo
    keep = []
    for r in rows:
        countries, _ = geo(r["company"], r["title"], r["location"] or "")
        if "US" in countries:
            keep.append(r)
    return keep


def cmd_fetch(args):
    conn = connect()
    conn.execute(DETAILS_SCHEMA)
    rows = target_postings(conn, us_only=not args.all)
    if not args.refetch:
        have = {r[0] for r in conn.execute(
            "SELECT posting_id FROM posting_details WHERE body IS NOT NULL AND length(body)>200")}
        rows = [r for r in rows if r["id"] not in have]
    print(f"{len(rows)} postings to fetch", flush=True)

    by_company = defaultdict(list)
    for r in rows:
        by_company[(r["company"], r["ats_type"], r["feed_url"])].append(r)

    results, need_page = {}, []
    for (company, ats, feed), items in by_company.items():
        table = bulk_descriptions(ats, feed) if feed else {}
        got = 0
        for r in items:
            body = table.get(str(r["posting_key"]), "")
            if len(body) > 200:
                results[r["id"]] = (f"feed:{ats}", body)
                got += 1
            else:
                need_page.append(r)
        if table:
            print(f"  feed {company}: {got}/{len(items)}", flush=True)

    print(f"{len(need_page)} need a page fetch", flush=True)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, r["url"]): r for r in need_page if r["url"]}
        for fut in cf.as_completed(futs):
            r = futs[fut]
            try:
                results[r["id"]] = fut.result()
            except Exception as e:
                results[r["id"]] = (f"error:{type(e).__name__}", "")
            done += 1
            if done % 20 == 0:
                print(f"  page {done}/{len(futs)}", flush=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for pid, (source, body) in results.items():
        status, regime, visa, evid = classify(body)
        conn.execute(
            "INSERT INTO posting_details (posting_id, fetched_at, source, body, export_status,"
            " export_regime, visa_sponsorship, export_evidence) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(posting_id) DO UPDATE SET"
            " fetched_at=excluded.fetched_at, source=excluded.source, body=excluded.body,"
            " export_status=excluded.export_status, export_regime=excluded.export_regime,"
            " visa_sponsorship=excluded.visa_sponsorship, export_evidence=excluded.export_evidence",
            (pid, now, source, body, status, regime, visa, evid))
    try:
        conn.commit()
    except Exception:
        pass
    print("stored", len(results))
    print(Counter(s.split(":")[0] for s, _ in results.values()))
    print(Counter(classify(b)[0] for _, b in results.values()))


def cmd_classify(args):
    conn = connect()
    conn.execute(DETAILS_SCHEMA)
    rows = [dict(r) for r in conn.execute("SELECT posting_id, body FROM posting_details")]
    n = Counter()
    for r in rows:
        status, regime, visa, evid = classify(r["body"] or "")
        n[status] += 1
        conn.execute("UPDATE posting_details SET export_status=?, export_regime=?,"
                     " visa_sponsorship=?, export_evidence=? WHERE posting_id=?",
                     (status, regime, visa, evid, r["posting_id"]))
        conn.execute("UPDATE postings SET export_status=?, export_regime=?,"
                     " visa_sponsorship=?, export_evidence=? WHERE id=?",
                     (status, regime, visa, evid, r["posting_id"]))
    try:
        conn.commit()
    except Exception:
        pass
    print(dict(n))


def cmd_report(args):
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT d.export_status, d.export_evidence, d.source, p.title, p.location, c.name AS company "
        "FROM posting_details d JOIN postings p ON p.id=d.posting_id "
        "JOIN companies c ON c.id=p.company_id WHERE p.closed=0")]
    print(Counter(r["export_status"] for r in rows))
    for r in sorted(rows, key=lambda r: (r["export_status"], r["company"])):
        if r["export_status"] in ("blocked", "export-controlled"):
            print(f'{r["export_status"]:18s} {r["company"][:20]:20s} {r["title"][:52]:52s} {r["export_evidence"][:90]}')


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("--all", action="store_true"); f.add_argument("--refetch", action="store_true")
    f.set_defaults(fn=cmd_fetch)
    sub.add_parser("classify").set_defaults(fn=cmd_classify)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("backfill").set_defaults(fn=cmd_backfill)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
