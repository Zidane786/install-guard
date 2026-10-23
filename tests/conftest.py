"""Make the plugin's engine importable as `engine.*` in every test."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "install-guard"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
