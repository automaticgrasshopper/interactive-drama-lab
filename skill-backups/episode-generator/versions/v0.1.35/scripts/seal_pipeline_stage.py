#!/usr/bin/env python3
"""Seal and verify a content-addressed episode production pipeline.

Every stage receipt binds its selected artifact sections, mandatory reference
receipts, and the complete receipt of the immediately preceding stage.  A
changed or missing upstream artifact therefore invalidates every downstream
stage without relying on an agent-authored PASS marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from load_reference_bundle import load_bundle


PIPELINE_VERSION = "episode-pipeline-v0.1.31"
STAGES = (
    "upstream",
    "emotional-spine",
    "causal-graph",
    "topology",
    "production-cards",
    "drafts",
    "reviews",
    "state-writeback",
    "final",
)
REFERENCE_PHASES = {
    "upstream": ("upstream",),
    "emotional-spine": ("emotional-spine",),
    "causal-graph": ("causal-graph",),
    "topology": ("topology",),
    "production-cards": ("production-cards",),
    "drafts": ("episode-writing",),
    "reviews": ("scene-review", "dialogue-review", "isolated-audience"),
    "state-writeback": ("state-writeback",),
    "final": ("whole-play-review",),
}
NODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EPISODE_HEADER = re.compile(r"^##\s+(episode-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
EMOTION_HEADER = re.compile(r"^##\s+(ES-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)
CAUSAL_HEADER = re.compile(r"^##\s+(CG-\d{3})\s*｜\s*(.+?)\s*$", re.MULTILINE)


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def blocks(text: str, pattern: re.Pattern[str]) -> dict[str, str]:
    matches = list(pattern.finditer(text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[match.group(1)] = text[match.end() : end].strip()
    return result


def read_required(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"缺少阶段产物：{path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"阶段产物为空：{path}")
    return text


def parse_vad(value: str, label: str) -> tuple[float, float, float]:
    numbers = re.findall(r"-?(?:\d+(?:\.\d+)?|\.\d+)", value)
    if len(numbers) != 3:
        raise ValueError(f"{label} 必须包含三个 VAD 数值")
    result = tuple(float(item) for item in numbers)
    if any(item < -1 or item > 1 for item in result):
        raise ValueError(f"{label} 的 VAD 数值必须位于 -1 到 1")
    return result  # type: ignore[return-value]


def validate_emotional_spine(cache: Path) -> tuple[set[str], str]:
    text = read_required(cache / "emotional-spine.md")
    entries = blocks(text, EMOTION_HEADER)
    if len(entries) < 4:
        raise ValueError("emotional-spine.md 至少需要四个情绪事件")
    functions: set[str] = set()
    displacements: list[float] = []
    required = (
        "阶段功能",
        "当前欲望",
        "当前恐惧",
        "压力来源",
        "造成行动",
        "不可逆结果",
        "下一催化",
        "起点VAD",
        "落点VAD",
        "强度处理",
    )
    for event_id, block in entries.items():
        for name in required:
            value = field(block, name)
            minimum = 3 if "VAD" in name else 4 if name in {"阶段功能", "强度处理"} else 6
            if len(re.sub(r"\s+", "", value)) < minimum:
                raise ValueError(f"{event_id} 缺少具体字段：{name}")
        functions.add(field(block, "阶段功能"))
        start = parse_vad(field(block, "起点VAD"), f"{event_id}/起点VAD")
        end = parse_vad(field(block, "落点VAD"), f"{event_id}/落点VAD")
        displacement = math.dist(start, end)
        displacements.append(displacement)
        declared = field(block, "情绪位移")
        if not declared:
            raise ValueError(f"{event_id} 缺少情绪位移")
        try:
            declared_value = float(declared)
        except ValueError as exc:
            raise ValueError(f"{event_id} 的情绪位移不是数值") from exc
        if abs(declared_value - displacement) > 0.03:
            raise ValueError(f"{event_id} 的情绪位移与 VAD 坐标不一致")
        handling = field(block, "强度处理")
        if handling not in {"原始通过", "放大后重算"}:
            raise ValueError(f"{event_id} 的强度处理必须为原始通过或放大后重算")
        if handling == "放大后重算":
            before = field(block, "放大前位移")
            upgrade = field(block, "事件升级")
            try:
                before_value = float(before)
            except ValueError as exc:
                raise ValueError(f"{event_id} 放大后缺少放大前位移") from exc
            if before_value >= displacement or len(re.sub(r"\s+", "", upgrade)) < 10:
                raise ValueError(f"{event_id} 放大坐标后必须同步升级具体事件")
    required_functions = {"开场钩子", "首次释放", "中段反噬", "高潮解法"}
    if not required_functions <= functions:
        raise ValueError(f"情绪脊缺少阶段功能：{sorted(required_functions - functions)}")
    if max(displacements) < 1.1 or sum(displacements) / len(displacements) < 0.7:
        raise ValueError("情绪坐标强度不足；必须放大 VAD 并重算对应行动、损失和不可逆结果")
    return set(entries), text


def validate_causal_graph(cache: Path, emotion_ids: set[str]) -> tuple[set[str], str]:
    text = read_required(cache / "causal-graph.md")
    entries = blocks(text, CAUSAL_HEADER)
    if not entries:
        raise ValueError("causal-graph.md 未找到因果事件")
    used_emotions: set[str] = set()
    successors: dict[str, set[str]] = {}
    for causal_id, block in entries.items():
        emotional = set(re.findall(r"ES-\d{3}", field(block, "情绪事件")))
        if not emotional or not emotional <= emotion_ids:
            raise ValueError(f"{causal_id} 未引用有效情绪事件")
        used_emotions |= emotional
        for name in ("原因", "人物行动", "阻力", "不可逆结果"):
            if len(re.sub(r"\s+", "", field(block, name))) < 8:
                raise ValueError(f"{causal_id} 缺少具体因果字段：{name}")
        next_value = field(block, "下一事件")
        if not next_value:
            raise ValueError(f"{causal_id} 缺少下一事件")
        targets = set(re.findall(r"CG-\d{3}", next_value))
        if next_value != "结局" and not targets:
            raise ValueError(f"{causal_id} 的下一事件必须引用因果事件或结局")
        successors[causal_id] = targets
    if emotion_ids - used_emotions:
        raise ValueError(f"存在未进入因果图的情绪事件：{sorted(emotion_ids - used_emotions)}")
    for causal_id, targets in successors.items():
        unknown = targets - set(entries)
        if unknown:
            raise ValueError(f"{causal_id} 指向未知因果事件：{sorted(unknown)}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visiting:
            raise ValueError("causal-graph.md 存在循环")
        if node in visited:
            return
        visiting.add(node)
        for target in successors[node]:
            walk(target)
        visiting.remove(node)
        visited.add(node)

    for causal_id in entries:
        walk(causal_id)
    return set(entries), text


def validate_topology_provenance(
    cache: Path, emotion_ids: set[str], causal_ids: set[str]
) -> tuple[set[str], str]:
    text = read_required(cache / "topology.md")
    entries = blocks(text, NODE_HEADER)
    if not entries:
        raise ValueError("topology.md 未找到节点")
    for node_id, block in entries.items():
        emotional = set(re.findall(r"ES-\d{3}", field(block, "情绪事件")))
        causal = set(re.findall(r"CG-\d{3}", field(block, "因果事件")))
        if not emotional or not emotional <= emotion_ids:
            raise ValueError(f"{node_id} 未引用有效情绪事件")
        if not causal or not causal <= causal_ids:
            raise ValueError(f"{node_id} 未引用有效因果事件")
        successors = set(re.findall(r"episode-\d{3}", field(block, "后续节点")))
        choices = set(
            re.findall(r"^-\s*选择：.*?(?:->|→)\s*(episode-\d{3})\s*$", block, re.MULTILINE)
        )
        if choices:
            consequence_lines = re.findall(
                r"^-\s*选择后果：\s*(episode-\d{3})\s*｜\s*(ES-\d{3})\s*｜\s*(CG-\d{3})\s*｜\s*(\S.*?)\s*$",
                block,
                re.MULTILINE,
            )
            consequence_targets = {item[0] for item in consequence_lines}
            if consequence_targets != successors:
                raise ValueError(f"{node_id} 的每条选择边必须有可追溯的选择后果")
            for target, emotion_id, causal_id, consequence in consequence_lines:
                if emotion_id not in emotion_ids or causal_id not in causal_ids:
                    raise ValueError(f"{node_id}->{target} 的选择后果引用无效")
                if len(re.sub(r"\s+", "", consequence)) < 10:
                    raise ValueError(f"{node_id}->{target} 的不可逆后果不具体")
        predecessors = set(re.findall(r"episode-\d{3}", field(block, "前置节点")))
        if len(predecessors) > 1:
            merge = field(block, "合流保留差异")
            if any(item not in merge for item in predecessors) or len(re.sub(r"\s+", "", merge)) < 18:
                raise ValueError(f"{node_id} 合流时未保留全部来路差异")
    return set(entries), text


def validate_episode_map(cache: Path, node_ids: set[str]) -> tuple[dict[str, set[str]], str]:
    text = read_required(cache / "episode-map.md")
    entries = blocks(text, EPISODE_HEADER)
    if not entries:
        raise ValueError("episode-map.md 未找到分集容器")
    assigned: list[str] = []
    mapping: dict[str, set[str]] = {}
    for episode_id, block in entries.items():
        members = set(re.findall(r"node-\d{3}", field(block, "剧情节点")))
        if not members:
            raise ValueError(f"{episode_id} 未映射剧情节点")
        unknown = members - node_ids
        if unknown:
            raise ValueError(f"{episode_id} 映射未知剧情节点：{sorted(unknown)}")
        mapping[episode_id] = members
        assigned.extend(members)
    duplicates = sorted({item for item in assigned if assigned.count(item) > 1})
    missing = sorted(node_ids - set(assigned))
    if duplicates:
        raise ValueError(f"剧情节点被重复映射：{duplicates}")
    if missing:
        raise ValueError(f"剧情节点未映射：{missing}")
    return mapping, text


def episode_section_payload(cache: Path, headings: tuple[str, ...]) -> dict[str, Any]:
    episode_paths = sorted((cache / "episodes").glob("episode-*.md"))
    if not episode_paths:
        raise ValueError("未找到私有逐集文件")
    payload: dict[str, Any] = {}
    for path in episode_paths:
        text = read_required(path)
        selected: dict[str, str] = {}
        for heading in headings:
            content = section(text, heading)
            if not content:
                raise ValueError(f"{path.stem} 缺少阶段栏目：{heading}")
            selected[heading] = content
        payload[path.stem] = selected
    return payload


def artifact_payload(cache: Path, canvas: Path, stage: str) -> dict[str, Any]:
    if stage == "upstream":
        return {
            name: read_required(cache / name)
            for name in ("input.md", "upstream-packet.md", "upstream-reference.md")
        }
    emotion_ids, emotion_text = validate_emotional_spine(cache)
    if stage == "emotional-spine":
        return {"emotional-spine.md": emotion_text}
    causal_ids, causal_text = validate_causal_graph(cache, emotion_ids)
    if stage == "causal-graph":
        return {"causal-graph.md": causal_text}
    node_ids, topology_text = validate_topology_provenance(
        cache, emotion_ids, causal_ids
    )
    if stage == "topology":
        return {
            "topology.md": topology_text,
            "branch-audit.md": read_required(cache / "branch-audit.md"),
        }
    if stage == "production-cards":
        cards = episode_section_payload(
            cache,
            (
                "生产卡",
                "单集梗概",
                "本集冲突与前后承接",
                "前置节点",
                "后续节点",
                "题材透镜记录",
            ),
        )
        if set(cards) != node_ids:
            raise ValueError("生产卡与分集拓扑集合不一致")
        for episode_id, selected in cards.items():
            card = selected["生产卡"]
            if field(card, "分集容器") != episode_id:
                raise ValueError(f"{episode_id} 生产卡未绑定自身分集容器")
            topology_blocks = blocks(topology_text, NODE_HEADER)
            for label, prefix in (("情绪事件", "ES"), ("因果事件", "CG")):
                card_refs = set(re.findall(rf"{prefix}-\d{{3}}", field(card, label)))
                topology_refs = set(re.findall(rf"{prefix}-\d{{3}}", field(topology_blocks[episode_id], label)))
                if not card_refs or card_refs != topology_refs:
                    raise ValueError(f"{episode_id} 生产卡的{label}未继承本集拓扑")
        return cards
    if stage == "drafts":
        return episode_section_payload(cache, ("当前集初稿",))
    if stage == "reviews":
        return episode_section_payload(
            cache,
            (
                "当前集定稿",
                "场面事实复核记录",
                "人物交流复核记录",
                "隔离观众复核记录",
                "校验状态",
            ),
        )
    if stage == "state-writeback":
        return {
            "root": {
                name: read_required(cache / name)
                for name in ("global.md", "characters.md", "props.md", "hooks.md", "validation.md")
            },
            "episodes": episode_section_payload(cache, ("真实结尾状态",)),
        }
    if stage == "final":
        return {
            name: read_required(canvas / name)
            for name in ("episode-synopsis.md", "episode-flowchart.svg", "episode-script.md")
        }
    raise ValueError(f"未知阶段：{stage}")


def reference_payload(cache: Path, stage: str) -> dict[str, str]:
    receipt_dir = cache / ".reference-receipts"
    result: dict[str, str] = {}
    for phase in REFERENCE_PHASES[stage]:
        valid: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(receipt_dir.glob(f"{phase}--*.json")):
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                current = load_bundle(
                    phase, tuple(str(item) for item in stored.get("profiles") or [])
                )
                expected = {key: value for key, value in current.items() if key != "bundle"}
                if stored == expected:
                    valid.append((path, stored))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        if not valid:
            raise ValueError(f"阶段 {stage} 缺少当前有效 reference 凭证：{phase}")
        path, stored = valid[0]
        result[path.name] = sha_text(canonical_json(stored))
    return result


def receipt_path(cache: Path, stage: str) -> Path:
    return cache / ".stage-receipts" / f"{stage}.json"


def compute_receipt(cache: Path, canvas: Path, stage: str) -> dict[str, Any]:
    index = STAGES.index(stage)
    upstream: dict[str, Any] | None = None
    if index:
        previous = STAGES[index - 1]
        verify_stage(cache, canvas, previous)
        upstream = json.loads(receipt_path(cache, previous).read_text(encoding="utf-8"))
    artifacts = artifact_payload(cache, canvas, stage)
    core = {
        "pipeline_version": PIPELINE_VERSION,
        "stage": stage,
        "previous_stage": STAGES[index - 1] if index else None,
        "previous_receipt_sha256": upstream.get("receipt_sha256") if upstream else None,
        "reference_receipts": reference_payload(cache, stage),
        "artifacts_sha256": sha_text(canonical_json(artifacts)),
    }
    return core | {"receipt_sha256": sha_text(canonical_json(core))}


def verify_stage(cache: Path, canvas: Path, stage: str) -> dict[str, Any]:
    path = receipt_path(cache, stage)
    if not path.is_file():
        raise ValueError(f"缺少阶段封印：{stage}")
    stored = json.loads(path.read_text(encoding="utf-8"))
    current = compute_receipt(cache, canvas, stage)
    if stored != current:
        raise ValueError(f"阶段 {stage} 已过期或无法追溯，必须从该阶段重新执行")
    return stored


def seal_stage(cache: Path, canvas: Path, stage: str) -> Path:
    result = compute_receipt(cache, canvas, stage)
    path = receipt_path(cache, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def resume_status(cache: Path, canvas: Path) -> dict[str, Any]:
    last_valid: str | None = None
    for stage in STAGES:
        path = receipt_path(cache, stage)
        if not path.is_file():
            return {
                "pipeline_version": PIPELINE_VERSION,
                "last_valid_stage": last_valid,
                "resume_from_stage": stage,
                "reason": "missing",
            }
        try:
            verify_stage(cache, canvas, stage)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {
                "pipeline_version": PIPELINE_VERSION,
                "last_valid_stage": last_valid,
                "resume_from_stage": stage,
                "reason": str(exc),
            }
        last_valid = stage
    return {
        "pipeline_version": PIPELINE_VERSION,
        "last_valid_stage": last_valid,
        "resume_from_stage": None,
        "reason": "complete",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--mode", choices=("seal", "verify", "status"), required=True)
    args = parser.parse_args()
    cache = args.cache_root.resolve()
    canvas = args.canvas_root.resolve()
    try:
        if args.mode == "status":
            print(json.dumps(resume_status(cache, canvas), ensure_ascii=False, sort_keys=True))
        elif not args.stage:
            raise ValueError("seal/verify 必须提供 --stage")
        elif args.mode == "seal":
            path = seal_stage(cache, canvas, args.stage)
            print(f"stage sealed: {args.stage} ({path})")
        else:
            verify_stage(cache, canvas, args.stage)
            print(f"stage verified: {args.stage}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
