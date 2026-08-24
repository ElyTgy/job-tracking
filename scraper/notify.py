"""Email digest of postings no earlier digest has reported.

Reads Gmail SMTP credentials from .env (GMAIL_ADDRESS, GMAIL_APP_PASSWORD).
Without credentials it falls back to a macOS notification so new postings are
never silent. Run right after run_check:
    python -m scraper.notify
"""
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from . import db

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
TO_ADDRESS = "yeganehtagh13@gmail.com"

TAG_ORDER = {"relevant": 0, "other": 1, "excluded-interest": 2}
TAG_LABEL = {
    "relevant": "Relevant to you",
    "other": "Other internships",
    "excluded-interest": "Filtered interests (materials/mechanical)",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def new_postings(conn):
    """Postings no digest has reported yet, in preferred locations only; the ids of
    the ones elsewhere are returned separately and reported as a count.

    Selects on notified_at, NOT is_new. is_new is the board's NEW badge and is
    recomputed on every full run, so it says "appeared in the latest run" rather
    than "you haven't been told yet" -- and notify runs more often than full runs
    do (launchd calls it daily, but run_check's 40h guard skips most of those
    days), which is exactly how the same postings went out several days running.
    """
    rows = conn.execute(
        """SELECT p.*, c.name AS company FROM postings p
           JOIN companies c ON c.id=p.company_id
           WHERE p.notified_at IS NULL AND p.closed=0 AND p.loc_ok=1
           ORDER BY p.tag='relevant' DESC, c.name COLLATE NOCASE, p.title"""
    ).fetchall()
    elsewhere_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM postings WHERE notified_at IS NULL AND closed=0 AND loc_ok=0"
        ).fetchall()
    ]
    return rows, elsewhere_ids


def mark_notified(conn, ids, sent_at: str) -> None:
    """Stamp exactly the postings this digest reported -- the listed ones and the
    ones behind the elsewhere count. Stamping by id rather than by a WHERE that
    re-runs the selection means a scrape landing between building the email and
    sending it can't mark postings as sent that the email never listed.

    Only ever called after a successful send: if the email failed and we fell back
    to the macOS notification, the user still hasn't seen the list, so those
    postings stay unnotified and go out with the next digest.
    """
    ids = list(ids)
    for i in range(0, len(ids), 200):  # keep the IN list a sane size
        chunk = ids[i : i + 200]
        conn.execute(
            f"UPDATE postings SET notified_at=? WHERE id IN ({','.join('?' * len(chunk))})",
            (sent_at, *chunk),
        )
    conn.commit()


def build_html(rows, elsewhere: int = 0) -> str:
    sections: dict[str, list] = {}
    for r in rows:
        sections.setdefault(r["tag"], []).append(r)
    parts = [
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:640px'>",
        f"<h2 style='margin-bottom:4px'>{len(rows)} new internship posting"
        f"{'s' if len(rows) != 1 else ''}</h2>",
        "<p style='color:#666;margin-top:0'>From your internship tracker &middot; "
        "full board: <code>make serve</code> in job-tracking</p>",
    ]
    for tag in sorted(sections, key=lambda t: TAG_ORDER.get(t, 9)):
        parts.append(
            f"<h3 style='border-bottom:1px solid #ddd;padding-bottom:4px'>"
            f"{TAG_LABEL.get(tag, tag)} ({len(sections[tag])})</h3><ul style='padding-left:18px'>"
        )
        for r in sections[tag]:
            loc = f" — {r['location']}" if r["location"] else ""
            link = r["url"] or "#"
            parts.append(
                f"<li style='margin:6px 0'><a href='{link}'>{r['title']}</a>"
                f"<br><span style='color:#666'>{r['company']}{loc}</span></li>"
            )
        parts.append("</ul>")
    if elsewhere:
        parts.append(
            f"<p style='color:#999'>+{elsewhere} new posting"
            f"{'s' if elsewhere != 1 else ''} outside your preferred locations "
            "(visible on the board under Location: Anywhere).</p>"
        )
    parts.append("</div>")
    return "".join(parts)


def send_email(subject: str, html: str) -> bool:
    addr = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = TO_ADDRESS
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw)
        s.sendmail(addr, [TO_ADDRESS], msg.as_string())
    return True


def macos_notify(count: int):
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{count} new internship postings on your board" '
            'with title "Internship Tracker"',
        ],
        check=False,
    )


def main():
    load_env()
    conn = db.connect()
    rows, elsewhere_ids = new_postings(conn)
    elsewhere = len(elsewhere_ids)
    if not rows:
        print(f"No new postings in preferred locations ({elsewhere} elsewhere); nothing to send.")
        return 0
    relevant = sum(1 for r in rows if r["tag"] == "relevant")
    subject = f"[Internships] {len(rows)} new posting{'s' if len(rows) != 1 else ''}"
    if relevant:
        subject += f" ({relevant} relevant)"
    if send_email(subject, build_html(rows, elsewhere)):
        mark_notified(conn, [r["id"] for r in rows] + elsewhere_ids, _now())
        print(f"Emailed digest of {len(rows)} postings to {TO_ADDRESS}.")
    else:
        macos_notify(len(rows))
        print(
            f"No Gmail credentials in .env — sent macOS notification instead "
            f"({len(rows)} new postings)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
