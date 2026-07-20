#!/usr/bin/env python3
"""Mechanically validate the frozen upstream production reference."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from prepare_upstream_packet import (
    PROP_FIELDS,
    ROLE_FIELDS,
    SCENE_FIELDS,
    build,
    records,
    sections as input_sections,
)


H2 = (
    "来源指纹",
    "整理后游戏企划",
    "人物制作事实",
    "场景制作事实",
    "道具制作事实",
    "结构简化结论",
    "主干情绪脊",
    "核心因果链",
    "转译校验",
)
PLAN_H3 = (
    "游戏标题", "一句话概述", "游戏核心目标", "游戏关键词", "时代与地点",
    "玩家身份与视角", "剧情整体描述", "游戏核心冲突", "游戏世界观",
    "游戏基调与情绪", "游戏主题", "游戏体量", "推荐结局数", "结构信息",
)
STRUCTURE_H4 = (
    "游戏结构类型", "剧情节点总数建议", "结局构成", "主线情绪变化", "给分集的重点提醒",
)
DESIGN_H4 = (
    "核心人物与关系方向", "关键场景方向", "关键道具与视觉符号",
    "影响资产的剧情节点与状态变化", "资产一致性要求", "安全与生成限制", "参考材料边界",
)


def sections(text: str, level: int) -> dict[str, str]:
    prefix = "#" * level
    matches = list(re.finditer(rf"^{prefix}\s+(.+?)\s*$", text, re.MULTILINE))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match.group(1)] = text[match.end():end].strip()
    return result


def field_names(block: str) -> set[str]:
    return set(re.findall(r"(?m)^\s*[-*]\s*([^：:\n]+)\s*[：:]", block))


def entity_blocks(block: str) -> list[str]:
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", block, re.MULTILINE))
    return [
        block[match.end():(matches[index + 1].start() if index + 1 < len(matches) else len(block))].strip()
        for index, match in enumerate(matches)
    ]


def validate_entities(block: str, allowed: tuple[str, ...], label: str, errors: list[str], allow_none: bool = False) -> None:
    if allow_none and block.strip() == "无":
        return
    entities = entity_blocks(block)
    if not entities:
        errors.append(f"{label}未找到正式记录")
        return
    expected = set(allowed)
    for index, entity in enumerate(entities, 1):
        names = field_names(entity)
        missing = expected - names
        extra = names - expected
        if missing:
            errors.append(f"{label}{index}缺少字段：{sorted(missing)}")
        if extra:
            errors.append(f"{label}{index}包含非白名单字段：{sorted(extra)}")
        for field in expected:
            match = re.search(rf"(?m)^\s*[-*]\s*{re.escape(field)}\s*[：:]\s*(\S.*)$", entity)
            if match and len(re.sub(r"\s+", "", match.group(1))) < 2:
                errors.append(f"{label}{index}的{field}缺少实际内容")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_root", type=Path)
    args = parser.parse_args()
    cache = args.cache_root
    input_path = cache / "input.md"
    packet_path = cache / "upstream-packet.md"
    reference_path = cache / "upstream-reference.md"
    errors: list[str] = []
    for path in (input_path, packet_path, reference_path):
        if not path.is_file():
            errors.append(f"缺少 {path.name}")
    if errors:
        print("\n".join(f"[{i}] {error}" for i, error in enumerate(errors, 1)))
        return 1
    input_text = input_path.read_text(encoding="utf-8")
    expected_packet = build(input_text)
    if packet_path.read_text(encoding="utf-8") != expected_packet:
        errors.append("upstream-packet.md 与当前 input.md 的确定性白名单抽取不一致")
    text = reference_path.read_text(encoding="utf-8")
    if not re.match(r"^#\s+上游制作参考\s*$", text, re.MULTILINE):
        errors.append("upstream-reference.md 缺少固定一级标题")
    h2 = sections(text, 2)
    if tuple(h2) != H2:
        errors.append("upstream-reference.md 的二级栏目缺失、改名或顺序错误")
    if errors:
        print("\n".join(f"[{i}] {error}" for i, error in enumerate(errors, 1)))
        return 1
    expected_hash = hashlib.sha256(input_text.strip().encode("utf-8")).hexdigest()
    hash_match = re.search(r"input\.md SHA-256\s*[：:]\s*([0-9a-f]{64})", h2["来源指纹"])
    if not hash_match or hash_match.group(1) != expected_hash:
        errors.append("来源指纹与当前 input.md 不一致")
    plan = h2["整理后游戏企划"]
    plan_h3 = sections(plan, 3)
    for heading in PLAN_H3:
        if heading not in plan_h3 or len(re.sub(r"\s+", "", plan_h3[heading])) < 2:
            errors.append(f"整理后游戏企划缺少实际字段：{heading}")
    expected_plan_order = list(PLAN_H3)
    if "资产设计材料" in plan_h3:
        expected_plan_order.append("资产设计材料")
    if "待确认问题" in plan_h3:
        expected_plan_order.append("待确认问题")
    if tuple(plan_h3) != tuple(expected_plan_order):
        errors.append("整理后游戏企划的原字段顺序错误")
    structure_h4 = sections(plan_h3.get("结构信息", ""), 4)
    if tuple(structure_h4) != STRUCTURE_H4:
        errors.append("结构信息的五个原字段缺失、改名或顺序错误")
    if "资产设计材料" in plan_h3:
        design_h4 = sections(plan_h3["资产设计材料"], 4)
        if tuple(design_h4) != DESIGN_H4:
            errors.append("资产设计材料的七个原字段缺失、改名或顺序错误")
    allowed_plan = set(PLAN_H3) | {"资产设计材料", "待确认问题"}
    extra_plan = set(plan_h3) - allowed_plan
    if extra_plan:
        errors.append(f"整理后游戏企划包含非原字段：{sorted(extra_plan)}")
    if "待确认问题" in plan_h3 and not re.fullmatch(r"\s*(?:无|无[。.]|none)\s*", plan_h3["待确认问题"], re.IGNORECASE):
        errors.append("整理后游戏企划仍含重要待确认问题，不得冻结或开始拓扑")
    validate_entities(h2["人物制作事实"], ROLE_FIELDS, "人物", errors)
    validate_entities(h2["场景制作事实"], SCENE_FIELDS, "场景", errors)
    validate_entities(h2["道具制作事实"], PROP_FIELDS, "道具", errors, allow_none=True)
    raw = input_sections(input_text)
    try:
        raw_roles = records(raw["角色描述"], "角色名称", ROLE_FIELDS)
        ref_roles = records(h2["人物制作事实"], "角色名称", ROLE_FIELDS)
        raw_scenes = records(raw["场景描述"], "场景名称", SCENE_FIELDS)
        ref_scenes = records(h2["场景制作事实"], "场景名称", SCENE_FIELDS)
        raw_props = records(raw["道具描述"], "道具名称", PROP_FIELDS)
        ref_props = records(h2["道具制作事实"], "道具名称", PROP_FIELDS)
        if ref_roles != raw_roles:
            errors.append("人物制作事实未逐项原样保留角色白名单字段")
        if ref_scenes != raw_scenes:
            errors.append("场景制作事实未逐项原样保留场景白名单字段")
        if ref_props != raw_props:
            errors.append("道具制作事实未逐项原样保留道具白名单字段")
    except SystemExit as exc:
        errors.append(str(exc))
    sentence_count = len(re.findall(r"[。！？!?]", h2["结构简化结论"]))
    if not 3 <= sentence_count <= 5:
        errors.append("结构简化结论必须使用三至五句话复述故事")
    if len(re.findall(r"(?m)^\s*[-*]\s*(?:起|承|转|合)\s*[：:]\s*\S", h2["主干情绪脊"])) != 4:
        errors.append("主干情绪脊必须包含起、承、转、合四项实际内容")
    for field in ("主链", "权限与信息", "反事实检查"):
        if not re.search(rf"(?m)^\s*[-*]\s*{field}\s*[：:]\s*\S", h2["核心因果链"]):
            errors.append(f"核心因果链缺少实际字段：{field}")
    main_chain = re.search(r"(?m)^\s*[-*]\s*主链\s*[：:]\s*(.+)$", h2["核心因果链"])
    if main_chain and len(re.findall(r"→|->", main_chain.group(1))) < 3:
        errors.append("核心因果主链至少需要四个连续环节")
    audit = h2["转译校验"]
    for field in ("结构简化", "主干情绪脊", "核心因果链", "固定事实一致"):
        match = re.search(rf"(?m)^\s*[-*]\s*{field}\s*[：:]\s*PASS｜(.+)$", audit)
        if not match or len(re.sub(r"\s+", "", match.group(1))) < 8:
            errors.append(f"转译校验缺少具体 PASS 依据：{field}")
    if not re.search(r"(?m)^\s*[-*]\s*未解决问题\s*[：:]\s*无\s*$", audit):
        errors.append("转译校验仍有未解决问题")
    if errors:
        print("\n".join(f"[{i}] {error}" for i, error in enumerate(errors, 1)))
        return 1
    print("upstream reference validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
