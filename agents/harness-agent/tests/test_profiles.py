"""Profile card loading for harness virtual agents (unified products)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "profiles"

EXPECTED = {
    "claude-code",
    "deepseek-harness",
    "pi",
    "openclaw",
    "hermes",
}


def test_all_profiles_load():
    assert {p.name for p in PROFILES.iterdir() if p.is_dir()} == EXPECTED
    for name in EXPECTED:
        card = json.loads((PROFILES / name / "agent-card.json").read_text(encoding="utf-8"))
        assert (PROFILES / name / "system.md").exists()
        skills = card.get("skills") or []
        assert skills == [], name
        # Endpoint is set by aopd / AGENT_URL / PORT — not baked into the card.
        assert "url" not in card, name


def test_only_harness_agent_dir_exists():
    agents_root = ROOT.parent
    dirs = {p.name for p in agents_root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    assert dirs == {"harness-agent"} or dirs <= {"harness-agent", ".pytest_cache"}
    assert "search-agent" not in dirs
