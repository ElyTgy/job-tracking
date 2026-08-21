"""Add / update people you want to reach out to (shown on the board's People tab).

    python -m scraper.people add "Jane Doe" --linkedin URL [--title T] [--company C] [--email E] [--notes N]
    python -m scraper.people list
"""
import argparse
import sys
from datetime import datetime, timezone

from . import db


def cmd_add(a):
    conn = db.connect()
    conn.execute(
        """INSERT INTO people (name, linkedin_url, title, company, email, email_status, notes, added)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(linkedin_url) DO UPDATE SET
             name=excluded.name,
             title=COALESCE(excluded.title, people.title),
             company=COALESCE(excluded.company, people.company),
             email=COALESCE(excluded.email, people.email),
             email_status=COALESCE(excluded.email_status, people.email_status),
             notes=COALESCE(excluded.notes, people.notes)""",
        (a.name, a.linkedin, a.title, a.company, a.email,
         ("verified" if a.email else None) if a.email_status is None else a.email_status,
         a.notes, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
    )
    conn.commit()
    print(f"saved {a.name}")


def cmd_list(a):
    conn = db.connect()
    for r in conn.execute("SELECT * FROM people ORDER BY added DESC, name"):
        print(f"{r['name']} | {r['title'] or ''} @ {r['company'] or ''} | {r['email'] or '-'} | {r['user_status']}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("name"); s.add_argument("--linkedin", required=True)
    s.add_argument("--title"); s.add_argument("--company"); s.add_argument("--email")
    s.add_argument("--email-status", dest="email_status"); s.add_argument("--notes")
    s.set_defaults(fn=cmd_add)
    l = sub.add_parser("list"); l.set_defaults(fn=cmd_list)
    a = ap.parse_args(); return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
