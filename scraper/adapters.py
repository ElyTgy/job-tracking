"""One fetcher per ATS. Each takes the company row's feed_url (or slug baked into
it) and returns normalized postings:
    {posting_key, title, url, location, department, posted_date}
posting_key must be stable across runs (ATS job id preferred).

Public JSON endpoints (no auth):
  greenhouse       https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
  lever            https://api.lever.co/v0/postings/{slug}?mode=json
  ashby            https://api.ashbyhq.com/posting-api/job-board/{slug}
  workable         https://apply.workable.com/api/v1/widget/accounts/{slug}
  smartrecruiters  https://api.smartrecruiters.com/v1/companies/{slug}/postings
  recruitee        https://{slug}.recruitee.com/api/offers/
  workday          POST {origin}/wday/cxs/{tenant}/{site}/jobs
"""
import html as html_lib
import re
from urllib.parse import urljoin, urlparse

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
TIMEOUT = 40.0
RETRIES = 2  # large feeds occasionally time out on a cold CDN cache; retry is cheap


def _request(method: str, url: str, **kwargs):
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = httpx.request(
                method, url, headers={**UA, **kwargs.pop("headers", {})},
                timeout=TIMEOUT, follow_redirects=True, **kwargs,
            )
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500:
                raise  # 4xx won't heal with a retry
            last_exc = e
    raise last_exc


def _get_json(url: str):
    return _request("GET", url).json()


def fetch_greenhouse(feed_url: str):
    data = _get_json(feed_url)
    return [
        {
            "posting_key": str(j["id"]),
            "title": j["title"],
            "url": j.get("absolute_url"),
            "location": (j.get("location") or {}).get("name"),
            "department": ",".join(d["name"] for d in j.get("departments") or []),
            "posted_date": (j.get("updated_at") or "")[:10],
        }
        for j in data.get("jobs", [])
    ]


def fetch_lever(feed_url: str):
    data = _get_json(feed_url)
    return [
        {
            "posting_key": j["id"],
            "title": j["text"],
            "url": j.get("hostedUrl"),
            "location": (j.get("categories") or {}).get("location"),
            "department": (j.get("categories") or {}).get("team")
            or (j.get("categories") or {}).get("department"),
            "posted_date": "",
        }
        for j in data
    ]


def fetch_ashby(feed_url: str):
    data = _get_json(feed_url)
    return [
        {
            "posting_key": j["id"],
            "title": j["title"],
            "url": j.get("jobUrl") or j.get("applyUrl"),
            "location": j.get("location"),
            "department": j.get("department") or j.get("team"),
            "posted_date": (j.get("publishedAt") or "")[:10],
        }
        for j in data.get("jobs", [])
    ]


def fetch_workable(feed_url: str):
    data = _get_json(feed_url)
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        out.append(
            {
                "posting_key": j.get("shortcode") or j["url"],
                "title": j["title"],
                "url": j.get("url"),
                "location": loc,
                "department": j.get("department"),
                "posted_date": (j.get("published_on") or "")[:10],
            }
        )
    return out


def fetch_smartrecruiters(feed_url: str):
    out, offset = [], 0
    while True:
        data = _get_json(f"{feed_url}?limit=100&offset={offset}")
        batch = data.get("content", [])
        for j in batch:
            out.append(
                {
                    "posting_key": str(j.get("id") or j.get("uuid")),
                    "title": j["name"],
                    "url": f"https://jobs.smartrecruiters.com/{j['company']['identifier']}/{j['id']}"
                    if j.get("company")
                    else None,
                    "location": (j.get("location") or {}).get("city"),
                    "department": (j.get("department") or {}).get("label"),
                    "posted_date": (j.get("releasedDate") or "")[:10],
                }
            )
        offset += len(batch)
        if offset >= data.get("totalFound", 0) or not batch:
            return out


def fetch_recruitee(feed_url: str):
    data = _get_json(feed_url)
    return [
        {
            "posting_key": str(j["id"]),
            "title": j["title"],
            "url": j.get("careers_url"),
            "location": j.get("location"),
            "department": j.get("department"),
            "posted_date": (j.get("created_at") or "")[:10],
        }
        for j in data.get("offers", [])
    ]


