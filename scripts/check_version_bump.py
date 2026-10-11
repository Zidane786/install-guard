#!/usr/bin/env python3
"""CI guard: a PR that changes the shipped plugin (plugins/**) must carry a new version.

usage: python3 scripts/check_version_bump.py BASE_REF      (e.g. origin/main)

Passes when nothing under plugins/ changed. Otherwise requires: plugin.json's version is higher
than on BASE_REF, the CHANGELOG has a "## X.Y.Z — date" section for it, and there is no
"## Unreleased" heading (every merge to main ships as a release). Standard library only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = "plugins/install-guard/.claude-plugin/plugin.json"
CHANGELOG = ROOT / "plugins" / "install-guard" / "CHANGELOG.md"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def parse(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def sections(text: str) -> dict:
    """{version: section body (whitespace-normalised)} for every '## X.Y.Z — date' section."""
    out = {}
    for m in re.finditer(r"^## (\d+\.\d+\.\d+) — \d{4}-\d{2}-\d{2}$\n(.*?)(?=^## |\Z)", text, re.S | re.M):
        out[m.group(1)] = " ".join(m.group(2).split())
    return out


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 ("→" would crash)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    if not argv:
        print("usage: check_version_bump.py BASE_REF", file=sys.stderr)
        return 2
    base = argv[0]
    changed = [f for f in git("diff", "--name-only", f"{base}...HEAD").splitlines() if f.startswith("plugins/")]
    if not changed:
        print("No plugin files changed: no version bump needed.")
        return 0
    head_v = json.loads((ROOT / PLUGIN_JSON).read_text(encoding="utf-8"))["version"]
    try:
        base_v = json.loads(git("show", f"{base}:{PLUGIN_JSON}"))["version"]
    except subprocess.CalledProcessError:
        base_v = "0.0.0"
    problems = []
    if parse(head_v) <= parse(base_v):
        problems.append(f"plugin files changed but the version is still {head_v} (main has {base_v})\n"
                        f"  fix: bump the version everywhere (see CLAUDE.md 'Releasing a version') and add a "
                        f"CHANGELOG section for it")
    text = CHANGELOG.read_text(encoding="utf-8")
    if not re.search(r"^## " + re.escape(head_v) + r" — \d{4}-\d{2}-\d{2}$", text, re.M):
        problems.append(f"plugins/install-guard/CHANGELOG.md has no '## {head_v} — YYYY-MM-DD' section\n"
                        f"  fix: add it (Highlights / Added / Changed / Fixed / Security / Removed / Upgrade notes)")
    if re.search(r"^## Unreleased", text, re.M | re.I):
        problems.append("plugins/install-guard/CHANGELOG.md has an 'Unreleased' section; every merge to main is a release\n"
                        "  fix: rename it to the new version's '## X.Y.Z — YYYY-MM-DD'")
    # released sections are frozen: every "## X.Y.Z — date" section on the base branch must be unchanged
    try:
        base_text = git("show", f"{base}:plugins/install-guard/CHANGELOG.md")
    except subprocess.CalledProcessError:
        base_text = ""
    for v, body in sections(base_text).items():
        now = sections(text).get(v)
        if now is None:
            problems.append(f"the released {v} section was removed from the CHANGELOG\n  fix: restore it")
        elif now != body:
            problems.append(f"the released {v} section of the CHANGELOG was changed\n"
                            f"  fix: put new changes under {head_v} instead; a released version's notes stay as shipped")
    if problems:
        print(f"{len(changed)} plugin file(s) changed:", *("  " + f for f in changed[:10]), sep="\n")
        for p in problems:
            print("error: " + p, file=sys.stderr)
        return 1
    print(f"Version bump ok: {base_v} → {head_v}, CHANGELOG section present.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
