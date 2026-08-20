"""Merge company lists from inputs/ into the companies table.

Handles:
  - LinkedIn data export:  inputs/Company Follows.csv   (column "Organization")
  - Notion CSV export:     any inputs/*.csv with a name-ish column
  - Twitter/X archive:     inputs/following.js (best effort) or inputs/twitter.txt
  - Plain lists:           inputs/*.txt, one company per line

Re-runnable: dedupes on normalized name and unions the `sources` field.
Writes data/companies_review.csv for the user to prune.
"""
import csv
import json
import re
import sys
from pathlib import Path

from . import db

INPUTS = Path(__file__).resolve().parent.parent / "inputs"
REVIEW = Path(__file__).resolve().parent.parent / "data" / "companies_review.csv"

NAME_COLUMNS = ["organization", "company", "company name", "name", "companies"]
WEBSITE_COLUMNS = ["website", "url", "site", "domain", "link"]


def normalize(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[,.]?\s*(inc|llc|ltd|corp|corporation|co)\.?$", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def parse_csv(path: Path) -> tuple[list[dict], str]:
    source = "linkedin" if "follow" in path.name.lower() else "notion"
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        # LinkedIn exports sometimes prefix note lines before the header
        sample = f.read()
    lines = sample.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:10]):
        cells = [c.strip().strip('"').lower() for c in line.split(",")]
        if any(c in NAME_COLUMNS for c in cells):
            header_idx = i
            break
    reader = csv.DictReader(lines[header_idx:])
    cols = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
    name_col = next((cols[c] for c in NAME_COLUMNS if c in cols), None)
    site_col = next((cols[c] for c in WEBSITE_COLUMNS if c in cols), None)
    if not name_col:
        print(f"  ! {path.name}: no company-name column found, skipping", file=sys.stderr)
        return [], source
    for row in reader:
        name = (row.get(name_col) or "").strip()
        if name:
            out.append({"name": name, "website": (row.get(site_col) or "").strip() if site_col else ""})
    return out, source


def parse_txt(path: Path) -> tuple[list[dict], str]:
    source = "twitter" if "twitter" in path.name.lower() else "manual"
    out = []
    for line in path.read_text().splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if line and not line.startswith("#"):
            out.append({"name": line, "website": ""})
    return out, source


def parse_following_js(path: Path) -> tuple[list[dict], str]:
    """Twitter archive following.js only has account ids/handles; keep handles
    as names for the user to map, better than dropping them."""
    text = path.read_text()
    text = re.sub(r"^window\.[^=]+=\s*", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], "twitter"
    out = []
    for item in data:
        f = item.get("following", {})
        handle = f.get("userLink", "").rsplit("/", 1)[-1] or f.get("accountId", "")
        if handle:
            out.append({"name": handle, "website": ""})
    return out, "twitter"


def main():
    conn = db.connect()
    files = sorted(INPUTS.glob("*")) if INPUTS.exists() else []
    if not files:
        print(f"No input files found in {INPUTS}/ — drop your exports there.")
        return 0

    added = merged = 0
    for path in files:
        if path.name.startswith("."):
            continue
        if path.suffix == ".csv":
            rows, source = parse_csv(path)
        elif path.name == "following.js":
            rows, source = parse_following_js(path)
        elif path.suffix == ".txt":
            rows, source = parse_txt(path)
        else:
            print(f"  ? skipping unrecognized file {path.name}")
            continue
        print(f"  {path.name}: {len(rows)} companies ({source})")
        for r in rows:
            norm = normalize(r["name"])
            if not norm:
                continue
            row = conn.execute(
                "SELECT id, sources, website FROM companies WHERE normalized_name=?", (norm,)
            ).fetchone()
            if row:
                sources = set((row["sources"] or "").split(",")) | {source}
                conn.execute(
                    "UPDATE companies SET sources=?, website=COALESCE(NULLIF(website,''),?) WHERE id=?",
                    (",".join(sorted(filter(None, sources))), r["website"], row["id"]),
                )
                merged += 1
            else:
                conn.execute(
                    "INSERT INTO companies (name, normalized_name, website, sources) VALUES (?,?,?,?)",
                    (r["name"], norm, r["website"], source),
                )
                added += 1
    conn.commit()

    rows = conn.execute(
        "SELECT name, website, sources, discovery_status FROM companies ORDER BY name COLLATE NOCASE"
    ).fetchall()
    with open(REVIEW, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "website", "sources", "discovery_status"])
        w.writerows([tuple(r) for r in rows])
    print(f"\n{added} added, {merged} merged. Total: {len(rows)} companies.")
    print(f"Review list written to {REVIEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
