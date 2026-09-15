#!/usr/bin/env python3
"""Remove retired ai-devteam workflow hooks without touching other hooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEGACY_COMMAND = re.compile(
    r"(?:^|/)flowctl(?:\.py)?['\"]?\s+hook\s+--provider\s+(?:codex|claude)\b"
)


def is_retired_group(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        command = str(hook.get("command", ""))
        if ".ai-devteam/bin/flowctl" in command or LEGACY_COMMAND.search(command):
            return True
    return False


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.ai-devteam-cleanup.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def clean(path: Path) -> bool:
    if not path.is_file():
        print(f"legacy hooks: no config -> {path}")
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSONを解析できないため変更しません: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"設定のrootがobjectではないため変更しません: {path}")

    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        print(f"legacy hooks: absent -> {path}")
        return False

    changed = False
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        retained = [group for group in groups if not is_retired_group(group)]
        if retained == groups:
            continue
        changed = True
        if retained:
            hooks[event] = retained
        else:
            del hooks[event]
    if not changed:
        print(f"legacy hooks: already absent -> {path}")
        return False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.ai-devteam-retired-backup-{stamp}")
    shutil.copy2(path, backup)
    atomic_write_json(path, value)
    print(f"legacy hooks: removed -> {path}")
    print(f"backup: {backup}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retired ai-devteam workflow hooksだけを設定から除去する"
    )
    parser.add_argument("config", nargs="*", type=Path)
    args = parser.parse_args(argv)
    paths = args.config or [Path.home() / ".codex" / "hooks.json", Path.home() / ".claude" / "settings.json"]
    try:
        for path in paths:
            clean(path.expanduser().resolve())
    except (OSError, ValueError) as error:
        print(f"legacy hook cleanup: FAIL\n{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