def fetch_workday(feed_url: str):
    """feed_url is the cxs jobs endpoint, e.g.
    https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
    """
    m = re.search(r"(https://[^/]+)/wday/cxs/([^/]+)/([^/]+)/jobs", feed_url)
    if not m:
        raise ValueError(f"unrecognized workday feed url: {feed_url}")
    origin, _tenant, site = m.groups()
    out, offset = [], 0
    while True:
        r = _request(
            "POST",
            feed_url,
            headers={"Content-Type": "application/json"},
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
        )
        data = r.json()
        batch = data.get("jobPostings", [])
        for j in batch:
            path = j.get("externalPath", "")
            out.append(
                {
                    "posting_key": path or j.get("title"),
                    "title": j.get("title", ""),
                    "url": f"{origin}/en-US/{site}{path}" if path else None,
                    "location": j.get("locationsText"),
                    "department": "",
                    "posted_date": "",
                }
            )
        offset += len(batch)
        if offset >= data.get("total", 0) or not batch:
            return out


def fetch_html(feed_url: str):
    """Generic fallback: scan anchor tags on the careers page for intern-ish links.

    Coarser than the API adapters (no location/department), but a stable-enough
    net for companies with custom careers pages. posting_key is the absolute URL.
    """
    r = _request("GET", feed_url)
    page = r.text
    seen, out = set(), []
    for m in re.finditer(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
        href, inner = m.groups()
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", inner))
        text = re.sub(r"\s+", " ", text).strip()
        if not text or len(text) > 120:
            continue
        if not re.search(r"\bintern(ship)?\b|\bco-?op\b", text, re.I):
            continue
        url = urljoin(str(r.url), href)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "posting_key": url,
                "title": text,
                "url": url,
                "location": "",
                "department": "",
                "posted_date": "",
            }
        )
    # Short text nodes that name an intern/co-op role but aren't links
    # (accordion buttons, headings, cards with a separate Apply button).
    body = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", page, flags=re.S | re.I)
    seen_titles = {o["title"].lower() for o in out}
    for m in re.finditer(r">([^<>]{6,90})<", body):
        text = html_lib.unescape(m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if not re.search(r"\bintern(ship)?s?\b|\bco-?op\b", text, re.I):
            continue
        if text.lower() in seen_titles:
            continue
        seen_titles.add(text.lower())
        # prose filters: questions, sentences, generic nav words
        if "?" in text or re.search(r"\.\s", text) or len(text.split()) > 10:
            continue
        if re.fullmatch(r"(intern(ship)?s?|co-?ops?|careers?|jobs?)[\s/&-]*", text, re.I):
            continue
        if re.search(r"\b(we|our|you|your|offer|hire|hiring|apply now|learn more)\b", text, re.I):
            continue
        key = f"{r.url}#{re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "posting_key": key,
                "title": text,
                "url": str(r.url),
                "location": "",
                "department": "",
                "posted_date": "",
            }
        )
    return out


def fetch_jibe(feed_url: str):
    """Jibe/iCIMS career sites (e.g. https://careers.amd.com/api/jobs?page=1).
    Paginated JSON: {"jobs": [{"data": {...}}], "totalCount": N}."""
    base = feed_url.split("?")[0]
    out, page = [], 1
    while True:
        data = _get_json(f"{base}?page={page}")
        batch = data.get("jobs", [])
        for wrap in batch:
            j = wrap.get("data", wrap)
            loc = (j.get("city") or "")
            if j.get("country"):
                loc = ", ".join(filter(None, [loc, j.get("country")]))
            out.append(
                {
                    "posting_key": str(j.get("req_id") or j.get("slug") or j.get("apply_url")),
                    "title": j.get("title", ""),
                    "url": j.get("apply_url") or j.get("canonical_url"),
                    "location": loc,
                    "department": j.get("category", [None])[0]
                    if isinstance(j.get("category"), list)
                    else j.get("category"),
                    "posted_date": (j.get("posted_date") or "")[:10],
                }
            )
        if len(out) >= data.get("totalCount", 0) or not batch:
            return out
        page += 1


def fetch_gem(feed_url: str):
    """Gem job boards (e.g. https://api.gem.com/job_board/v0/{slug}/job_posts/).
    Bare JSON array with title/absolute_url."""
    data = _get_json(feed_url)
    return [
        {
            "posting_key": str(j.get("id") or j.get("absolute_url")),
            "title": j.get("title", ""),
            "url": j.get("absolute_url"),
            "location": (j.get("location") or {}).get("name")
            if isinstance(j.get("location"), dict)
            else j.get("location"),
            "department": "",
            "posted_date": "",
        }
        for j in data
    ]


