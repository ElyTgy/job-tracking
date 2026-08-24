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
import threading
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
    is_new INTEGER DEFAULT 1,   -- board's NEW badge: recomputed at the end of every full run
    notified_at TEXT,           -- when a digest reported this posting; NULL = never emailed
    closed INTEGER DEFAULT 0,
    user_status TEXT DEFAULT 'new',  -- new | apply later | backlog | irrelevant | applied | interviewing | rejected | offer
    export_status TEXT,         -- us-person-required | export-license-possible
                                -- | export-mentioned | clear | unknown | NULL (unread)
    export_regime TEXT,         -- itar | ear | itar+ear | none
    visa_sponsorship TEXT,      -- no-sponsorship | unstated
    export_evidence TEXT,       -- the sentence that triggered export_status
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
    scope TEXT DEFAULT 'full',  -- full | company (a --company run checks one feed)
    finished TEXT,
    companies_checked INTEGER DEFAULT 0,
    companies_failed INTEGER DEFAULT 0,
    new_postings INTEGER DEFAULT 0,
    closed_postings INTEGER DEFAULT 0,
    notes TEXT
);
"""


MIGRATIONS = [
    # audit_note / manual_note: coverage-audit findings and the user's own notes (Issues tab)
    "ALTER TABLE companies ADD COLUMN audit_note TEXT",
    "ALTER TABLE companies ADD COLUMN manual_note TEXT",
    # loc_ok: 1 when the posting's location matches config/locations.yaml
    "ALTER TABLE postings ADD COLUMN loc_ok INTEGER DEFAULT 1",
    # pinned: manual standing postings (e.g. "hires interns year-round") the scraper never closes
    "ALTER TABLE postings ADD COLUMN pinned INTEGER DEFAULT 0",
    # misses: consecutive check runs in which the posting was absent from the feed;
    # closed only once this reaches 2 so a single flaky scrape can't close jobs
    "ALTER TABLE postings ADD COLUMN misses INTEGER DEFAULT 0",
    # eligibility gates read out of the job description during the scrape.
    # export_status: us-person-required | export-license-possible | export-mentioned
    #                | clear | unknown | NULL (not yet read)
    "ALTER TABLE postings ADD COLUMN export_status TEXT",
    # export_regime: which rule the text names -- itar | ear | itar+ear | none
    "ALTER TABLE postings ADD COLUMN export_regime TEXT",
    # visa_sponsorship: no-sponsorship | unstated
    "ALTER TABLE postings ADD COLUMN visa_sponsorship TEXT",
    # the sentence that triggered the status, so the board can show its evidence
    "ALTER TABLE postings ADD COLUMN export_evidence TEXT",
    # user_status triage rename: not seen/seen -> new, hidden -> irrelevant. Idempotent,
    # and also catches rows an older DB's 'not seen' column default may still insert.
    "UPDATE postings SET user_status='new' WHERE user_status IN ('not seen', 'seen')",
    "UPDATE postings SET user_status='irrelevant' WHERE user_status='hidden'",
    # notified_at: the digest that reported this posting; NULL = never emailed. This
    # is what makes the email exactly-once -- is_new can't, because it is recomputed
    # per run and notify may fire more than once between runs.
    # The backfill runs ONLY on the migration that adds the column (see _apply_schema),
    # so upgrading an existing DB doesn't email the entire board once; re-running it
    # on every connect would stamp brand-new postings as sent before notify saw them.
    # Rows still carrying the NEW badge are the ones the pending digest was about to
    # cover, so they stay unnotified and go out once; everything older is stamped as
    # already sent.
    ("ALTER TABLE postings ADD COLUMN notified_at TEXT",
     ["UPDATE postings SET notified_at=last_seen WHERE is_new=0"]),
    # scope: 'company' for --company runs, so they can't reset the 40h spacing guard
    # and starve the full run that the digest depends on.
    ("ALTER TABLE runs ADD COLUMN scope TEXT",
     ["UPDATE runs SET scope=CASE WHEN companies_checked>1 THEN 'full' ELSE 'company' END"]),
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
    """Wraps a connection (sqlite3 or libsql) to return wrapped cursors and
    serialize access.

    A single connection is shared for the whole process (see connect() below),
    reused across FastAPI's request threads rather than opened fresh per
    request -- so every call here takes a lock instead of assuming exclusive
    access. SQLite-family connections aren't safe for concurrent use from
    multiple threads at once, and this is a single-user personal board where
    queries are cheap, so serializing is the simple, correct trade-off.

    On error the connection drops itself from the connect() cache, so one
    that's gone stale (idle timeout, dropped network) self-heals on the next
    connect() instead of poisoning every request after it.
    """

    def __init__(self, conn, lock):
        self._conn = conn
        self._lock = lock

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, params=()):
        with self._lock:
            try:
                return _Cursor(self._conn.execute(sql, tuple(params)))
            except Exception:
                _invalidate_cache()
                raise

    def executemany(self, sql, seq):
        with self._lock:
            try:
                return _Cursor(self._conn.executemany(sql, [tuple(p) for p in seq]))
            except Exception:
                _invalidate_cache()
                raise

    def commit(self):
        with self._lock:
            return self._conn.commit()


# ------------------------------------------------------------------------ connect

def _apply_schema(conn) -> None:
    bare = "\n".join(line.split("--", 1)[0] for line in SCHEMA.splitlines())
    for stmt in bare.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for mig in MIGRATIONS:
        # A migration is either a bare statement or (statement, [follow-ups]); the
        # follow-ups run only when the statement itself succeeded, which is how a
        # one-shot backfill rides along with the ALTER that adds its column without
        # re-running on every later connect.
        mig, follow_ups = mig if isinstance(mig, tuple) else (mig, ())
        try:
            conn.execute(mig)
        except Exception:  # sqlite3.OperationalError locally, ValueError from libsql
            continue  # column already exists -- and its backfill already ran
        for stmt in follow_ups:
            conn.execute(stmt)
    conn.commit()


_SCHEMA_APPLIED: dict = {}

# One connection for the whole process, reused across every call instead of
# reopened per request. FastAPI's sync route handlers run on a threadpool, so
# a per-thread cache would still pay a fresh connection cost every time a new
# worker thread picks up a request (threads come and go -- they aren't a
# fixed pool). A single shared connection guarded by a re-entrant lock avoids
# that: the TLS handshake to Turso happens once per process, and callers just
# take turns. Keyed by (url, token) so a changed backend never hands back a
# connection to the wrong database.
_LOCK = threading.RLock()
_STATE = {"conn": None, "key": None}


def _invalidate_cache() -> None:
    _STATE["conn"] = None
    _STATE["key"] = None


def connect():
    load_env()
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")
    key = (url, token)
    with _LOCK:
        if _STATE["key"] == key and _STATE["conn"] is not None:
            return _STATE["conn"]

        if url and token:
            import libsql  # hosted backend; only needed when configured
            conn = _Conn(libsql.connect(url, auth_token=token), _LOCK)
        else:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            # timeout: wait for a concurrent writer (scraper/audit runs) instead of
            # failing the request with "database is locked". check_same_thread=False
            # because this one connection is now shared across request threads --
            # safe since _Conn serializes all access through _LOCK.
            raw = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
            raw.row_factory = sqlite3.Row
            conn = _Conn(raw, _LOCK)
        # Schema/migrations need a write lock; do it once per process, not per request.
        if not _SCHEMA_APPLIED.get(str(DB_PATH)):
            _apply_schema(conn)
            _SCHEMA_APPLIED[str(DB_PATH)] = True

        _STATE["conn"] = conn
        _STATE["key"] = key
        return conn
