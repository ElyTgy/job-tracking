"""Internship detection + interest tagging, driven by config/keywords.yaml."""
import re
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "keywords.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


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
