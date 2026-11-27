"""Every alert links to a runbook section that exists (a broken link costs time in an incident)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ALERTS = ROOT / "deploy" / "monitoring" / "alerts.yml"


def _anchor(heading: str) -> str:
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


@pytest.mark.skipif(not ALERTS.exists(), reason="deploy/ is not in this checkout")
def test_alert_runbook_links_resolve() -> None:
    rules = [r for g in yaml.safe_load(ALERTS.read_text())["groups"] for r in g["rules"]]
    assert rules
    broken = []
    for rule in rules:
        link = rule["annotations"]["runbook"]
        path, _, anchor = link.partition("#")
        doc = ROOT / path
        headings = (
            {
                _anchor(line.lstrip("#"))
                for line in doc.read_text().splitlines()
                if line.startswith("#")
            }
            if doc.exists()
            else set()
        )
        if not doc.exists() or (anchor and anchor not in headings):
            broken.append(f"{rule['alert']}: {link}")
    assert broken == []
