# Internship Tracker

UBC co-op is so ass i had to take matters into my own hands

Personal job board: watches your companies' careers pages every other day,
flags new internship postings, emails a digest, and keeps a recruiter
directory per company.

## Daily use

```bash
make serve        # job board at http://localhost:8787
make check        # scrape everything right now
make notify       # send digest of NEW postings (email or macOS notification)
```

The scheduled job (`make schedule-install`) runs check+notify daily at 10:00;
a built-in 40-hour guard makes that effectively **every other day**, and
launchd catches up after sleep.

## Adding companies

Drop exports into `inputs/` then run `make ingest && make discover`:

- **LinkedIn**: Settings → Data privacy → *Get a copy of your data* →
  `Company Follows.csv`
- **Notion**: your table → ••• → Export → CSV
- **Twitter/X**: archive's `following.js`, or just a `twitter.txt` with one
  company per line
- anything else: any `.txt`/`.csv` with one company per line/row

`make discover` auto-detects each company's ATS (Greenhouse/Lever/Ashby/
Workable/SmartRecruiters/Recruitee/Workday) and stores a JSON feed URL;
leftovers are marked `needs_manual` and resolved by hand/agent via:

```bash
.venv/bin/python -m scraper.discover set "Company" --ats greenhouse --feed <api-url>
```

## Email digests

Create a Gmail **app password** (Google Account → Security → 2-Step
Verification → App passwords) and put it in `.env`:

```
GMAIL_ADDRESS=yeganehtagh13@gmail.com
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

Without it, you get a macOS notification instead of email.

## Layout

- `scraper/` — ingest → discover → run_check → notify pipeline (Python)
- `board/` — FastAPI + single-page UI
- `config/keywords.yaml` — internship markers + relevant/excluded keywords (edit freely)
- `data/tracker.db` — SQLite source of truth
- `inputs/` — your raw exports (gitignored)

## Hosted board (open it from any device)

The scraper keeps running on the laptop (launchd), but the database lives in
[Turso](https://turso.tech) (hosted SQLite) and the board is served by Vercel, so the
same data — including seen/applied statuses — is available everywhere.

One-time setup:

1. **Turso** — sign up (GitHub login), then in the dashboard create a database and an
   auth token. You'll get `libsql://<name>-<org>.turso.io` and a token.
2. **Local `.env`** — add `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `BOARD_PASSWORD`
   (see `.env.example`). From now on the scraper writes to Turso.
3. **Copy the existing data up:** `.venv/bin/python -m scraper.migrate_to_turso`
4. **Vercel** — import the GitHub repo; in *Settings → Environment Variables* add the
   same three variables; redeploy. The entrypoint is declared in `pyproject.toml`.

The board asks for a password (any username) whenever `BOARD_PASSWORD` is set.
`/api/health` reports which backend is in use. Without the Turso variables everything
falls back to the local `data/tracker.db` exactly as before.
