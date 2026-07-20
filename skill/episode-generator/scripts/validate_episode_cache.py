#!/usr/bin/env python3
"""Validate deterministic structure and file coverage for episode-generator caches."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path

from cache_paths import cache_root_for
from validate_storyboard_fields import (
    build_storyboard_fields,
    list_section,
    validate_fields,
)
from load_reference_bundle import verify_receipts
from prepare_dialogue_review import build_dialogue_packet
from seal_pipeline_stage import verify_stage


NODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE_REF = re.compile(r"episode-\d{3}")
PUBLIC_HEADER = re.compile(r"^#\s+第(\d+)集", re.MULTILINE)
SYNOPSIS_HEADER = re.compile(r"^##\s+第(\d+)集", re.MULTILINE)
PUBLIC_ROOT_FILES_LEGACY = {"episode-flowchart.svg", "episode-structure.md", "episode-script.md"}
PUBLIC_ROOT_FILES_V22 = {"episode-flowchart.svg", "episode-script.md"}
PUBLIC_ROOT_FILES_V26 = {"episode-flowchart.svg", "episode-synopsis.md", "episode-script.md"}
FORBIDDEN_SHARED_CANVAS_ITEMS = {
    "episode-structure.md",
    "episode-delivery.md",
    "episodes",
    ".episode-generator-cache",
}
PLACEHOLDER = re.compile(r"(?:已完成|见公开|与.*公开.*一致|同公开|略)$")
DEFAULT_MIN_VISIBLE_CHARS = 850
DEFAULT_MAX_VISIBLE_CHARS = 1300
DEFAULT_MIN_ACTION_BEATS = 16
DEFAULT_MAX_ACTION_BEATS = 24
REQUIRED_REFERENCE_PHASES = (
    "upstream",
    "topology",
    "production-cards",
    "episode-writing",
    "scene-review",
    "dialogue-review",
    "isolated-audience",
    "state-writeback",
    "whole-play-review",
)
CAUSAL_AUDIT_FIELDS = (
    "上游事实",
    "地点与权限",
    "事件与反应链",
    "物件与状态",
    "后续进入条件",
)
DIALOGUE_AUDIT_FIELDS = (
    "话茬与当下目的",
    "人物声音",
    "设定发布与承重句",
    "朴素中文",
)
COLD_AUDIT_FIELDS = (
    "动作可拍门",
    "对白可说门",
    "冷读六问",
)
SCENE_FACT_AUDIT_FIELDS = (
    "触发登记覆盖",
    "地点权限与首次出场",
    "事件感知与第一反应",
    "行动阻力与可见结果",
    "物件路径与后续入口",
)
CHARACTER_EXCHANGE_AUDIT_FIELDS_V18 = (
    "关系触发与行动优先级",
    "话茬与当下目的",
    "人物声音与关系距离",
    "设定发布与朴素中文",
)
CHARACTER_EXCHANGE_AUDIT_FIELDS_V19 = (
    "台词抽取与原位写回",
    "话茬与朴素表达",
    "姓名知情与关系距离",
    "人物声音与承重句",
)
AUDIENCE_AUDIT_FIELDS = (
    "新人物可识别",
    "事件顺序可见",
    "关系证据可见",
    "对白现场性",
    "结果与下一行动",
)
TRIGGER_FIELDS = (
    "首次出现人物",
    "核心关系触发",
    "关键事件",
    "关键干预",
    "互动选择",
)
INPUT_FIELDS = ("游戏企划", "角色描述", "场景描述", "道具描述")


def read_text(path: Path, errors: list[str]) -> str:
    if not path.is_file():
        errors.append(f"缺少文件：{path}")
        return ""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        errors.append(f"文件为空：{path}")
    return text


def validate_input_contract(text: str, errors: list[str]) -> None:
    headings = tuple(re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    if headings != INPUT_FIELDS:
        errors.append("input.md 必须且只能按顺序包含四个一级输入字段")
        return
    for heading in INPUT_FIELDS:
        content = section(text, heading)
        if len(re.sub(r"\s+", "", content)) < 30:
            errors.append(f"input.md 的{heading}只有名称/标识或缺少实际描述")


def field(block: str, name: str, errors: list[str], node_id: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        errors.append(f"{node_id} 缺少字段：{name}")
        return ""
    return match.group(1)


def refs(value: str) -> set[str]:
    if value.strip() == "无":
        return set()
    return set(EPISODE_REF.findall(value))


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def episode_metrics(text: str) -> tuple[int, int]:
    visible_chars = len(re.sub(r"\s+", "", text))
    action_beats = len(re.findall(r"^\s*△", text, re.MULTILINE))
    return visible_chars, action_beats


def scene_names(text: str) -> set[str]:
    return {
        match.strip()
        for match in re.findall(r"^【\s*(场[^·】]+?)\s*·", text, re.MULTILINE)
    }


def dialogue_by_scene(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        scene = re.match(r"^【\s*(场[^·】]+?)\s*·", line)
        if scene:
            current = scene.group(1).strip()
            result.setdefault(current, [])
            continue
        if not current or line.startswith(("△", "#", "【")):
            continue
        match = re.match(r"^([^：\n]{1,30})：\s*(\S.*)$", line)
        if match and match.group(1).strip() not in ("单集梗概", "出场", "选择"):
            result[current].append(match.group(2).strip())
    return result


def validate_v19_dialogue_evidence(
    cache_text: str, final_text: str, node_id: str, errors: list[str]
) -> None:
    block = section(cache_text, "人物交流复核记录")
    dialogue_map = dialogue_by_scene(final_text)
    all_dialogue = [line for lines in dialogue_map.values() for line in lines]
    covered: set[str] = set()
    for name in CHARACTER_EXCHANGE_AUDIT_FIELDS_V19:
        value = audit_field(block, name)
        quotes = re.findall(r"[“\"]([^”\"]{2,})[”\"]", value)
        if not quotes or not any(
            quote in dialogue for quote in quotes for dialogue in all_dialogue
        ):
            errors.append(f"{node_id} 的人物交流证据未逐字引用真实台词：{name}")
        covered.update(
            re.findall(r"场(?:[一二三四五六七八九十百零〇两\d]+|[A-Za-z]+)", value)
        )
    required = {scene for scene, lines in dialogue_map.items() if lines}
    if not required <= covered:
        errors.append(
            f"{node_id} 的人物交流证据未覆盖对白场次：{sorted(required - covered)}"
        )


def parse_topology(text: str, errors: list[str]) -> dict[str, dict[str, object]]:
    matches = list(NODE_HEADER.finditer(text))
    if not matches:
        errors.append("topology.md 未找到固定格式节点")
        return {}

    nodes: dict[str, dict[str, object]] = {}
    for index, match in enumerate(matches):
        node_id = match.group(1)
        if node_id in nodes:
            errors.append(f"节点编号重复：{node_id}")
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        predecessors = refs(field(block, "前置节点", errors, node_id))
        successors = refs(field(block, "后续节点", errors, node_id))
        interaction = field(block, "互动类型", errors, node_id)
        ending = field(block, "结局", errors, node_id)
        choice_targets = set(
            re.findall(r"^-\s*选择：.*?(?:->|→)\s*(episode-\d{3})\s*$", block, re.MULTILINE)
        )
        nodes[node_id] = {
            "title": match.group(2).strip(),
            "predecessors": predecessors,
            "successors": successors,
            "interaction": interaction,
            "ending": ending,
            "choice_targets": choice_targets,
        }
    return nodes


def validate_graph(nodes: dict[str, dict[str, object]], errors: list[str]) -> None:
    if not nodes:
        return

    node_ids = set(nodes)
    incoming: dict[str, set[str]] = defaultdict(set)
    for node_id, node in nodes.items():
        successors = node["successors"]
        assert isinstance(successors, set)
        unknown = successors - node_ids
        if unknown:
            errors.append(f"{node_id} 指向不存在节点：{', '.join(sorted(unknown))}")
        for target in successors & node_ids:
            incoming[target].add(node_id)

        ending = node["ending"]
        if ending not in {"是", "否"}:
            errors.append(f"{node_id} 的结局字段必须为“是”或“否”")
        elif ending == "是" and successors:
            errors.append(f"结局节点仍有出边：{node_id}")
        elif ending == "否" and not successors:
            errors.append(f"非结局节点没有出边：{node_id}")

        choice_targets = node["choice_targets"]
        assert isinstance(choice_targets, set)
        missing_edges = choice_targets - successors
        if missing_edges:
            errors.append(f"{node_id} 的选项目标未列入后续节点：{', '.join(sorted(missing_edges))}")
        interaction = node["interaction"]
        assert isinstance(interaction, str)
        if "选择" in interaction:
            if not choice_targets:
                errors.append(f"选择节点缺少选项：{node_id}")
            elif choice_targets != successors:
                errors.append(f"{node_id} 的选项目标与后续节点不一致")
        elif choice_targets:
            errors.append(f"非选择节点包含选项：{node_id}")
        elif ending == "否" and len(successors) != 1:
            errors.append(f"非选择、非结局节点必须只有一个后续：{node_id}")

    entries = sorted(node_id for node_id in node_ids if not incoming[node_id])
    if entries != ["episode-001"]:
        errors.append(f"唯一入口必须为 episode-001，当前入口：{', '.join(entries) or '无'}")

    for node_id, node in nodes.items():
        declared = node["predecessors"]
        assert isinstance(declared, set)
        actual = incoming[node_id]
        if declared != actual:
            errors.append(
                f"{node_id} 前置节点不一致：声明 {sorted(declared)}，实际 {sorted(actual)}"
            )

    reached: set[str] = set()
    queue: deque[str] = deque(["episode-001"] if "episode-001" in nodes else [])
    while queue:
        node_id = queue.popleft()
        if node_id in reached:
            continue
        reached.add(node_id)
        successors = nodes[node_id]["successors"]
        assert isinstance(successors, set)
        queue.extend(sorted(successors & node_ids))
    unreachable = node_ids - reached
    if unreachable:
        errors.append(f"存在不可达节点：{', '.join(sorted(unreachable))}")

    indegree = {node_id: len(incoming[node_id]) for node_id in node_ids}
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        successors = nodes[node_id]["successors"]
        assert isinstance(successors, set)
        for target in successors & node_ids:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(node_ids):
        errors.append("拓扑中存在循环")


def validate_branch_audit(
    text: str,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
    require_creative_gates: bool = False,
) -> None:
    for heading in ("目标轴", "候选岔点", "前五分钟交互", "审计结论"):
        if not section(text, heading):
            errors.append(f"branch-audit.md 缺少有效栏目：{heading}")

    fork_pattern = re.compile(
        r"^###\s+(fork-\d{3})\s*｜\s*(.+?)\s*$\n(.*?)(?=^###\s+|^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    forks = list(fork_pattern.finditer(text))
    if not forks:
        errors.append("branch-audit.md 未记录候选岔点")
    for match in forks:
        fork_id = match.group(1)
        block = match.group(3)
        handling = re.search(r"^-\s*处理：\s*(保留|删除)\s*$", block, re.MULTILINE)
        landing = re.search(
            r"^-\s*落图节点：\s*(episode-\d{3}|无)\s*$", block, re.MULTILINE
        )
        if not handling:
            errors.append(f"{fork_id} 缺少有效处理结论")
            continue
        if not landing:
            errors.append(f"{fork_id} 缺少有效落图节点")
            continue
        decision = handling.group(1)
        node_id = landing.group(1)
        if decision == "保留":
            if node_id == "无" or node_id not in nodes:
                errors.append(f"{fork_id} 保留但未指向有效拓扑节点")
            elif "选择" not in str(nodes[node_id]["interaction"]):
                errors.append(f"{fork_id} 指向的 {node_id} 不是选择节点")
        elif node_id != "无":
            errors.append(f"{fork_id} 已删除但仍声明落图节点 {node_id}")
        if decision == "删除" and not re.search(r"^-\s*理由：\s*\S+", block, re.MULTILINE):
            errors.append(f"{fork_id} 删除但未记录理由")

    early = section(text, "前五分钟交互")
    early_node = re.search(r"^-\s*落图节点：\s*(episode-\d{3})\s*$", early, re.MULTILINE)
    if not early_node:
        errors.append("branch-audit.md 未记录前五分钟交互的落图节点")
    else:
        node_id = early_node.group(1)
        if node_id not in nodes:
            errors.append(f"前五分钟交互指向不存在节点：{node_id}")
        elif "选择" not in str(nodes[node_id]["interaction"]):
            errors.append(f"前五分钟交互节点不是选择节点：{node_id}")

    if "PASS" not in section(text, "审计结论"):
        errors.append("branch-audit.md 审计结论未通过")

    if require_creative_gates:
        novelty = section(text, "反俗套候选")
        if not novelty:
            errors.append("branch-audit.md 缺少反俗套候选")
            return
        pattern = re.compile(
            r"^###\s+novelty-\d{3}\s*｜\s*(开场钩子|首次释放|中段反噬|高潮解法)\s*$\n"
            r"(.*?)(?=^###\s+|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        entries = list(pattern.finditer(novelty))
        required_beats = {"开场钩子", "首次释放", "中段反噬", "高潮解法"}
        actual_beats = {match.group(1) for match in entries}
        if actual_beats != required_beats or len(entries) != 4:
            errors.append("反俗套候选必须且只能覆盖开场钩子、首次释放、中段反噬和高潮解法")
        for match in entries:
            beat = match.group(1)
            block = match.group(2)
            candidates = [field(block, f"候选{label}", errors, beat) for label in "一二三"]
            normalized = {re.sub(r"\s+", "", value) for value in candidates if value}
            if len(normalized) != 3:
                errors.append(f"{beat} 的三个候选必须实质不同且不得为空")
            if field(block, "第一候选处理", errors, beat).strip() != "丢弃":
                errors.append(f"{beat} 未强制丢弃候选一")
            adopted = field(block, "采用方案", errors, beat).strip()
            if adopted not in {"候选二", "候选三"}:
                errors.append(f"{beat} 只能采用候选二或候选三")
            reason = field(block, "采用依据", errors, beat).strip()
            if len(re.sub(r"\s+", "", reason)) < 12 or reason in {"更精彩", "更反转", "不俗套"}:
                errors.append(f"{beat} 缺少项目专属的采用依据")


def validate_flowchart_svg(
    text: str,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
) -> None:
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        errors.append(f"episode-flowchart.svg 不是有效 XML：{exc}")
        return

    expected_nodes = set(nodes)
    actual_nodes = set(re.findall(r'data-node-id="(episode-\d{3})"', text))
    if actual_nodes != expected_nodes:
        errors.append(
            "静态流程图节点覆盖不一致："
            f"缺少 {sorted(expected_nodes - actual_nodes)}，"
            f"多出 {sorted(actual_nodes - expected_nodes)}"
        )

    expected_edges = {
        (node_id, target)
        for node_id, node in nodes.items()
        for target in node["successors"]
    }
    actual_edges = set(
        re.findall(
            r'data-edge-from="(episode-\d{3})"\s+data-edge-to="(episode-\d{3})"',
            text,
        )
    )
    if actual_edges != expected_edges:
        errors.append(
            "静态流程图边覆盖不一致："
            f"缺少 {sorted(expected_edges - actual_edges)}，"
            f"多出 {sorted(actual_edges - expected_edges)}"
        )


def validate_episode_files(
    directory: Path,
    nodes: dict[str, dict[str, object]],
    errors: list[str],
    public: bool,
    limits: tuple[int, int, int, int],
    stats: dict[str, tuple[int, int]],
    allow_pending: bool = False,
    require_creative_gates: bool = False,
) -> None:
    expected = set(nodes)
    actual = {path.stem for path in directory.glob("episode-*.md")} if directory.is_dir() else set()
    if expected != actual:
        errors.append(
            f"{'公开' if public else '隐藏'}分集文件不一致："
            f"缺少 {sorted(expected - actual)}，多出 {sorted(actual - expected)}"
        )
    for node_id in sorted(expected & actual):
        path = directory / f"{node_id}.md"
        text = read_text(path, errors)
        number = int(node_id.rsplit("-", 1)[1])
        header = PUBLIC_HEADER.search(text)
        if not header or int(header.group(1)) != number:
            errors.append(f"分集标题编号不匹配：{path}")
        if public:
            if "单集梗概" not in text:
                errors.append(f"公开分集缺少单集梗概：{path}")
            visible_chars, action_beats = episode_metrics(text)
            stats[node_id] = (visible_chars, action_beats)
            min_chars, max_chars, min_beats, max_beats = limits
            if not min_chars <= visible_chars <= max_chars:
                errors.append(
                    f"{node_id} 可见字符数 {visible_chars} 超出 {min_chars}—{max_chars}"
                )
            if not min_beats <= action_beats <= max_beats:
                errors.append(
                    f"{node_id} 动作段数 {action_beats} 超出 {min_beats}—{max_beats}"
                )
        else:
            for heading in (
                "## 生产卡",
                "## 当前集初稿",
                "## 当前集定稿",
                "## 度量记录",
                "## 真实结尾状态",
                "## 校验状态",
            ):
                if heading not in text:
                    errors.append(f"私有分集缓存缺少栏目 {heading}：{path}")
            for heading in ("当前集初稿", "当前集定稿"):
                content = section(text, heading)
                if allow_pending and (not content or content == "PENDING"):
                    continue
                if len(re.sub(r"\s+", "", content)) < 300 or PLACEHOLDER.search(content):
                    errors.append(f"{node_id} 的{heading}不是实际完整正文")
            for heading in ("生产卡", "单集梗概"):
                content = section(text, heading)
                if not content or re.search(r"见公开|与.*公开.*一致|同公开", content):
                    errors.append(f"{node_id} 的{heading}仍含公开稿引用占位")
            if require_creative_gates:
                lens = section(text, "题材透镜记录")
                if not lens:
                    errors.append(f"{node_id} 缺少题材透镜记录")
                else:
                    for name in (
                        "具体事件",
                        "题材反差物",
                        "题材内升级",
                        "后续催化剂或结局余波",
                    ):
                        value = field(lens, name, errors, node_id).strip()
                        if len(re.sub(r"\s+", "", value)) < 8 or value in {
                            "无",
                            "符合题材",
                            "已完成",
                            "见梗概",
                        }:
                            errors.append(f"{node_id} 的题材透镜记录缺少具体内容：{name}")
                    if field(lens, "判定", errors, node_id).strip() != "PASS":
                        errors.append(f"{node_id} 的题材透镜记录未通过")
            metric_record = section(text, "度量记录")
            if allow_pending and "判定：PENDING" in metric_record:
                continue
            if not re.search(r"可见字符数：\s*\d+", metric_record):
                errors.append(f"{node_id} 度量记录缺少可见字符数")
            if not re.search(r"动作段数：\s*\d+", metric_record):
                errors.append(f"{node_id} 度量记录缺少动作段数")


def audit_field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def validate_audit_block(
    block: str,
    heading: str,
    fields: tuple[str, ...],
    expected_digest: str,
    node_id: str,
    errors: list[str],
    scenes: set[str] | None = None,
) -> None:
    if not block:
        errors.append(f"{node_id} 缺少{heading}")
        return
    if audit_field(block, "状态") != "PASS":
        errors.append(f"{node_id} 的{heading}未 PASS")
    if audit_field(block, "正文 SHA-256") != expected_digest:
        errors.append(f"{node_id} 的{heading}正文指纹与当前定稿不一致")
    for name in fields:
        value = audit_field(block, name)
        if scenes is None:
            match = re.fullmatch(r"PASS[｜|]\s*(\S.*)", value)
            if not match or len(re.sub(r"\s+", "", match.group(1))) < 8:
                errors.append(f"{node_id} 的{heading}缺少具体依据：{name}")
        else:
            match = re.fullmatch(r"PASS[｜|]\s*([^｜|]+?)[｜|]\s*(\S.*)", value)
            if not match or len(re.sub(r"\s+", "", match.group(2))) < 12:
                errors.append(
                    f"{node_id} 的{heading}依据必须使用 PASS｜场次｜具体证据：{name}"
                )
                continue
            locator = match.group(1).strip()
            locator_scenes = set(
                re.findall(r"场(?:[一二三四五六七八九十百零〇两\d]+|[A-Za-z]+)", locator)
            )
            if not (scenes & locator_scenes):
                errors.append(
                    f"{node_id} 的{heading}引用了正文不存在的场次：{name}={locator}"
                )
    if audit_field(block, "未解决问题") != "无":
        errors.append(f"{node_id} 的{heading}仍有未解决问题")


def validate_v25_dialogue_audit(
    block: str,
    final_text: str,
    expected_digest: str,
    node_id: str,
    errors: list[str],
) -> None:
    packet = build_dialogue_packet(final_text)
    if not block:
        errors.append(f"{node_id} 缺少人物交流复核记录")
        return
    if audit_field(block, "状态") != "PASS":
        errors.append(f"{node_id} 的人物交流复核记录未 PASS")
    if audit_field(block, "正文 SHA-256") != expected_digest:
        errors.append(f"{node_id} 的人物交流正文指纹与当前定稿不一致")
    if audit_field(block, "台词包 SHA-256") != packet["dialogue_sha256"]:
        errors.append(f"{node_id} 的人物交流台词包指纹与当前定稿不一致")
    if audit_field(block, "台词总数") != str(packet["dialogue_count"]):
        errors.append(f"{node_id} 的人物交流台词总数与当前定稿不一致")
    if audit_field(block, "覆盖状态") != "COMPLETE":
        errors.append(f"{node_id} 的人物交流复核未覆盖全部台词")
    if audit_field(block, "问题项") != "无" or audit_field(block, "未解决问题") != "无":
        errors.append(f"{node_id} 的人物交流复核仍有未解决问题")


def validate_trigger_registry(
    cache_text: str, final_text: str, node_id: str, errors: list[str]
) -> None:
    block = section(cache_text, "语义触发登记")
    if not block:
        errors.append(f"{node_id} 缺少语义触发登记")
        return
    scenes = scene_names(final_text)
    for name in TRIGGER_FIELDS:
        value = audit_field(block, name)
        if not value:
            errors.append(f"{node_id} 的语义触发登记缺少字段：{name}")
            continue
        if name == "关键事件" and value == "无":
            errors.append(f"{node_id} 的关键事件不得为无")
        for locator in re.findall(r"@\s*(场[^；;，,\s]+)", value):
            if locator not in scenes:
                errors.append(
                    f"{node_id} 的语义触发登记引用了正文不存在的场次：{locator}"
                )


def validate_v12_episode_audits(
    canvas_root: Path,
    cache_root: Path,
    topology_text: str,
    nodes: dict[str, dict[str, object]],
    known_assets: dict[str, set[str]],
    errors: list[str],
    require_public: bool,
    limits: tuple[int, int, int, int],
    require_cold: bool = False,
    require_public_episode_files: bool = True,
    is_v18: bool = False,
    is_v19: bool = False,
    is_v25: bool = False,
) -> None:
    for node_id in sorted(nodes):
        cache_path = cache_root / "episodes" / f"{node_id}.md"
        if not cache_path.is_file():
            continue
        cache_text = cache_path.read_text(encoding="utf-8")
        for heading, asset_kind in (
            ("关联角色", "角色"),
            ("关联场景", "场景"),
            ("关联道具", "道具"),
        ):
            try:
                values = list_section(cache_text, heading)
            except ValueError as exc:
                errors.append(f"{node_id}：{exc}")
                continue
            unknown = set(values) - known_assets[asset_kind]
            if unknown:
                errors.append(f"{node_id} 的{heading}含非上游正式名称：{sorted(unknown)}")

        if is_v18:
            required_headings = [
                "语义触发登记",
                "场面事实复核记录",
                "人物交流复核记录",
                "隔离观众复核记录",
            ]
        else:
            required_headings = ["因果复核记录", "对白复核记录"]
            if require_cold:
                required_headings.append("冷读复核记录")
        for heading in required_headings:
            if f"## {heading}" not in cache_text:
                errors.append(f"{node_id} 缺少栏目：{heading}")

        status = section(cache_text, "校验状态")
        frozen = "冻结状态：已冻结" in status
        if not frozen:
            if require_public:
                errors.append(f"{node_id} 尚未具备有效语义复核记录并冻结")
            continue

        final_text = section(cache_text, "当前集定稿").strip()
        if len(re.sub(r"\s+", "", final_text)) < 300:
            errors.append(f"{node_id} 当前集定稿不是实际完整正文")
            continue
        expected_digest = hashlib.sha256(final_text.encode("utf-8")).hexdigest()
        scenes = scene_names(final_text)
        if is_v18:
            validate_trigger_registry(cache_text, final_text, node_id, errors)
            validate_audit_block(
                section(cache_text, "场面事实复核记录"),
                "场面事实复核记录",
                SCENE_FACT_AUDIT_FIELDS,
                expected_digest,
                node_id,
                errors,
                scenes,
            )
        else:
            validate_audit_block(
                section(cache_text, "因果复核记录"),
                "因果复核记录",
                CAUSAL_AUDIT_FIELDS,
                expected_digest,
                node_id,
                errors,
            )

        try:
            validate_fields(build_storyboard_fields(cache_text, topology_text))
        except ValueError as exc:
            errors.append(f"{node_id} 无法按九个固定字段直接交给故事版：{exc}")

        if is_v18:
            character_fields = (
                CHARACTER_EXCHANGE_AUDIT_FIELDS_V19
                if is_v19
                else CHARACTER_EXCHANGE_AUDIT_FIELDS_V18
            )
            if is_v25:
                validate_v25_dialogue_audit(
                    section(cache_text, "人物交流复核记录"),
                    final_text,
                    expected_digest,
                    node_id,
                    errors,
                )
            else:
                validate_audit_block(
                    section(cache_text, "人物交流复核记录"),
                    "人物交流复核记录",
                    character_fields,
                    expected_digest,
                    node_id,
                    errors,
                    scenes,
                )
            audience_block = section(cache_text, "隔离观众复核记录")
            validate_audit_block(
                audience_block,
                "隔离观众复核记录",
                AUDIENCE_AUDIT_FIELDS,
                expected_digest,
                node_id,
                errors,
                scenes,
            )
            if audit_field(audience_block, "隔离方式") not in (
                "独立冷读",
                "最小上下文复检",
            ):
                errors.append(f"{node_id} 的隔离观众复核记录缺少有效隔离方式")
            if len(re.sub(r"\s+", "", audit_field(audience_block, "一句话复述"))) < 12:
                errors.append(f"{node_id} 的隔离观众复核记录缺少具体一句话复述")
            if is_v19 and not is_v25:
                validate_v19_dialogue_evidence(cache_text, final_text, node_id, errors)
        else:
            validate_audit_block(
                section(cache_text, "对白复核记录"),
                "对白复核记录",
                DIALOGUE_AUDIT_FIELDS,
                expected_digest,
                node_id,
                errors,
            )
            if require_cold:
                cold_block = section(cache_text, "冷读复核记录")
                validate_audit_block(
                    cold_block,
                    "冷读复核记录",
                    COLD_AUDIT_FIELDS,
                    expected_digest,
                    node_id,
                    errors,
                )
                if audit_field(cold_block, "隔离方式") not in (
                    "独立冷读",
                    "最小上下文复检",
                ):
                    errors.append(f"{node_id} 的冷读复核记录缺少有效隔离方式")
                if len(re.sub(r"\s+", "", audit_field(cold_block, "一句话复述"))) < 12:
                    errors.append(f"{node_id} 的冷读复核记录缺少具体一句话复述")

        visible_chars, action_beats = episode_metrics(final_text)
        metric_record = section(cache_text, "度量记录")
        char_match = re.search(r"可见字符数：\s*(\d+)", metric_record)
        beat_match = re.search(r"动作段数：\s*(\d+)", metric_record)
        if not char_match or int(char_match.group(1)) != visible_chars:
            errors.append(f"{node_id} 度量记录的可见字符数与当前定稿不一致")
        if not beat_match or int(beat_match.group(1)) != action_beats:
            errors.append(f"{node_id} 度量记录的动作段数与当前定稿不一致")
        passed = (
            limits[0] <= visible_chars <= limits[1]
            and limits[2] <= action_beats <= limits[3]
        )
        if ("判定：PASS" in metric_record) != passed:
            errors.append(f"{node_id} 度量记录判定与重新计算结果不一致")

        public_path = canvas_root / "episodes" / f"{node_id}.md"
        if require_public and require_public_episode_files:
            if not public_path.is_file():
                continue
            public_text = public_path.read_text(encoding="utf-8").strip()
            if public_text != final_text:
                errors.append(f"{node_id} 公开逐集文件与通过复核的当前集定稿不一致")


def validate_public_root(
    canvas_root: Path, errors: list[str], allowed_files: set[str]
) -> None:
    if not canvas_root.is_dir():
        errors.append(f"公开 Canvas 目录不存在：{canvas_root}")
        return
    extras = sorted(path.name for path in canvas_root.iterdir() if path.name not in allowed_files)
    if extras:
        errors.append(f"公开 Canvas 根目录含未授权产物：{extras}")


def validate_shared_canvas_root(canvas_root: Path, errors: list[str]) -> None:
    if not canvas_root.is_dir():
        errors.append(f"公开 Canvas 目录不存在：{canvas_root}")
        return
    forbidden = sorted(
        path.name
        for path in canvas_root.iterdir()
        if path.name in FORBIDDEN_SHARED_CANVAS_ITEMS
    )
    if forbidden:
        errors.append(f"Canvas 根目录含分集 Skill 禁止公开的产物：{forbidden}")


def validate(
    canvas_root: Path,
    require_public: bool,
    limits: tuple[int, int, int, int],
    cache_root: Path | None = None,
) -> tuple[list[str], dict[str, tuple[int, int]]]:
    errors: list[str] = []
    stats: dict[str, tuple[int, int]] = {}
    cache = cache_root.resolve() if cache_root else cache_root_for(canvas_root)
    required = (
        "manifest.md",
        "input.md",
        "global.md",
        "characters.md",
        "props.md",
        "hooks.md",
        "validation.md",
    )
    for name in required:
        read_text(cache / name, errors)

    topology = read_text(cache / "topology.md", errors)
    nodes = parse_topology(topology, errors) if topology else {}
    input_text = read_text(cache / "input.md", errors)
    validate_input_contract(input_text, errors)
    known_assets = {
        "角色": set(re.findall(r"^角色名称:\s*(\S.*?)\s*$", input_text, re.MULTILINE)),
        "场景": set(re.findall(r"^场景名称:\s*(\S.*?)\s*$", input_text, re.MULTILINE)),
        "道具": set(re.findall(r"^道具名称:\s*(\S.*?)\s*$", input_text, re.MULTILINE)),
    }
    for kind, values in known_assets.items():
        if not values:
            errors.append(f"input.md 未解析到正式{kind}名称")
    manifest = read_text(cache / "manifest.md", [])
    is_v12 = "episode-cache-v0.1.12" in manifest
    is_v13 = "episode-cache-v0.1.13" in manifest
    is_v14 = "episode-cache-v0.1.14" in manifest
    is_v17 = "episode-cache-v0.1.17" in manifest
    is_v28 = "episode-cache-v0.1.28" in manifest
    is_v27 = "episode-cache-v0.1.27" in manifest or is_v28
    is_v26 = "episode-cache-v0.1.26" in manifest or is_v27
    is_v25 = "episode-cache-v0.1.25" in manifest or is_v26
    is_v24 = "episode-cache-v0.1.24" in manifest or is_v25
    is_v23 = "episode-cache-v0.1.23" in manifest or is_v24
    is_v22 = "episode-cache-v0.1.22" in manifest or is_v23
    is_v21 = "episode-cache-v0.1.21" in manifest or is_v22
    is_v19 = "episode-cache-v0.1.19" in manifest or is_v21
    is_v18 = "episode-cache-v0.1.18" in manifest or is_v19
    is_current_cache = is_v12 or is_v13 or is_v14 or is_v17 or is_v18
    if is_v28:
        for name in ("emotional-spine.md", "causal-graph.md"):
            read_text(cache / name, errors)
        if require_public:
            try:
                verify_stage(cache, canvas_root, "final")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"阶段依赖链校验失败：{exc}")
    if is_current_cache:
        declared_canvas = section(manifest, "公开 Canvas").strip()
        if not declared_canvas:
            errors.append("manifest.md 缺少有效栏目：公开 Canvas")
        elif Path(declared_canvas).expanduser().resolve() != canvas_root.resolve():
            errors.append("manifest.md 的公开 Canvas 与当前校验项目不一致")
    if is_v21:
        upstream_check = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("validate_upstream_reference.py")), str(cache)],
            capture_output=True,
            text=True,
        )
        if upstream_check.returncode:
            errors.append("上游制作参考校验失败：\n" + (upstream_check.stdout or upstream_check.stderr).strip())
    if is_v23:
        errors.extend(
            verify_receipts(
                cache / ".reference-receipts",
                REQUIRED_REFERENCE_PHASES if require_public else REQUIRED_REFERENCE_PHASES[:2],
            )
        )
    if re.search(r"episode-cache-v0\.1\.(?:8|9|10|11|12|13|14|17|18|19|21|22|23|24|25|26|27|28)\b", manifest):
        branch_audit = read_text(cache / "branch-audit.md", errors)
        if branch_audit:
            validate_branch_audit(
                branch_audit,
                nodes,
                errors,
                require_creative_gates=is_v27,
            )
    if re.search(r"episode-cache-v0\.1\.(?:9|10|11|12|13|14|17|18|19|21|22|23|24|25|26|27|28)\b", manifest):
        if is_v18:
            manifest_headings = [
                "场面事实复核状态",
                "人物交流复核状态",
                "隔离观众复核状态",
                "机械冻结状态",
            ]
        else:
            manifest_headings = ["对白校验状态", "因果连续性校验状态"]
            if is_v13 or is_v14 or is_v17:
                manifest_headings.append("冷读校验状态")
        for heading in manifest_headings:
            if not section(manifest, heading):
                errors.append(f"manifest.md 缺少有效栏目：{heading}")
        runtime_markers = re.compile(
            r"(?:threadId|child\s+thread|子线程\s*(?:ID|编号)|agentId|subagent)",
            re.IGNORECASE,
        )
        for name in ("manifest.md", "validation.md"):
            state_text = read_text(cache / name, [])
            if runtime_markers.search(state_text):
                errors.append(f"{name} 不得保存子线程、子 Agent 或运行时标识")
    count_match = re.search(r"^## 节点总数\s*\n+\s*(\d+)\s*$", manifest, re.MULTILINE)
    if not count_match:
        errors.append("manifest.md 缺少有效的节点总数")
    elif int(count_match.group(1)) != len(nodes):
        errors.append(
            f"manifest 节点总数为 {count_match.group(1)}，topology 实际为 {len(nodes)}"
        )
    validate_graph(nodes, errors)
    validate_episode_files(
        cache / "episodes",
        nodes,
        errors,
        public=False,
        limits=limits,
        stats=stats,
        allow_pending=is_current_cache,
        require_creative_gates=is_v27,
    )
    if is_current_cache:
        validate_v12_episode_audits(
            canvas_root,
            cache,
            topology,
            nodes,
            known_assets,
            errors,
            require_public,
            limits,
            require_cold=is_v13 or is_v14 or is_v17,
            require_public_episode_files=not (is_v17 or is_v18),
            is_v18=is_v18,
            is_v19=is_v19,
            is_v25=is_v25,
        )

    if require_public:
        if is_v24:
            validate_shared_canvas_root(canvas_root, errors)
        elif is_v17 or is_v18:
            validate_public_root(
                canvas_root,
                errors,
                PUBLIC_ROOT_FILES_V22 if is_v22 else PUBLIC_ROOT_FILES_LEGACY,
            )
        if is_v22:
            flowchart = read_text(canvas_root / "episode-flowchart.svg", errors)
            if flowchart:
                validate_flowchart_svg(flowchart, nodes, errors)
        else:
            structure = read_text(canvas_root / "episode-structure.md", errors)
            if re.search(r"episode-cache-v0\.1\.(?:10|11|12|13|14|17|18|19|21)\b", manifest):
                if "episode-flowchart.svg" not in structure:
                    errors.append("episode-structure.md 未引用静态流程图")
                if "<details>" not in structure or "```mermaid" not in structure:
                    errors.append("episode-structure.md 未折叠保留 Mermaid 可编辑源码")
                flowchart = read_text(canvas_root / "episode-flowchart.svg", errors)
                if flowchart:
                    validate_flowchart_svg(flowchart, nodes, errors)
        script = read_text(canvas_root / "episode-script.md", errors)
        if is_v26:
            synopsis_public = read_text(canvas_root / "episode-synopsis.md", errors)
            synopsis_numbers = {int(number) for number in SYNOPSIS_HEADER.findall(synopsis_public)}
            expected_synopsis_numbers = {
                int(node_id.rsplit("-", 1)[1]) for node_id in nodes
            }
            if synopsis_numbers != expected_synopsis_numbers:
                errors.append(
                    "各集梗概覆盖不一致："
                    f"缺少 {sorted(expected_synopsis_numbers - synopsis_numbers)}，"
                    f"多出 {sorted(synopsis_numbers - expected_synopsis_numbers)}"
                )
            if re.search(r"前置节点|后续节点|选择问题|选项列表|默认下一|episode-\d{3}|(?:->|→)", synopsis_public):
                errors.append("episode-synopsis.md 只能包含分集标题和单集梗概，不得复制流程字段")
            for node_id in sorted(nodes):
                cache_path = cache / "episodes" / f"{node_id}.md"
                if not cache_path.is_file():
                    continue
                frozen_synopsis = section(
                    cache_path.read_text(encoding="utf-8"), "单集梗概"
                ).strip()
                if frozen_synopsis and frozen_synopsis not in synopsis_public:
                    errors.append(f"各集梗概未原样包含私有冻结梗概：{node_id}")
        expected_numbers = {int(node_id.rsplit("-", 1)[1]) for node_id in nodes}
        actual_numbers = {int(number) for number in PUBLIC_HEADER.findall(script)}
        if expected_numbers != actual_numbers:
            errors.append(
                "完整剧本汇总覆盖不一致："
                f"缺少 {sorted(expected_numbers - actual_numbers)}，"
                f"多出 {sorted(actual_numbers - expected_numbers)}"
            )
        if is_v17 or is_v18:
            for node_id in sorted(nodes):
                cache_path = cache / "episodes" / f"{node_id}.md"
                if not cache_path.is_file():
                    continue
                final_text = section(
                    cache_path.read_text(encoding="utf-8"), "当前集定稿"
                ).strip()
                if final_text and final_text not in script:
                    errors.append(f"完整剧本汇总未原样包含私有定稿：{node_id}")
        else:
            validate_episode_files(
                canvas_root / "episodes",
                nodes,
                errors,
                public=True,
                limits=limits,
                stats=stats,
            )
            for node_id in sorted(nodes):
                public_episode = canvas_root / "episodes" / f"{node_id}.md"
                if public_episode.is_file():
                    episode_text = public_episode.read_text(encoding="utf-8").strip()
                    if episode_text and episode_text not in script:
                        errors.append(f"完整剧本汇总未原样包含：{public_episode}")

    return errors, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--require-public", action="store_true")
    parser.add_argument("--report-stats", action="store_true")
    parser.add_argument("--min-visible-chars", type=int, default=DEFAULT_MIN_VISIBLE_CHARS)
    parser.add_argument("--max-visible-chars", type=int, default=DEFAULT_MAX_VISIBLE_CHARS)
    parser.add_argument("--min-action-beats", type=int, default=DEFAULT_MIN_ACTION_BEATS)
    parser.add_argument("--max-action-beats", type=int, default=DEFAULT_MAX_ACTION_BEATS)
    args = parser.parse_args()

    limits = (
        args.min_visible_chars,
        args.max_visible_chars,
        args.min_action_beats,
        args.max_action_beats,
    )
    errors, stats = validate(
        args.canvas_root.resolve(), args.require_public, limits, args.cache_root
    )
    if args.report_stats and stats:
        print("episode\tvisible_chars\taction_beats\tstatus")
        for node_id, (visible_chars, action_beats) in sorted(stats.items()):
            passed = (
                limits[0] <= visible_chars <= limits[1]
                and limits[2] <= action_beats <= limits[3]
            )
            print(f"{node_id}\t{visible_chars}\t{action_beats}\t{'PASS' if passed else 'FAIL'}")
    if errors:
        for index, message in enumerate(errors, 1):
            print(f"[{index}] {message}", file=sys.stderr)
        return 1
    print("episode cache validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
