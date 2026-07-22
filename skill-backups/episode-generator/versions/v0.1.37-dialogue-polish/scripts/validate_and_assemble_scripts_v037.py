#!/usr/bin/env python3
"""Run the v0.1.37 nine-field assembler with causal-dialogue format checks."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import validate_and_assemble_scripts_v036 as base


original_validate_episode = base.validate_episode


def validate_episode(node_id: str, node: dict[str, object], text: str) -> list[str]:
    issues = original_validate_episode(node_id, node, text)
    try:
        script = base.section(text, "分集剧本")
    except ValueError:
        return issues
    if re.search(r"^[^\n：]{1,20}：\s*[“\"]", script, re.MULTILINE):
        issues.append(f"对白必须使用人物：台词，不使用台词引号：{node_id}")
    return issues


base.validate_episode = validate_episode


if __name__ == "__main__":
    sys.argv[0] = str(Path(__file__).name)
    raise SystemExit(base.main())
