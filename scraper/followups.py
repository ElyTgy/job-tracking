"""Follow-up reminders for outreach: two nudges after you message someone.

Marking a contact "messaged" on the board (or a company's outreach stage
"reached out" with nobody attached) queues two rows in the followups table,
due 5 and 10 days after the message. Whatever is due gets emailed once, as a
single digest, by either of:

    python -m scraper.followups [--dry-run] [--today YYYY-MM-DD]
    GET /api/cron/followups on the board  (Vercel cron, see vercel.json)

sent_at makes each nudge exactly-once no matter how many of those fire on the
same day, so the laptop's launchd job and the hosted cron can both run it.
The schedule is fixed from the first message (day 5 / day 10): marking
"followed up" retires the earliest open nudge, "replied" / "no reply" retire
them all, and re-marking "messaged" starts over.
"""
import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape

from . import db, notify

STEP_DAYS = {1: 5, 2: 10}          # nudge number -> days after the first message
LAST_STEP = max(STEP_DAYS)
PURPOSES = ["referral", "application", "other"]
PURPOSE_TEXT = {
    "referral": "for a referral",
    "application": "to get your application looked at",
    "other": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _target(recruiter_id):
    """WHERE fragment for one person, or for the company-level thread when None."""
    if recruiter_id is None:
        return "recruiter_id IS NULL", ()
    return "recruiter_id=?", (recruiter_id,)


# --------------------------------------------------------------------- queue

def schedule(conn, company_id: int, recruiter_id, start: date, now: str | None = None) -> None:
    """Queue both nudges for this thread, replacing anything still open on it."""
    now = now or _now()
    cancel(conn, company_id, recruiter_id, now)
    for step, days in STEP_DAYS.items():
        conn.execute(
            "INSERT INTO followups (company_id, recruiter_id, step, due, created_at) VALUES (?,?,?,?,?)",
            (company_id, recruiter_id, step, (start + timedelta(days=days)).isoformat(), now),
        )


def cancel(conn, company_id: int, recruiter_id, now: str | None = None) -> None:
    """Retire every open nudge on this thread (they replied, you gave up, or reset)."""
    where, params = _target(recruiter_id)
    conn.execute(
        f"UPDATE followups SET done_at=? WHERE company_id=? AND {where} AND done_at IS NULL",
        (now or _now(), company_id, *params),
    )


def acknowledge(conn, company_id: int, recruiter_id, now: str | None = None) -> None:
    """You followed up: retire the earliest open nudge. The schedule is fixed from the
    first message, so the later nudge stays where it is."""
    where, params = _target(recruiter_id)
    row = conn.execute(
        f"SELECT id FROM followups WHERE company_id=? AND {where} AND done_at IS NULL "
        "ORDER BY step LIMIT 1", (company_id, *params)).fetchone()
    if row:
        conn.execute("UPDATE followups SET done_at=? WHERE id=?", (now or _now(), row["id"]))


def on_contact_status(conn, company_id: int, recruiter_id: int, status: str,
                      today: date | None = None) -> None:
    """Keep the queue in step with a contact's stage on the board."""
    today = today or _today()
    if status == "messaged":
        schedule(conn, company_id, recruiter_id, today)
    elif status == "followed up":
        acknowledge(conn, company_id, recruiter_id)
    else:  # replied, no reply, or back to a pre-send stage
        cancel(conn, company_id, recruiter_id)


def on_company_status(conn, company_id: int, status: str, today: date | None = None) -> None:
    """Company-level thread: only used when you reached out without a named contact
    (a DM to a founder, a generic careers inbox). Contact-level nudges always win."""
    today = today or _today()
    if status == "reached out":
        contact_level = conn.execute(
            "SELECT 1 FROM followups WHERE company_id=? AND recruiter_id IS NOT NULL AND done_at IS NULL",
            (company_id,)).fetchone()
        if not contact_level:
            schedule(conn, company_id, None, today)
    elif status == "following up":
        acknowledge(conn, company_id, None)
    elif status == "no response":
        # giving up on the company retires every thread, named contacts included
        conn.execute("UPDATE followups SET done_at=? WHERE company_id=? AND done_at IS NULL",
                     (_now(), company_id))
    elif status in ("replied", "not started"):
        cancel(conn, company_id, None)


def pending(conn, company_id: int | None = None) -> list:
    """Open nudges (sent or not) for the board: the earliest one per thread is what
    the contact row shows as 'follow up by …' / 'overdue'."""
    sql = ("SELECT id, company_id, recruiter_id, step, due, sent_at FROM followups "
           "WHERE done_at IS NULL")
    params: tuple = ()
    if company_id is not None:
        sql += " AND company_id=?"
        params = (company_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY due, step", params).fetchall()]


# ---------------------------------------------------------------------- send

def due(conn, today: date) -> list:
    """Nudges to email today: unsent, not retired, due on or before today. When
    both nudges of a thread are overdue at once (the cron didn't run for days),
    only the later one is listed; send() still stamps both as sent."""
    rows = conn.execute(
        """SELECT f.id, f.step, f.due, f.recruiter_id, f.company_id,
                  c.name AS company, c.website, c.outreach_at, c.outreach_notes,
                  r.name, r.title, r.email, r.linkedin_url, r.purpose, r.status_at, r.notes
           FROM followups f
           JOIN companies c ON c.id=f.company_id
           LEFT JOIN recruiters r ON r.id=f.recruiter_id
           WHERE f.sent_at IS NULL AND f.done_at IS NULL AND f.due <= ?
           ORDER BY f.due, c.name COLLATE NOCASE, r.name""",
        (today.isoformat(),)).fetchall()
    by_thread: dict = {}
    for r in rows:
        d = dict(r)
        key = (d["company_id"], d["recruiter_id"])
        prev = by_thread.get(key)
        if prev is None:
            by_thread[key] = {**d, "ids": [d["id"]]}
        else:
            prev["ids"].append(d["id"])
            if d["step"] > prev["step"]:
                prev.update({k: d[k] for k in ("id", "step", "due")})
    return list(by_thread.values())


def _days_ago(day: str | None, today: date) -> str:
    if not day:
        return ""
    n = (today - date.fromisoformat(day[:10])).days
    return "today" if n == 0 else f"{n} day{'s' if n != 1 else ''} ago"


def build_html(rows: list, today: date) -> str:
    board = os.environ.get("BOARD_URL", "").rstrip("/")
    parts = [
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:640px'>",
        f"<h2 style='margin-bottom:4px'>{len(rows)} follow-up{'s' if len(rows) != 1 else ''} due</h2>",
        "<p style='color:#666;margin-top:0'>From your internship tracker"
        + (f" &middot; <a href='{board}'>open the board</a>" if board else "")
        + "</p><ul style='padding-left:18px'>",
    ]
    for r in rows:
        nudge = f"{r['step']} of {LAST_STEP}" + (" (the last one)" if r["step"] >= LAST_STEP else "")
        if r["recruiter_id"] is None:
            who = f"<b>{escape(r['company'])}</b>"
            when = f"you marked outreach as reached out {_days_ago(r['outreach_at'], today)}"
            links = ""
            note = r["outreach_notes"]
        else:
            title = f" <span style='color:#666'>{escape(r['title'])}</span>" if r["title"] else ""
            who = f"<b>{escape(r['name'])}</b>{title} &middot; {escape(r['company'])}"
            why = PURPOSE_TEXT.get(r["purpose"] or "", "")
            when = f"messaged {_days_ago(r['status_at'], today)}" + (f" {why}" if why else "")
            bits = []
            if r["email"]:
                bits.append(f"<a href='mailto:{escape(r['email'])}'>{escape(r['email'])}</a>")
            if r["linkedin_url"]:
                site = "X" if "x.com" in r["linkedin_url"] or "twitter.com" in r["linkedin_url"] else "LinkedIn"
                bits.append(f"<a href='{escape(r['linkedin_url'])}'>{site}</a>")
            links = " &middot; ".join(bits)
            note = r["notes"]
        parts.append(
            f"<li style='margin:8px 0'>{who}<br>"
            f"<span style='color:#666'>{when} &middot; nudge {nudge}</span>"
            + (f"<br>{links}" if links else "")
            + (f"<br><span style='color:#888;font-size:13px'>{escape(note[:300])}</span>" if note else "")
            + "</li>"
        )
    parts.append("</ul></div>")
    return "".join(parts)


def send(conn, today: date | None = None, dry_run: bool = False) -> dict:
    """Email everything due and stamp it sent. Returns what happened so the cron
    endpoint and the CLI can both report it."""
    today = today or _today()
    rows = due(conn, today)
    if not rows:
        return {"due": 0, "sent": 0}
    subject = f"[Outreach] {len(rows)} follow-up{'s' if len(rows) != 1 else ''} due"
    html = build_html(rows, today)
    if dry_run:
        return {"due": len(rows), "sent": 0, "dry_run": True, "subject": subject, "html": html}
    if not notify.send_email(subject, html):
        return {"due": len(rows), "sent": 0, "error": "no Gmail credentials (GMAIL_ADDRESS / GMAIL_APP_PASSWORD)"}
    ids = [i for r in rows for i in r["ids"]]
    conn.execute(f"UPDATE followups SET sent_at=? WHERE id IN ({','.join('?' * len(ids))})",
                 (_now(), *ids))
    conn.commit()
    return {"due": len(rows), "sent": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the email instead of sending")
    ap.add_argument("--today", help="pretend it is this date (YYYY-MM-DD), for testing")
    a = ap.parse_args()
    notify.load_env()
    today = date.fromisoformat(a.today) if a.today else _today()
    conn = db.connect()
    r = send(conn, today, dry_run=a.dry_run)
    if a.dry_run and r.get("due"):
        print(r["subject"]); print(r["html"])
    elif r.get("error"):
        print(f"{r['due']} follow-ups due but not sent: {r['error']}", file=sys.stderr)
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{r["due"]} outreach follow-ups due" '
                            'with title "Internship Tracker"'], check=False)
        return 1
    else:
        print(f"{r['due']} follow-ups due, {r['sent']} emailed to {notify.TO_ADDRESS}."
              if r["due"] else "No follow-ups due today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
