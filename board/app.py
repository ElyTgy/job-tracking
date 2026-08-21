"""Job board API.

Local:   make serve  (uvicorn board.app:app --port 8787)
Hosted:  Vercel (entrypoint in pyproject.toml) with TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
         and BOARD_PASSWORD set as environment variables.
"""
import csv
import io
import os
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from typing import Optional
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper import adapters, classify, db, run_check  # noqa: E402
from scraper.ingest import normalize  # noqa: E402

app = FastAPI(title="Internship Tracker")
STATIC = Path(__file__).resolve().parent / "static"
db.load_env()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic auth when BOARD_PASSWORD is set (i.e. when deployed publicly)."""
    password = os.environ.get("BOARD_PASSWORD")
    if password and request.url.path != "/api/health":
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            import base64
            try:
                _, _, given = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, password)
            except Exception:
                ok = False
        if not ok:
            return Response(
                "Unauthorized", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Internship Tracker"'},
            )
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"ok": True, "backend": "turso" if os.environ.get("TURSO_DATABASE_URL") else "sqlite"}

VALID_STATUSES = {"not seen", "seen", "applied", "rejected", "offer"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/postings")
def postings():
    conn = db.connect()
    rows = conn.execute(
        """SELECT p.id, p.title, p.url, p.location, p.department, p.tag, p.tag_hits,
                  p.loc_ok, p.first_seen, p.last_seen, p.is_new, p.closed, p.user_status,
                  p.export_status, p.export_regime, p.visa_sponsorship, p.export_evidence,
                  c.name AS company, c.id AS company_id
           FROM postings p JOIN companies c ON c.id = p.company_id
           ORDER BY p.is_new DESC, p.first_seen DESC, c.name COLLATE NOCASE"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/companies")
