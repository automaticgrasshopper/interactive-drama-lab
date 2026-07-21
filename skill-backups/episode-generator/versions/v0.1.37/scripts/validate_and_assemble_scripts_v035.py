#!/usr/bin/env python3
"""Validate v0.1.35 screenplay structure and optionally assemble all episodes."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from validate_topology_v032 import parse


INTERACTIVE = ("情感选择", "关键选择")
SCENE_HEADING = re.compile(r"^【[^】·]+(?:·[^】·]+){2,}】$", re.MULTILINE)
FORBIDDEN_LABELS = ("扩展场面", "收束场面", "补充场面", "返工", "优化后", "润色版")


def visible_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def validate_episode(
    node_id: str,
    index: int,
    node: dict[str, object],
    text: str,
) -> list[str]:
    issues: list[str] = []
    expected_heading = f"# 第{index}集《{node['title']}》"
    if not text.startswith(expected_heading):
        issues.append(f"标题不一致：{node_id}")
    if not SCENE_HEADING.search(text):
        issues.append(f"缺少标准场次标题：{node_id}")
    if re.search(r"^\s*△", text, re.MULTILINE):
        issues.append(f"动作行不得使用△：{node_id}")
    if re.search(r"^\s*出场：", text, re.MULTILINE):
        issues.append(f"不得输出出场栏：{node_id}")
    for label in FORBIDDEN_LABELS:
        if label in text:
            issues.append(f"含修订标签：{node_id}/{label}")

    interaction = str(node["interaction"])
    is_player_choice = any(label in interaction for label in INTERACTIVE) and "结果" not in interaction
    option_targets = re.findall(r"→\s*(episode-\d{3})", text)
    expected_targets = [target for _, target in node["choices"]]
    if is_player_choice:
        if "选择：" not in text:
            issues.append(f"缺少选择问题：{node_id}")
        if option_targets != expected_targets:
            issues.append(
                f"选择目标不一致：{node_id}={option_targets} expected={expected_targets}"
            )
    elif option_targets:
        issues.append(f"非玩家选择集出现选项箭头：{node_id}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_root", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--episode")
    parser.add_argument("--script", type=Path)
    args = parser.parse_args()

    topology = parse(args.cache_root / "topology.md")
    if args.episode and args.episode not in topology:
        raise SystemExit(f"未知分集：{args.episode}")
    if args.script and not args.episode:
        raise SystemExit("--script 必须与 --episode 同时使用")

    issues: list[str] = []
    scripts: list[str] = []
    digests: list[str] = []
    items = list(topology.items())
    for index, (node_id, node) in enumerate(items, start=1):
        if args.episode and node_id != args.episode:
            continue
        path = args.script or args.cache_root / "episodes" / f"{node_id}.md"
        if not path.is_file():
            issues.append(f"缺少正文：{node_id}")
            continue
        text = path.read_text(encoding="utf-8").strip()
        issues.extend(validate_episode(node_id, index, node, text))
        scripts.append(text)
        digests.append(
            f"{node_id} {visible_count(text)} {hashlib.sha256(text.encode('utf-8')).hexdigest()}"
        )

    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1

    if args.episode:
        print(f"PASS: {args.episode}")
    else:
        if not args.output:
            raise SystemExit("全剧验收必须提供 output")
        assembled = "# 《昨日关系人》完整分集剧本\n\n" + "\n\n---\n\n".join(scripts) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(assembled, encoding="utf-8")
        print(f"PASS: {len(scripts)} frozen scripts validated and assembled")
        print(f"public {hashlib.sha256(assembled.encode('utf-8')).hexdigest()}")
    for item in digests:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
