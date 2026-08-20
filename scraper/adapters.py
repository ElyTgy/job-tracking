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
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "html": fetch_html,
}
