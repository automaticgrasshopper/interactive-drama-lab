#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲编剧台的后台生产工人。

任务由本地后端线程持续执行，与浏览器连接解耦；页面刷新后只需重新
读取 RunStore 中的三阶段进度和最终结果。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from backend.run_store import RunStore


GROUP_STATES = (
    "未开始",
    "已装载",
    "候选正文",
    "机械预检通过",
    "场面事实通过",
    "人物交流通过",
    "隔离观众通过",
    "机械冻结",
    "状态已回写",
)
REFERENCE_LOADER = (
    Path(__file__).resolve().parent
    / "episode_pipeline"
    / "load_reference_bundle.py"
)
REFERENCE_MANIFEST = (
    Path(__file__).resolve().parent
    / "execution"
    / "execution_manifest.json"
)
DIALOGUE_PREPARER = (
    Path(__file__).resolve().parent
    / "episode_pipeline"
    / "prepare_dialogue_review.py"
)
REQUIRED_BACKEND_REFERENCE_PHASES = {
    "upstream",
    "topology",
    "episode-writing",
    "dialogue-polish",
    "episode-quality-review",
}
EPISODE_COMPREHENSION_FIELDS = {
    "character_task": "人物任务",
    "trigger_cost": "触发代价",
    "action_result": "行动结果",
    "next_entry": "下一入口",
}
EPISODE_COMPREHENSION_FAILURE_MARKERS = ("正文不清楚", "无法判断", "无法确认", "信息不足", "未说明")
WRITTEN_TEXT_ACTION_COORDINATE = re.compile(
    r"(?:"
    r"[“\"][^”\"\n]{1,40}[”\"](?:\s*与\s*[“\"][^”\"\n]{1,40}[”\"])?"
    r"[^\n]{0,24}(?:压|按|圈|划|画|指|盖|标|点)"
    r"|"
    r"(?:压|按|圈|划|画|指|盖|标|点)[^\n]{0,24}"
    r"[“\"][^”\"\n]{1,40}[”\"]"
    r")"
)


def visible_count(value: Any) -> int:
    return len(re.sub(r"\s", "", str(value or "")))


def action_count(value: Any) -> int:
    return len(re.findall(r"^\s*△", str(value or ""), flags=re.MULTILINE))


def script_digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").strip().encode("utf-8")).hexdigest()


def dialogue_review_packet(value: Any) -> dict[str, Any]:
    """Number every real dialogue line and return a deterministic coverage receipt."""
    completed = subprocess.run(
        [sys.executable, str(DIALOGUE_PREPARER)],
        input=json.dumps({"script": str(value or "")}, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("人物交流复核包生成失败：" + (completed.stderr or completed.stdout).strip())
    packet = json.loads(completed.stdout)
    if packet.get("script_sha256") != script_digest(value) or packet.get("coverage") != "COMPLETE":
        raise RuntimeError("人物交流复核包覆盖凭证无效")
    return packet


def action_skeleton(value: Any) -> str:
    """Return every non-dialogue line, preserving order and exact text."""
    lines = []
    for line in str(value or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "单集梗概：", "【", "出场：", "△", "互动选项：", "选项")) and re.match(r"^[^：:\n]{1,20}[：:]", stripped):
            continue
        lines.append(line)
    return "\n".join(lines)


def numbered_dialogue_lines(value: Any) -> list[dict[str, Any]]:
    """Return editable dialogue coordinates without exposing action lines to a dialogue-only repair."""
    found: list[dict[str, Any]] = []
    for source_line, line in enumerate(str(value or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "单集梗概：", "【", "出场：", "△", "互动选项：", "选项")):
            continue
        match = re.match(r"^([^：:\n]{1,20})([：:])(.*)$", stripped)
        if not match:
            continue
        found.append({
            "number": len(found) + 1,
            "source_line": source_line,
            "speaker": match.group(1),
            "text": match.group(3),
        })
    return found


def apply_dialogue_replacements(
    value: Any,
    replacements: Any,
    *,
    allow_empty: bool = False,
    skip_invalid: bool = False,
    warnings: list[str] | None = None,
) -> str:
    """Apply content-only dialogue edits while deterministically preserving every non-dialogue byte."""
    notes = warnings if warnings is not None else []
    if not isinstance(replacements, list):
        if allow_empty and skip_invalid:
            notes.append("整批替换不是列表，已保留原台词")
            return str(value or "")
        raise ValueError("对白返修没有返回有效替换项")
    if not replacements:
        if allow_empty:
            return str(value or "")
        raise ValueError("对白返修没有返回有效替换项")
    coordinates = numbered_dialogue_lines(value)
    by_number = {int(item["number"]): item for item in coordinates}
    parsed: dict[int, str] = {}
    for item in replacements:
        if not isinstance(item, dict):
            if skip_invalid:
                notes.append("跳过非对象替换项")
                continue
            raise ValueError("对白替换项格式错误")
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError) as exc:
            if skip_invalid:
                notes.append("跳过缺少有效编号的替换项")
                continue
            raise ValueError("对白替换项缺少有效编号") from exc
        text = str(item.get("text") or "").strip()
        if number not in by_number or not text or "\n" in text or "\r" in text:
            if skip_invalid:
                notes.append(f"跳过第 {number} 项无效替换")
                continue
            raise ValueError(f"对白第 {number} 项替换无效")
        # 常见格式偏差：模型把原说话人也写进 text。原说话人前缀可安全剥离；真正换人则丢弃或拒绝。
        speaker_match = re.match(r"^([^：:\n]{1,20})[：:](.*)$", text)
        if speaker_match:
            proposed_speaker = speaker_match.group(1).strip()
            original_speaker = str(by_number[number]["speaker"]).strip()
            if proposed_speaker == original_speaker:
                text = speaker_match.group(2).strip()
                if not text:
                    if skip_invalid:
                        notes.append(f"跳过第 {number} 项空台词")
                        continue
                    raise ValueError(f"对白第 {number} 项替换无效")
                notes.append(f"第 {number} 项自动剥离重复说话人前缀")
            elif skip_invalid:
                notes.append(f"第 {number} 项试图把{original_speaker}改成{proposed_speaker}，已跳过")
                continue
            else:
                raise ValueError(f"对白第 {number} 项不得改说话人")
        parsed[number] = text
    lines = str(value or "").splitlines()
    for number, text in parsed.items():
        coord = by_number[number]
        index = int(coord["source_line"]) - 1
        original = lines[index]
        indent = original[: len(original) - len(original.lstrip())]
        colon = "：" if "：" in original.strip().split(str(coord["speaker"]), 1)[-1][:1] else ":"
        lines[index] = f"{indent}{coord['speaker']}{colon}{text}"
    revised = "\n".join(lines)
    if action_skeleton(revised) != action_skeleton(value):
        raise ValueError("对白替换触碰了非台词骨架")
    return revised


def numbered_script_lines(value: Any) -> list[dict[str, Any]]:
    """Expose immutable line coordinates for causal/ending micro-patches."""
    return [
        {"line": index, "text": line}
        for index, line in enumerate(str(value or "").splitlines(), 1)
    ]


def apply_script_patches(value: Any, patches: Any) -> str:
    """Apply a small line patch while preserving every non-target source line byte-for-byte."""
    if not isinstance(patches, list) or not patches:
        raise ValueError("因果返修没有返回有效补丁")
    if len(patches) > 6:
        raise ValueError("单轮因果返修最多允许6个局部补丁")
    source = str(value or "").splitlines()
    by_anchor: dict[int, list[dict[str, str]]] = {}
    replaced: set[int] = set()
    for item in patches:
        if not isinstance(item, dict):
            raise ValueError("局部补丁格式错误")
        op = str(item.get("op") or "")
        if op not in {"replace", "insert_before", "insert_after"}:
            raise ValueError(f"不允许的局部补丁操作：{op}")
        try:
            line_no = int(item.get("line"))
        except (TypeError, ValueError) as exc:
            raise ValueError("局部补丁缺少有效行号") from exc
        text = str(item.get("text") or "").strip()
        if line_no < 1 or line_no > len(source) or not text or "\n" in text or "\r" in text:
            raise ValueError(f"第 {line_no} 行局部补丁无效")
        if op == "replace":
            if line_no in replaced:
                raise ValueError(f"第 {line_no} 行重复替换")
            replaced.add(line_no)
        by_anchor.setdefault(line_no, []).append({"op": op, "text": text})
    output: list[str] = []
    for line_no, original in enumerate(source, 1):
        actions = by_anchor.get(line_no, [])
        output.extend(item["text"] for item in actions if item["op"] == "insert_before")
        replacement = next((item["text"] for item in actions if item["op"] == "replace"), original)
        output.append(replacement)
        output.extend(item["text"] for item in actions if item["op"] == "insert_after")
    return "\n".join(output)


def written_text_coordinate_issues(value: Any) -> list[str]:
    """Reject narration that uses unreadable on-screen words as action coordinates."""
    issues = []
    for number, line in enumerate(str(value or "").splitlines(), 1):
        stripped = line.strip()
        if not stripped or re.match(r"^[^：:\n]{1,20}[：:]", stripped):
            continue
        if WRITTEN_TEXT_ACTION_COORDINATE.search(stripped):
            issues.append(
                f"causal｜第{number}行｜画内文字被当作动作坐标；"
                "先用台词说清所指事实，再以行序或刚念完的位置承接动作"
            )
    return issues


def normalize_scene_heading(node: dict[str, Any]) -> bool:
    """Convert a readable Markdown title into the locked screenplay heading."""
    script = str(node.get("script") or "").lstrip()
    if re.search(r"^【场[^】]+】", script):
        return False
    location = str(node.get("loc") or "未标注场景").strip()
    raw_time = str(node.get("time") or "日 内")
    day_night = "夜" if "夜" in raw_time else "日"
    in_out = "外" if "外" in raw_time else "内"
    lines = script.splitlines()
    if lines:
        first = lines[0].strip()
        if (first.startswith("**") and first.endswith("**")) or first.startswith("#") or re.match(r"^场[一二三四五六七八九十0-9]", first):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines.pop(0)
    body = "\n".join(lines)
    prefix = f"【场一 · {location} · {day_night} · {in_out}】"
    if not re.match(r"^出场[：:]", body.strip()):
        prefix += "\n出场：" + (str(node.get("cast") or "").strip() or "未标注人物")
    node["script"] = prefix + "\n\n" + body
    return True


