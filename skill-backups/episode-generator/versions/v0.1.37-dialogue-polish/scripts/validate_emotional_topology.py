#!/usr/bin/env python3
"""Validate the frozen emotional-spine topology graph."""

from __future__ import annotations

import argparse
from pathlib import Path

from validate_topology_v032 import parse, validate


def validate_emotional_topology(path: Path, synopsis: Path | None) -> list[str]:
    nodes = parse(path)
    return [
        issue
        for issue in validate(nodes, synopsis)
        if not issue.startswith("结局数量错误：")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("topology", type=Path)
    parser.add_argument("--synopsis", type=Path)
    args = parser.parse_args()
    try:
        issues = validate_emotional_topology(args.topology, args.synopsis)
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    count = len(parse(args.topology))
    print(f"PASS: {count} nodes; one root, all reachable, acyclic, and choice exits valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
