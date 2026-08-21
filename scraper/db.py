"""DB layer: schema + small helpers.

Two backends, chosen at connect() time:
  * Turso/libSQL (hosted SQLite) when TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set
    (read from the environment or the repo's .env). This is what lets the board on
    Vercel and the scraper on the laptop share one database.
  * Plain local sqlite3 at data/tracker.db otherwise.

Both expose the same sqlite3-style API the rest of the code uses: conn.execute(...)
returning a cursor with fetchone/fetchall/rowcount/lastrowid, rows that support
r["col"], r[0], dict(r) and tuple(r).
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    website TEXT,
    careers_url TEXT,
    ats_type TEXT,              -- greenhouse | lever | ashby | workable | smartrecruiters
                                -- | recruitee | workday | html | needs_manual | NULL (undiscovered)
    feed_url TEXT,              -- machine-readable jobs endpoint when ats_type has one
    sources TEXT,               -- comma list: linkedin, twitter, notion, manual
    discovery_status TEXT DEFAULT 'pending',  -- pending | ok | needs_manual | dead
    last_checked TEXT,
    last_check_status TEXT,     -- ok | error:<msg>
    no_contact_found INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    posting_key TEXT NOT NULL,  -- stable id: ats job id, else url, else hash
    title TEXT NOT NULL,
    url TEXT,
    location TEXT,
    department TEXT,
    posted_date TEXT,
    tag TEXT NOT NULL DEFAULT 'other',   -- relevant | excluded-interest | other
    tag_hits TEXT,              -- which keywords matched, for display chips
    loc_ok INTEGER DEFAULT 1,   -- location matches config/locations.yaml
    pinned INTEGER DEFAULT 0,   -- manual standing posting; never auto-closed
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_new INTEGER DEFAULT 1,   -- set on insert, cleared after the next run's digest
    closed INTEGER DEFAULT 0,
    user_status TEXT DEFAULT 'not seen',  -- not seen | seen | applied | rejected | offer
    UNIQUE(company_id, posting_key)
);

CREATE TABLE IF NOT EXISTS recruiters (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    title TEXT,
    email TEXT,
    email_status TEXT,          -- verified | unverified | not_found
    linkedin_url TEXT,
    source TEXT,                -- clay | websearch
    UNIQUE(company_id, name)
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    linkedin_url TEXT UNIQUE,
    title TEXT,
    company TEXT,
    email TEXT,
    email_status TEXT,          -- verified | unverified | not_found
    notes TEXT,
    user_status TEXT DEFAULT 'to contact',  -- to contact | contacted | replied | meeting | closed
    added TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    companies_checked INTEGER DEFAULT 0,
    companies_failed INTEGER DEFAULT 0,
    new_postings INTEGER DEFAULT 0,
    closed_postings INTEGER DEFAULT 0,
    notes TEXT
);
"""


MIGRATIONS = [
    # loc_ok: 1 when the posting's location matches config/locations.yaml
    "ALTER TABLE postings ADD COLUMN loc_ok INTEGER DEFAULT 1",
    # pinned: manual standing postings (e.g. "hires interns year-round") the scraper never closes
    "ALTER TABLE postings ADD COLUMN pinned INTEGER DEFAULT 0",
    # misses: consecutive check runs in which the posting was absent from the feed;
    # closed only once this reaches 2 so a single flaky scrape can't close jobs
    "ALTER TABLE postings ADD COLUMN misses INTEGER DEFAULT 0",
]


def load_env() -> None:
    """Populate os.environ from the repo's .env (never overriding real env vars)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- rows

class Row(tuple):
    """Tuple that also answers to column names, like sqlite3.Row."""
    __slots__ = ()
    _cols: tuple = ()

    def keys(self):
        return list(self._cols)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._cols.index(key))
            except ValueError:
                raise KeyError(key) from None
        return tuple.__getitem__(self, key)


def _row_class(cols):
    return type("Row", (Row,), {"__slots__": (), "_cols": tuple(cols)})


class _Cursor:
    """Wraps a libsql cursor so rows come back as Row objects."""

    def __init__(self, cur):
        self._cur = cur
        self._rowcls = _row_class([d[0] for d in (cur.description or ())])

    def __getattr__(self, name):  # rowcount, lastrowid, description, ...
        return getattr(self._cur, name)

    def _wrap(self, r):
        return None if r is None else self._rowcls(r)

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Conn:
    """Wraps a libsql connection to return wrapped cursors."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, params=()):
        return _Cursor(self._conn.execute(sql, tuple(params)))

    def executemany(self, sql, seq):
        return _Cursor(self._conn.executemany(sql, [tuple(p) for p in seq]))


# ------------------------------------------------------------------------ connect

def _apply_schema(conn) -> None:
    bare = "\n".join(line.split("--", 1)[0] for line in SCHEMA.splitlines())
    for stmt in bare.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for mig in MIGRATIONS:
        try:
            conn.execute(mig)
        except Exception:  # sqlite3.OperationalError locally, ValueError from libsql
            pass  # column already exists
    conn.commit()


def connect():
    load_env()
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    if url and token:
        import libsql  # hosted backend; only needed when configured
        conn = _Conn(libsql.connect(url, auth_token=token))
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    _apply_schema(conn)
    return conn
