#!/usr/bin/env python3
"""Record drafts and freeze only episodes with current, evidenced semantic audits."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from cache_paths import cache_root_for
from validate_storyboard_fields import (
    build_storyboard_fields,
    validate_fields,
    validate_final_script_format,
)
from load_reference_bundle import verify_receipts
from prepare_dialogue_review import build_dialogue_packet
from seal_pipeline_stage import seal_stage, verify_stage
from episode_topology import load_collapsed, parse_episode_map


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


LEGACY_CAUSAL_FIELDS = (
    "上游事实",
    "地点与权限",
    "事件与反应链",
    "物件与状态",
    "后续进入条件",
)
LEGACY_DIALOGUE_FIELDS = (
    "话茬与当下目的",
    "人物声音",
    "设定发布与承重句",
    "朴素中文",
)
LEGACY_COLD_FIELDS = (
    "动作可拍门",
    "对白可说门",
    "冷读六问",
)
SCENE_FACT_FIELDS = (
    "触发登记覆盖",
    "地点权限与首次出场",
    "事件感知与第一反应",
    "行动阻力与可见结果",
    "物件路径与后续入口",
)
CHARACTER_EXCHANGE_FIELDS_V18 = (
    "关系触发与行动优先级",
    "话茬与当下目的",
    "人物声音与关系距离",
    "设定发布与朴素中文",
)
CHARACTER_EXCHANGE_FIELDS_V19 = (
    "台词抽取与原位写回",
    "话茬与朴素表达",
    "姓名知情与关系距离",
    "人物声音与承重句",
)
AUDIENCE_FIELDS = (
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


def section(text: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def replace_section(text: str, heading: str, content: str) -> str:
    pattern = re.compile(
        rf"(^##\s+{re.escape(heading)}\s*$\n)(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    replacement = rf"\g<1>{content.strip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    anchor = re.search(r"^##\s+真实结尾状态\s*$", text, re.MULTILINE)
    block = f"## {heading}\n{content.strip()}\n\n"
    if anchor:
        return text[: anchor.start()] + block + text[anchor.start() :]
    return text.rstrip() + "\n\n" + block


def metrics(text: str) -> tuple[int, int]:
    return len(re.sub(r"\s+", "", text)), len(re.findall(r"^\s*△", text, re.MULTILINE))


def digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def field(block: str, name: str) -> str:
    match = re.search(rf"^-\s*{re.escape(name)}：\s*(.*?)\s*$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def pending_audit(fields: tuple[str, ...]) -> str:
    lines = ["- 状态：PENDING", "- 正文 SHA-256：PENDING"]
    lines.extend(f"- {name}：PENDING｜待当前主 Agent 复核" for name in fields)
    lines.append("- 未解决问题：待复核")
    return "\n".join(lines)


def pending_audience_audit(fields: tuple[str, ...]) -> str:
    lines = [
        "- 状态：PENDING",
        "- 隔离方式：PENDING",
        "- 正文 SHA-256：PENDING",
    ]
    lines.extend(f"- {name}：PENDING｜待隔离观众复核" for name in fields)
    lines.extend(("- 一句话复述：PENDING", "- 未解决问题：待复核"))
    return "\n".join(lines)


def pending_dialogue_audit_v25() -> str:
    return "\n".join(
        (
            "- 状态：PENDING",
            "- 正文 SHA-256：PENDING",
            "- 台词包 SHA-256：PENDING",
            "- 台词总数：PENDING",
            "- 覆盖状态：PENDING",
            "- 问题项：待复核",
            "- 未解决问题：待复核",
        )
    )


def validate_genre_lens(cache_text: str, episode_id: str, require_declared_pass: bool = True) -> None:
    block = section(cache_text, "题材透镜记录")
    if not block:
        raise SystemExit(f"{episode_id} 缺少题材透镜记录")
    for name in (
        "具体事件",
        "题材反差物",
        "题材内升级",
        "后续催化剂或结局余波",
    ):
        value = field(block, name).strip()
        if len(re.sub(r"\s+", "", value)) < 8 or value in {
            "无",
            "符合题材",
            "已完成",
            "见梗概",
        }:
            raise SystemExit(f"{episode_id} 的题材透镜记录缺少具体内容：{name}")
    if require_declared_pass and field(block, "判定").strip() != "PASS":
        raise SystemExit(f"{episode_id} 的题材透镜记录未通过")


def validate_atomic_ledger(cache_root: Path) -> None:
    path = cache_root / "atomic-tasks.json"
    if not path.is_file():
        raise SystemExit("v0.1.29 缺少私有原子任务账本")
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("原子任务账本无法读取") from exc
    tasks = ledger.get("tasks") if isinstance(ledger, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise SystemExit("原子任务账本为空")
    running = [item for item in tasks if isinstance(item, dict) and item.get("status") == "running"]
    if len(running) > 1:
        raise SystemExit("原子任务账本存在多个 running 任务")
    for item in tasks:
        if not isinstance(item, dict):
            raise SystemExit("原子任务账本含非法记录")
        if item.get("id") == "final-assembly":
            if item.get("status") not in {"running", "complete"}:
                raise SystemExit("最终组装任务尚未领取")
        elif item.get("status") != "complete":
            raise SystemExit(f"原子任务尚未完成：{item.get('id')}")


def validate_audit(
    cache_text: str,
    heading: str,
    required_fields: tuple[str, ...],
    expected_digest: str,
    episode_id: str,
    scene_names: set[str] | None = None,
) -> None:
    block = section(cache_text, heading)
    if not block:
        raise SystemExit(f"{episode_id} 缺少{heading}")
    if field(block, "状态") != "PASS":
        raise SystemExit(f"{episode_id} 的{heading}未 PASS")
    if field(block, "正文 SHA-256") != expected_digest:
        raise SystemExit(f"{episode_id} 的{heading}正文指纹已失效，必须重跑复核")
    for name in required_fields:
        value = field(block, name)
        if scene_names is None:
            match = re.fullmatch(r"PASS[｜|]\s*(\S.*)", value)
            if not match or len(re.sub(r"\s+", "", match.group(1))) < 8:
                raise SystemExit(f"{episode_id} 的{heading}缺少具体依据：{name}")
        else:
            match = re.fullmatch(r"PASS[｜|]\s*([^｜|]+?)[｜|]\s*(\S.*)", value)
            if not match or len(re.sub(r"\s+", "", match.group(2))) < 12:
                raise SystemExit(
                    f"{episode_id} 的{heading}依据必须使用 PASS｜场次｜具体证据：{name}"
                )
            locator = match.group(1).strip()
            locator_scenes = set(
                re.findall(r"场(?:[一二三四五六七八九十百零〇两\d]+|[A-Za-z]+)", locator)
            )
            located = scene_names & locator_scenes
            if not located:
                raise SystemExit(
                    f"{episode_id} 的{heading}引用了正文不存在的场次：{name}={locator}"
                )
    if field(block, "未解决问题") != "无":
        raise SystemExit(f"{episode_id} 的{heading}仍有未解决问题")


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
    cache_text: str, final_text: str, episode_id: str
) -> None:
    block = section(cache_text, "人物交流复核记录")
    dialogue_map = dialogue_by_scene(final_text)
    all_dialogue = [line for lines in dialogue_map.values() for line in lines]
    covered: set[str] = set()
    for name in CHARACTER_EXCHANGE_FIELDS_V19:
        value = field(block, name)
        quotes = re.findall(r"[“\"]([^”\"]{2,})[”\"]", value)
        if not quotes or not any(
            quote in dialogue for quote in quotes for dialogue in all_dialogue
        ):
            raise SystemExit(
                f"{episode_id} 的人物交流证据未逐字引用真实台词：{name}"
            )
        covered.update(
            re.findall(r"场(?:[一二三四五六七八九十百零〇两\d]+|[A-Za-z]+)", value)
        )


def validate_v25_dialogue_audit(
    cache_text: str, final_text: str, expected_digest: str, episode_id: str
) -> None:
    block = section(cache_text, "人物交流复核记录")
    packet = build_dialogue_packet(final_text)
    if field(block, "状态") != "PASS":
        raise SystemExit(f"{episode_id} 的人物交流复核记录未 PASS")
    if field(block, "正文 SHA-256") != expected_digest:
        raise SystemExit(f"{episode_id} 的人物交流正文指纹已失效")
    if field(block, "台词包 SHA-256") != packet["dialogue_sha256"]:
        raise SystemExit(f"{episode_id} 的人物交流台词包指纹已失效")
    if field(block, "台词总数") != str(packet["dialogue_count"]):
        raise SystemExit(f"{episode_id} 的人物交流台词总数与当前定稿不一致")
    if field(block, "覆盖状态") != "COMPLETE":
        raise SystemExit(f"{episode_id} 的人物交流复核未覆盖全部台词")
    if field(block, "问题项") != "无" or field(block, "未解决问题") != "无":
        raise SystemExit(f"{episode_id} 的人物交流复核仍有未解决问题")
def validate_trigger_registry(cache_text: str, script_text: str, episode_id: str) -> None:
    block = section(cache_text, "语义触发登记")
    if not block:
        raise SystemExit(f"{episode_id} 缺少语义触发登记")
    known_scenes = scene_names(script_text)
    for name in TRIGGER_FIELDS:
        value = field(block, name)
        if not value:
            raise SystemExit(f"{episode_id} 的语义触发登记缺少字段：{name}")
        if name == "关键事件" and value == "无":
            raise SystemExit(f"{episode_id} 的关键事件不得为无")
        for locator in re.findall(r"@\s*(场[^；;，,\s]+)", value):
            if locator not in known_scenes:
                raise SystemExit(
                    f"{episode_id} 的语义触发登记引用了正文不存在的场次：{locator}"
                )


def validate_audience_audit(
    cache_text: str,
    heading: str,
    fields: tuple[str, ...],
    expected_digest: str,
    episode_id: str,
    scenes: set[str] | None = None,
) -> None:
    validate_audit(
        cache_text, heading, fields, expected_digest, episode_id, scenes
    )
    block = section(cache_text, heading)
    if field(block, "隔离方式") not in ("独立冷读", "最小上下文复检"):
        raise SystemExit(f"{episode_id} 的{heading}缺少有效隔离方式")
    if len(re.sub(r"\s+", "", field(block, "一句话复述"))) < 12:
        raise SystemExit(f"{episode_id} 的{heading}缺少具体一句话复述")


def selected_paths(
    root: Path, selection: str | None, cache_root: Path | None = None
) -> list[Path]:
    cache_dir = (cache_root or cache_root_for(root)) / "episodes"
    all_paths = sorted(cache_dir.glob("episode-*.md"))
    if not all_paths:
        raise SystemExit("未找到 Canvas 外私有分集缓存")
    if not selection:
        return all_paths
    requested = {item.strip() for item in selection.split(",") if item.strip()}
    known = {path.stem: path for path in all_paths}
    missing = sorted(requested - set(known))
    if missing:
        raise SystemExit(f"未找到指定分集：{', '.join(missing)}")
    return [known[name] for name in sorted(requested)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canvas_root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--mode", choices=("record-draft", "freeze", "finalize"), required=True)
    parser.add_argument("--episodes", help="逗号分隔的 episode-NNN；省略时处理全部")
    parser.add_argument("--min-visible-chars", type=int, default=850)
    parser.add_argument("--max-visible-chars", type=int, default=1300)
    parser.add_argument("--min-action-beats", type=int, default=16)
    parser.add_argument("--max-action-beats", type=int, default=24)
    args = parser.parse_args()

    root = args.canvas_root.resolve()
    cache_root = args.cache_root.resolve() if args.cache_root else cache_root_for(root)
    manifest = (cache_root / "manifest.md").read_text(encoding="utf-8")
    is_v29 = "episode-cache-v0.1.29" in manifest
    is_v28 = "episode-cache-v0.1.28" in manifest or is_v29
    is_v27 = "episode-cache-v0.1.27" in manifest or is_v28
    is_v25 = (
        "episode-cache-v0.1.25" in manifest
        or "episode-cache-v0.1.26" in manifest
        or is_v27
    )
    is_v24 = "episode-cache-v0.1.24" in manifest or is_v25
    is_v23 = "episode-cache-v0.1.23" in manifest or is_v24
    is_v21 = (
        "episode-cache-v0.1.21" in manifest
        or "episode-cache-v0.1.22" in manifest
        or is_v23
    )
    is_v19 = "episode-cache-v0.1.19" in manifest or is_v21
    is_v18 = "episode-cache-v0.1.18" in manifest or is_v19
    if is_v28:
        required_stage = {
            "record-draft": "production-cards",
            "freeze": "drafts",
            "finalize": "state-writeback",
        }[args.mode]
        verify_stage(
            cache_root,
            root,
            required_stage,
        )
    if is_v23:
        required_phases = (
            REQUIRED_REFERENCE_PHASES
            if args.mode != "record-draft"
            else REQUIRED_REFERENCE_PHASES[:4]
        )
        receipt_errors = verify_receipts(
            cache_root / ".reference-receipts", required_phases
        )
        if receipt_errors:
            raise SystemExit("reference 装载门禁失败：\n" + "\n".join(receipt_errors))
    if is_v21:
        upstream_check = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("validate_upstream_reference.py")), str(cache_root)],
            capture_output=True,
            text=True,
        )
        if upstream_check.returncode:
            raise SystemExit("上游制作参考校验失败：\n" + (upstream_check.stdout or upstream_check.stderr).strip())
    if is_v27:
        creative_check = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("validate_episode_cache.py")),
                str(root),
                "--cache-root",
                str(cache_root),
                "--min-visible-chars",
                str(args.min_visible_chars),
                "--max-visible-chars",
                str(args.max_visible_chars),
                "--min-action-beats",
                str(args.min_action_beats),
                "--max-action-beats",
                str(args.max_action_beats),
            ],
            capture_output=True,
            text=True,
        )
        if creative_check.returncode:
            raise SystemExit(
                "反俗套与题材透镜门禁失败：\n"
                + (creative_check.stdout or creative_check.stderr).strip()
            )
    character_exchange_fields = (
        CHARACTER_EXCHANGE_FIELDS_V19 if is_v19 else CHARACTER_EXCHANGE_FIELDS_V18
    )
    topology = load_collapsed(cache_root) if is_v29 else (cache_root / "topology.md").read_text(encoding="utf-8")
    targets = selected_paths(root, args.episodes, cache_root)

    for cache_path in targets:
        episode_id = cache_path.stem
        cache_text = cache_path.read_text(encoding="utf-8")
        if is_v27:
            validate_genre_lens(cache_text, episode_id, require_declared_pass=not is_v29)

        if args.mode == "record-draft":
            draft_text = section(cache_text, "当前集初稿")
            if len(re.sub(r"\s+", "", draft_text)) < 300:
                raise SystemExit(f"{episode_id} 当前集初稿不是实际完整正文")
            if is_v18:
                validate_trigger_registry(cache_text, draft_text, episode_id)
            cache_text = replace_section(cache_text, "当前集定稿", "PENDING")
            cache_text = replace_section(cache_text, "度量记录", "- 判定：PENDING")
            if is_v18:
                cache_text = replace_section(
                    cache_text, "场面事实复核记录", pending_audit(SCENE_FACT_FIELDS)
                )
                cache_text = replace_section(
                    cache_text,
                    "人物交流复核记录",
                    pending_dialogue_audit_v25()
                    if is_v25
                    else pending_audit(character_exchange_fields),
                )
                cache_text = replace_section(
                    cache_text,
                    "隔离观众复核记录",
                    pending_audience_audit(AUDIENCE_FIELDS),
                )
                status_text = (
                    "- 机械完整度：PENDING\n- 场面事实：PENDING\n"
                    "- 人物交流：PENDING\n- 隔离观众：PENDING\n- 冻结状态：未冻结"
                )
            else:
                cache_text = replace_section(
                    cache_text, "因果复核记录", pending_audit(LEGACY_CAUSAL_FIELDS)
                )
                cache_text = replace_section(
                    cache_text, "对白复核记录", pending_audit(LEGACY_DIALOGUE_FIELDS)
                )
                cache_text = replace_section(
                    cache_text,
                    "冷读复核记录",
                    pending_audience_audit(LEGACY_COLD_FIELDS),
                )
                status_text = (
                    "- 机械完整度：PENDING\n- 因果连续性：PENDING\n"
                    "- 中文对白：PENDING\n- 独立冷读：PENDING\n- 冻结状态：未冻结"
                )
            cache_text = replace_section(cache_text, "校验状态", status_text)
        else:
            candidate_text = section(cache_text, "当前集定稿").strip()
            if len(re.sub(r"\s+", "", candidate_text)) < 300:
                raise SystemExit(f"{episode_id} 当前集定稿不是实际完整正文")
            try:
                validate_final_script_format(candidate_text)
            except ValueError as exc:
                raise SystemExit(f"{episode_id} 正式剧本格式未通过：{exc}") from exc
            synopsis_match = re.search(r"^单集梗概：\s*(.+)$", candidate_text, re.MULTILINE)
            if not synopsis_match:
                raise SystemExit(f"{episode_id} 当前集定稿缺少单集梗概")
            if is_v28:
                frozen_synopsis = section(cache_text, "单集梗概").strip()
                if synopsis_match.group(1).strip() != frozen_synopsis:
                    raise SystemExit(
                        f"{episode_id} 正文梗概与 production-cards 封印不一致，"
                        "必须返回生产卡阶段重算"
                    )
            else:
                cache_text = replace_section(cache_text, "单集梗概", synopsis_match.group(1))
            visible_chars, action_beats = metrics(candidate_text)
            if not args.min_visible_chars <= visible_chars <= args.max_visible_chars:
                raise SystemExit(
                    f"{episode_id} 可见字符数 {visible_chars} 超出 "
                    f"{args.min_visible_chars}—{args.max_visible_chars}"
                )
            if not args.min_action_beats <= action_beats <= args.max_action_beats:
                raise SystemExit(
                    f"{episode_id} 动作段数 {action_beats} 超出 "
                    f"{args.min_action_beats}—{args.max_action_beats}"
                )
            current_digest = digest(candidate_text)
            if is_v18:
                scenes = scene_names(candidate_text)
                validate_trigger_registry(cache_text, candidate_text, episode_id)
                validate_audit(
                    cache_text,
                    "场面事实复核记录",
                    SCENE_FACT_FIELDS,
                    current_digest,
                    episode_id,
                    scenes,
                )
                if is_v25:
                    validate_v25_dialogue_audit(
                        cache_text, candidate_text, current_digest, episode_id
                    )
                else:
                    validate_audit(
                        cache_text,
                        "人物交流复核记录",
                        character_exchange_fields,
                        current_digest,
                        episode_id,
                        scenes,
                    )
                validate_audience_audit(
                    cache_text,
                    "隔离观众复核记录",
                    AUDIENCE_FIELDS,
                    current_digest,
                    episode_id,
                    scenes,
                )
                if is_v19 and not is_v25:
                    validate_v19_dialogue_evidence(cache_text, candidate_text, episode_id)
            else:
                validate_audit(
                    cache_text,
                    "因果复核记录",
                    LEGACY_CAUSAL_FIELDS,
                    current_digest,
                    episode_id,
                )
                validate_audit(
                    cache_text,
                    "对白复核记录",
                    LEGACY_DIALOGUE_FIELDS,
                    current_digest,
                    episode_id,
                )
                validate_audience_audit(
                    cache_text,
                    "冷读复核记录",
                    LEGACY_COLD_FIELDS,
                    current_digest,
                    episode_id,
                )
            record = (
                f"- 可见字符数：{visible_chars}\n"
                f"- 动作段数：{action_beats}\n"
                f"- 门槛：{args.min_visible_chars}—{args.max_visible_chars} 个去空白可见字符；"
                f"{args.min_action_beats}—{args.max_action_beats} 个动作段\n"
                "- 判定：PASS"
            )
            cache_text = replace_section(cache_text, "度量记录", record)
            if is_v18:
                status_text = (
                    "- 机械完整度：PASS\n"
                    "- 场面事实：PASS（语义判定见当前正文指纹对应记录）\n"
                    "- 人物交流：PASS（语义判定见当前正文指纹对应记录）\n"
                    "- 隔离观众：PASS（语义判定见当前正文指纹对应记录）\n"
                    "- 机械脚本职责：仅验证记录结构、场次定位与一致性\n"
                    "- 冻结状态：已冻结"
                )
            else:
                status_text = (
                    "- 机械完整度：PASS\n"
                    "- 因果连续性：PASS（见当前正文指纹对应的因果复核记录）\n"
                    "- 中文对白：PASS（见当前正文指纹对应的对白复核记录）\n"
                    "- 独立冷读：PASS（见当前正文指纹对应的冷读复核记录）\n"
                    "- 冻结状态：已冻结"
                )
            cache_text = replace_section(cache_text, "校验状态", status_text)
        if args.mode != "finalize":
            cache_path.write_text(cache_text.rstrip() + "\n", encoding="utf-8")
        if args.mode in {"freeze", "finalize"}:
            validate_fields(build_storyboard_fields(cache_text, topology))

    if args.mode == "finalize":
        if is_v29:
            validate_atomic_ledger(cache_root)
        all_cache = sorted((cache_root / "episodes").glob("episode-*.md"))
        if is_v29:
            node_ids = set(re.findall(r"node-\d{3}", (cache_root / "topology.md").read_text(encoding="utf-8")))
            order = [episode_id for episode_id, _, _ in parse_episode_map((cache_root / "episode-map.md").read_text(encoding="utf-8"), node_ids)]
            known = {path.stem: path for path in all_cache}
            if set(known) != set(order):
                raise SystemExit("分集文件集合与 episode-map.md 不一致")
            all_cache = [known[episode_id] for episode_id in order]
        final_texts: list[str] = []
        synopsis_texts: list[str] = []
        for cache_path in all_cache:
            cache_text = cache_path.read_text(encoding="utf-8")
            episode_id = cache_path.stem
            if "冻结状态：已冻结" not in section(cache_text, "校验状态"):
                raise SystemExit(f"{episode_id} 尚未冻结，不能组装玩家可见完整剧本")
            final_text = section(cache_text, "当前集定稿").strip()
            try:
                validate_final_script_format(final_text)
                validate_fields(build_storyboard_fields(cache_text, topology))
            except ValueError as exc:
                raise SystemExit(f"{episode_id} 不能组装：{exc}") from exc
            final_texts.append(final_text)
            title_match = re.search(r"^#\s+(第\d+集[^\n]*)$", final_text, re.MULTILINE)
            if not title_match:
                raise SystemExit(f"{episode_id} 当前集定稿缺少标准分集标题")
            synopsis = section(cache_text, "单集梗概").strip()
            if not synopsis:
                raise SystemExit(f"{episode_id} 缺少冻结单集梗概")
            synopsis_texts.append(f"## {title_match.group(1).strip()}\n\n{synopsis}")
        combined = "\n\n---\n\n".join(final_texts)
        synopsis_combined = "# 各集梗概\n\n" + "\n\n".join(synopsis_texts) + "\n"
        if is_v28:
            staging = cache_root / ".final-staging"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "episode-script.md").write_text(combined + "\n", encoding="utf-8")
            (staging / "episode-synopsis.md").write_text(synopsis_combined, encoding="utf-8")
            render = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("render_episode_flowchart.py")),
                    str(root),
                    "--cache-root",
                    str(cache_root),
                    "--output",
                    str(staging / "episode-flowchart.svg"),
                ],
                capture_output=True,
                text=True,
            )
            if render.returncode:
                raise SystemExit(
                    "公开流程图组装失败：\n"
                    + (render.stdout or render.stderr).strip()
                )
            for name in (
                "episode-synopsis.md",
                "episode-flowchart.svg",
                "episode-script.md",
            ):
                (staging / name).replace(root / name)
            seal_stage(cache_root, root, "final")
        else:
            (root / "episode-script.md").write_text(combined + "\n", encoding="utf-8")
            (root / "episode-synopsis.md").write_text(synopsis_combined, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
