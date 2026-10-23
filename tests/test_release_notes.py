"""The release workflow publishes the CHANGELOG section of plugin.json's version: keep them in step."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "release_notes.py"
PLUGIN_JSON = REPO / "plugins" / "install-guard" / ".claude-plugin" / "plugin.json"
CHANGELOG = REPO / "plugins" / "install-guard" / "CHANGELOG.md"
KNOWN = {"Highlights", "Added", "Changed", "Fixed", "Security", "Removed", "Deprecated", "Upgrade notes"}


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)


def test_plugin_version_has_a_changelog_section() -> None:
    version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    r = run()
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "### Highlights" in out
    # install and upgrade come first, before the change sections, and appear once
    assert out.index("### Install (new project)") < out.index("### Upgrade (existing projects)") < out.index("### Highlights")
    # the Upgrade notes section is moved up into "Upgrade (existing projects)", not repeated below
    assert out.count("### Install (new project)") == 1 and out.count("### Upgrade (existing projects)") == 1
    assert "### Upgrade notes" not in out
    assert run("--version").stdout.strip() == version


def test_every_version_section_uses_the_standard_headings() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"^## (\d+\.\d+\.\d+) — \d{4}-\d{2}-\d{2}$", text, re.M)
    assert versions, "no '## X.Y.Z — YYYY-MM-DD' headings"
    for v in versions:
        body = run(v).stdout
        heads = re.findall(r"^### (.+?)\s*$", body, re.M)
        unknown = [h for h in heads if h not in KNOWN | {"Install (new project)", "Upgrade (existing projects)"}]
        assert not unknown, f"{v}: unknown section(s) {unknown}"
        assert "Highlights" in heads and ("Added" in heads or "Changed" in heads or "Fixed" in heads), v


def test_unknown_version_fails_with_a_fix() -> None:
    r = run("9.9.9")
    assert r.returncode == 1 and "fix:" in r.stderr


def test_version_is_the_same_everywhere() -> None:
    """A release bumps every copy of the version; a half-done bump would ship mixed files."""
    version = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    # Task 6 adds guard.py: its `VERSION = "..."` must equal this version too.
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)


def test_no_unreleased_section() -> None:
    assert not re.search(r"^## Unreleased", CHANGELOG.read_text(encoding="utf-8"), re.M | re.I), \
        "every merge to main is a release: name the section after the new version"
