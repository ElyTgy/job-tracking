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

serve:            ## run the job board at http://localhost:8787
	.venv/bin/uvicorn board.app:app --port 8787

schedule-install: ## install the every-other-day launchd job
	cp launchd/com.yeganeh.internship-check.plist ~/Library/LaunchAgents/
	launchctl unload ~/Library/LaunchAgents/com.yeganeh.internship-check.plist 2>/dev/null; \
	launchctl load ~/Library/LaunchAgents/com.yeganeh.internship-check.plist
	@echo "Installed. Test with: launchctl start com.yeganeh.internship-check"

.PHONY: setup ingest discover check notify serve schedule-install
