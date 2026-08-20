# Internship Tracker

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
