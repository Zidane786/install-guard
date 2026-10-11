"""The marketplace and plugin manifests carry the agreed names, version and metadata."""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "install-guard"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_json_fields() -> None:
    p = load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert p["name"] == "install-guard"
    assert p["version"] == "1.0.0"
    assert p["author"] == {"name": "Zidane786"}
    assert p["homepage"] == "https://github.com/Zidane786/install-guard"
    assert p["repository"] == "https://github.com/Zidane786/install-guard"
    assert p["description"] == ("Stop coding agents from installing or downloading anything outside "
                                "your allowed routes, in Claude Code, Copilot CLI and Kiro.")
    assert p["keywords"] == ["security", "supply-chain", "install", "download", "registry", "nexus"]


def test_marketplace_points_at_the_plugin() -> None:
    m = load(REPO / ".claude-plugin" / "marketplace.json")
    assert m["name"] == "install-guard"
    assert m["owner"] == {"name": "Zidane786"}
    (entry,) = m["plugins"]
    assert entry["name"] == "install-guard" and entry["source"] == "./plugins/install-guard"
    assert (REPO / entry["source"]).is_dir()


def test_agent_guide_files() -> None:
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    for n in range(1, 8):
        assert f"\n## {n}. " in agents
    assert "@AGENTS.md" in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    s = load(REPO / ".claude" / "settings.json")
    assert s["attribution"] == {"commit": "", "pr": ""} and s["includeCoAuthoredBy"] is False


def test_release_workflow() -> None:
    w = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "plugins/install-guard/.claude-plugin/plugin.json" in w
    assert 'install-guard $VERSION' in w and "branches: [main]" in w
    assert "Slate" not in w and "slate" not in w
