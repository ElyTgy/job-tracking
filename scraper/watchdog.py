"""Staleness watchdog: alert when the scheduled full check has stopped running.

The laptop's launchd job can die before any of our code runs (Sep 11-26 2026:
every fire exited 78/EX_CONFIG, silently, for two weeks), so the alarm can't
live in that job. It runs from the hosted board instead -- Vercel calls
GET /api/cron/watchdog daily (vercel.json) -- and reads the shared Turso DB:
if no full run has *finished* in STALE_HOURS, it emails once per day until one
does. The board header shows the same check as a red banner.

    python -m scraper.watchdog [--dry-run]
"""
import argparse
import sys
from datetime import datetime, timezone

from . import db, notify

# The job fires daily with a 40h guard, so a healthy gap is ~48h; 72h means at
# least one scheduled run has been missed outright.
STALE_HOURS = 72


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def status(conn, now: datetime | None = None) -> dict:
    """Last finished full run, its age, and whether that counts as stale."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT id, started, finished, companies_checked, companies_failed, new_postings "
        "FROM runs WHERE scope='full' AND finished IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    # a started-but-unfinished full run newer than the last good one = crashed or in progress
    attempt = conn.execute(
        "SELECT started FROM runs WHERE scope='full' AND finished IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"stale": True, "last_full": None, "age_hours": None, "stale_hours": STALE_HOURS}
    age = (now - _parse(row["finished"])).total_seconds() / 3600
    return {
        "stale": age > STALE_HOURS,
        "age_hours": round(age, 1),
        "stale_hours": STALE_HOURS,
        "last_full": dict(row),
        "unfinished_since": attempt["started"] if attempt and attempt["started"] > row["started"] else None,
    }


def _alerted_today(conn, today: str) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='watchdog_alerted'").fetchone()
    return bool(row and row["value"] == today)


def check(conn, dry_run: bool = False) -> dict:
    """Email an alert if stale (at most once per UTC day). Returns what happened."""
    st = status(conn)
    if not st["stale"]:
        return {**st, "emailed": False}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _alerted_today(conn, today):
        return {**st, "emailed": False, "note": "already alerted today"}
    last = st["last_full"]
    when = f"{last['finished'][:16].replace('T', ' ')} UTC ({st['age_hours'] / 24:.1f} days ago)" if last else "never"
    extra = (f"<p>A full run started at {st['unfinished_since']} but never finished (crashed, or the "
             f"laptop slept mid-run).</p>") if st.get("unfinished_since") else ""
    html = f"""<p><b>The internship tracker hasn't completed a full check since {when}.</b>
    Job boards aren't being scraped and no digests are going out.</p>{extra}
    <p>On the laptop:</p><ul>
    <li><code>launchctl print gui/$(id -u)/com.yeganeh.internship-check | grep -E "state|last exit"</code>
        &mdash; exit code 78 means launchd couldn't start the job at all (see the plist comments).</li>
    <li><code>tail logs/check.log logs/check.err.log</code></li>
    <li>Run one now: <code>make check</code>; reinstall the schedule: <code>make schedule-install</code></li>
    </ul><p>This alert repeats daily until a full run finishes.</p>"""
    if dry_run:
        return {**st, "emailed": False, "note": "dry run"}
    sent = notify.send_email("⚠️ Internship tracker: no full check in "
                             f"{(st['age_hours'] or 0) / 24:.0f}+ days", html)
    if sent:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('watchdog_alerted', ?)", (today,))
        conn.commit()
    return {**st, "emailed": bool(sent)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    notify.load_env()
    res = check(db.connect(), dry_run=args.dry_run)
    print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
