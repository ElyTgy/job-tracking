"""SQLite layer: schema + small helpers. Single source of truth in data/tracker.db."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracker.db"

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


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for mig in MIGRATIONS:
        try:
            conn.execute(mig)
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn
