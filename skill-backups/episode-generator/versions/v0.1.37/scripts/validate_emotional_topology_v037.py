#!/usr/bin/env python3
"""Validate v0.1.37 emotional-spine topology and state read/write reachability."""

from __future__ import annotations

import argparse
import re
from collections import defaultdict, deque
from pathlib import Path

from validate_topology_v032 import parse, validate


HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
STATE_SPLIT = re.compile(r"[、，,；;]\s*")


def blocks(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    matches = list(HEADER.finditer(text))
    return {
        match.group(1): text[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        for index, match in enumerate(matches)
    }


def required(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        raise ValueError(f"缺少字段：{name}")
    return match.group(1).strip()


def states(value: str) -> set[str]:
    if value in {"", "无"}:
        return set()
    result: set[str] = set()
    for item in STATE_SPLIT.split(value):
        token = re.split(r"(?:\+=|-=|=|→|->|:|：)", item.strip(), maxsplit=1)[0].strip("` ")
        if token:
            result.add(token)
    return result


def ancestors(nodes: dict[str, dict[str, object]]) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        for target in node["successors"]:
            incoming[target].add(node_id)
    result: dict[str, set[str]] = {}
    for node_id in nodes:
        seen: set[str] = set()
        queue = deque(incoming[node_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(incoming[current])
        result[node_id] = seen
    return result


def validate_v037(path: Path, synopsis: Path | None) -> list[str]:
    nodes = parse(path)
    # Reuse the proven graph checks, but do not inherit v0.1.32's project-specific
    # assumption that every story must have exactly three formal endings and one
    # failure ending. The requested ending contract belongs to the upstream brief.
    issues = [
        issue for issue in validate(nodes, synopsis)
        if not issue.startswith("结局数量错误：")
    ]
    raw = blocks(path)
    reads: dict[str, set[str]] = {}
    writes: dict[str, set[str]] = {}
    emotions = {"起", "承", "转", "合"}
    required_fields = [
        "节点类型", "情绪位置", "现场触发", "人物当下欲望", "现实阻力", "采取行动",
        "即时事实", "控制权变化", "状态读取", "状态写入", "下一催化",
    ]
    for node_id, block in raw.items():
        values: dict[str, str] = {}
        for name in required_fields:
            try:
                values[name] = required(block, name)
            except ValueError:
                issues.append(f"{node_id}缺少{name}")
        if values.get("情绪位置") not in emotions:
            issues.append(f"{node_id}情绪位置无效：{values.get('情绪位置', '')}")
        reads[node_id] = states(values.get("状态读取", ""))
        writes[node_id] = states(values.get("状态写入", ""))

    prior = ancestors(nodes)
    for node_id, read_set in reads.items():
        available: set[str] = set()
        for ancestor in prior[node_id]:
            available.update(writes.get(ancestor, set()))
        for state in sorted(read_set - available):
            issues.append(f"{node_id}读取状态但此前无写入：{state}")

    for node_id, node in nodes.items():
        choices = list(node["choices"])
        if len(choices) < 2:
            continue
        targets = [target for _, target in choices]
        signatures: list[tuple[frozenset[str], str]] = []
        for target in targets:
            target_block = raw.get(target, "")
            target_type = ""
            fact = ""
            try:
                target_type = required(target_block, "节点类型")
                fact = required(target_block, "即时事实")
            except ValueError:
                pass
            if target_type not in {"result", "ending"}:
                issues.append(f"{node_id}选项未先进入独立结果或结局节点：{target}")
            signatures.append((frozenset(writes.get(target, set())), fact))
        if len(set(signatures)) < len(signatures):
            issues.append(f"{node_id}存在即时事实和状态效果均相同的假分支")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("topology", type=Path)
    parser.add_argument("--synopsis", type=Path)
    args = parser.parse_args()
    try:
        issues = validate_v037(args.topology, args.synopsis)
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    count = len(parse(args.topology))
    print(f"PASS: {count} emotional-spine nodes; structure, state reads/writes, result branches and endings valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
