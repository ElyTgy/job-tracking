PY := .venv/bin/python

setup:            ## create venv + install deps
	uv venv .venv --python 3.12
	uv pip install -p .venv/bin/python -r requirements.txt

ingest:           ## merge inputs/ files into the company list
	$(PY) -m scraper.ingest

discover:         ## auto-detect careers pages / ATS feeds
	$(PY) -m scraper.discover probe

check:            ## scrape all feeds now (ignores the 40h guard)
	$(PY) -m scraper.run_check --force

notify:           ## email/notify digest of NEW postings
	$(PY) -m scraper.notify

followups:        ## email the outreach follow-up reminders due today
	$(PY) -m scraper.followups

serve:            ## run the job board at http://localhost:8787
	.venv/bin/uvicorn board.app:app --port 8787

schedule-install: ## install the every-other-day launchd job
	cp launchd/com.yeganeh.internship-check.plist ~/Library/LaunchAgents/
	# bootout/bootstrap, not the legacy unload/load: a job registered the old way picked up
	# a managed code requirement in Sep 2026 and every fire died with EX_CONFIG (78).
	launchctl bootout gui/$$(id -u)/com.yeganeh.internship-check 2>/dev/null; \
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/com.yeganeh.internship-check.plist
	@echo "Installed. Test with: launchctl kickstart gui/$$(id -u)/com.yeganeh.internship-check"

.PHONY: setup ingest discover check notify followups serve schedule-install
