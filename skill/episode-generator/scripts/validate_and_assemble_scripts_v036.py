#!/usr/bin/env python3
"""Validate v0.1.36 nine-field episode scripts and assemble the public script."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from validate_topology import parse


FIELDS = [
    "分集编号",
    "分集标题",
    "分集剧本",
    "剧本分析",
    "关联角色",
    "关联场景",
    "关联道具",
    "是否结局",
    "互动节点",
]
SCRIPT_SUBFIELDS = ["单集梗概", "完整剧本"]
ANALYSIS_SUBFIELDS = ["本集冲突", "前置节点编号列表", "后续节点编号列表"]
INTERACTION_SUBFIELDS = [
    "是否为分支节点",
    "是否有选择问题",
    "选择问题",
    "选项列表",
    "默认下一分集编号",
]
CHARACTERS = {"周屿", "林乔", "陈铮", "顾惟安", "许曼青"}
SCENES = {"临川社区档案馆", "雨夜老城区", "停运地铁支线", "火灾档案库房", "河岸仓储区"}
PROPS = {"黑色旧录音机", "焦痕值班表", "停摆旧钟", "缺段监控硬盘"}


def section(text: str, name: str) -> str:
    match = re.search(rf"^# {re.escape(name)}\n\n(.*?)(?=^# |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError(f"缺少一级字段：{name}")
    return match.group(1).strip()


def subfields(block: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", block, re.MULTILINE)


def values(block: str) -> set[str]:
    return {item.strip() for item in re.split(r"[、，,]", block) if item.strip() and item.strip() != "无"}


def validate_episode(node_id: str, node: dict[str, object], text: str) -> list[str]:
    issues: list[str] = []
    headings = re.findall(r"^# (.+?)\s*$", text, re.MULTILINE)
    if headings != FIELDS:
        issues.append(f"九字段错误：{node_id}={headings}")
        return issues
    try:
        blocks = {name: section(text, name) for name in FIELDS}
    except ValueError as error:
        return [f"{node_id}/{error}"]
    if blocks["分集编号"] != node_id:
        issues.append(f"编号错误：{node_id}")
    if blocks["分集标题"] != str(node["title"]):
        issues.append(f"标题错误：{node_id}")
    if subfields(blocks["分集剧本"]) != SCRIPT_SUBFIELDS:
        issues.append(f"分集剧本子字段错误：{node_id}")
    if subfields(blocks["剧本分析"]) != ANALYSIS_SUBFIELDS:
        issues.append(f"剧本分析子字段错误：{node_id}")
    if subfields(blocks["互动节点"]) != INTERACTION_SUBFIELDS:
        issues.append(f"互动节点子字段错误：{node_id}")
    if re.search(r"^\s*△", text, re.MULTILINE) or re.search(r"^\s*出场：", text, re.MULTILINE):
        issues.append(f"含禁用正文格式：{node_id}")
    if not re.search(r"^【[^】·]+(?:·[^】·]+){2,}】$", blocks["分集剧本"], re.MULTILINE):
        issues.append(f"缺少场次标题：{node_id}")
    if not values(blocks["关联角色"]) <= CHARACTERS:
        issues.append(f"出现非正式角色资产：{node_id}")
    if not values(blocks["关联场景"]) <= SCENES:
        issues.append(f"出现非正式场景资产：{node_id}")
    if not values(blocks["关联道具"]) <= PROPS:
        issues.append(f"出现非正式道具资产：{node_id}")
    expected_ending = "是" if bool(node["ending"]) else "否"
    if blocks["是否结局"] != expected_ending:
        issues.append(f"结局标记错误：{node_id}")
    expected_targets = [target for _, target in node["choices"]]
    option_block = re.search(
        r"^## 选项列表\n\n(.*?)(?=^## 默认下一分集编号)",
        blocks["互动节点"],
        re.MULTILINE | re.DOTALL,
    )
    actual_targets = re.findall(r"目标分集编号：(episode-\d{3})", option_block.group(1) if option_block else "")
    if actual_targets != expected_targets:
        issues.append(f"选项目标错误：{node_id}={actual_targets} expected={expected_targets}")
    option_numbers = re.findall(r"^- 选项编号：(.+?)\s*$", option_block.group(1) if option_block else "", re.MULTILINE)
    option_texts = re.findall(r"^\s+- 选项文字：(.+?)\s*$", option_block.group(1) if option_block else "", re.MULTILINE)
    if expected_targets and (len(option_numbers) != len(expected_targets) or len(option_texts) != len(expected_targets)):
        issues.append(f"选项字段不完整：{node_id}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    topology = parse(args.cache_root / "topology.md")
    scripts: list[str] = []
    issues: list[str] = []
    digests: list[str] = []
    for node_id, node in topology.items():
        path = args.cache_root / "episodes" / f"{node_id}.md"
        if not path.is_file():
            issues.append(f"缺少正文：{node_id}")
            continue
        text = path.read_text(encoding="utf-8").strip()
        issues.extend(validate_episode(node_id, node, text))
        scripts.append(text)
        digests.append(f"{node_id} {hashlib.sha256(text.encode()).hexdigest()}")
    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    assembled = "\n\n---\n\n".join(scripts) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(assembled, encoding="utf-8")
    print(f"PASS: {len(scripts)} nine-field scripts validated and assembled")
    print(f"public {hashlib.sha256(assembled.encode()).hexdigest()}")
    for digest in digests:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
