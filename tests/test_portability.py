"""Windows guard: every Python script install-guard ships or runs in CI must speak UTF-8.

Windows consoles and pipes default to cp1252, so printing "→", "·" or "—" crashes a script that
doesn't reconfigure its output, and text files or subprocess output are misread without an
explicit encoding. These checks are static, so they catch the problem on any OS.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Every .py under plugins/install-guard (hooks, the generator and audit copied into projects, ...) and scripts/.
SCRIPTS = sorted([*(REPO / "plugins/install-guard").rglob("*.py"), *(REPO / "scripts").glob("*.py")])


def calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def kw(call: ast.Call, name: str):
    return next((k for k in call.keywords if k.arg == name), None)


def fname(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: str(p.relative_to(REPO)))
def test_entry_points_reconfigure_output_to_utf8(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    if "__main__" not in src:
        pytest.skip("not an entry point")
    assert "reconfigure(" in src and "utf-8" in src, f"{path.name} must reconfigure stdout/stderr to UTF-8"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: str(p.relative_to(REPO)))
def test_text_io_and_subprocess_name_an_encoding(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for c in calls(tree):
        name = fname(c)
        if name in ("read_text", "write_text") and not kw(c, "encoding"):
            bad.append(f"line {c.lineno}: {name}() without encoding=")
        if name == "open" and isinstance(c.func, ast.Name):
            mode = c.args[1] if len(c.args) > 1 else (kw(c, "mode").value if kw(c, "mode") else None)
            binary = isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value
            if not binary and not kw(c, "encoding"):
                bad.append(f"line {c.lineno}: open() in text mode without encoding=")
        if name in ("run", "Popen", "check_output"):
            t = kw(c, "text") or kw(c, "universal_newlines")
            if t is not None and isinstance(t.value, ast.Constant) and t.value.value is True and not kw(c, "encoding"):
                bad.append(f"line {c.lineno}: subprocess.{name}(text=True) without encoding=")
    assert not bad, f"{path.name}:\n  " + "\n  ".join(bad)
