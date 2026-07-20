#!/usr/bin/env python3
"""Validate the nine episode fields before direct handoff to storyboard."""

from __future__ import annotations

import re


TOP_LEVEL_FIELDS = (
    "分集编号",
    "分集标题",
    "分集剧本",
    "剧本分析",
    "关联角色",
    "关联场景",
    "关联道具",
    "是否结局",
    "互动节点",
)
SCRIPT_FIELDS = ("单集梗概", "完整剧本")
ANALYSIS_FIELDS = ("本集冲突", "前置节点编号列表", "后续节点编号列表")
INTERACTION_FIELDS = (
    "是否为分支节点",
    "是否有选择问题",
    "选择问题",
    "选项列表",
    "默认下一分集编号",
)
OPTION_FIELDS = ("选项编号", "选项文字", "目标分集编号")


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def list_section(text: str, heading: str) -> list[str]:
    block = section(text, heading)
    if not block:
        raise ValueError(f"缺少固定字段来源：{heading}")
    values: list[str] = []
    for line in block.splitlines():
        match = re.fullmatch(r"-\s+(\S.*)", line.strip())
        if not match:
            raise ValueError(f"{heading} 必须使用逐项 Markdown 列表")
        values.append(match.group(1).strip())
    if len(values) != len(set(values)):
        raise ValueError(f"{heading} 存在重复项")
    return values


def episode_block(topology: str, episode_id: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(episode_id)}\s*｜.*?$\n(.*?)(?=^##\s+episode-\d{{3}}\s*｜|\Z)",
        topology,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"拓扑缺少 {episode_id}")
    return match.group(1).strip()


def topology_field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    if not match:
        raise ValueError(f"拓扑缺少字段：{name}")
    return match.group(1).strip()


def numeric_refs(value: str) -> list[int]:
    if value == "无":
        return []
    refs = re.findall(r"episode-(\d{3})", value)
    if not refs:
        raise ValueError(f"节点编号列表格式错误：{value}")
    return [int(item) for item in refs]


def build_storyboard_fields(cache_text: str, topology: str) -> dict[str, object]:
    header = re.search(r"^#\s+第(\d+)集《(.+?)》\s*$", cache_text, re.MULTILINE)
    if not header:
        raise ValueError("私有分集缓存缺少标准标题")
    number = int(header.group(1))
    title = header.group(2).strip()
    episode_id = f"episode-{number:03d}"
    block = episode_block(topology, episode_id)
    predecessors = numeric_refs(topology_field(block, "前置节点"))
    successors = numeric_refs(topology_field(block, "后续节点"))
    ending_value = topology_field(block, "结局")
    if ending_value not in {"是", "否"}:
        raise ValueError(f"{episode_id} 的结局字段必须为是或否")

    choice_rows = re.findall(
        r"^-\s*选择：\s*(.+?)\s*->\s*episode-(\d{3})\s*$", block, re.MULTILINE
    )
    choice_question = topology_field(block, "选择问题") if choice_rows else ""
    if choice_rows and not choice_question:
        raise ValueError(f"{episode_id} 是分支节点但缺少选择问题")
    options = [
        {"选项编号": index, "选项文字": text.strip(), "目标分集编号": int(target)}
        for index, (text, target) in enumerate(choice_rows, 1)
    ]
    default_next = None
    if not choice_rows and ending_value == "否":
        if len(successors) != 1:
            raise ValueError(f"{episode_id} 非分支非结局节点必须只有一个默认后续")
        default_next = successors[0]

    final_text = section(cache_text, "当前集定稿")
    if not final_text or final_text == "PENDING":
        raise ValueError(f"{episode_id} 尚无可传递的当前集定稿")
    synopsis = section(cache_text, "单集梗概")
    conflict = section(cache_text, "本集冲突与前后承接")
    if not synopsis or not conflict:
        raise ValueError(f"{episode_id} 缺少单集梗概或本集冲突")

    return {
        "分集编号": number,
        "分集标题": title,
        "分集剧本": {"单集梗概": synopsis, "完整剧本": final_text},
        "剧本分析": {
            "本集冲突": conflict,
            "前置节点编号列表": predecessors,
            "后续节点编号列表": successors,
        },
        "关联角色": list_section(cache_text, "关联角色"),
        "关联场景": list_section(cache_text, "关联场景"),
        "关联道具": list_section(cache_text, "关联道具"),
        "是否结局": ending_value,
        "互动节点": {
            "是否为分支节点": "是" if choice_rows else "否",
            "是否有选择问题": "是" if choice_rows else "否",
            "选择问题": choice_question,
            "选项列表": options,
            "默认下一分集编号": default_next,
        },
    }


def validate_fields(fields: object) -> None:
    if not isinstance(fields, dict) or tuple(fields) != TOP_LEVEL_FIELDS:
        raise ValueError("下游一级字段名称、数量或顺序与固定契约不一致")
    script = fields["分集剧本"]
    analysis = fields["剧本分析"]
    interaction = fields["互动节点"]
    if not isinstance(script, dict) or tuple(script) != SCRIPT_FIELDS:
        raise ValueError("分集剧本字段与固定契约不一致")
    if not isinstance(analysis, dict) or tuple(analysis) != ANALYSIS_FIELDS:
        raise ValueError("剧本分析字段与固定契约不一致")
    if not isinstance(interaction, dict) or tuple(interaction) != INTERACTION_FIELDS:
        raise ValueError("互动节点字段与固定契约不一致")
    options = interaction["选项列表"]
    if not isinstance(options, list):
        raise ValueError("选项列表必须是列表")
    for option in options:
        if not isinstance(option, dict) or tuple(option) != OPTION_FIELDS:
            raise ValueError("选项字段与固定契约不一致")
