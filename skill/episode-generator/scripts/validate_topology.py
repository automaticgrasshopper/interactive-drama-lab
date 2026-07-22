#!/usr/bin/env python3
"""Deterministically validate a v0.1.32 one-node-one-episode topology."""

from __future__ import annotations

import argparse
import re
from collections import deque
from pathlib import Path


HEADER = re.compile(r"^##\s+(episode-(\d{3}))\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE = re.compile(r"episode-\d{3}")


def field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        raise ValueError(f"缺少字段：{name}")
    return match.group(1).strip()


def parse(path: Path) -> dict[str, dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    matches = list(HEADER.finditer(text))
    if not matches:
        raise ValueError("未找到分集节点")
    nodes: dict[str, dict[str, object]] = {}
    for index, match in enumerate(matches):
        node_id = match.group(1)
        if node_id in nodes:
            raise ValueError(f"重复节点：{node_id}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        successor_text = field(block, "后续节点")
        successors = EPISODE.findall(successor_text) if successor_text != "无" else []
        choices = re.findall(
            r"^-\s*选择：\s*(.*?)\s*(?:->|→)\s*(episode-\d{3})\s*$",
            block,
            re.MULTILINE,
        )
        nodes[node_id] = {
            "number": int(match.group(2)),
            "title": match.group(3).strip(),
            "successors": successors,
            "choices": choices,
            "interaction": field(block, "互动类型"),
            "ending": field(block, "结局") == "是",
        }
    return nodes


def validate(nodes: dict[str, dict[str, object]], synopsis: Path | None) -> list[str]:
    issues: list[str] = []
    expected = list(range(1, len(nodes) + 1))
    actual = sorted(int(node["number"]) for node in nodes.values())
    if actual != expected:
        issues.append(f"编号不连续：{actual}")

    incoming = {node_id: 0 for node_id in nodes}
    for node_id, node in nodes.items():
        successors = list(node["successors"])
        ending = bool(node["ending"])
        if ending and successors:
            issues.append(f"结局仍有后继：{node_id}")
        if not ending and not successors:
            issues.append(f"非结局缺少后继：{node_id}")
        for target in successors:
            if target not in nodes:
                issues.append(f"跳转目标不存在：{node_id}->{target}")
            else:
                incoming[target] += 1
        choices = list(node["choices"])
        if "选择" in str(node["interaction"]) and "结果" not in str(node["interaction"]):
            targets = [target for _, target in choices]
            if len(set(targets)) < 2:
                issues.append(f"选择出口不足两个不同目标：{node_id}")
            if any(target not in successors for target in targets):
                issues.append(f"选择目标未列入后继：{node_id}")

    roots = sorted(node_id for node_id, count in incoming.items() if count == 0)
    if roots != ["episode-001"]:
        issues.append(f"入口不唯一或错误：{roots}")

    visited: set[str] = set()
    queue = deque(roots)
    while queue:
        node_id = queue.popleft()
        if node_id in visited:
            continue
        visited.add(node_id)
        queue.extend(target for target in nodes[node_id]["successors"] if target in nodes)
    missing = sorted(set(nodes) - visited)
    if missing:
        issues.append(f"存在不可达节点：{missing}")

    indegree = incoming.copy()
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    count = 0
    while queue:
        node_id = queue.popleft()
        count += 1
        for target in nodes[node_id]["successors"]:
            if target not in indegree:
                continue
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if count != len(nodes):
        issues.append("拓扑存在循环")

    if synopsis:
        synopsis_ids = re.findall(r"^##\s+(episode-\d{3})", synopsis.read_text(encoding="utf-8"), re.MULTILINE)
        if synopsis_ids != list(nodes):
            issues.append("公开梗概编号或顺序与拓扑不一致")

    formal = sum(1 for node in nodes.values() if node["ending"] and "正式结局" in str(node["interaction"]))
    failure = sum(1 for node in nodes.values() if node["ending"] and "失败小结局" in str(node["interaction"]))
    if formal != 3 or failure != 1:
        issues.append(f"结局数量错误：正式{formal}，失败{failure}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("topology", type=Path)
    parser.add_argument("--synopsis", type=Path)
    args = parser.parse_args()
    try:
        nodes = parse(args.topology)
        issues = validate(nodes, args.synopsis)
    except (OSError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print(f"PASS: {len(nodes)} nodes, one root, all reachable, acyclic, 3 formal endings, 1 failure ending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