def parse_json_text(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("返回中没有 JSON 对象")
    result = json.loads(text[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("返回 JSON 不是对象")
    return result


def public_error_message(value: Any) -> str:
    """Keep actionable failure diagnostics without exposing provider payloads."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return "后台步骤异常，未返回错误说明"
    if text.startswith("OpenRouter 请求失败："):
        match = re.search(r"OpenRouter 请求失败：(\d+)", text)
        return "OpenRouter 请求失败" + (("（HTTP " + match.group(1) + "）") if match else "")
    if "必需 reference 无法装载" in text:
        return "必需 reference 无法装载"
    return text[:240] + ("…" if len(text) > 240 else "")


def execution_schema_version() -> str:
    # 平台自有数据结构版本（不是 skill 版本，不对外展示）
    try:
        value = str(json.loads(REFERENCE_MANIFEST.read_text(encoding="utf-8")).get("execution_schema_version") or "")
    except (OSError, json.JSONDecodeError):
        value = ""
    return value if re.fullmatch(r"v\d+", value) else "v1"


class ProductionStopped(RuntimeError):
    pass


class ProductionCheckpointReached(RuntimeError):
    pass


class ProductionManager:
    def __init__(self, runs: RunStore, settings_loader: Callable[[], dict[str, Any]]) -> None:
        self.runs = runs
        self.settings_loader = settings_loader
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        self._responses: dict[tuple[str, str], Any] = {}
        self._reference_receipts: dict[tuple[str, str], dict[str, str]] = {}
        self._lock = threading.Lock()

    def stop(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        """Persist cancellation and actively tear down the current HTTP response."""
        self.runs.request_stop(project_id, run_id)
        key = (project_id, run_id)
        with self._lock:
            response = self._responses.get(key)
        if response is not None:
            try:
                raw = getattr(getattr(response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
                if sock is not None:
                    sock.shutdown(2)
            except (AttributeError, OSError):
                pass
            try:
                response.close()
            except OSError:
                pass
        return self.runs.read(project_id, run_id)

    def start(self, project_id: str, run_id: str, title: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = (project_id, run_id)
        with self._lock:
            active = self._threads.get(key)
            if active and active.is_alive():
                return self.runs.read(project_id, run_id) or {}
            existing = self.runs.read(project_id, run_id)
            if existing and existing.get("status") == "completed":
                return existing
            record = self.runs.create(
                project_id,
                run_id,
                title,
                "topology",
                {
                    "kind": "episode-production",
                    "duration": payload.get("duration"),
                    "endings": payload.get("endings"),
                    "model": (payload.get("outline") or {}).get("model", ""),
                },
            )
            self.runs.update(
                project_id,
                run_id,
                kind="production",
                pipeline=self._pipeline(),
                result_data=None,
            )
            worker = threading.Thread(
                target=self._run,
                args=(project_id, run_id, payload),
                name=f"production-{run_id}",
                daemon=True,
            )
            self._threads[key] = worker
            worker.start()
            return self.runs.read(project_id, run_id) or record

    def resume(self, project_id: str, run_id: str) -> dict[str, Any]:
        """Continue a stopped production task from its last persisted checkpoint."""
        key = (project_id, run_id)
        with self._lock:
            active = self._threads.get(key)
            if active and active.is_alive():
                return self.runs.read(project_id, run_id) or {}
            record = self.runs.read(project_id, run_id)
            if not record:
                raise RuntimeError("任务不存在")
            if record.get("kind") != "production":
                raise RuntimeError("这不是可续跑的完整生产任务")
            if record.get("status") == "completed":
                raise RuntimeError("任务已经完成，无需继续")
            checkpoint = record.get("result_data")
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("nodes"), list) or not checkpoint.get("nodes"):
                raise RuntimeError("任务尚未形成可恢复的拓扑断点，只能回到编剧台重新生成")
            self.runs.clear_stop(project_id, run_id)
            self.runs.update(
                project_id,
                run_id,
                status="running",
                error="",
                diagnostic_error="",
                response_text="",
            )
            worker = threading.Thread(
                target=self._resume,
                args=(project_id, run_id, checkpoint),
                name=f"production-resume-{run_id}",
                daemon=True,
            )
            self._threads[key] = worker
            worker.start()
            return self.runs.read(project_id, run_id) or record

    @staticmethod
    def _pipeline() -> dict[str, Any]:
        return {
            "overall": {"label": "正在整理上游资料", "state": "active"},
            "topology": {"pct": 0, "label": "正在生成分集流程图", "state": "active"},
            "scripts": {"pct": 0, "label": "正在组装最终剧本", "state": ""},
            "audit": {"pct": 0, "label": "正在校验人话台词", "state": ""},
        }

    @staticmethod
    def _compact_topology_instruction() -> dict[str, str]:
        return {
            "role": "user",
            "content": (
                "【v0.1.47 长项目压缩】本轮只写清楚各分集的具体事件、选择后果、连接和结局，不得省略分集或边。"
                "一个节点就是一集，使用 episode-NNN；每集 text 用能复述的短梗概表达：人物目标、阻力、实际结果和下一催化。script 留空。"
                "JSON 保持紧凑，但 JSON 前仍必须按平台生成过程协议写清题材判断、情绪脊、岔点取舍、拓扑铺法、结局分配和自检。"
            ),
        }

    @staticmethod
    def _duration_engine_instruction(duration: int) -> dict[str, str]:
        if duration <= 15:
            rule = (
                "当前单线时长属于短片（T≤15）：必须用结局树。装置先行，前15%完成世界/装置/代价暗示；"
                "随后一次兑现加一记反转；设置1–3个会改变结局的抉择，形成2–4个价值落点鲜明的结局。"
                "禁止套长片七拍、四段互动配额或多章情绪弧。"
            )
        elif duration <= 30:
            rule = (
                "当前单线时长属于中片（15<T≤30）：必须用主干＋关键分支＋可见后果＋带差异汇合的过渡逻辑。"
                "故事要有完整起承转合；只在关键行动处设分支，每个选项先进入不同结果集，再携带关系/信息/风险/资源差异汇合。"
                "禁止套短片密集结局树，也禁止强套长片多章和四段满载配额。"
            )
        else:
            rule = (
                "当前单线时长属于长片（30<T≤60）：必须用完整情绪弧。主干走完起承转合与七拍，"
                "分支强收敛但保留状态差异，末段用阶梯式岔点分配主结局；四段互动数量按T/60缩放。"
            )
        return {
            "role": "user",
            "content": (
                f"【单线时长硬分档】T={duration}分钟，T表示从开头到一个结局的一次完整观看路径，不是全分支素材相加。"
                + rule
                + "60分钟是硬上限；不得自行上调，内容超载时裁剪次要支线、功能角色或次要弧光。"
            ),
        }

    @staticmethod
    def _lightweight_topology_instruction() -> dict[str, str]:
        return {
            "role": "user",
            "content": (
                "【平台拓扑输出协议】本轮整理故事主线和分集拓扑：资产沿用上游，每个流程节点就是一集，必须使用 episode-NNN。"
                "先输出【生成过程】，供生产后台实时打印。必须结合本案例写具体决策，不能只写标题或套话："
                "第0步写输入与题材判断（题材、类型融合、视角、核心冲突、时长与结局裁决）；"
                "第1步写情绪脊（主干关键拍、人物控制权变化、观众与角色信息差）；"
                "第2步列候选岔点（每个候选的价值岔向、两边代价、保留或淘汰理由）；"
                "第3步写拓扑铺法（节点如何分集、选择后果如何先分开、在哪里汇合、汇合后保留什么差异、结局如何分配）；"
                "第4步写内容配置（关键节点事件、人物/场景/道具怎么承载冲突和下一催化）；"
                "第5步写自检（节点数、连通、选择出口、结局数、终点、可理解性逐项检查，发现问题先修正并说明）。"
                "生成过程结束后另起一行只写 ===JSON===，随后输出唯一 JSON 对象。"
                "选择写在当前集结尾，每个选项直接指向另一集；选择结果、反馈、汇合和结局节点也必须各自是一集。禁止把多个节点折叠进一集。"
                "只在玩家确实拥有两个以上合理行动时设置选择，不为凑类型或数量增加互动。"
                "不同选项必须先进入不同结果集；汇合时保留尚未解决的关系、信息、风险、资源和承诺差异。"
                "每集 text 必须让普通人看懂谁想做什么、受到什么阻力、发生什么结果、为什么进入下一集。"
                "JSON 中 script 留空；不要输出生产卡和分镜字段。"
            ),
        }

    def _check_stop(self, project_id: str, run_id: str) -> None:
        if self.runs.should_stop(project_id, run_id):
            raise ProductionStopped("用户已停止")

    def _update_pipeline(self, project_id: str, run_id: str, pipeline: dict[str, Any], **changes: Any) -> None:
        overall = pipeline.get("overall") or {}
        phase = changes.pop("phase", None) or overall.get("label") or "production"
        percentages = [int((pipeline.get(k) or {}).get("pct") or 0) for k in ("topology", "scripts", "audit")]
        progress = round(sum(percentages) / 3)
        public_pipeline = json.loads(json.dumps(pipeline, ensure_ascii=False))
        public_pipeline.setdefault("topology", {})["label"] = "正在生成分集流程图"
        result_data = changes.get("result_data") if isinstance(changes.get("result_data"), dict) else {}
        nodes = result_data.get("nodes") if isinstance(result_data.get("nodes"), list) else []
        total = len(nodes)
        written = sum(1 for node in nodes if isinstance(node, dict) and str(node.get("script") or "").strip())
        public_pipeline.setdefault("scripts", {})["label"] = (
            f"第 {min(max(written, 1), total)}/{total} 集正在写作"
            if total
            else "正在组装最终剧本"
        )
        public_pipeline.setdefault("audit", {})["label"] = "正在校验人话台词"
        public_pipeline.setdefault("overall", {})["label"] = {
            "topology": "正在生成分集流程图",
            "scripts": public_pipeline["scripts"]["label"],
            "audit": "正在校验人话台词",
            "completed": "已完成",
        }.get(str(phase), "正在整理上游资料")
        if "error" in changes:
            changes["diagnostic_error"] = public_error_message(changes.pop("error"))
            changes["error"] = ""
        self.runs.update(
            project_id,
            run_id,
            pipeline=public_pipeline,
            phase=phase,
            progress=progress,
            **changes,
        )

    def _log(self, project_id: str, run_id: str, message: str) -> None:
        # 后台专用生成日志：内部推理/装载凭证/分阶段决策写进 task 记录，仅供生产后台排查，不进前端可见 response_text。
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        try:
            self.runs.append_log(project_id, run_id, f"[{stamp}] {message}\n")
        except Exception:  # noqa: BLE001
            pass

    def _log_chunk(self, project_id: str, run_id: str, chunk: str) -> None:
        # 流式生成内容与结构化输出诊断：同样只入后台日志，前端不公开。
        if not chunk:
            return
        try:
            self.runs.append_log(project_id, run_id, chunk)
        except Exception:  # noqa: BLE001
            pass

    def _log_input_decisions(self, project_id: str, run_id: str, payload: dict[str, Any], base_messages: list[dict[str, Any]], duration: int, endings: int, compact_mode: bool) -> None:
        summary = payload.get("input_summary") if isinstance(payload.get("input_summary"), dict) else {}
        tags = [str(item) for item in summary.get("tags") or [] if str(item).strip()]
        profiles = self._reference_profiles({"messages": base_messages, "summary": summary})
        profile_names = {"suspense": "悬疑/惊悚/犯罪", "confrontation": "谈判/审讯/对质", "written-text": "书面信息/文字证据"}
        detected = [profile_names.get(item, item) for item in profiles]
        brief = re.sub(r"\s+", " ", str(summary.get("brief") or payload.get("title") or "")).strip()
        self._log(project_id, run_id, "========== 输入识别与生产裁决 ==========")
        self._log(project_id, run_id, f"题材输入：{'、'.join(tags) if tags else '未手动选择标签'}")
        self._log(project_id, run_id, f"系统检测：{'、'.join(detected) if detected else '通用剧情规则'}；依据为输入中的题材词、事件类型与交流场景")
        self._log(project_id, run_id, f"故事任务：{brief[:240] or '未提供简述'}")
        engine = "短片结局树" if duration <= 15 else "中片过渡结构" if duration <= 30 else "长片情绪弧"
        self._log(project_id, run_id, f"体量裁决：单线 {duration} 分钟 · {endings} 个结局 · {engine} · {'长项目分段协议' if compact_mode else '标准拓扑协议'}")
        self._log(project_id, run_id, f"输入方式：{summary.get('intent') or '直接描述'}；附件：文稿 {len(summary.get('docs') or [])} 份、图片 {len(summary.get('images') or [])} 张")
        recommendations = [item for item in summary.get("ai_recommendations") or [] if isinstance(item, dict)]
        for item in recommendations:
            source = "离线估算" if item.get("offline") else "模型推定"
            self._log(project_id, run_id, f"AI 推荐·{item.get('kind')}：{item.get('value')} {item.get('unit') or ''}（{source}）")
            if item.get("reasoning"):
                self._log(project_id, run_id, "  推定过程：" + re.sub(r"\s+", " ", str(item.get("reasoning"))).strip())
            if item.get("conclusion"):
                self._log(project_id, run_id, "  结论：" + re.sub(r"\s+", " ", str(item.get("conclusion"))).strip())
        self._log(project_id, run_id, "生产链：题材与输入识别 → 分集拓扑 → 逐集写作 → 人话复写 → 独立质量复检 → 锁稿")

    def _log_topology_decisions(self, project_id: str, run_id: str, data: dict[str, Any], duration: int, endings: int) -> None:
        nodes = [item for item in data.get("nodes") or [] if isinstance(item, dict)]
        choices = [item for item in nodes if item.get("kind") in ("choice", "interaction") or len(item.get("next") or []) > 1]
        majors = [item for item in nodes if item.get("kind") == "major"]
        minors = [item for item in nodes if item.get("kind") == "minor"]
        self._log(project_id, run_id, "========== 拓扑判定结果 ==========")
        self._log(project_id, run_id, f"结构规模：{len(nodes)} 集 · {len(choices)} 个互动岔点 · {len(majors)} 个正式结局 · {len(minors)} 个失败/小结局（目标结局数 {endings}）")
        self._log(project_id, run_id, f"正式资产：角色 {len(data.get('characters') or [])} · 场景 {len(data.get('scenes') or [])} · 道具 {len(data.get('props') or [])}")
        if data.get("logline"):
            self._log(project_id, run_id, "一句话定位：" + re.sub(r"\s+", " ", str(data.get("logline")))[:260])
        if choices:
            self._log(project_id, run_id, "互动节点：" + "；".join(f"{n.get('id')}（{n.get('node_title') or n.get('text') or '未命名'} → {len(n.get('next') or [])} 路）" for n in choices))
        ending_items = majors + minors
        if ending_items:
            self._log(project_id, run_id, "结局分配：" + "；".join(f"{n.get('id')}={n.get('node_title') or n.get('text') or n.get('kind')}" for n in ending_items))
        self._log(project_id, run_id, f"硬校验：节点连通、结局总数、选择出口、终点无后继均已通过；按 {duration} 分钟体量进入逐集生产")

    def _log_episode_brief(self, project_id: str, run_id: str, node: dict[str, Any], index: int, total: int, predecessors: dict[str, list[str]]) -> None:
        node_id = str(node.get("id") or index)
        title = node.get("episode_title") or node.get("node_title") or node.get("text") or "未命名"
        next_ids = [str(edge.get("to")) for edge in node.get("next") or [] if isinstance(edge, dict) and edge.get("to")]
        self._log(project_id, run_id, f"========== 第 {index}/{total} 集 · {node_id} · {title} ==========")
        self._log(project_id, run_id, f"本集任务：{node.get('dramatic_core') or (node.get('production_card') or {}).get('dramatic_task') or node.get('text') or '按冻结拓扑完成本集冲突'}")
        self._log(project_id, run_id, f"人物/场景：{node.get('cast') or '按正式角色表'} · {node.get('loc') or '按正式场景表'}")
        self._log(project_id, run_id, f"因果边界：前置 {('、'.join(predecessors.get(node_id, [])) or '起点')} → 后续 {('、'.join(next_ids) or '结局')}；拓扑连接不得改写")

    @staticmethod
    def _reference_profiles(data: Any, node: Any = None) -> tuple[str, ...]:
        text = json.dumps({"data": data, "node": node}, ensure_ascii=False)
        node_text = json.dumps(node, ensure_ascii=False) if node is not None else ""
        profiles = []
        if any(word in text for word in ("悬疑", "惊悚", "犯罪", "反转", "秘密", "证据")):
            profiles.append("suspense")
        if any(word in text for word in ("谈判", "审讯", "威胁", "举证", "对质")):
            profiles.append("confrontation")
        written_text_markers = (
            "信件", "书信", "来信", "信封", "短信", "短讯", "聊天记录", "聊天框",
            "纸条", "字条", "便笺", "日记", "书页", "看书", "读书", "邮件", "电邮",
            "报告", "病历", "档案", "通知", "名单", "清单", "告示", "海报",
            "屏幕文字", "终端文字", "新闻标题", "留言", "备忘录",
        )
        if node_text and any(marker in node_text for marker in written_text_markers):
            profiles.append("written-text")
        return tuple(profiles)

    def _load_reference_context(
        self,
        project_id: str,
        run_id: str,
        phase: str,
        profiles: tuple[str, ...] = (),
    ) -> str:
        command = [
            sys.executable,
            str(REFERENCE_LOADER),
            "--phase",
            phase,
            "--format",
            "json",
        ]
        for profile in profiles:
            command.extend(("--profile", profile))
        process = subprocess.run(command, capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError("必需 reference 无法装载")
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("reference 装载凭证不可解析") from exc
        if payload.get("execution_schema_version") != execution_schema_version() or payload.get("phase") != phase:
            raise RuntimeError("reference 装载版本或阶段不匹配")
        receipt = str(payload.get("receipt_sha256") or "")
        bundle = str(payload.get("bundle") or "")
        if not receipt or not bundle:
            raise RuntimeError("reference 装载结果不完整")
        self._reference_receipts.setdefault((project_id, run_id), {})[phase] = receipt
        return bundle

    def _assert_reference_receipts(self, project_id: str, run_id: str) -> None:
        receipts = self._reference_receipts.get((project_id, run_id), {})
        missing = sorted(REQUIRED_BACKEND_REFERENCE_PHASES - set(receipts))
        if missing:
            raise RuntimeError("必需 reference 装载门禁未完成")

    def _preload_resume_references(self, project_id: str, run_id: str, data: dict[str, Any]) -> None:
        profiles = self._reference_profiles(data)
        for phase in sorted(REQUIRED_BACKEND_REFERENCE_PHASES):
            self._load_reference_context(project_id, run_id, phase, profiles)

    def _chat(self, project_id: str, run_id: str, messages: list[dict[str, Any]], *, validator: bool = False, max_tokens: int = 9000, temperature: float = 0.72, stream_log: bool = False, reference_phase: str | None = None, reference_profiles: tuple[str, ...] = ()) -> str:
        self._check_stop(project_id, run_id)
        settings = self.settings_loader()
        if not settings.get("config_locked") or not settings.get("api_key"):
            raise RuntimeError("生产后台未锁定或 Key 未配置")
        model = (settings.get("validator_model") or settings.get("model")) if validator else settings.get("model")
        effective_messages = list(messages)
        if reference_phase:
            effective_messages.insert(
                0,
                {
                    "role": "system",
                    "content": self._load_reference_context(
                        project_id,
                        run_id,
                        reference_phase,
                        reference_profiles,
                    ),
                },
            )
        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": effective_messages,
            "stream": stream_log,
        }
        request = urllib.request.Request(
            settings["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + settings["api_key"], "Content-Type": "application/json"},
            method="POST",
        )
        response = None
        key = (project_id, run_id)
        try:
            # 拓扑和全集剧本会产生很长的结构化输出；此处是 socket 无数据等待时限，
            # 不是总任务时限。保留 20 分钟窗口，强制停止仍会主动关闭连接。
            response = urllib.request.urlopen(request, timeout=1200)
            with self._lock:
                self._responses[key] = response
            if not stream_log:
                data = json.loads(response.read().decode("utf-8"))
            else:
                result = self._read_streamed_chat(project_id, run_id, response)
                self._check_stop(project_id, run_id)
                return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenRouter 请求失败：{exc.code} {detail}") from exc
        except Exception as exc:
            if self.runs.should_stop(project_id, run_id):
                raise ProductionStopped("用户已强制停止") from exc
            raise
        finally:
            with self._lock:
                self._responses.pop(key, None)
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
        self._check_stop(project_id, run_id)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("模型返回缺少 message.content") from exc

    def _read_streamed_chat(self, project_id: str, run_id: str, response: Any) -> str:
        """读取模型 SSE；完整结果用于解析，只把 JSON 前的生成过程写进可见日志。"""
        output, pending, marker = "", "", "===JSON==="
        log_open = True
        try:
            for raw_line in response:
                self._check_stop(project_id, run_id)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                    delta = event.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                except (json.JSONDecodeError, IndexError, TypeError):
                    continue
                if not delta:
                    continue
                output += delta
                if not log_open:
                    continue
                pending += delta
                if marker in pending:
                    visible, pending = pending.split(marker, 1)
                    self._log_chunk(project_id, run_id, visible.rstrip() + "\n──────────────────────────────\n▸ 逻辑链输出完毕，正在接收结构化 JSON…\n")
                    log_open = False
                    continue
                safe_length = len(pending) - (len(marker) - 1)
                if safe_length >= 320:
                    self._log_chunk(project_id, run_id, pending[:safe_length])
                    pending = pending[safe_length:]
        except TimeoutError:
            # 某些上游在完整内容后迟迟不发 [DONE]。JSON 已完整时直接进入校验，
            # 不因为结束帧缺失把已花费的成果丢掉。
            parse_json_text(output.split(marker, 1)[-1])
        if log_open and pending:
            # 模型没有按约定给标记时仍保留它已输出的过程，便于排错。
            self._log_chunk(project_id, run_id, pending)
        return output

    def _json_chat(self, project_id: str, run_id: str, messages: list[dict[str, Any]], *, validator: bool = False, max_tokens: int = 9000, temperature: float = 0.72, reference_phase: str, reference_profiles: tuple[str, ...] = ()) -> dict[str, Any]:
        current = list(messages)
        round_no = 1
        while True:
            text = self._chat(
                project_id,
                run_id,
                current,
                validator=validator,
                max_tokens=max_tokens,
                temperature=temperature,
                reference_phase=reference_phase,
                reference_profiles=reference_profiles,
            )
            try:
                return parse_json_text(text)
            except (ValueError, json.JSONDecodeError):
                if round_no >= 3:
                    raise RuntimeError("模型连续三次没有返回完整合法 JSON，已停止本步骤以避免重复消耗")
                self._log(project_id, run_id, f"  ↳ 结构化返回无法解析，后台自动重做（第 {round_no} 轮）")
                current = current + [{"role": "user", "content": "上一轮不是可解析 JSON。保持原任务和约束，只重做语法正确、无 Markdown 围栏的 JSON。"}]
                round_no += 1

    @staticmethod
    def _outline_errors(data: dict[str, Any], duration: int, endings: int) -> list[str]:
        errors: list[str] = []
        nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else []
        by_id = {str(n.get("id")): n for n in nodes if isinstance(n, dict) and n.get("id")}
        if not nodes:
            return ["缺少 nodes"]
        short = [str(n.get("id")) for n in nodes if visible_count(n.get("text")) < 30]
        if short:
            errors.append("节点情节内容不足 30 字：" + "、".join(short))
        majors = [n for n in nodes if n.get("kind") == "major"]
        minors = [n for n in nodes if n.get("kind") == "minor"]
        choices = [n for n in nodes if n.get("kind") in ("choice", "interaction")]
        if len(majors) + len(minors) != endings:
            errors.append(f"结局总数应为 {endings}，实际 {len(majors) + len(minors)}")
        for node in choices:
            next_items = node.get("next") if isinstance(node.get("next"), list) else []
            if len(next_items) < 2:
                errors.append(f"{node.get('id')} 没有形成真实选择")
            if any(not str(edge.get("label") or "").strip() for edge in next_items if isinstance(edge, dict)):
                errors.append(f"{node.get('id')} 存在空选项文字")
            targets = [str(edge.get("to")) for edge in next_items if isinstance(edge, dict)]
            if len(targets) != len(set(targets)):
                errors.append(f"{node.get('id')} 存在多选项直接合流")
        broken_edges = [f"{node.get('id')}→{edge.get('to')}" for node in nodes for edge in (node.get("next") or []) if isinstance(edge, dict) and str(edge.get("to")) not in by_id]
        if broken_edges:
            errors.append("流程图存在断边：" + "、".join(broken_edges))
        bad_endings = [str(n.get("id")) for n in majors + minors if n.get("next")]
        if bad_endings:
            errors.append("结局节点仍有后续：" + "、".join(bad_endings))
        seen: set[str] = set()
        queue = [str(nodes[0].get("id"))]
        while queue:
            current = queue.pop(0)
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            for edge in by_id[current].get("next") or []:
                target = str(edge.get("to")) if isinstance(edge, dict) else ""
                if target and target not in seen:
                    queue.append(target)
        orphan = [node_id for node_id in by_id if node_id not in seen and not ((by_id[node_id].get("trigger") or {}).get("type") == "passive")]
        if orphan:
            errors.append("流程图存在孤儿节点：" + "、".join(orphan))
        characters = data.get("characters") if isinstance(data.get("characters"), list) else []
        if not characters:
            errors.append("缺少角色资料")
        if characters and not any(str(item.get("role") or "").strip() == "主角" for item in characters if isinstance(item, dict)):
            errors.append("缺少主角")
        scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
        if not scenes:
            errors.append("缺少场景资料")
        props = data.get("props") if isinstance(data.get("props"), list) else []
        if not props:
            errors.append("缺少道具资料")
        outline = data.get("outline") if isinstance(data.get("outline"), list) else []
        if not outline or any(visible_count(item.get("summary")) < 40 for item in outline if isinstance(item, dict)):
            errors.append("大纲段落不完整")
        return errors

    @staticmethod
    def _normalize_episode_nodes(data: dict[str, Any]) -> None:
        """Make the only topology layer the episode layer, preserving every edge."""
        nodes = [item for item in data.get("nodes") or [] if isinstance(item, dict)]
        mapping = {
            str(node.get("id")): f"episode-{index:03d}"
            for index, node in enumerate(nodes, 1)
        }
        for index, node in enumerate(nodes, 1):
            old_id = str(node.get("id"))
            episode_id = mapping[old_id]
            node["id"] = episode_id
            node["episode_id"] = episode_id
            node["episode_title"] = node.get("episode_title") or node.get("node_title") or f"第{index}集"
            node["node_title"] = node["episode_title"]
            for edge in node.get("next") or []:
                if isinstance(edge, dict) and str(edge.get("to")) in mapping:
                    edge["to"] = mapping[str(edge.get("to"))]
        data["nodes"] = nodes
        data.pop("story_nodes", None)
        data.pop("episode_map", None)

    @staticmethod
    def _episode_map_errors(data: dict[str, Any], episodes: Any) -> list[str]:
        """Legacy v0.1.29 cache reader; never used by the v0.1.31 production path."""
        nodes = [item for item in data.get("nodes") or [] if isinstance(item, dict)]
        node_ids = {str(item.get("id")) for item in nodes}
        if not isinstance(episodes, list) or not episodes:
            return ["分集映射为空"]
        if len(nodes) >= 8 and len(episodes) >= len(nodes):
            return ["分集映射机械地把每个剧情节点当成一集"]
        assigned: list[str] = []
        errors: list[str] = []
        by_id = {str(item.get("id")): item for item in nodes}
        for index, episode in enumerate(episodes, 1):
            if not isinstance(episode, dict):
                errors.append(f"第 {index} 个分集映射不是对象")
                continue
            members = [str(item) for item in episode.get("node_ids") or []]
            if not members:
                errors.append(f"第 {index} 个分集没有剧情节点")
                continue
            assigned.extend(members)
            member_set = set(members)
            adjacent = {item: set() for item in member_set}
            for member in member_set:
                for edge in (by_id.get(member) or {}).get("next") or []:
                    target = str(edge.get("to")) if isinstance(edge, dict) else ""
                    if target in member_set:
                        adjacent[member].add(target)
                        adjacent[target].add(member)
            seen, queue = set(), [members[0]]
            while queue:
                current = queue.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                queue.extend(adjacent.get(current, set()) - seen)
            if seen != member_set:
                errors.append(f"第 {index} 集内部剧情节点不连续")
        missing = sorted(node_ids - set(assigned))
        unknown = sorted(set(assigned) - node_ids)
        duplicate = sorted({item for item in assigned if assigned.count(item) > 1})
        if missing:
            errors.append("未映射剧情节点：" + "、".join(missing))
        if unknown:
            errors.append("映射含未知剧情节点：" + "、".join(unknown))
        if duplicate:
            errors.append("剧情节点被重复映射：" + "、".join(duplicate))
        return errors

    def _assign_episode_map(self, project_id: str, run_id: str, data: dict[str, Any], duration: int, pipeline: dict[str, Any]) -> None:
        """Reject the removed v0.1.29 mapping stage if an old caller invokes it."""
        raise RuntimeError("v0.1.31 不存在节点到分集映射；流程节点本身就是一集")
        # The unreachable body below is retained only to deserialize unfinished
        # v0.1.29 checkpoints. New v0.1.31 runs never call it.
        if isinstance(data.get("story_nodes"), list) and isinstance(data.get("episode_map"), list):
            return
        existing = data.get("episode_map")
        if not self._episode_map_errors(data, existing):
            return
        compact_nodes = [
            {key: node.get(key) for key in ("id", "node_title", "kind", "text", "next")}
            for node in data.get("nodes") or []
            if isinstance(node, dict)
        ]
        for attempt in range(1, 4):
            forge = data.setdefault("__episodeForge", {})
            forge["current_task"] = {"id": "episode-map-write", "role": "author", "status": "running", "attempt": attempt}
            self.runs.update(project_id, run_id, result_data=data)
            mapped = self._json_chat(
                project_id,
                run_id,
                [
                    {"role": "system", "content": "已停用的 v0.1.29 兼容路径"},
                    {"role": "user", "content": "已停用的 v0.1.29 兼容路径"},
                ],
                validator=False,
                max_tokens=6000,
                temperature=0.35,
                reference_phase="topology",
                reference_profiles=self._reference_profiles(data),
            )
            episodes = mapped.get("episodes")
            mechanical = self._episode_map_errors(data, episodes)
            if mechanical:
                continue
            normalized = []
            for index, episode in enumerate(episodes, 1):
                normalized.append({
                    "id": f"episode-{index:03d}",
                    "title": str(episode.get("title") or f"第{index}集"),
                    "main_dramatic_question": str(episode.get("main_dramatic_question") or ""),
                    "node_ids": [str(item) for item in episode.get("node_ids") or []],
                })
            data["episode_map"] = normalized
            for episode in normalized:
                for node_id in episode["node_ids"]:
                    node = next(item for item in data.get("nodes") or [] if str(item.get("id")) == node_id)
                    node["episode_id"] = episode["id"]
                    node["episode_title"] = episode["title"]
            self._collapse_story_nodes_to_episodes(data)
            forge["current_task"] = {"id": "episode-map-write", "role": "author", "status": "complete", "attempt": attempt}
            self.runs.update(project_id, run_id, result_data=data)
            return
        raise RuntimeError("剧情节点到分集的映射连续三次未通过，已停止")

    @staticmethod
    def _collapse_story_nodes_to_episodes(data: dict[str, Any]) -> None:
        """Keep causal nodes private and expose episode containers to production."""
        story_nodes = [item for item in data.get("nodes") or [] if isinstance(item, dict)]
        by_id = {str(item.get("id")): item for item in story_nodes}
        episode_map = [item for item in data.get("episode_map") or [] if isinstance(item, dict)]
        node_to_episode = {str(node_id): str(episode.get("id")) for episode in episode_map for node_id in episode.get("node_ids") or []}
        units = []
        for episode in episode_map:
            episode_id = str(episode.get("id"))
            members = [by_id[str(node_id)] for node_id in episode.get("node_ids") or []]
            member_ids = {str(item.get("id")) for item in members}
            next_edges = []
            for member in members:
                for edge in member.get("next") or []:
                    if not isinstance(edge, dict):
                        continue
                    target_node = str(edge.get("to"))
                    target_episode = node_to_episode.get(target_node)
                    if not target_episode or target_episode == episode_id:
                        continue
                    candidate = {"to": target_episode, "label": edge.get("label") or "继续"}
                    if candidate not in next_edges:
                        next_edges.append(candidate)
            casts = list(dict.fromkeys(name.strip() for item in members for name in re.split(r"[、,，]", str(item.get("cast") or "")) if name.strip()))
            locations = list(dict.fromkeys(str(item.get("loc")) for item in members if item.get("loc")))
            emotion_refs = list(dict.fromkeys(str(ref) for item in members for ref in (item.get("emotion_refs") or item.get("emotional_events") or []) if ref))
            causal_refs = list(dict.fromkeys(str(ref) for item in members for ref in (item.get("causal_refs") or item.get("causal_events") or []) if ref))
            if not next_edges:
                ending_kinds = [str(item.get("kind")) for item in members]
                kind = "major" if "major" in ending_kinds else "minor" if "minor" in ending_kinds else "normal"
            else:
                kind = "choice" if len(next_edges) > 1 else "normal"
            compact_cards = [item.get("production_card") for item in members if isinstance(item.get("production_card"), dict)]
            unit = {
                "id": episode_id,
                "episode_id": episode_id,
                "node_title": episode.get("title") or episode_id,
                "episode_title": episode.get("title") or episode_id,
                "kind": kind,
                "text": "；".join(str(item.get("text") or "") for item in members if item.get("text")),
                "cast": "、".join(casts),
                "loc": "／".join(locations),
                "time": next((item.get("time") for item in members if item.get("time")), ""),
                "next": next_edges,
                "choice_question": next((item.get("choice_question") or item.get("question") for item in members if item.get("choice_question") or item.get("question")), ""),
                "member_node_ids": [str(item.get("id")) for item in members],
                "story_nodes": [{key: item.get(key) for key in ("id", "node_title", "kind", "text", "next", "production_card")} for item in members],
                "production_card": {
                    "episode_goal": episode.get("main_dramatic_question") or "；".join(str(card.get("episode_goal") or "") for card in compact_cards),
                    "entry_state": "；".join(str(card.get("entry_state") or "") for card in compact_cards if card.get("entry_state")),
                    "must_payoff": list(dict.fromkeys(str(value) for card in compact_cards for value in card.get("must_payoff") or [])),
                    "must_seed": list(dict.fromkeys(str(value) for card in compact_cards for value in card.get("must_seed") or [])),
                    "forbidden": list(dict.fromkeys(str(value) for card in compact_cards for value in card.get("forbidden") or [])),
                    "story_node_ids": [str(item.get("id")) for item in members],
                    "emotion_refs": emotion_refs,
                    "causal_refs": causal_refs,
                },
            }
            units.append(unit)
        data["story_nodes"] = story_nodes
        data["nodes"] = units

    @staticmethod
    def _graph(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[dict[str, Any]]]:
        nodes = data.get("nodes") or []
        by_id = {str(node.get("id")): node for node in nodes}
        predecessors = {node_id: [] for node_id in by_id}
        indegree = {node_id: 0 for node_id in by_id}
        for node in nodes:
            for edge in node.get("next") or []:
                target = str(edge.get("to"))
                if target in predecessors:
                    predecessors[target].append(str(node.get("id")))
                    indegree[target] += 1
        queue = [node for node in nodes if indegree[str(node.get("id"))] == 0]
        order: list[dict[str, Any]] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for edge in node.get("next") or []:
                target = str(edge.get("to"))
                if target in indegree:
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        queue.append(by_id[target])
        if len(order) != len(nodes):
            order = list(nodes)
        topology = [
            {key: node.get(key) for key in ("id", "kind", "sub", "text", "next", "production_card")}
            for node in nodes
        ]
        return order, predecessors, topology

    @staticmethod
    def _execution_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Build atomic groups from choice outcomes and convergence inputs."""
        order, predecessors, _ = ProductionManager._graph(data)
        nodes = data.get("nodes") or []
        by_id = {str(node.get("id")): node for node in nodes}
        parent = {node_id: node_id for node_id in by_id}

        def find(node_id: str) -> str:
            while parent[node_id] != node_id:
                parent[node_id] = parent[parent[node_id]]
                node_id = parent[node_id]
            return node_id

        def union(left: str, right: str) -> None:
            if left not in parent or right not in parent:
                return
            a, b = find(left), find(right)
            if a != b:
                parent[b] = a

        for node in nodes:
            node_id = str(node.get("id"))
            next_ids = [str(edge.get("to")) for edge in node.get("next") or [] if isinstance(edge, dict)]
            if node.get("kind") in ("choice", "interaction"):
                for target in next_ids:
                    union(node_id, target)
            if len(predecessors.get(node_id, [])) > 1:
                for source in predecessors[node_id]:
                    union(node_id, source)

        components: dict[str, list[str]] = {}
        for node in order:
            node_id = str(node.get("id"))
            components.setdefault(find(node_id), []).append(node_id)
        rank = {str(node.get("id")): index for index, node in enumerate(order)}
        ordered = sorted(components.values(), key=lambda ids: min(rank[node_id] for node_id in ids))
        groups = []
        for index, node_ids in enumerate(ordered, 1):
            kinds = {str((by_id.get(node_id) or {}).get("kind") or "normal") for node_id in node_ids}
            if "choice" in kinds or "interaction" in kinds:
                group_type = "互动组"
            elif any(len(predecessors.get(node_id, [])) > 1 for node_id in node_ids):
                group_type = "汇合组"
            elif kinds & {"major", "minor"}:
                group_type = "结局组"
            else:
                group_type = "线性组"
            groups.append({"id": f"G{index:03d}", "nodes": node_ids, "type": group_type, "state": "未开始"})
        return groups

    @staticmethod
    def _group_state(group: dict[str, Any], state: str) -> None:
        if state not in GROUP_STATES:
            raise RuntimeError(f"无效执行组状态：{state}")
        group["state"] = state

    @staticmethod
    def _context(data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]]) -> dict[str, Any]:
        by_id = {str(item.get("id")): item for item in data.get("nodes") or []}
        incoming = []
        for node_id in predecessors.get(str(node.get("id")), []):
            prior = by_id.get(node_id) or {}
            incoming.append({"id": node_id, "audience_summary": prior.get("audience_summary") or prior.get("working_summary") or "", "exit_state": prior.get("exit_state") or ""})
        characters = [{key: item.get(key) for key in ("name", "role", "desc", "voice")} for item in data.get("characters") or []]
        return {
            "title": data.get("title") or data.get("logline") or "",
            "logline": data.get("logline") or "",
            "synopsis": data.get("synopsis") or "",
            "characters": characters,
            "scenes": data.get("scenes") or [],
            "props": data.get("props") or [],
            "topology": topology,
            "current": node,
            "predecessors": predecessors.get(str(node.get("id")), []),
            "incoming_memories": incoming,
            "successor_entry_conditions": [
                {"id": str(edge.get("to")), "entry_state": ((by_id.get(str(edge.get("to"))) or {}).get("production_card") or {}).get("entry_state") or ""}
                for edge in node.get("next") or [] if isinstance(edge, dict)
            ],
        }

    def _write_episode(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]]) -> None:
        system = (
            "你是分集逐集编剧。"
            "拓扑已经冻结，只写当前一集，不改分集编号、连接、选项目标、状态效果、结局属性和正式资产名。"
            "用网文的因果清晰度写可拍摄现场：从人物已经面对的具体事情开始，按需要写出触发、判断、欲望、行动、阻力、调整、即时后果和下一压力；"
            "不得跳过移动、接触、失败尝试或代价直接抵达结果。场次使用【场景名称·时段·内/外】，动作按正常段落顺写，对白统一使用人物：台词。"
            "不使用△、出场栏、镜号、景别或运镜，不设硬字数、动作段数或台词比例。只返回JSON。"
        )
        context = self._context(data, node, predecessors, topology)
        prompt = "只读取当前节点、直接前置真实结果、仍有效的状态差异、正式资产和直接后续入口。人物必须围绕眼前事情交流；功能信息通过证据、追问、质疑和反应逐层说清。\n" + json.dumps(context, ensure_ascii=False) + '\n返回：{"script":"完整剧本","entry_state":"本集开始时已经成立的事实","exit_state":"本集结束时实际成立的事实","working_summary":"只写本集实际发生的事"}'
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            reference_phase="episode-writing",
            reference_profiles=self._reference_profiles(data, node),
        )
        node.update(result)
        self._log(project_id, run_id, f"  ↳ {node.get('id')} 初稿完成：{visible_count(node.get('script'))} 可见字 · {action_count(node.get('script'))} 个动作段")

    def _polish_episode_dialogue(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any]) -> None:
        """Polish dialogue by numbered replacements; the model never owns the full script."""
        before = str(node.get("script") or "")
        system = (
            "你是整场人话润色编剧。通读全部编号对白后，只返回确实需要改变的台词。"
            "目标是让人物真正接话，允许打断、省略、反问、口语垫词和必要冗余；去掉说明书对白、过分整齐的短句、书面结论和同声同气。"
            "不得返回完整剧本，不得改变说话人、动作、场景、事实、选择、结局或资产名；不需要修改的台词不要列。只返回JSON。"
        )
        payload = {
            "characters": [{key: item.get(key) for key in ("name", "role", "desc", "voice")} for item in data.get("characters") or []],
            "locked_facts": {
                "entry_state": node.get("entry_state") or "",
                "exit_state": node.get("exit_state") or "",
                "working_summary": node.get("working_summary") or "",
                "facts_lock_digest": node.get("facts_lock_digest") or "",
            },
            "read_only_script_context": before,
            "dialogue_lines": numbered_dialogue_lines(before),
        }
        request_text = json.dumps(payload, ensure_ascii=False) + '\n返回：{"replacements":[{"number":对白编号,"text":"只写冒号后的新台词"}]}'
        result: dict[str, Any] = {}
        warnings: list[str] = []
        try:
            result = self._json_chat(
                project_id,
                run_id,
                [{"role": "system", "content": system}, {"role": "user", "content": request_text}],
                max_tokens=5000,
                temperature=0.35,
                reference_phase="dialogue-polish",
                reference_profiles=self._reference_profiles(data, node),
            )
            revised = apply_dialogue_replacements(
                before,
                result.get("replacements"),
                allow_empty=True,
                skip_invalid=True,
                warnings=warnings,
            )
        except ProductionStopped:
            raise
        except Exception as exc:  # noqa: BLE001
            # 事实层已锁后，语言优化属于可降级步骤；服务限流/格式问题不得毁掉已合格正文。
            revised = before
            warnings.append("人话复写服务失败，保留事实层正文：" + public_error_message(exc))
        node["script"] = revised
        node["dialogue_polished_digest"] = script_digest(revised)
        if warnings:
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 人话复写容错：" + "；".join(warnings))
        changed = sum(1 for item in (result.get("replacements") or []) if isinstance(item, dict)) - sum(1 for note in warnings if "已跳过" in note or note.startswith("跳过"))
        self._log(project_id, run_id, f"  ✓ {node.get('id')} 人话复写完成：平台接受 {max(0, changed)} 行台词替换；无效项跳过，动作与叙述逐字保留")

    def _episode_quality_review(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], receipt_feedback: list[str] | None = None, *, facts_only: bool = False) -> dict[str, Any]:
        """Run one independent, digest-bound second pass over the current episode."""
        digest = script_digest(node.get("script"))
        required_checks = [
            "事实来源与知情",
            "首次出现与必要交代",
            "因果相邻与可见后果",
            "话茬与现场目的",
            "结尾画面与下一入口",
        ] if facts_only else [
            "事实来源与知情",
            "首次出现与必要交代",
            "因果相邻与可见后果",
            "话茬与现场目的",
            "口语组织与反过度压缩",
            "普通话表达与叙述可读",
            "结尾画面与下一入口",
        ]
        context = self._context(data, node, predecessors, topology)
        payload = {
            "episode_id": node.get("id"),
            "script_sha256": digest,
            "script": node.get("script"),
            "incoming_results": [
                {
                    "id": item["id"],
                    "actual_facts": item["audience_summary"],
                }
                for item in context["incoming_memories"]
            ],
            "formal_assets": {
                "characters": [item.get("name") for item in data.get("characters") or []],
                "scenes": [item.get("name") for item in data.get("scenes") or []],
                "props": [item.get("name") for item in data.get("props") or []],
            },
            "required_checks": required_checks,
            "receipt_feedback": list(receipt_feedback or []),
            "review_scope": "只查事实、因果、关系承接与结尾入口；普通话措辞留给最后人话复写" if facts_only else "完整七项复检",
        }
        system = (
            "你是逐集事实锁定复检员。"
            "这是人话复写之前的事实层审查，只查事实来源、知情、因果、关系承接和结尾入口；不评价口语风格。"
            "先只凭已经发生的有限前情和正文完成理解门四项，再逐场通读 required_checks，并绑定给定正文SHA。"
            if facts_only else
            "你是逐集独立复检员。"
            "这是正文与整场人话复写完成后的第二遍审查，不得沿用作者自检，只判不改。"
            "先只凭已经发生的有限前情和正文完成理解门四项，再逐场通读七项，并绑定给定正文SHA。"
        ) + (
            "不得推测作者预期出口或后续剧情；结尾只判断是否逼出具体行动、问题或真实选择。"
            "问题必须可定位且归属 causal、dialogue 或 ending。"
            "普通话检查必须在evidence中分别给dialogue_location/dialogue_proof和narration_location/narration_proof。"
            "只返回JSON：{\"comprehension\":{\"character_task\":{\"answer\":\"普通话复述\",\"proof\":\"正文依据\"},"
            "\"trigger_cost\":{\"answer\":\"普通话复述\",\"proof\":\"正文依据\"},\"action_result\":{\"answer\":\"普通话复述\",\"proof\":\"正文依据\"},"
            "\"next_entry\":{\"answer\":\"普通话复述\",\"proof\":\"正文依据\"}},\"covered_checks\":[\"逐字检查项\"],"
            "\"issues\":[\"归属｜位置｜具体问题\"],\"evidence\":[{\"check\":\"检查项\"}],"
            "\"note\":\"简短结论\"}。没有实际问题时issues必须为空，不得仅因句子短或个人风格判错。"
        )
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            validator=True,
            max_tokens=4200,
            temperature=0.1,
            reference_phase="episode-quality-review",
            reference_profiles=self._reference_profiles(data, node),
        )
        covered = {str(item) for item in result.get("covered_checks") or []}
        script_issues = [str(item) for item in result.get("issues") or []] if isinstance(result.get("issues"), list) else []
        receipt_issues: list[str] = [] if isinstance(result.get("issues"), list) else ["复检结果缺少问题列表"]
        comprehension = result.get("comprehension") if isinstance(result.get("comprehension"), dict) else {}
        for key, label in EPISODE_COMPREHENSION_FIELDS.items():
            item = comprehension.get(key) if isinstance(comprehension.get(key), dict) else {}
            answer = str(item.get("answer") or "").strip()
            proof = str(item.get("proof") or "").strip()
            if len(answer) < 4 or len(proof) < 6:
                receipt_issues.append(f"理解门证据不完整｜当前集｜{label}")
            elif any(marker in answer or marker in proof for marker in EPISODE_COMPREHENSION_FAILURE_MARKERS):
                script_issues.append(f"causal｜当前集｜理解门未通过：{label}")
        missing = [item for item in required_checks if item not in covered]
        if missing:
            receipt_issues.append("复检覆盖不完整｜当前集｜" + "、".join(missing))
        evidence = result.get("evidence") if isinstance(result.get("evidence"), list) else []
        if not facts_only:
            plain = next((item for item in evidence if isinstance(item, dict) and item.get("check") == "普通话表达与叙述可读"), {})
            for field in ("dialogue_location", "dialogue_proof", "narration_location", "narration_proof"):
                if len(str(plain.get(field) or "").strip()) < (2 if field.endswith("location") else 6):
                    receipt_issues.append("普通话复检证据不完整｜当前集｜台词与描述必须分别举证")
                    break
        script_issues.extend(written_text_coordinate_issues(node.get("script")))
        script_issues = list(dict.fromkeys(script_issues))
        receipt_issues = list(dict.fromkeys(receipt_issues))
        return {
            "name": "facts" if facts_only else "quality",
            "pass": not script_issues and not receipt_issues,
            "issues": script_issues,
            "receipt_issues": receipt_issues,
            "covered_checks": sorted(covered),
            "required_checks": required_checks,
            "comprehension": comprehension,
            "note": str(result.get("note") or ""),
            "evidence": evidence,
            "digest": digest,
        }

    def _verify_registered_issues(self, project_id: str, run_id: str, node: dict[str, Any], issues: list[str]) -> list[str]:
        """Verify only previously registered defects; do not discover new defects after a local patch."""
        result = self._json_chat(
            project_id,
            run_id,
            [{
                "role": "system",
                "content": (
                    "你是局部修稿验收员。只核对 registered_issues 是否已由当前正文解决，不得重新扫描或新增问题。"
                    "逐项返回 resolved=true/false；false必须简述仍缺什么。只返回JSON。"
                ),
            }, {
                "role": "user",
                "content": json.dumps({"registered_issues": issues, "script": node.get("script")}, ensure_ascii=False)
                + '\n返回：{"results":[{"issue":"原问题","resolved":true,"note":"依据或仍缺内容"}]}',
            }],
            validator=True,
            max_tokens=2600,
            temperature=0.05,
            reference_phase="episode-quality-review",
            reference_profiles=self._reference_profiles({"node": node}),
        )
        rows = result.get("results") if isinstance(result.get("results"), list) else []
        unresolved = []
        for index, issue in enumerate(issues):
            row = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
            if row.get("resolved") is not True:
                unresolved.append(str(issue) + ("｜仍缺：" + str(row.get("note")) if row.get("note") else ""))
        return unresolved

    def _repair_episode_quality(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], review: dict[str, Any]) -> None:
        """Repair only independently registered episode-quality issues."""
        before = str(node.get("script") or "")
        issues = [str(item) for item in review.get("issues") or []]
        dialogue_only = bool(issues) and all(item.startswith("dialogue｜") for item in issues)
        if dialogue_only:
            dialogue_lines = numbered_dialogue_lines(before)
            system = (
                "你是逐集对白定点返修编剧。只处理登记的对白问题。"
                "平台只允许你返回需要改变的对白内容；不得返回完整剧本，不得改变说话人、动作、场景、事实、选择、结局或资产名。"
                "未列入 replacements 的对白保持原样。只返回JSON。"
            )
            payload = {
                "issues": issues,
                "dialogue_lines": dialogue_lines,
            }
            request_text = json.dumps(payload, ensure_ascii=False) + '\n返回：{"replacements":[{"number":对白编号,"text":"只写冒号后的新台词"}]}'
            result: dict[str, Any] = {}
            revised = ""
            last_error = ""
            for format_attempt in range(2):
                suffix = "" if not last_error else f"\n上一版越权或格式无效：{last_error}。只重做 replacements，不要返回完整剧本。"
                result = self._json_chat(
                    project_id,
                    run_id,
                    [{"role": "system", "content": system}, {"role": "user", "content": request_text + suffix}],
                    max_tokens=3200,
                    temperature=0.25,
                    reference_phase="episode-writing",
                    reference_profiles=self._reference_profiles(data, node),
                )
                try:
                    revised = apply_dialogue_replacements(before, result.get("replacements"))
                    break
                except ValueError as exc:
                    last_error = str(exc)
                    self._log(project_id, run_id, f"  ↳ {node.get('id')} 对白返修第 {format_attempt + 1} 次越权，已丢弃并要求按指定行重做：{last_error}")
            if not revised:
                raise RuntimeError("对白定点返修连续两次格式无效：" + last_error)
            node["script"] = revised
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 对白定点返修：平台原位替换 {len(result.get('replacements') or [])} 行，非台词骨架由程序锁定")
            return

        system = (
            "你是逐集因果与结尾定点返修编剧。主事实、拓扑、人物、场景、选项、结局和状态均已锁定。"
            "只允许针对登记问题修改指定一行，或在最接近的锚点行前后插入一条可见动作/反馈。"
            "有明确行号的问题优先只替换该行；没有行号的问题选择最小锚点，在相邻处补一句。"
            "不得返回完整剧本，不得顺手润色其他行，不新增世界规则或未来事实，不用主题总结替代可见后果。"
            "每个补丁只写单行；单轮最多6个补丁。只返回JSON。"
        )
        payload = {
            "locked_context": self._context(data, node, predecessors, topology),
            "issues": issues,
            "script_lines": numbered_script_lines(before),
        }
        request_text = json.dumps(payload, ensure_ascii=False) + '\n返回：{"patches":[{"op":"replace|insert_before|insert_after","line":原文行号,"text":"单行补丁"}]}'
        result: dict[str, Any] = {}
        revised = ""
        last_error = ""
        for format_attempt in range(2):
            suffix = "" if not last_error else f"\n上一版补丁无效：{last_error}。只重做局部 patches，不要返回完整剧本。"
            result = self._json_chat(
                project_id,
                run_id,
                [{"role": "system", "content": system}, {"role": "user", "content": request_text + suffix}],
                max_tokens=4200,
                temperature=0.25,
                reference_phase="episode-writing",
                reference_profiles=self._reference_profiles(data, node),
            )
            try:
                revised = apply_script_patches(before, result.get("patches"))
                break
            except ValueError as exc:
                last_error = str(exc)
                self._log(project_id, run_id, f"  ↳ {node.get('id')} 因果补丁第 {format_attempt + 1} 次格式无效，已丢弃并重做：{last_error}")
        if not revised:
            raise RuntimeError("因果定点返修连续两次补丁无效：" + last_error)
        node["script"] = revised
        self._log(project_id, run_id, f"  ↳ {node.get('id')} 因果定点返修：平台应用 {len(result.get('patches') or [])} 个行级补丁，其余原文逐字保留")

    def _ensure_episode_quality(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]]) -> None:
        """Lock facts first: scan once, repair registered defects, then verify only that fixed list."""
        current_digest = script_digest(node.get("script"))
        previous = self._audit_map(node).get("facts") or {}
        if previous.get("pass") and previous.get("digest") == current_digest and node.get("facts_lock_digest") == current_digest:
            return
        review: dict[str, Any] = {}
        receipt_feedback: list[str] = []
        for receipt_attempt in range(3):
            review = self._episode_quality_review(
                project_id, run_id, data, node, predecessors, topology, receipt_feedback, facts_only=True
            )
            receipt_feedback = [str(item) for item in review.get("receipt_issues") or []]
            self._set_review(node, review)
            self.runs.update(project_id, run_id, result_data=data)
            if not receipt_feedback:
                break
            self._log(
                project_id, run_id,
                f"  ↳ {node.get('id')} 事实检查回执第 {receipt_attempt + 1} 次不完整，只重做回执、不改正文："
                + "；".join(receipt_feedback),
            )
        if receipt_feedback:
            raise RuntimeError(f"{node.get('id')} 事实检查连续三次未提交完整回执：" + "；".join(receipt_feedback))
        registered = [str(item) for item in review.get("issues") or []]
        if registered:
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 首次事实检查登记 {len(registered)} 个局部问题：" + "；".join(registered))
        unresolved = list(registered)
        for repair_round in range(3):
            if not unresolved:
                break
            patch_review = dict(review)
            patch_review["issues"] = unresolved
            self._repair_episode_quality(project_id, run_id, data, node, predecessors, topology, patch_review)
            self.runs.update(project_id, run_id, result_data=data)
            unresolved = self._verify_registered_issues(project_id, run_id, node, registered)
            if unresolved:
                self._log(project_id, run_id, f"  ↳ {node.get('id')} 局部问题验收仍有 {len(unresolved)} 项未解决（不新增问题）：" + "；".join(unresolved))
        if unresolved:
            raise RuntimeError(f"{node.get('id')} 局部补丁连续三轮仍未解决原登记问题：" + "；".join(unresolved))
        final_digest = script_digest(node.get("script"))
        review.update({"pass": True, "issues": [], "receipt_issues": [], "digest": final_digest, "registered_issues": registered})
        node["facts_lock_digest"] = final_digest
        node["lightweight_status"] = "事实已锁"
        self._set_review(node, review)
        self.runs.update(project_id, run_id, result_data=data)
        self._log(project_id, run_id, f"  ✓ {node.get('id')} 事实层已锁：首次登记 {len(registered)} 项，局部修补后仅验原问题，不再整集新增检查项")

    def _expand_group_cards(self, project_id: str, run_id: str, data: dict[str, Any], nodes: list[dict[str, Any]], predecessors: dict[str, list[str]]) -> None:
        pending = [node for node in nodes if not node.get("production_card_expanded")]
        if not pending:
            return
        by_id = {str(node.get("id")): node for node in data.get("nodes") or []}
        system = (
            "你是生产卡展开器。工作台已独立完成上游题材与资产准备；拓扑、正式输入和资产名已经冻结。"
            "只把当前一个分集容器的紧凑生产卡展开为可执行卡。"
            "不写正文、不改变成员剧情节点、边、选择、结局或资产。"
            "每张卡必须写清本集作用与冲突、前后承接、定性体验功能、开场处境、核心事件、人物第一反应、"
            "行动阻力与可见结果、事件对核心人物的意义、关系与知情变化、场景与物件路径、钩子、真实入口硬状态和后续进入条件。只返回JSON。"
        )
        schema = '\n返回：{"cards":[{"id":"分集id","production_card":{"episode_goal":"本集作用与唯一主事件","entry_state":"全部可靠来路共同成立的入口硬状态","experience_function":"定性体验功能","opening_situation":"开场处境","core_event":"核心事件","character_reaction":"人物第一反应及事件意义","action_resistance_result":"行动、阻力、调整、可见结果","relationship_information_change":"关系、知情、风险与权力变化","scene_prop_path":"场景、物件来源持有使用与结尾状态","next_condition":"后续进入条件","must_payoff":["必须兑现"],"must_seed":["必须埋设"],"forbidden":["禁区"]}}]}'
        global_context = {"logline": data.get("logline"), "synopsis": data.get("synopsis"), "characters": data.get("characters"), "scenes": data.get("scenes"), "props": data.get("props")}
        # v0.1.29: one model call owns one semantic production-card task.
        # The former three-card transport batch made omissions hard to locate
        # and allowed the model to trade detail between unrelated nodes.
        batch_size = 1
        total_batches = (len(pending) + batch_size - 1) // batch_size
        for batch_index, batch in enumerate((pending[index:index + batch_size] for index in range(0, len(pending), batch_size)), 1):
            payload_nodes = []
            for node in batch:
                node_id = str(node.get("id"))
                payload_nodes.append({
                    "id": node_id,
                    "title": node.get("node_title"),
                    "kind": node.get("kind"),
                    "summary": node.get("text"),
                    "cast": node.get("cast"),
                    "scene": node.get("loc"),
                    "predecessors": predecessors.get(node_id, []),
                    "successors": [str(edge.get("to")) for edge in node.get("next") or [] if isinstance(edge, dict)],
                    "incoming_summaries": [(by_id.get(prior) or {}).get("audience_summary") or "" for prior in predecessors.get(node_id, [])],
                    "compact_card": node.get("production_card") or {},
                })
            prompt = json.dumps({"global": global_context, "batch": {"index": batch_index, "total": total_batches}, "nodes": payload_nodes}, ensure_ascii=False) + schema
            result = self._json_chat(
                project_id,
                run_id,
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                max_tokens=9000,
                temperature=0.35,
                reference_phase="production-cards",
                reference_profiles=self._reference_profiles(data, batch),
            )
            cards = result.get("cards") if isinstance(result.get("cards"), list) else []
            returned = {str(item.get("id")): item.get("production_card") for item in cards if isinstance(item, dict) and isinstance(item.get("production_card"), dict)}
            missing = [str(node.get("id")) for node in batch if str(node.get("id")) not in returned]
            if missing:
                raise RuntimeError("执行组生产卡展开缺少节点：" + "、".join(missing))
            for node in batch:
                node["production_card"] = returned[str(node.get("id"))]
                node["production_card_expanded"] = True
            self.runs.update(project_id, run_id, result_data=data)
            self._log(project_id, run_id, f"  ↳ 生产卡展开批次 {batch_index}/{total_batches} 已写入检查点")
        self._log(project_id, run_id, f"  ↳ 已展开当前执行组 {len(pending)} 张完整生产卡")

    @staticmethod
    def _mechanical(node: dict[str, Any], digest: str) -> dict[str, Any]:
        length, actions, issues = visible_count(node.get("script")), action_count(node.get("script")), []
        script = str(node.get("script") or "")
        if length < 850 or length > 1300:
            issues.append(f"可见字数 {length}，要求 850–1300")
        if actions < 16 or actions > 24:
            issues.append(f"△动作段 {actions}，要求 16–24")
        if not re.search(r"【场[^】]+】", script):
            issues.append("缺少正式场次标题")
        if any(label in script for label in ("扩展场面", "收束场面", "补充场面", "返工", "优化后", "润色版")):
            issues.append("正文含修订标签")
        return {"name": "mechanical", "pass": not issues, "issues": issues, "note": "机械预检", "digest": digest, "stats": {"visible_chars": length, "action_paragraphs": actions}}

    def _review(self, project_id: str, run_id: str, kind: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], digest: str) -> dict[str, Any]:
        specs = {
            "causal": "你是场面事实复核员。只查来源、权限、首次出场、地点、知情、事件—感知—第一反应—行动—阻力—调整—结果、物件路径、选择即时结果和后续入口。不得润色对白。",
            "dialogue": "你是人物交流复核员。逐场通读带编号的完整剧本，只报告真正有问题的话轮；不得逐句输出PASS，不得修改动作或补剧情。",
            "cold": "你是与创作历史隔离的普通观众。只能读取路径内观众摘要和当前正文，判断新人物能否识别、事件顺序是否可见、关系是否有正文证据、对白是否围绕眼前人事、结果和下一行动是否清楚。不得读取生产卡、入口硬状态、作者计划或后续答案。",
        }
        required_checks = {
            "causal": ["来源与权限", "首次出场与感知", "行动阻力结果", "物件路径", "后续入口"],
            "dialogue": ["话茬承接", "单句信息负载", "现场目的", "人物声音", "口语自然度", "口语组织", "反过度压缩"],
            "cold": ["人物可识别", "事件顺序", "关系证据", "对白现场性", "结果与下一行动"],
        }[kind]
        system = specs[kind]
        context = self._context(data, node, predecessors, topology)
        if kind == "cold":
            payload = {"prior_audience_summaries": [{"id": item["id"], "audience_summary": item["audience_summary"]} for item in context["incoming_memories"]], "script": node.get("script")}
        elif kind == "dialogue":
            packet = dialogue_review_packet(node.get("script"))
            payload = {
                "episode_id": node.get("id"),
                "relationship_state": node.get("relationship_state") or "",
                "dialogue_review_packet": packet,
            }
        else:
            payload = {"episode_id": node.get("id"), "production_card": node.get("production_card"), "semantic_triggers": node.get("semantic_triggers"), "incoming_memories": context["incoming_memories"], "successor_entry_conditions": context["successor_entry_conditions"], "script": node.get("script"), "entry_state": node.get("entry_state"), "exit_state": node.get("exit_state")}
        if kind == "dialogue":
            system += (
                " 编号由确定性脚本覆盖全部真实台词。必须结合相邻动作和整场关系检查每个编号，重点检查："
                "问句、请求或命令是否得到回答、拒绝或可识别的回避，且对方随后有符合回避的反应；"
                "因果连接是否在当地可懂；是否连续每句都只替作者发布剧情；人物声音是否可区分；"
                "一句是否塞入多个交流任务；台词是否真回应眼前的人事；"
                "口语组织：连续多个普通关系话轮只有最短语义骨架、无复述迟疑态度或关系成分时，标记字幕式对白；"
                "危急口令、无线电协作、程序确认，以及被相邻话轮或动作接住的纯事实短答除外；"
                "反过度压缩：检查称谓、代词、状态副词、语气词和关系指向是否承担对象、承接、态度或关系功能；"
                "当前表达若因省略这些成分而失去对象、状态承接或关系指向，按问题列出；不得仅因句子短而判错。"
                "故意回避只有在对方动作或下一话轮识别并承接时才成立。"
                "只返回JSON：{\"covered_checks\":[\"逐字检查项\"],\"issues\":[\"S01-L02｜问题类型｜简短原因\"],"
                "\"note\":\"简短结论\",\"owner\":\"character_exchange\"}。正常行不列出；没有问题时issues必须为空。"
            )
        else:
            system += ' 必须绑定给定正文SHA，只判不改。每项依据必须给当前正文真实场次和可核对证据。只返回JSON：{"covered_checks":["逐字检查项"],"issues":["场次+问题"],"evidence":[{"scene":"真实场次标题","proof":"正文证据"}],"note":"简短结论","owner":"scene_facts或isolated_audience"}。没有问题时issues为空；不得因个人偏好判失败。冷读失败必须用owner归责。'
        phase = {
            "causal": "scene-review",
            "dialogue": "dialogue-review",
            "cold": "isolated-audience",
        }[kind]
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": "digest=" + digest + "\nrequired_checks=" + json.dumps(required_checks, ensure_ascii=False) + "\n" + json.dumps(payload, ensure_ascii=False)}],
            validator=True,
            max_tokens=3500,
            temperature=0.15,
            reference_phase=phase,
            reference_profiles=self._reference_profiles(data, node),
        )
        raw_issues = [str(item) for item in result.get("issues", [])] if isinstance(result.get("issues"), list) else []
        covered = {str(item) for item in result.get("covered_checks") or []}
        issues = list(raw_issues)
        missing_checks = [item for item in required_checks if item not in covered]
        if missing_checks:
            issues.append("审查覆盖不完整：" + "、".join(missing_checks))
        review = {"name": kind, "pass": not issues, "issues": issues, "covered_checks": sorted(covered), "required_checks": required_checks, "evidence": result.get("evidence") if isinstance(result.get("evidence"), list) else [], "note": result.get("note") or "", "owner": result.get("owner") or "", "digest": digest}
        if kind == "dialogue":
            valid_ids = set(packet["dialogue_ids"])
            if any(not (set(re.findall(r"S\d{2}-L\d{2}", issue)) & valid_ids) for issue in raw_issues):
                raise RuntimeError("人物交流问题项缺少当前台词包中的有效编号")
            review.update({"dialogue_sha256": packet["dialogue_sha256"], "dialogue_count": packet["dialogue_count"], "coverage": packet["coverage"]})
        return review

    def _repair(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], audit: dict[str, Any], layer: str) -> None:
        previous_script = str(node.get("script") or "")
        shared = "不得改拓扑、选项目标、结局属性、正式角色/场景/道具名，也不得提前使用未来事实。只修被定位的当前场次，只返回JSON。"
        if layer == "dialogue":
            system = "你是人物交流层定点返修器。输入剧本中的方括号编号只用于定位，输出时必须全部移除。只能修改问题编号对应的现有说话人台词；场次、出场、动作、选择和全部非台词文字必须逐字不变。不得用新对白补世界规则、证据来源或动作过程。不得默认追求最短表达；允许补回不改变事实的称谓、代词、状态副词、语气和关系指向，承担对象、承接、态度或关系功能的口语成分不得删除。危急口令、无线电协作、程序确认和被相邻话轮或动作接住的纯事实短答，不得为显得口语而强行加口水。" + shared
            schema = '{"script":"只改台词后的完整剧本"}'
        elif layer == "mechanical":
            system = "你是候选正文机械返修器。第一行必须逐字采用【场一 · 地点 · 日/夜 · 内/外】，第二个非空行必须为出场：人物，禁止Markdown粗体、井号或竖线标题。只补足或压缩真实事件的可拍表达来满足字数、动作段和正式格式，不得改变生产卡事实、边界状态或对白含义。" + shared
            schema = '{"script":"机械返修后的完整剧本"}'
        else:
            system = "你是场面事实层定点返修器。通过动作修正来源、权限、出场、事件链、物件路径或入口出口；不得顺手润色无关台词。发现必须新增世界规则、权限或关键证据时返回needs_card=true，不得硬补。" + shared
            schema = '{"script":"返修后完整剧本","exit_state":"候选出口硬状态","needs_card":false}'
        current_script = dialogue_review_packet(previous_script)["numbered_script"] if layer == "dialogue" else previous_script
        prompt = json.dumps({"context": self._context(data, node, predecessors, topology), "current_script": current_script, "issues": audit.get("issues") or []}, ensure_ascii=False) + "\n返回：" + schema
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            reference_phase=("dialogue-review" if layer == "dialogue" else "episode-writing"),
            reference_profiles=self._reference_profiles(data, node),
        )
        if result.get("needs_card"):
            raise RuntimeError("REPAIR_CARD_REQUIRED")
        revised = str(result.get("script") or "")
        if not revised:
            raise RuntimeError("返修结果缺少完整剧本")
        if layer == "dialogue" and action_skeleton(revised) != action_skeleton(previous_script):
            raise RuntimeError("人物交流返修改动了动作骨架")
        node["script"] = revised
        if result.get("exit_state"):
            node["exit_state"] = result["exit_state"]

    def _repair_card(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], audit: dict[str, Any]) -> None:
        system = "你是生产卡诊断员。正文对同一验收层连续两次定点返修仍失败。只修当前节点生产卡中不可执行、互相冲突或缺失的事件要求；不得改变拓扑、选择目标、结局属性和正式输入事实。只返回JSON。"
        payload = {"node_id": node.get("id"), "production_card": node.get("production_card"), "issues": audit.get("issues") or [], "formal_assets": {"characters": data.get("characters"), "scenes": data.get("scenes"), "props": data.get("props")}}
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False) + '\n返回：{"production_card":{...},"note":"修改原因"}'}],
            max_tokens=4500,
            temperature=0.25,
            reference_phase="production-cards",
            reference_profiles=self._reference_profiles(data, node),
        )
        if not isinstance(result.get("production_card"), dict):
            raise RuntimeError("生产卡返修没有返回 production_card")
        node["production_card"] = result["production_card"]
        self._log(project_id, run_id, f"  ↳ {node.get('id')} 连续两次未过，已返回生产卡检查并解冻当前节点")

    def _freeze(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]]) -> None:
        system = "你是状态回写员。当前执行组全部节点已经通过同一正文指纹的机械、场面事实、人物交流和隔离观众复核。只从冻结正文提取实际发生的状态；禁止写作者计划、未来答案、路线评价或主题解释。只返回JSON。"
        prompt = json.dumps({"episode_id": node.get("id"), "predecessors": predecessors.get(str(node.get("id")), []), "entry_state": node.get("entry_state"), "script": node.get("script")}, ensure_ascii=False) + '\n返回：{"audience_summary":"60–100个汉字","exit_state":"人物位置、关系、知情、道具归属、未决行动"}'
        while True:
            result = self._json_chat(
                project_id,
                run_id,
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                reference_phase="state-writeback",
                reference_profiles=self._reference_profiles(data, node),
            )
            if 60 <= visible_count(result.get("audience_summary")) <= 100:
                node["audience_summary"] = result.get("audience_summary") or ""
                if result.get("exit_state"):
                    node["exit_state"] = result["exit_state"]
                return
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 锁稿摘要长度不合规，后台自动重做")

    @staticmethod
    def _audit_map(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
        audit = node.get("episode_audit") if isinstance(node.get("episode_audit"), dict) else {}
        return {str(item.get("name")): item for item in audit.get("reviews") or [] if isinstance(item, dict)}

    def _set_review(self, node: dict[str, Any], review: dict[str, Any], *, locked: bool = False) -> None:
        reviews = self._audit_map(node)
        reviews[str(review.get("name"))] = review
        node["episode_audit"] = {
            "digest": script_digest(node.get("script")),
            "locked": locked,
            "attempts": int((node.get("episode_audit") or {}).get("attempts") or 0),
            "reviews": [reviews[name] for name in ("mechanical", "causal", "dialogue", "cold", "facts", "quality") if name in reviews],
        }

    def _ensure_layer(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], kind: str) -> None:
        label = {"mechanical": "机械预检", "causal": "场面事实", "dialogue": "人物交流", "cold": "隔离观众"}[kind]
        repairs = 0
        while True:
            self._check_stop(project_id, run_id)
            digest = script_digest(node.get("script"))
            previous = self._audit_map(node).get(kind) or {}
            if previous.get("pass") and previous.get("digest") == digest:
                return
            review = self._mechanical(node, digest) if kind == "mechanical" else self._review(project_id, run_id, kind, data, node, predecessors, topology, digest)
            self._set_review(node, review)
            details = "；".join(str(item) for item in review.get("issues") or []) or str(review.get("note") or "通过")
            self._log(project_id, run_id, f"  {'✓' if review['pass'] else '✗'} {node.get('id')} {label}：{details}")
            if review.get("pass"):
                return
            if kind == "mechanical" and "缺少正式场次标题" in (review.get("issues") or []) and normalize_scene_heading(node):
                self._log(project_id, run_id, f"  ↳ {node.get('id')} 已将可读标题确定性转换为正式制片场次标题")
                continue
            repairs += 1
            node["episode_audit"]["attempts"] = int(node["episode_audit"].get("attempts") or 0) + 1
            repair_layer = kind
            if kind == "cold":
                repair_layer = "dialogue" if review.get("owner") == "character_exchange" else "causal"
            if repairs > 2:
                self._repair_card(project_id, run_id, data, node, review)
                self._write_episode(project_id, run_id, data, node, predecessors, topology)
                self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "mechanical")
                if kind in ("dialogue", "cold"):
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "causal")
                if kind == "cold":
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "dialogue")
                repairs = 0
                continue
            self._log(project_id, run_id, f"  ↳ {node.get('id')} {label}未通过，只返修归属场次（第 {repairs} 次）")
            try:
                self._repair(project_id, run_id, data, node, predecessors, topology, review, repair_layer)
            except RuntimeError as exc:
                if str(exc) != "REPAIR_CARD_REQUIRED":
                    raise
                self._repair_card(project_id, run_id, data, node, review)
                self._write_episode(project_id, run_id, data, node, predecessors, topology)
                self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "mechanical")
                if kind in ("dialogue", "cold"):
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "causal")
                if kind == "cold":
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "dialogue")
                repairs = 0
                continue
            # Any body change invalidates old fingerprints. Rebuild only the
            # prerequisite records required by the authoritative matrix.
            mechanical = self._mechanical(node, script_digest(node.get("script")))
            self._set_review(node, mechanical)
            if not mechanical.get("pass"):
                if kind == "mechanical":
                    continue
                self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "mechanical")
                continue
            if kind in ("dialogue", "cold"):
                causal = self._review(project_id, run_id, "causal", data, node, predecessors, topology, script_digest(node.get("script")))
                self._set_review(node, causal)
                if not causal.get("pass"):
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "causal")
                    continue
                if kind == "cold":
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, "dialogue")

    def _process_group(self, project_id: str, run_id: str, data: dict[str, Any], group: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], pipeline: dict[str, Any], index: int, total_groups: int) -> None:
        by_id = {str(node.get("id")): node for node in data.get("nodes") or []}
        nodes = [by_id[node_id] for node_id in group.get("nodes") or []]
        self._group_state(group, "已装载")
        self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)
        self._log(project_id, run_id, f"▸ {group['id']} {group['type']}｜{'、'.join(group['nodes'])} 已装载")
        # v0.1.29 processes one node through the complete write/review/freeze
        # chain before moving to the next. Group membership remains context for
        # branches and merges; it is no longer a batching licence.
        for node in nodes:
            node_id = str(node.get("id"))
            if self._is_locked(node):
                continue
            if data.get("__compactTopologyCards") and not node.get("production_card_expanded"):
                self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:card", "author", "running")
                self._expand_group_cards(project_id, run_id, data, [node], predecessors)
                self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:card", "author", "complete")
            if not str(node.get("script") or "").strip():
                self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:draft", "author", "running")
                self._write_episode(project_id, run_id, data, node, predecessors, topology)
                self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:draft", "author", "complete")
            for state, kind, role in (
                ("机械预检通过", "mechanical", "controller"),
                ("场面事实通过", "causal", "reviewer"),
                ("人物交流通过", "dialogue", "reviewer"),
                ("隔离观众通过", "cold", "reviewer"),
            ):
                task_id = f"{node_id}:{kind}"
                self._atomic_checkpoint(project_id, run_id, data, pipeline, task_id, role, "running")
                try:
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, kind)
                except ProductionStopped:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._atomic_checkpoint(project_id, run_id, data, pipeline, task_id, role, "needs_fix", [public_error_message(exc)])
                    raise RuntimeError(f"{group['id']} · {node_id} · {state}：{public_error_message(exc)}") from exc
                self._atomic_checkpoint(project_id, run_id, data, pipeline, task_id, role, "complete")
            self._ensure_current_reviews(project_id, run_id, data, node, predecessors, topology)
            node["episode_audit"]["locked"] = True
            self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:freeze", "controller", "complete")
            self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:state-writeback", "controller", "running")
            self._freeze(project_id, run_id, data, node, predecessors)
            self._atomic_checkpoint(project_id, run_id, data, pipeline, f"{node_id}:state-writeback", "controller", "complete")
            self._sync_episodes(data, predecessors)
            stop_after = int(data.get("__stop_after_episode") or 0)
            locked_count = sum(1 for item in data.get("nodes") or [] if self._is_locked(item))
            if stop_after and locked_count >= stop_after:
                raise ProductionCheckpointReached(f"已完成并冻结前 {stop_after} 集")
        for state in GROUP_STATES[2:]:
            self._group_state(group, state)
        self._group_state(group, "状态已回写")
        self._sync_episodes(data, predecessors)
        self._log(project_id, run_id, f"  ✓ {group['id']} 全组冻结并完成状态回写")

    def _atomic_checkpoint(self, project_id: str, run_id: str, data: dict[str, Any], pipeline: dict[str, Any], task_id: str, role: str, status: str, issues: list[str] | None = None) -> None:
        """Persist the current atomic task before and after every semantic call."""
        forge = data.setdefault("__episodeForge", {})
        ledger = forge.setdefault("atomic_tasks", [])
        record = next((item for item in ledger if item.get("id") == task_id), None)
        if record is None:
            record = {"id": task_id, "role": role, "attempt": 0}
            ledger.append(record)
        if status == "running" and record.get("status") != "running":
            record["attempt"] = int(record.get("attempt") or 0) + 1
        record.update({"status": status, "issues": list(issues or []), "updated_at": time.time()})
        forge["current_task"] = {"id": task_id, "role": role, "status": status}
        self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)

    def _ensure_current_reviews(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]]) -> None:
        """Reconcile stale layer receipts before freezing a recovered node."""
        kinds = ("mechanical", "causal", "dialogue", "cold")
        for _ in range(8):
            changed = False
            for kind in kinds:
                before = script_digest(node.get("script"))
                self._ensure_layer(project_id, run_id, data, node, predecessors, topology, kind)
                if script_digest(node.get("script")) != before:
                    changed = True
                    break
            if changed:
                continue
            digest = script_digest(node.get("script"))
            reviews = self._audit_map(node)
            if all(reviews.get(name, {}).get("pass") and reviews.get(name, {}).get("digest") == digest for name in kinds):
                return
        raise RuntimeError(f"{node.get('id')} 冻结前复核记录无法稳定绑定当前正文")

    def _whole_play_review(self, project_id: str, run_id: str, data: dict[str, Any], kind: str) -> dict[str, Any]:
        specs = {
            "causal": "只读检查全剧人物知情时间、证据与道具连续性、事件顺序、分支汇合和状态边界。",
            "dialogue": "清除事实审查视角后，只读检查全剧关系触发、人物行动优先级、对白现场性、人物声音和朴素中文。",
        }
        episodes = [{"id": node.get("id"), "script": node.get("script"), "audience_summary": node.get("audience_summary"), "exit_state": node.get("exit_state"), "next": node.get("next")} for node in data.get("nodes") or []]
        required_checks = {
            "causal": ["人物知情时间", "证据与道具连续性", "事件顺序", "分支汇合", "状态边界"],
            "dialogue": ["关系触发", "行动优先级", "对白现场性", "人物声音", "朴素中文"],
        }[kind]
        system = "你是全剧终检员。" + specs[kind] + ' 只判不改，只报告会破坏理解、连续性或人物成立的真实问题。不得声明PASS。返回JSON：{"covered_checks":["逐字检查项"],"issues":["问题"],"affected_ids":["分集id"]}。'
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"required_checks": required_checks, "topology": [{"id": n.get("id"), "next": n.get("next")} for n in data.get("nodes") or []], "episodes": episodes}, ensure_ascii=False)}],
            validator=True,
            max_tokens=6000,
            temperature=0.1,
            reference_phase="whole-play-review",
            reference_profiles=self._reference_profiles(data),
        )
        issues = [str(item) for item in result.get("issues") or []] if isinstance(result.get("issues"), list) else []
        covered = {str(item) for item in result.get("covered_checks") or []}
        missing = [item for item in required_checks if item not in covered]
        if missing:
            issues.append("审查覆盖不完整：" + "、".join(missing))
        return {"name": kind, "pass": not issues, "issues": issues, "covered_checks": sorted(covered), "required_checks": required_checks, "affected_ids": [str(item) for item in result.get("affected_ids") or []]}

    @staticmethod
    def _descendants(data: dict[str, Any], starts: set[str]) -> set[str]:
        by_id = {str(node.get("id")): node for node in data.get("nodes") or []}
        seen, queue = set(starts), list(starts)
        while queue:
            node = by_id.get(queue.pop(0)) or {}
            for edge in node.get("next") or []:
                target = str(edge.get("to")) if isinstance(edge, dict) else ""
                if target and target not in seen:
                    seen.add(target)
                    queue.append(target)
        return seen

    @staticmethod
    def _sync_episodes(data: dict[str, Any], predecessors: dict[str, list[str]]) -> None:
        nodes = [node for node in data.get("nodes") or [] if isinstance(node, dict)]
        by_episode: dict[str, list[dict[str, Any]]] = {
            str(node.get("id")): [node] for node in nodes
        }
        node_to_episode = {str(node.get("id")): episode_id for episode_id, members in by_episode.items() for node in members}
        by_node = {str(node.get("id")): node for node in nodes}
        episodes = []
        for episode_id, members in by_episode.items():
            node = members[0]
            member_ids = {str(item.get("id")) for item in members}
            external_next = []
            for member in members:
                for edge in member.get("next") or []:
                    target = str(edge.get("to")) if isinstance(edge, dict) else ""
                    target_episode = node_to_episode.get(target, target)
                    if target and target_episode != episode_id:
                        external_next.append({"to": target_episode, "label": edge.get("label") or "继续"})
            deduped_next = []
            seen_targets = set()
            for edge in external_next:
                key = (edge["to"], edge["label"])
                if key not in seen_targets:
                    seen_targets.add(key)
                    deduped_next.append(edge)
            script_parts = []
            for member in members:
                incoming_labels = []
                for source_id in predecessors.get(str(member.get("id")), []):
                    if source_id not in member_ids:
                        continue
                    for edge in (by_node.get(source_id) or {}).get("next") or []:
                        if str(edge.get("to")) == str(member.get("id")) and edge.get("label"):
                            incoming_labels.append(str(edge.get("label")))
                route = ("｜路线：" + "／".join(incoming_labels)) if incoming_labels else ""
                heading = f"## {member.get('node_title') or member.get('id')}（剧情节点 {member.get('id')}{route}）" if len(members) > 1 else ""
                script_parts.append((heading + "\n\n" if heading else "") + str(member.get("script") or ""))
            next_items = deduped_next
            next_items = node.get("next") or []
            if len(members) > 1:
                next_items = deduped_next
            interactive = any(item.get("kind") in ("choice", "interaction") for item in members) or len({item.get("to") for item in next_items}) > 1
            node_id = episode_id
            related_props = []
            for item in data.get("props") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                appearances = {part.strip() for part in re.split(r"[、,，]", str(item.get("appears") or "")) if part.strip()}
                if member_ids.intersection(appearances) or str(item.get("name")) in "\n".join(str(member.get("script") or "") for member in members):
                    related_props.append(item.get("name"))
            episodes.append({
                "分集编号": episode_id,
                "分集标题": node.get("episode_title") or node.get("node_title") or episode_id,
                "分集剧本": {"单集梗概": "；".join(str(item.get("text") or "") for item in members if item.get("text")), "完整剧本": "\n\n".join(script_parts)},
                "剧本分析": {"本集冲突": "；".join(str(item.get("dramatic_core") or "") for item in members if item.get("dramatic_core")), "前置节点编号列表": sorted({node_to_episode.get(source, source) for member in members for source in predecessors.get(str(member.get("id")), []) if source not in member_ids}), "后续节点编号列表": [item.get("to") for item in next_items]},
                "关联角色": sorted({name for member in members for name in re.split(r"[、,，]", str(member.get("cast") or "")) if name}),
                "关联场景": list(dict.fromkeys(item.get("loc") for item in members if item.get("loc"))),
                "关联道具": related_props,
                "是否结局": any(item.get("kind") in ("major", "minor") for item in members),
                "互动节点": {"是否为分支节点": interactive, "是否有选择问题": interactive and bool(next_items), "选择问题": node.get("choice_question") or node.get("question") or "", "选项列表": [{"选项编号": str(index + 1), "选项文字": item.get("label") or "继续", "目标分集编号": item.get("to")} for index, item in enumerate(next_items)], "默认下一分集编号": "" if interactive else ((next_items[0].get("to") if next_items else "") or "")},
            })
        data["episodes"] = episodes

    @staticmethod
    def _is_locked(node: dict[str, Any]) -> bool:
        digest = script_digest(node.get("script"))
        if node.get("lightweight_status") == "已锁稿" and node.get("final_lock_digest") == digest:
            return True
        # 兼容升级前的可靠检查点：仅当旧质量凭证与当前正文完全同摘要时承认锁稿。
        audit = node.get("episode_audit") if isinstance(node.get("episode_audit"), dict) else {}
        quality = next((item for item in audit.get("reviews") or [] if isinstance(item, dict) and item.get("name") == "quality"), {})
        return node.get("lightweight_status") == "已复检" and quality.get("pass") is True and quality.get("digest") == digest

    def _finish_from_checkpoint(self, project_id: str, run_id: str, data: dict[str, Any], pipeline: dict[str, Any]) -> None:
        """Run the v0.1.47 write → dialogue polish → independent episode review pipeline."""
        order, predecessors, topology = self._graph(data)
        total = len(order)
        forge = data.get("__episodeForge") if isinstance(data.get("__episodeForge"), dict) else {}
        forge.update({
            "version": execution_schema_version() + "-platform",
            "cache_version": "episode-cache-" + execution_schema_version(),
            "status": "running",
            "total": total,
            "pipeline": "episode-writing → dialogue-polish → episode-quality-review",
            "generated": sum(1 for node in order if str(node.get("script") or "").strip()),
            "reviewed": sum(1 for node in order if self._is_locked(node)),
            "locked": sum(1 for node in order if self._is_locked(node)),
        })
        data["__episodeForge"] = forge
        locked_at_start = [node for node in order if self._is_locked(node)]
        first_pending = next((node for node in order if not self._is_locked(node)), None)
        if locked_at_start and first_pending:
            self._log(project_id, run_id, f"▸ 恢复点：已锁稿 {len(locked_at_start)}/{total} 集，直接从 {first_pending.get('id')} 继续；已锁稿集不重复打印、不重复调用")

        pipeline["topology"] = {"pct": 100, "label": f"分集梗概与流程已完成 · {total} 集", "state": "pass"}
        pipeline["overall"] = {"label": "逐集写作", "state": "active"}
        forge["phase"] = "episode-writing"
        for index, node in enumerate(order, 1):
            self._check_stop(project_id, run_id)
            if self._is_locked(node):
                continue
            node_id = str(node.get("id"))
            forge["current_episode"] = node_id
            self._log_episode_brief(project_id, run_id, node, index, total, predecessors)
            if not str(node.get("script") or "").strip():
                node["lightweight_status"] = "待写作"
                self._write_episode(project_id, run_id, data, node, predecessors, topology)
                node["lightweight_status"] = "已写作"
                self.runs.update(project_id, run_id, result_data=data)
            elif node.get("lightweight_status") not in ("已写作", "已口语化", "已复检"):
                node["lightweight_status"] = "已写作"
            forge["generated"] = sum(1 for item in order if str(item.get("script") or "").strip())
            pipeline["scripts"] = {"pct": round(index / max(total, 1) * 100), "label": f"第 {index}/{total} 集已完成", "state": "active" if index < total else "pass"}
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)

        forge["phase"] = "facts-then-dialogue"
        pipeline["overall"] = {"label": "正在锁定事实并做人话复写", "state": "active"}
        pipeline["audit"] = {"pct": 0, "label": "准备逐集事实检查", "state": "active"}
        self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)
        for index, node in enumerate(order, 1):
            self._check_stop(project_id, run_id)
            if self._is_locked(node):
                continue
            # 正确顺序：先锁事实/因果/关系/入口；局部问题只验原登记项。
            self._ensure_episode_quality(project_id, run_id, data, node, predecessors, topology)
            facts_digest = script_digest(node.get("script"))
            if node.get("facts_lock_digest") != facts_digest:
                raise RuntimeError(f"{node.get('id')} 事实锁摘要与当前正文不一致")
            # 最后一次人话复写只改编号台词；程序保证动作、叙述和说话人不变。此后不再模型复检。
            if node.get("dialogue_polished_digest") != facts_digest:
                self._polish_episode_dialogue(project_id, run_id, data, node)
            final_digest = script_digest(node.get("script"))
            node["dialogue_polished_digest"] = final_digest
            node["final_lock_digest"] = final_digest
            node["lightweight_status"] = "已锁稿"
            audit = node.setdefault("episode_audit", {})
            audit["locked"] = True
            audit["facts_lock_digest"] = facts_digest
            audit["final_lock_digest"] = final_digest
            self.runs.update(project_id, run_id, result_data=data)
            self._log(project_id, run_id, f"  ✓ {node.get('id')} 已锁稿：事实层已锁；最后人话复写仅替换台词；正文 {visible_count(node.get('script'))} 可见字")
            forge["reviewed"] = index
            forge["locked"] = sum(1 for item in order if self._is_locked(item))
            pipeline["audit"] = {
                "pct": round(index / max(total, 1) * 100),
                "label": f"第 {index}/{total} 集已完成人话复写与独立复检",
                "state": "active" if index < total else "pass",
            }
            self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)

            stop_after = int(data.get("__stop_after_episode") or 0)
            if stop_after and forge["locked"] >= stop_after:
                raise ProductionCheckpointReached(f"已完成前 {stop_after} 集")

        forge.update({"status": "completed", "phase": "completed"})
        data["__forgeComplete"] = True
        self._sync_episodes(data, predecessors)
        self._assert_reference_receipts(project_id, run_id)
        pipeline["audit"] = {"pct": 100, "label": "逐集独立复检已全部通过", "state": "pass"}
        pipeline["overall"] = {"label": "已交付", "state": "pass"}
        self._log(project_id, run_id, "========== 最终交付 ==========")
        self._log(project_id, run_id, f"✓ 全部 {total} 集完成并锁稿：拓扑、逐集正文、人话复写、独立复检、状态回写均已完成。")
        self._update_pipeline(project_id, run_id, pipeline, phase="completed", status="completed", result_data=data)

    def _resume(self, project_id: str, run_id: str, data: dict[str, Any]) -> None:
        record = self.runs.read(project_id, run_id) or {}
        pipeline = record.get("pipeline") if isinstance(record.get("pipeline"), dict) else self._pipeline()
        try:
            self._preload_resume_references(project_id, run_id, data)
            self._log(project_id, run_id, "▸ 已从最近断点继续，已完成内容不会重复生成。")
            self._finish_from_checkpoint(project_id, run_id, data, pipeline)
        except ProductionStopped:
            pipeline["overall"] = {"label": "已停止", "state": "fail"}
            self._update_pipeline(project_id, run_id, pipeline, phase="stopped", status="stopped")
        except Exception as exc:  # noqa: BLE001
            pipeline["overall"] = {"label": "生产中断", "state": "fail"}
            self._log(project_id, run_id, f"✗ 后台生产中断：{exc}")
            self._update_pipeline(project_id, run_id, pipeline, phase="failed", status="failed", error=str(exc))
        finally:
            with self._lock:
                self._threads.pop((project_id, run_id), None)
            self._reference_receipts.pop((project_id, run_id), None)

    def _run(self, project_id: str, run_id: str, payload: dict[str, Any]) -> None:
        pipeline = self._pipeline()
        try:
            outline = payload.get("outline") if isinstance(payload.get("outline"), dict) else {}
            base_messages = outline.get("messages") if isinstance(outline.get("messages"), list) else []
            if not base_messages:
                raise RuntimeError("缺少大纲生成 messages")
            self._load_reference_context(
                project_id,
                run_id,
                "upstream",
                self._reference_profiles(base_messages),
            )
            duration, endings = int(payload.get("duration") or 20), int(payload.get("endings") or 3)
            if duration < 1 or duration > 60:
                raise RuntimeError("单次完整观看路径时长必须在 1–60 分钟内；超过 60 分钟请先裁剪支线、角色或次要弧光")
            compact_mode = duration > 30 or endings >= 6
            self._log_input_decisions(project_id, run_id, payload, base_messages, duration, endings, compact_mode)
            topology_base = list(base_messages) + [self._duration_engine_instruction(duration), self._lightweight_topology_instruction()] + ([self._compact_topology_instruction()] if compact_mode else [])
            round_no, json_failures, messages = 1, 0, list(topology_base)
            while True:
                self._check_stop(project_id, run_id)
                pipeline["overall"] = {"label": "分集梗概与流程", "state": "active"}
                pipeline["topology"] = {"pct": min(90, 15 + round_no * 10), "label": f"第 {round_no} 轮结构检查", "state": "active"}
                self._update_pipeline(project_id, run_id, pipeline, phase="topology")
                self._log(project_id, run_id, f"▸ 后台拓扑生成与校验｜第 {round_no} 轮")
                text = self._chat(
                    project_id,
                    run_id,
                    messages,
                    max_tokens=int(outline.get("max_tokens") or 20000),
                    temperature=float(outline.get("temperature") or 0.8),
                    stream_log=True,
                    reference_phase="topology",
                    reference_profiles=self._reference_profiles(base_messages),
                )
                json_text = text.split("===JSON===", 1)[-1]
                try:
                    data = parse_json_text(json_text)
                    self._normalize_episode_nodes(data)
                except (ValueError, json.JSONDecodeError) as exc:
                    json_failures += 1
                    errors = [f"JSON 无法解析：{exc}"]
                    if len(json_text) >= 18000 and not compact_mode:
                        compact_mode = True
                        json_failures = 0
                        topology_base = list(base_messages) + [self._duration_engine_instruction(duration), self._lightweight_topology_instruction(), self._compact_topology_instruction()]
                        self._log(project_id, run_id, f"  ↳ 检测到结构化输出在 {len(json_text)} 字符附近截断，切换长项目分段协议；这不是剧情返工")
                    elif compact_mode and len(json_text) >= 18000 and json_failures >= 2:
                        raise RuntimeError("长项目分段协议下 JSON 仍连续两次被截断，已停止以避免重复消耗") from exc
                    elif json_failures >= 3:
                        raise RuntimeError("拓扑 JSON 连续三次无法解析，已停止以避免重复消耗") from exc
                else:
                    json_failures = 0
                    errors = self._outline_errors(data, duration, endings)
                if not errors:
                    self._log(project_id, run_id, f"  ✓ 第 {round_no} 轮拓扑硬校验通过")
                    self._log_topology_decisions(project_id, run_id, data, duration, endings)
                    self._log(project_id, run_id, "========== 已验证拓扑 JSON ==========")
                    self._log_chunk(project_id, run_id, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                    break
                self._log(project_id, run_id, "  ↳ 未通过：" + "；".join(errors))
                messages = list(topology_base) + [{"role": "user", "content": "上一版未通过硬规则，必须完整重做并修正：\n- " + "\n- ".join(errors)}]
                round_no += 1

            data["__compactTopologyCards"] = compact_mode
            data["__planning"] = {"story_spine": data.get("outline") or []}
            data["__stop_after_episode"] = max(0, int(payload.get("stop_after_episode") or 0))
            self._finish_from_checkpoint(project_id, run_id, data, pipeline)
        except ProductionCheckpointReached as exc:
            pipeline["overall"] = {"label": "已完成指定分集", "state": "pass"}
            record = self.runs.read(project_id, run_id) or {}
            checkpoint = record.get("result_data") if isinstance(record.get("result_data"), dict) else None
            self._update_pipeline(project_id, run_id, pipeline, phase="episode-checkpoint", status="stopped", result_data=checkpoint, checkpoint_note=str(exc))
        except ProductionStopped:
            pipeline["overall"] = {"label": "已停止", "state": "fail"}
            self._update_pipeline(project_id, run_id, pipeline, phase="stopped", status="stopped")
        except Exception as exc:  # noqa: BLE001
            pipeline["overall"] = {"label": "生产中断", "state": "fail"}
            self._log(project_id, run_id, f"✗ 后台生产中断：{exc}")
            self._update_pipeline(project_id, run_id, pipeline, phase="failed", status="failed", error=str(exc))
        finally:
            with self._lock:
                self._threads.pop((project_id, run_id), None)
            self._reference_receipts.pop((project_id, run_id), None)
