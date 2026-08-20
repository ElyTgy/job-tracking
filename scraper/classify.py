"""Internship detection + interest tagging, driven by config/keywords.yaml."""
import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"
LOCATIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "locations.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    if LOCATIONS_PATH.exists():
        with open(LOCATIONS_PATH) as f:
            cfg["locations"] = yaml.safe_load(f) or {}
    else:
        cfg["locations"] = {}
    return cfg


def is_degree_excluded(title: str, cfg: dict) -> bool:
    """True when the title demands a degree the user can't apply with (PhD/MS/...)."""
    exc = cfg.get("title_exclusions") or {}
    text = title.lower()
    return bool(_phrase_hits(text, exc.get("phrase")) or _word_hits(text, exc.get("word")))


def location_ok(location: str, cfg: dict) -> bool:
    """True when any of the posting's locations matches the allowed list.
    Unknown/empty locations pass — unknown is not the same as excluded."""
    allowed = (cfg.get("locations") or {}).get("allowed") or []
    if not location or not allowed:
        return True
    text = location.lower()
    return any(a.lower() in text for a in allowed)


def _phrase_hits(text: str, phrases: list[str]) -> list[str]:
    return [p for p in phrases or [] if p.lower() in text]


def _word_hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words or [] if re.search(rf"\b{re.escape(w.lower())}\b", text)]


def is_internship(title: str, cfg: dict) -> bool:
    text = title.lower()
    # word-boundary check so "internal tools" or "international" don't match "intern"
    for marker in cfg["internship_markers"]:
        m = marker.lower()
        if " " in m or "-" in m:
            if m in text:
                return True
        elif re.search(rf"\b{re.escape(m)}\b", text):
            return True
    return False


def tag_posting(title: str, department: str, cfg: dict) -> tuple[str, str]:
    """Returns (tag, comma-joined keyword hits). Relevant wins over excluded."""
    text = f"{title} {department or ''}".lower()
    rel = _phrase_hits(text, cfg["relevant"].get("phrase")) + _word_hits(
        text, cfg["relevant"].get("word")
    )
    if rel:
        return "relevant", ",".join(dict.fromkeys(rel))
    exc = _phrase_hits(text, cfg["excluded"].get("phrase")) + _word_hits(
        text, cfg["excluded"].get("word")
    )
    if exc:
        return "excluded-interest", ",".join(dict.fromkeys(exc))
    return "other", ""
