"""Local job board API. Run with:  make serve  (uvicorn board.app:app --port 8787)"""
import csv
import io
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper import db  # noqa: E402

app = FastAPI(title="Internship Tracker")
STATIC = Path(__file__).resolve().parent / "static"

VALID_STATUSES = {"not seen", "seen", "applied", "rejected", "offer"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/postings")
def postings():
    conn = db.connect()
    rows = conn.execute(
        """SELECT p.id, p.title, p.url, p.location, p.department, p.tag, p.tag_hits,
                  p.first_seen, p.is_new, p.closed, p.user_status,
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
