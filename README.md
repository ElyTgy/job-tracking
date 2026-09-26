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
make followups    # email the outreach follow-up reminders due today
```

The scheduled job (`make schedule-install`) runs check+notify+followups daily at
10:00; a built-in 40-hour guard makes the scrape effectively **every other day**,
and launchd catches up after sleep. If `launchctl print gui/$(id -u)/com.yeganeh.internship-check`
shows `last exit code = 78` the job never spawned: that happened once when the
log files picked up a macOS privacy tag, which is why the plist redirects its own
output instead of using StandardOutPath.

## Outreach follow-ups

On the Companies tab, set a contact to **messaged** (and pick what you asked for:
referral, application push, other). Two reminders are queued: 5 and 10 days after
the message. Each morning whatever is due goes out as one email listing the person,
company, email/LinkedIn, and your notes. Marking them **followed up** retires the
next reminder; **replied** or **no reply** retires both; re-marking **messaged**
starts over. Setting a company's outreach stage to **reached out** with no contact
attached queues the same two reminders for the company itself. The **Follow-ups**
filter lists what is queued, overdue first.

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

Follow-up reminders use the same credentials. They are sent by the laptop job and
by a daily Vercel cron (`vercel.json` → `GET /api/cron/followups`), so they go out
even when the laptop is asleep; each reminder is emailed once whichever runs first.

**Watchdog.** A second Vercel cron (`GET /api/cron/watchdog`, `scraper/watchdog.py`)
emails you if no full check has *finished* in 72 hours, and the board shows a red
banner for the same condition. It runs off the laptop on purpose: in Sep 2026 the
launchd job died silently for two weeks (every fire exited 78 before any code ran)
and nothing noticed. If you get the alert, check
`launchctl print gui/$(id -u)/com.yeganeh.internship-check` and `logs/check.err.log`,
then `make check` / `make schedule-install`.

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
5. **Follow-up cron** — also add `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `CRON_SECRET`
   (any long random string; Vercel sends it as a bearer token when it calls the cron
   path) and optionally `BOARD_URL` (linked from the email). Redeploy so the cron in
   `vercel.json` is registered.

The board asks for a password (any username) whenever `BOARD_PASSWORD` is set.
`/api/health` reports which backend is in use. Without the Turso variables everything
falls back to the local `data/tracker.db` exactly as before.
