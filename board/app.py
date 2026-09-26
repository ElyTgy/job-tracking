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
from scraper import adapters, classify, db, followups, run_check, watchdog  # noqa: E402
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
        # Vercel cron calls /api/cron/* with "Authorization: Bearer $CRON_SECRET" (an
        # env var you set on the project); that token opens nothing else.
        cron_secret = os.environ.get("CRON_SECRET")
        if cron_secret and request.url.path.startswith("/api/cron/") and auth.startswith("Bearer "):
            ok = secrets.compare_digest(auth[7:], cron_secret)
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

VALID_STATUSES = {"new", "apply later", "backlog", "irrelevant", "applied", "interviewing", "rejected", "offer"}
# pre-triage names that may still come out of an older DB default between scrape and restart
LEGACY_STATUS = {"not seen": "new", "seen": "new", "hidden": "irrelevant"}


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
                  p.status_at, p.posting_key LIKE 'manual:%' AS manual,
                  c.name AS company, c.id AS company_id
           FROM postings p JOIN companies c ON c.id = p.company_id
           ORDER BY p.is_new DESC, p.first_seen DESC, c.name COLLATE NOCASE"""
    ).fetchall()
    out = [dict(r) for r in rows]
    for p in out:
        p["user_status"] = LEGACY_STATUS.get(p["user_status"], p["user_status"])
    return out


@app.get("/api/companies")
def companies():
    conn = db.connect()
    comps = [
        dict(r)
        for r in conn.execute(
            """SELECT c.id, c.name, c.website, c.careers_url, c.ats_type,
                      c.discovery_status, c.last_checked, c.last_check_status, c.audit_note,
                      c.feed_url, c.sources, c.manual_note, c.applied_at, c.outreach_status, c.outreach_notes, c.outreach_at,
                      c.country, c.hq, c.description, c.source_detail, c.priority,
                      (SELECT COUNT(*) FROM postings p
                       WHERE p.company_id=c.id AND p.closed=0) AS open_count
               FROM companies c ORDER BY c.name COLLATE NOCASE"""
        ).fetchall()
    ]
    recs: dict[int, list] = {}
    for r in conn.execute("SELECT * FROM recruiters"):
        recs.setdefault(r["company_id"], []).append(dict(r))
    fups: dict[int, list] = {}
    for f in followups.pending(conn):
        fups.setdefault(f["company_id"], []).append(f)
    for c in comps:
        c["recruiters"] = recs.get(c["id"], [])
        c["followups"] = fups.get(c["id"], [])
    return comps


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/postings/{posting_id}/status")
def set_status(posting_id: int, body: StatusUpdate):
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    conn = db.connect()
    cur = conn.execute(
        "UPDATE postings SET user_status=?, status_at=? WHERE id=?",
        (body.status, run_check._now()[:10], posting_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "posting not found")
    return {"ok": True}


def _today() -> str:
    return run_check._now()[:10]


OUTREACH_STATUSES = ["not started", "finding contacts", "drafting", "reached out",
                     "following up", "replied", "no response"]
CONTACT_STATUSES = ["to contact", "finding email", "drafted", "messaged",
                    "followed up", "replied", "no reply"]
POSTING_COLS = ["id", "title", "url", "location", "department", "tag", "tag_hits", "loc_ok",
                "first_seen", "last_seen", "is_new", "closed", "user_status", "status_at",
                "export_status", "export_regime", "visa_sponsorship", "export_evidence", "posting_key"]
CONTACT_COLS = ["id", "company_id", "name", "title", "email", "email_status", "linkedin_url",
                "source", "status", "notes", "status_at", "purpose"]
FOLLOWUP_COLS = ["id", "company_id", "recruiter_id", "step", "due", "sent_at", "done_at"]


def _json_rows(table: str, cols: list, order: str) -> str:
    """Subquery that returns a table's rows for one company as a JSON array, so the
    whole detail view comes back in a single round trip (Turso: ~2s per query)."""
    obj = ", ".join(f"'{c}', {c}" for c in cols)
    return (f"(SELECT json_group_array(json_object({obj})) FROM "
            f"(SELECT * FROM {table} WHERE company_id=c.id ORDER BY {order}))")


@app.get("/api/companies/{company_id}/detail")
def company_detail(company_id: int):
    """Everything the expanded company card shows, read fresh from the DB: every
    posting ever seen there (open and taken down) with its status, plus contacts."""
    import json
    conn = db.connect()
    row = conn.execute(
        f"""SELECT c.*, {_json_rows("postings", POSTING_COLS, "closed, first_seen DESC")} AS _posts,
                   {_json_rows("recruiters", CONTACT_COLS, "id")} AS _contacts,
                   {_json_rows("followups", FOLLOWUP_COLS, "due, step")} AS _fups
            FROM companies c WHERE c.id=?""", (company_id,)).fetchone()
    if not row:
        raise HTTPException(404, "company not found")
    c = dict(row)
    posts, recs = json.loads(c.pop("_posts") or "[]"), json.loads(c.pop("_contacts") or "[]")
    fups = [f for f in json.loads(c.pop("_fups") or "[]") if not f["done_at"]]
    for p in posts:
        p["user_status"] = LEGACY_STATUS.get(p["user_status"], p["user_status"])
    return {"company": c, "postings": posts, "contacts": recs, "followups": fups}


class Priority(BaseModel):
    rank: Optional[int] = None   # 1-5, or null to unrank


@app.post("/api/companies/{company_id}/priority")
def set_priority(company_id: int, body: Priority):
    """Rank a company 1-5 in the reach-out-next list. A rank lives on one company at a
    time: taking a rank someone else holds hands them your old rank (or unranks them)."""
    if body.rank is not None and not 1 <= body.rank <= 5:
        raise HTTPException(400, "rank must be 1-5 or null")
    conn = db.connect()
    row = conn.execute("SELECT priority FROM companies WHERE id=?", (company_id,)).fetchone()
    if not row:
        raise HTTPException(404, "company not found")
    old = row["priority"]
    if body.rank is not None:
        conn.execute("UPDATE companies SET priority=? WHERE priority=? AND id<>?",
                     (old, body.rank, company_id))
    conn.execute("UPDATE companies SET priority=? WHERE id=?", (body.rank, company_id))
    conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT id, priority FROM companies WHERE priority IS NOT NULL ORDER BY priority")]


class ManualRole(BaseModel):
    title: str
    url: Optional[str] = None
    location: Optional[str] = None
    status: str = "applied"


@app.post("/api/companies/{company_id}/postings")
def add_role(company_id: int, body: ManualRole):
    """A role found somewhere the scraper doesn't look (another board, a referral).
    Pinned so the scraper never closes it, and stamped as already notified so the
    digest doesn't email you about a role you added yourself."""
    import hashlib
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title required")
    if body.status not in VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(VALID_STATUSES)}")
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        raise HTTPException(404, "company not found")
    url = (body.url or "").strip() or None
    key = "manual:" + hashlib.sha256((url or title.lower()).encode()).hexdigest()[:16]
    if conn.execute("SELECT 1 FROM postings WHERE company_id=? AND posting_key=?",
                    (company_id, key)).fetchone():
        raise HTTPException(409, "that role is already on this company")
    cfg = classify.load_config()
    tag, hits = classify.tag_posting(title, "", cfg)
    location = (body.location or "").strip() or None
    now = run_check._now()
    cur = conn.execute(
        """INSERT INTO postings (company_id, posting_key, title, url, location, tag, tag_hits,
                                 loc_ok, pinned, first_seen, last_seen, is_new, notified_at,
                                 user_status, status_at)
           VALUES (?,?,?,?,?,?,?,?,1,?,?,0,?,?,?)""",
        (company_id, key, title, url, location, tag, hits,
         1 if classify.location_ok(location or "", cfg) else 0,
         now, now, now, body.status, now[:10]))
    conn.commit()
    row = conn.execute(f"SELECT {', '.join(POSTING_COLS)} FROM postings WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    return dict(row)


@app.delete("/api/postings/{posting_id}")
def delete_role(posting_id: int):
    """Only hand-added roles can be deleted; scraped ones come back on the next check."""
    conn = db.connect()
    row = conn.execute("SELECT posting_key FROM postings WHERE id=?", (posting_id,)).fetchone()
    if not row:
        raise HTTPException(404, "posting not found")
    if not str(row["posting_key"]).startswith("manual:"):
        raise HTTPException(400, "only roles you added by hand can be deleted")
    conn.execute("DELETE FROM postings WHERE id=?", (posting_id,))
    conn.commit()
    return {"ok": True}


class OutreachUpdate(BaseModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    applied_offboard: Optional[bool] = None


@app.post("/api/companies/{company_id}/outreach")
def set_outreach(company_id: int, body: OutreachUpdate):
    conn = db.connect()
    fields = {}
    if body.status is not None:
        if body.status not in OUTREACH_STATUSES:
            raise HTTPException(400, f"status must be one of {OUTREACH_STATUSES}")
        fields["outreach_status"] = body.status
        fields["outreach_at"] = _today()
    if body.notes is not None:
        fields["outreach_notes"] = body.notes
    if body.applied_offboard is not None:
        fields["applied_at"] = _today() if body.applied_offboard else None
    if not fields:
        raise HTTPException(400, "nothing to update")
    sets = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(f"UPDATE companies SET {sets} WHERE id=?", (*fields.values(), company_id))
    if cur.rowcount == 0:
        raise HTTPException(404, "company not found")
    if body.status is not None:
        # "reached out" with nobody attached queues company-level nudges; see followups.py
        followups.on_company_status(conn, company_id, body.status)
        fields["followups"] = followups.pending(conn, company_id)
    conn.commit()
    return fields


class ContactEdit(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    purpose: Optional[str] = None   # referral | application | other | "" to clear


def _contact_fields(body: ContactEdit) -> dict:
    fields = {}
    for k in ("name", "title", "email", "linkedin_url", "notes"):
        v = getattr(body, k)
        if v is not None:
            fields[k] = v.strip() or None
    if body.status is not None:
        if body.status not in CONTACT_STATUSES:
            raise HTTPException(400, f"status must be one of {CONTACT_STATUSES}")
        fields["status"] = body.status
        fields["status_at"] = _today()
    if "email" in fields:
        # typed in by hand: nobody has verified it
        fields["email_status"] = "unverified" if fields["email"] else None
    if body.purpose is not None:
        purpose = body.purpose.strip() or None
        if purpose and purpose not in followups.PURPOSES:
            raise HTTPException(400, f"purpose must be one of {followups.PURPOSES}")
        fields["purpose"] = purpose
    return fields


@app.post("/api/companies/{company_id}/contacts")
def add_contact(company_id: int, body: ContactEdit):
    if not (body.name or "").strip():
        raise HTTPException(400, "name required")
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        raise HTTPException(404, "company not found")
    if body.status is None:
        body.status = "to contact"
    fields = {"company_id": company_id, "source": "manual", **_contact_fields(body)}
    cols = ", ".join(fields)
    if conn.execute("SELECT 1 FROM recruiters WHERE company_id=? AND name=?",
                    (company_id, fields["name"])).fetchone():
        raise HTTPException(409, f"{fields['name']} is already a contact here")
    cur = conn.execute(f"INSERT INTO recruiters ({cols}) VALUES ({', '.join('?' * len(fields))})",
                       tuple(fields.values()))
    if fields["status"] == "messaged":
        followups.on_contact_status(conn, company_id, cur.lastrowid, "messaged")
    conn.commit()
    return dict(conn.execute("SELECT * FROM recruiters WHERE id=?", (cur.lastrowid,)).fetchone())


@app.post("/api/recruiters/{recruiter_id}")
def edit_contact(recruiter_id: int, body: ContactEdit):
    conn = db.connect()
    fields = _contact_fields(body)
    if "name" in fields and not fields["name"]:
        raise HTTPException(400, "name can't be empty")
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        cur = conn.execute(f"UPDATE recruiters SET {sets} WHERE id=?", (*fields.values(), recruiter_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "contact not found")
    out = dict(conn.execute("SELECT * FROM recruiters WHERE id=?", (recruiter_id,)).fetchone())
    if "status" in fields:
        # messaged -> queue the day-5 / day-10 nudges; followed up / replied retire them
        followups.on_contact_status(conn, out["company_id"], recruiter_id, fields["status"])
        out["followups"] = followups.pending(conn, out["company_id"])
    conn.commit()
    return out


@app.delete("/api/recruiters/{recruiter_id}")
def delete_contact(recruiter_id: int):
    conn = db.connect()
    conn.execute("DELETE FROM followups WHERE recruiter_id=?", (recruiter_id,))
    cur = conn.execute("DELETE FROM recruiters WHERE id=?", (recruiter_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "contact not found")
    return {"ok": True}


@app.get("/api/cron/followups")
def cron_followups():
    """Email the outreach follow-ups due today. Vercel calls this daily (vercel.json
    crons, authenticated by CRON_SECRET); the laptop's launchd job runs the same
    code via `python -m scraper.followups`. Each nudge is emailed once, so both
    can fire on the same day."""
    conn = db.connect()
    return followups.send(conn)


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


@app.get("/api/analysis")
def analysis():
    """Live sector / role / location / eligibility rollup for the Analysis tab.

    Computed from the database on each request (~0.2s for a few hundred postings)
    so the page always reflects the latest check rather than a generated snapshot.
    """
    from scraper.analyze import summary
    return summary()


@app.get("/api/runs/latest")
def latest_run():
    """The last *full* run (a --company check says nothing about the schedule's health),
    plus the watchdog verdict the header uses for its stale banner."""
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM runs WHERE scope='full' AND finished IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    out = dict(row) if row else {}
    out["watchdog"] = watchdog.status(conn)
    return out


@app.get("/api/cron/watchdog")
def cron_watchdog():
    """Daily Vercel cron (vercel.json): email if the laptop's scheduled full check
    has stopped finishing. Lives here, not in the launchd job, because a broken
    launchd job can't report itself."""
    return watchdog.check(db.connect())


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
    "watch": "any page URL — the whole page is diffed and every change is reported",
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
        was_dead = conn.execute(
            "SELECT discovery_status FROM companies WHERE id=?", (cid,)
        ).fetchone()["discovery_status"] == "dead"
        fields["discovery_status"] = status
        if status == "dead":
            conn.execute("UPDATE postings SET closed=1 WHERE company_id=? AND closed=0", (cid,))
        elif was_dead:
            # reviving from dead: undo the bulk close so its postings come back
            conn.execute("UPDATE postings SET closed=0 WHERE company_id=? AND closed=1", (cid,))
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