def companies():
    conn = db.connect()
    comps = [
        dict(r)
        for r in conn.execute(
            """SELECT c.id, c.name, c.website, c.careers_url, c.ats_type,
                      c.discovery_status, c.last_checked, c.last_check_status, c.audit_note,
                      c.feed_url, c.sources, c.manual_note,
                      (SELECT COUNT(*) FROM postings p
                       WHERE p.company_id=c.id AND p.closed=0) AS open_count
               FROM companies c ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
    ]
    recs: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM recruiters"):
        recs.setdefault(r["company_id"], []).append(dict(r))
    for c in comps:
        c["recruiters"] = recs.get(c["id"], [])
    return comps


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/postings/{posting_id}/status")
def set_status(posting_id: int, body: StatusUpdate):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    conn = db.connect()
    cur = conn.execute(
        "UPDATE postings SET user_status=? WHERE id=?", (body.status, posting_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "posting not found")
    return {"ok": True}


@app.get("/api/people")
def people():
    conn = db.connect()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM people ORDER BY added DESC, name COLLATE NOCASE")]


class PersonUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


@app.post("/api/people/{person_id}")
def update_person(person_id: int, body: PersonUpdate):
    conn = db.connect()
    if body.status is not None:
        conn.execute("UPDATE people SET user_status=? WHERE id=?", (body.status, person_id))
    if body.notes is not None:
        conn.execute("UPDATE people SET notes=? WHERE id=?", (body.notes, person_id))
    conn.commit()
    return {"ok": True}


@app.get("/api/runs/latest")
def latest_run():
    conn = db.connect()
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else {}


@app.get("/api/recruiters.csv")
def recruiters_csv():
    conn = db.connect()
    rows = conn.execute(
        """SELECT c.name AS company, r.name, r.title, r.email, r.email_status,
                  r.linkedin_url, r.source
           FROM recruiters r JOIN companies c ON c.id=r.company_id
           ORDER BY c.name COLLATE NOCASE, r.name"""
    ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "name", "title", "email", "email_status", "linkedin", "source"])
    w.writerows([tuple(r) for r in rows])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=recruiters.csv"},
    )


# ----------------------------------------------------------------- company admin
# Manual fixes for the Issues tab and adding new companies/boards from the UI.

FEED_HINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/SLUG/jobs",
    "lever": "https://api.lever.co/v0/postings/SLUG?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/SLUG",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/SLUG",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/SLUG/postings",
    "recruitee": "https://SLUG.recruitee.com/api/offers",
    "workday": "https://TENANT.wdN.myworkdayjobs.com/wday/cxs/TENANT/SITE/jobs",
    "rippling": "https://api.rippling.com/platform/api/ats/v1/board/SLUG/jobs",
    "bamboohr": "https://SLUG.bamboohr.com/careers/list",
    "pinpoint": "https://careers.COMPANY.com/postings.json",
    "gem": "https://api.gem.com/job_board/v0/SLUG/job_posts/",
    "html": "the careers page URL itself (links + intern/co-op text are scanned)",
}


@app.get("/api/ats")
def ats_types():
    return [{"key": k, "hint": FEED_HINTS.get(k, "")} for k in adapters.FETCHERS]


class FeedTest(BaseModel):
    ats_type: str
    feed_url: str


def _test_feed(ats_type: str, feed_url: str) -> dict:
    if ats_type not in adapters.FETCHERS:
        raise HTTPException(400, f"unknown ats_type {ats_type}")
    cfg = classify.load_config()
    jobs = []
    for feed in feed_url.split(" | "):
        try:
            jobs.extend(adapters.FETCHERS[ats_type](feed.strip()))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"feed failed: {type(e).__name__}: {str(e)[:200]}")
    interns = [j for j in jobs if classify.is_internship(j["title"], cfg)
               and not classify.is_degree_excluded(j["title"], cfg)]
    return {
        "jobs": len(jobs),
        "internships": len(interns),
        "sample": [j["title"] for j in (interns or jobs)[:6]],
        "sample_url": next((j["url"] for j in jobs if j.get("url")), None),
    }


@app.post("/api/companies/test")
def test_feed(body: FeedTest):
    return _test_feed(body.ats_type, body.feed_url)


class CompanyEdit(BaseModel):
    name: Optional[str] = None
    website: Optional[str] = None
    careers_url: Optional[str] = None
    ats_type: Optional[str] = None
    feed_url: Optional[str] = None
    discovery_status: Optional[str] = None   # ok | needs_manual | dead
    manual_note: Optional[str] = None
    scrape: bool = True                      # run a check right away when a feed is set


def _apply_company_edit(conn, cid: int, body: CompanyEdit) -> dict:
    fields = {}
    for k in ("name", "website", "careers_url", "ats_type", "feed_url", "manual_note"):
        v = getattr(body, k)
        if v is not None:
            fields[k] = v.strip() or None
    if "name" in fields and fields["name"]:
        fields["normalized_name"] = normalize(fields["name"])
    status = body.discovery_status
    result = {}
    if fields.get("feed_url") and fields.get("ats_type"):
        result["test"] = _test_feed(fields["ats_type"], fields["feed_url"])
        status = status or "ok"
        fields["audit_note"] = None
        fields["last_check_status"] = None
    if status:
        if status not in ("ok", "needs_manual", "dead", "pending"):
            raise HTTPException(400, "bad discovery_status")
        fields["discovery_status"] = status
        if status == "dead":
            conn.execute("UPDATE postings SET closed=1 WHERE company_id=? AND closed=0", (cid,))
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE companies SET {sets} WHERE id=?", (*fields.values(), cid))
    conn.commit()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
    if body.scrape and company["discovery_status"] == "ok" and company["ats_type"] and company["feed_url"]:
        cfg = classify.load_config()
        try:
            r = run_check.check_company(conn, company, cfg, run_check._now())
            conn.execute("UPDATE postings SET is_new=1 WHERE company_id=? AND first_seen=last_seen", (cid,))
            conn.commit()
            result["check"] = r
        except Exception as e:  # noqa: BLE001
            conn.execute("UPDATE companies SET last_check_status=? WHERE id=?",
                         (f"error:{type(e).__name__}: {str(e)[:150]}", cid))
            conn.commit()
            result["check_error"] = str(e)[:200]
    result["company"] = dict(conn.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone())
    return result


@app.post("/api/companies")
def add_company(body: CompanyEdit):
    if not body.name or not body.name.strip():
        raise HTTPException(400, "name required")
    conn = db.connect()
    norm = normalize(body.name)
    existing = conn.execute("SELECT id FROM companies WHERE normalized_name=?", (norm,)).fetchone()
    if existing:
        raise HTTPException(409, f"company already exists (id {existing['id']})")
    cur = conn.execute(
        "INSERT INTO companies (name, normalized_name, sources, discovery_status) VALUES (?,?,?,?)",
        (body.name.strip(), norm, "manual", "needs_manual"),
    )
    conn.commit()
    return _apply_company_edit(conn, cur.lastrowid, body)


@app.post("/api/companies/{company_id}")
def edit_company(company_id: int, body: CompanyEdit):
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        raise HTTPException(404)
    return _apply_company_edit(conn, company_id, body)


@app.post("/api/companies/{company_id}/probe")
def probe_company(company_id: int):
    """Re-run automatic discovery (site sniff + verified slug guesses) for one company."""
    from scraper import discover
    conn = db.connect()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if not company:
        raise HTTPException(404)
    status = discover.probe_company(conn, company)
    conn.commit()
    return {"result": status, "company": dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())}
