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
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper import db  # noqa: E402

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
                      c.discovery_status, c.last_checked, c.last_check_status,
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
