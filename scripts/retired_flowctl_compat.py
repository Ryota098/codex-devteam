#!/usr/bin/env python3
"""Temporary pass-through for clients that already loaded the retired hook."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        # Existing clients may still invoke this path until their session ends.
        # Consume the hook payload and deliberately produce no decision.
        sys.stdin.read()
        return 0
    print("flowctlは廃止済みです。現在の役割Skillとdocs/flowの資料に従って続行してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