def fetch_kula(feed_url: str):
    """Kula ATS via the company site's own JSON proxy
    (e.g. https://www.precisionneuro.io/api/job-listings)."""
    data = _get_json(feed_url)
    jobs = data if isinstance(data, list) else data.get("jobs") or data.get("data") or []
    out = []
    for j in jobs:
        out.append(
            {
                "posting_key": str(j.get("id") or j.get("slug") or j.get("url") or j.get("title")),
                "title": j.get("title") or j.get("name", ""),
                "url": j.get("url") or j.get("absolute_url") or j.get("hostedUrl"),
                "location": j.get("location") if isinstance(j.get("location"), str)
                else (j.get("location") or {}).get("name"),
                "department": j.get("department") if isinstance(j.get("department"), str)
                else (j.get("department") or {}).get("name"),
                "posted_date": "",
            }
        )
    return out


def fetch_amazonjobs(feed_url: str):
    """amazon.jobs JSON search API, e.g.
    https://www.amazon.jobs/en/search.json?base_query=intern&result_limit=100&offset=0
    Top-level {"hits": N, "jobs": [{...}]} — pre-filtered server-side by base_query."""
    base = feed_url.split("&offset=")[0]
    out, offset = [], 0
    while True:
        data = _get_json(f"{base}&offset={offset}")
        batch = data.get("jobs", [])
        for j in batch:
            out.append(
                {
                    "posting_key": str(j.get("id_icims") or j.get("id") or j.get("job_path")),
                    "title": j.get("title", ""),
                    "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                    "location": j.get("normalized_location") or j.get("location"),
                    "department": j.get("job_category"),
                    "posted_date": (j.get("posted_date") or "")[:10],
                }
            )
        offset += len(batch)
        if offset >= data.get("hits", 0) or not batch:
            return out


def fetch_kulaboard(feed_url: str):
    """Kula-hosted career pages, e.g. https://careers.kula.ai/sanctuary-ai
    Server-rendered accordion: title <p>, a details line
    ("Co-op • Vancouver, BC, Canada • Internship • On-Site"), then an apply
    link /{slug}/{job_id}/ — parsed structurally so class-name churn is survivable."""
    r = _request("GET", feed_url)
    page = r.text
    slug = urlparse(feed_url).path.strip("/").split("/")[0]
    out, seen = [], set()
    # each job: a title paragraph ... an apply href to /{slug}/{digits}/
    pat = re.compile(
        r'<p[^>]*>([^<]{3,140})</p>\s*<div[^>]*>\s*<p[^>]*>(.*?)</p>.*?href="/' + re.escape(slug) + r'/(\d+)/?"',
        re.S,
    )
    for m in pat.finditer(page):
        title, details, job_id = m.group(1).strip(), m.group(2), m.group(3)
        if job_id in seen:
            continue
        seen.add(job_id)
        details = html_lib.unescape(re.sub(r"<[^>]+>|<!-- -->", "", details))
        parts = [x.strip() for x in details.split("•") if x.strip()]
        # details look like [employment type, location, category, on-site]
        location = next((x for x in parts if "," in x), parts[1] if len(parts) > 1 else "")
        out.append(
            {
                "posting_key": job_id,
                "title": html_lib.unescape(title),
                "url": f"https://careers.kula.ai/{slug}/{job_id}/",
                "location": location,
                "department": " / ".join(x for x in parts if x != location),
                "posted_date": "",
            }
        )
    return out


def fetch_eightfold(feed_url: str):
    """Eightfold career sites via the unauthenticated pcsx search, e.g.
    https://careers.qualcomm.com/api/pcsx/search?domain=qualcomm.com&query=intern&start=0&num=50
    Paginates with start/num."""
    base = re.sub(r"[&?]start=\d+", "", feed_url)
    origin = re.match(r"https://[^/]+", feed_url).group(0)
    out, start = [], 0
    while True:
        data = _get_json(f"{base}&start={start}")
        batch = (data.get("data") or {}).get("positions") or []
        for j in batch:
            locs = j.get("locations") or []
            out.append(
                {
                    "posting_key": str(j.get("id") or j.get("displayJobId")),
                    "title": j.get("name", ""),
                    "url": origin + (j.get("positionUrl") or ""),
                    "location": "; ".join(locs) if isinstance(locs, list) else str(locs),
                    "department": j.get("department") or "",
                    "posted_date": "",
                }
            )
        start += len(batch)
        if not batch or start >= (data.get("data") or {}).get("count", start):
            return out


