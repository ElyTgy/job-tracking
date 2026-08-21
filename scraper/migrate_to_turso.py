"""One-shot: copy the local data/tracker.db into the Turso database from .env.

    .venv/bin/python -m scraper.migrate_to_turso

Safe to re-run: it refuses to run if the remote already has companies unless --force.
"""
import argparse
import sqlite3

import libsql

from scraper import db

TABLES = ["companies", "postings", "recruiters", "people", "runs"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="wipe remote tables first")
    args = ap.parse_args()

    db.load_env()
    import os
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if not (url and token):
        raise SystemExit("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not set in .env")

    local = sqlite3.connect(db.DB_PATH)
    local.row_factory = sqlite3.Row
    remote = db._Conn(libsql.connect(url, auth_token=token))
    db._apply_schema(remote)

    n = remote.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    if n and not args.force:
        raise SystemExit(f"remote already has {n} companies; pass --force to overwrite")
    if args.force:
        for t in reversed(TABLES):
            remote.execute(f"DELETE FROM {t}")
        remote.commit()

    for t in TABLES:
        rows = local.execute(f"SELECT * FROM {t}").fetchall()
        if not rows:
            print(f"{t}: 0 rows"); continue
        cols = rows[0].keys()
        sql = f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        for i in range(0, len(rows), 500):
            remote.executemany(sql, [tuple(r) for r in rows[i:i + 500]])
            remote.commit()
        print(f"{t}: {len(rows)} rows")
    print("done")


if __name__ == "__main__":
    main()
