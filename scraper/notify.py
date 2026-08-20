"""Email digest of NEW postings from the latest run.

Reads Gmail SMTP credentials from .env (GMAIL_ADDRESS, GMAIL_APP_PASSWORD).
Without credentials it falls back to a macOS notification so new postings are
never silent. Run right after run_check:
    python -m scraper.notify
"""
import os
import smtplib
import subprocess
import sys
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


def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def new_postings(conn):
    return conn.execute(
        """SELECT p.*, c.name AS company FROM postings p
           JOIN companies c ON c.id=p.company_id
           WHERE p.is_new=1 AND p.closed=0
           ORDER BY p.tag='relevant' DESC, c.name COLLATE NOCASE, p.title"""
    ).fetchall()


def build_html(rows) -> str:
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
    rows = new_postings(conn)
    if not rows:
        print("No new postings; nothing to send.")
        return 0
    relevant = sum(1 for r in rows if r["tag"] == "relevant")
    subject = f"[Internships] {len(rows)} new posting{'s' if len(rows) != 1 else ''}"
    if relevant:
        subject += f" ({relevant} relevant)"
    if send_email(subject, build_html(rows)):
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