def fetch_rippling(feed_url: str):
    """Rippling ATS public board, e.g.
    https://api.rippling.com/platform/api/ats/v1/board/d-wave-quantum/jobs -> JSON array."""
    data = _get_json(feed_url)
    out, seen = [], set()
    for j in data:
        key = str(j.get("uuid") or j.get("url"))
        if key in seen:
            continue  # Rippling duplicates entries per job
        seen.add(key)
        out.append(
            {
                "posting_key": key,
                "title": j.get("name", ""),
                "url": j.get("url"),
                "location": (j.get("workLocation") or {}).get("label"),
                "department": (j.get("department") or {}).get("label"),
                "posted_date": "",
            }
        )
    return out


def fetch_ukg(feed_url: str):
    """UKG Pro (UltiPro) recruiting boards. feed_url is the LoadSearchResults endpoint, e.g.
    https://recruiting.ultipro.ca/MAC5000MCDW/JobBoard/<guid>/JobBoardView/LoadSearchResults"""
    board = re.sub(r"/JobBoardView/LoadSearchResults.*$", "", feed_url)
    out, skip = [], 0
    while True:
        r = _request(
            "POST", feed_url, headers={"Content-Type": "application/json"},
            json={"opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": "",
                   "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                   "Filters": []},
                  "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                                    "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}},
        )
        data = r.json()
        batch = data.get("opportunities") or []
        for j in batch:
            locs = j.get("Locations") or []
            cities = [((l.get("Address") or {}).get("City") or "") for l in locs]
            out.append(
                {
                    "posting_key": str(j.get("Id")),
                    "title": j.get("Title", ""),
                    "url": f"{board}/OpportunityDetail?opportunityId={j.get('Id')}",
                    "location": ", ".join(c for c in cities if c),
                    "department": j.get("JobCategoryName") or "",
                    "posted_date": (j.get("PostedDate") or "")[:10],
                }
            )
        skip += len(batch)
        if not batch or skip >= data.get("totalCount", skip):
            return out


def fetch_adp(feed_url: str):
    """ADP Workforce Now career center JSON, e.g.
    https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions?cid=...&ccId=...&lang=en_CA&$top=50&$skip=0"""
    base = re.sub(r"&\$skip=\d+", "", feed_url)
    cid = re.search(r"cid=([^&]+)", feed_url).group(1)
    ccid = re.search(r"ccId=([^&]+)", feed_url)
    out, skip = [], 0
    while True:
        data = _get_json(f"{base}&$skip={skip}")
        batch = data.get("jobRequisitions") or []
        for j in batch:
            jid = j.get("itemID") or j.get("customFieldGroup", {}).get("itemID")
            locs = j.get("requisitionLocations") or []
            city = ", ".join(filter(None, [(l.get("address") or {}).get("cityName") for l in locs]))
            out.append(
                {
                    "posting_key": str(jid),
                    "title": j.get("requisitionTitle", ""),
                    "url": f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}"
                           + (f"&ccId={ccid.group(1)}" if ccid else "") + f"&jobId={jid}",
                    "location": city,
                    "department": "",
                    "posted_date": (j.get("postDate") or "")[:10],
                }
            )
        skip += len(batch)
        if not batch or len(batch) < 50:
            return out


def fetch_hibob(feed_url: str):
    """HiBob career sites, e.g. https://onwardmedical.careers.hibob.com/api/job-ad
    (requires a Referer header matching the site)."""
    origin = re.match(r"https://[^/]+", feed_url).group(0)
    data = _request("GET", feed_url, headers={"Referer": origin + "/"}).json()
    return [
        {
            "posting_key": str(j.get("id")),
            "title": j.get("title", ""),
            "url": f"{origin}/jobs/{j.get('id')}",
            "location": ", ".join(filter(None, [j.get("site"), j.get("country")])),
            "department": " / ".join(filter(None, [j.get("department"), j.get("employmentType")])),
            "posted_date": "",
        }
        for j in data.get("jobAdDetails") or []
    ]


FETCHERS = {
    "hibob": fetch_hibob,
    "eightfold": fetch_eightfold,
    "rippling": fetch_rippling,
    "ukg": fetch_ukg,
    "adp": fetch_adp,
    "kulaboard": fetch_kulaboard,
    "amazonjobs": fetch_amazonjobs,
    "jibe": fetch_jibe,
    "gem": fetch_gem,
    "kula": fetch_kula,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "html": fetch_html,
}
