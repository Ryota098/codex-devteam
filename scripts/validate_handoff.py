#!/usr/bin/env python3
"""リポジトリ内のPM Skillに同梱した引き渡し検証を実行する。"""

from __future__ import annotations

import runpy
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "codex"
    / "skills"
    / "pm"
    / "scripts"
    / "validate_handoff.py"
)

if not SCRIPT.is_file():
    raise SystemExit(f"validator not found: {SCRIPT}")

runpy.run_path(str(SCRIPT), run_name="__main__")

