#!/usr/bin/env python3
"""Validate frozen v0.1.32 episode scripts and assemble the public screenplay."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from validate_topology_v032 import parse


INTERACTIVE = ("情感选择", "关键选择")


def visible_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--minimum", type=int, default=366)
    parser.add_argument("--maximum", type=int, default=448)
    args = parser.parse_args()

    topology = parse(args.cache_root / "topology.md")
    issues: list[str] = []
    scripts: list[str] = []
    digests: list[str] = []

    for index, (node_id, node) in enumerate(topology.items(), start=1):
        path = args.cache_root / "episodes" / f"{node_id}.md"
        if not path.exists():
            issues.append(f"缺少正文：{node_id}")
            continue
        text = path.read_text(encoding="utf-8").strip()
        expected_heading = f"# 第{index}集《{node['title']}》"
        if not text.startswith(expected_heading):
            issues.append(f"标题不一致：{node_id}")
        count = visible_count(text)
        if not args.minimum <= count <= args.maximum:
            issues.append(f"长度不合格：{node_id}={count}")

        interaction = str(node["interaction"])
        is_player_choice = any(label in interaction for label in INTERACTIVE) and "结果" not in interaction
        option_targets = re.findall(r"→\s*(episode-\d{3})", text)
        expected_targets = [target for _, target in node["choices"]]
        if is_player_choice:
            if "选择：" not in text:
                issues.append(f"缺少选择问题：{node_id}")
            if option_targets != expected_targets:
                issues.append(f"选择目标不一致：{node_id}={option_targets} expected={expected_targets}")
        elif option_targets:
            issues.append(f"非玩家选择集出现选项箭头：{node_id}")

        scripts.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        digests.append(f"{node_id} {count} {digest}")

    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1

    assembled = "# 《昨日关系人》完整分集剧本\n\n" + "\n\n---\n\n".join(scripts) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(assembled, encoding="utf-8")
    print("PASS: 20 frozen scripts validated and assembled")
    for item in digests:
        print(item)
    print(f"public {hashlib.sha256(assembled.encode('utf-8')).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
