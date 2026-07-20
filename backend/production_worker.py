#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大纲编剧台的后台生产工人。

任务由本地后端线程持续执行，与浏览器连接解耦；页面刷新后只需重新
读取 RunStore 中的三阶段进度和最终结果。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
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
    Path(__file__).resolve().parents[1]
    / "skill"
    / "episode-generator"
    / "scripts"
    / "load_reference_bundle.py"
)
REFERENCE_MANIFEST = REFERENCE_LOADER.parents[1] / "reference-manifest.json"
DIALOGUE_PREPARER = (
    Path(__file__).resolve().parents[1]
    / "skill"
    / "episode-generator"
    / "scripts"
    / "prepare_dialogue_review.py"
)
REQUIRED_BACKEND_REFERENCE_PHASES = {
    "upstream",
    "topology",
    "production-cards",
    "episode-writing",
    "scene-review",
    "dialogue-review",
    "isolated-audience",
    "state-writeback",
    "whole-play-review",
}


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


def current_episode_skill_version() -> str:
    try:
        value = str(json.loads(REFERENCE_MANIFEST.read_text(encoding="utf-8")).get("skill_version") or "")
    except (OSError, json.JSONDecodeError):
        value = ""
    return value if re.fullmatch(r"v\d+\.\d+\.\d+", value) else "v0.1.25"


class ProductionStopped(RuntimeError):
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
            "audit": {"pct": 0, "label": "正在检查人物与道具连续性", "state": ""},
        }

    @staticmethod
    def _compact_topology_instruction() -> dict[str, str]:
        return {
            "role": "user",
            "content": (
                "【后台长项目分段协议·覆盖本轮输出长度要求】本轮只冻结全局资产、完整拓扑和紧凑生产卡，"
                "不得省略任何节点或边，但必须压缩每个 production_card：episode_goal 与 entry_state 各不超过80个汉字；"
                "must_payoff最多2项、must_seed最多1项、forbidden最多1项，每项不超过30个汉字。"
                "script仍为空。生成日志每步最多2行，避免挤占结构化输出容量。完整生产卡会在拓扑通过后按原子执行组另行展开，"
                "不得为了写长卡截断JSON。"
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
        public_pipeline.setdefault("audit", {})["label"] = "正在检查人物与道具连续性"
        public_pipeline.setdefault("overall", {})["label"] = {
            "topology": "正在生成分集流程图",
            "scripts": public_pipeline["scripts"]["label"],
            "audit": "正在检查人物与道具连续性",
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
        # Internal production details never enter the user-visible response log.
        return

    def _log_chunk(self, project_id: str, run_id: str, chunk: str) -> None:
        # Streamed reasoning and structured-output diagnostics remain private.
        return

    @staticmethod
    def _reference_profiles(data: Any, node: Any = None) -> tuple[str, ...]:
        text = json.dumps({"data": data, "node": node}, ensure_ascii=False)
        profiles = []
        if any(word in text for word in ("悬疑", "惊悚", "犯罪", "反转", "秘密", "证据")):
            profiles.append("suspense")
        if any(word in text for word in ("谈判", "审讯", "威胁", "举证", "对质")):
            profiles.append("confrontation")
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
        if payload.get("skill_version") != current_episode_skill_version() or payload.get("phase") != phase:
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
        if duration <= 15:
            low, high = (1, 2) if duration <= 10 else (2, 3)
            if not low <= len(choices) <= high:
                errors.append(f"短片抉择数应为 {low}–{high}，实际 {len(choices)}")
        elif len(choices) < 2:
            errors.append("中长片抉择节点少于 2")
        for node in choices:
            next_items = node.get("next") if isinstance(node.get("next"), list) else []
            if not 2 <= len(next_items) <= 4:
                errors.append(f"{node.get('id')} 的出口数不在 2–4")
            if any(not str(edge.get("label") or "").strip() for edge in next_items if isinstance(edge, dict)):
                errors.append(f"{node.get('id')} 存在空选项文字")
            targets = [str(edge.get("to")) for edge in next_items if isinstance(edge, dict)]
            if len(targets) != len(set(targets)):
                errors.append(f"{node.get('id')} 存在多选项直接合流")
            if next_items and all((by_id.get(str(edge.get("to"))) or {}).get("kind") == "minor" for edge in next_items if isinstance(edge, dict)):
                errors.append(f"{node.get('id')} 的选项全是死路")
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
        beats = data.get("beats") if isinstance(data.get("beats"), list) else []
        if len(beats) < 5:
            errors.append("情绪脊 beats 少于 5 拍")
        if beats and not any(beat.get("D") is not None for beat in beats if isinstance(beat, dict)):
            errors.append("情绪脊缺少 D 轴")
        minimum_d, flipped = 1.0, False
        for beat in beats:
            if not isinstance(beat, dict) or beat.get("D") is None:
                continue
            try:
                value = float(beat.get("D"))
            except (TypeError, ValueError):
                errors.append("情绪脊 D 轴存在非数值")
                continue
            if minimum_d <= -0.3 and value >= 0.3:
                flipped = True
            minimum_d = min(minimum_d, value)
        if beats and not flipped:
            errors.append("情绪脊缺少 D 轴从被压到掌控的翻盘拍")
        characters = data.get("characters") if isinstance(data.get("characters"), list) else []
        if not characters or any(not isinstance(item.get("states"), list) or not item.get("states") for item in characters if isinstance(item, dict)):
            errors.append("人物全状态卡不完整")
        valid_roles = {"主角", "男主", "女主", "主配", "配角", "NPC"}
        if any(str(item.get("role") or "").strip() not in valid_roles for item in characters if isinstance(item, dict)):
            errors.append("角色分类不在固定标签内")
        if characters and not any(str(item.get("role") or "").strip() == "主角" for item in characters if isinstance(item, dict)):
            errors.append("缺少主角")
        choice_ids = {str(node.get("id")) for node in choices}
        payoffs = data.get("payoffs") if isinstance(data.get("payoffs"), list) else []
        if not payoffs or any(str(item.get("enabledBy")) not in choice_ids or not str(item.get("irony") or "").strip() for item in payoffs if isinstance(item, dict)):
            errors.append("爽点 payoffs 未完整登记翻盘、抉择促成与反讽")
        scenes = data.get("scenes") if isinstance(data.get("scenes"), list) else []
        if not scenes or any(not re.search(r"日|夜", str(item.get("time") or "")) for item in scenes if isinstance(item, dict)):
            errors.append("场景表缺失或未标日/夜")
        props = data.get("props") if isinstance(data.get("props"), list) else []
        core_props = [item for item in props if isinstance(item, dict) and item.get("level") == "core"]
        if not props or not core_props or any(not str(item.get("look") or "").strip() or not str(item.get("func") or "").strip() for item in core_props):
            errors.append("道具表缺失或核心道具没有外观与功能")
        outline = data.get("outline") if isinstance(data.get("outline"), list) else []
        if not outline or any(visible_count(item.get("summary")) < 40 for item in outline if isinstance(item, dict)):
            errors.append("大纲段落不完整")
        no_card = [str(n.get("id")) for n in nodes if not isinstance(n.get("production_card"), dict) or not n["production_card"].get("episode_goal")]
        if no_card:
            errors.append("缺少逐集制作卡：" + "、".join(no_card))
        return errors

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
        system = f"你是 episode-generator {current_episode_skill_version()} 的候选正文生成器。工作台已经独立完成上游题材与资产准备，本步骤直接从冻结拓扑和正式资产开始。只能兑现当前节点，不得改变编号、连接、选项目标、结局属性或正式资产名。先写连续戏，再抽取真实台词逐句润色并原位写回。正文第一行必须逐字采用制片标题格式【场一 · 地点 · 日/夜 · 内/外】，第二个非空行必须为出场：人物；禁止用Markdown粗体、井号标题或竖线标题代替。正文850–1300个可见非空白字符，16–24个以△开头的可拍动作段；只推进一个主事件，形成触发→感知→第一反应→行动→阻力→调整→可见结果→下一步。只返回JSON。"
        prompt = "只读取给定的压缩事实、当前生产卡、可靠前置真实结尾/观众摘要和直接后续入口。登记语义触发；未触发项写无。production_card 的 must_payoff 必须兑现，must_seed 必须埋下，forbidden 不得触碰。\n" + json.dumps(self._context(data, node, predecessors, topology), ensure_ascii=False) + '\n返回：{"script":"普通Markdown完整剧本","entry_state":"本集可靠入口硬状态","exit_state":"候选出口硬状态","working_summary":"60–100字临时连续性摘要","semantic_triggers":{"首次出现人物":"内容或无","核心关系触发":"内容或无","关键事件":"内容或无","关键干预":"内容或无","互动选择":"内容或无"},"dramatic_core":"唯一戏剧变化","power_shift":"权力变化","information_gap":"知情差","dialogue_intent":"对抗/交代/关系"}'
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            reference_phase="episode-writing",
            reference_profiles=self._reference_profiles(data, node),
        )
        node.update(result)
        self._log(project_id, run_id, f"  ↳ {node.get('id')} 初稿完成：{visible_count(node.get('script'))} 可见字 · {action_count(node.get('script'))} 个动作段")

    def _expand_group_cards(self, project_id: str, run_id: str, data: dict[str, Any], nodes: list[dict[str, Any]], predecessors: dict[str, list[str]]) -> None:
        pending = [node for node in nodes if not node.get("production_card_expanded")]
        if not pending:
            return
        by_id = {str(node.get("id")): node for node in data.get("nodes") or []}
        system = (
            f"你是 episode-generator {current_episode_skill_version()} 生产卡展开器。工作台已独立完成上游题材与资产准备；拓扑、正式输入和资产名已经冻结。"
            "只把当前原子执行组中本批给出的紧凑生产卡展开为可执行卡；批次只是传输切片，不能改变原子执行组边界。"
            "不写正文、不改变节点、边、选择、结局或资产。"
            "每张卡必须写清本集作用与冲突、前后承接、定性体验功能、开场处境、核心事件、人物第一反应、"
            "行动阻力与可见结果、事件对核心人物的意义、关系与知情变化、场景与物件路径、钩子、真实入口硬状态和后续进入条件。只返回JSON。"
        )
        schema = '\n返回：{"cards":[{"id":"节点id","production_card":{"episode_goal":"本集作用与唯一主事件","entry_state":"全部可靠来路共同成立的入口硬状态","experience_function":"定性体验功能","opening_situation":"开场处境","core_event":"核心事件","character_reaction":"人物第一反应及事件意义","action_resistance_result":"行动、阻力、调整、可见结果","relationship_information_change":"关系、知情、风险与权力变化","scene_prop_path":"场景、物件来源持有使用与结尾状态","next_condition":"后续进入条件","must_payoff":["必须兑现"],"must_seed":["必须埋设"],"forbidden":["禁区"]}}]}'
        global_context = {"logline": data.get("logline"), "synopsis": data.get("synopsis"), "characters": data.get("characters"), "scenes": data.get("scenes"), "props": data.get("props")}
        batch_size = 3
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
        if length < 850 or length > 1300:
            issues.append(f"可见字数 {length}，要求 850–1300")
        if actions < 16 or actions > 24:
            issues.append(f"△动作段 {actions}，要求 16–24")
        script = str(node.get("script") or "")
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
                "一句是否塞入多个交流任务；台词是否真回应眼前的人事。故意回避只有在对方动作或下一话轮识别并承接时才成立。"
                "只返回JSON：{\"pass\":true或false,\"issues\":[\"S01-L02｜问题类型｜简短原因\"],"
                "\"note\":\"简短结论\",\"owner\":\"character_exchange\"}。正常行不列出；没有问题时issues必须为空。"
            )
        else:
            system += ' 必须绑定给定正文SHA，只判不改。每项依据必须给当前正文真实场次和可核对证据。只返回JSON：{"pass":true或false,"issues":["场次+问题"],"evidence":[{"scene":"真实场次标题","proof":"正文证据"}],"note":"简短结论","owner":"scene_facts或character_exchange"}。没有问题时issues为空；不得因个人偏好判失败。冷读失败必须用owner归责。'
        phase = {
            "causal": "scene-review",
            "dialogue": "dialogue-review",
            "cold": "isolated-audience",
        }[kind]
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": "digest=" + digest + "\n" + json.dumps(payload, ensure_ascii=False)}],
            validator=True,
            max_tokens=3500,
            temperature=0.15,
            reference_phase=phase,
            reference_profiles=self._reference_profiles(data, node),
        )
        issues = [str(item) for item in result.get("issues", [])] if isinstance(result.get("issues"), list) else []
        review = {"name": kind, "pass": bool(result.get("pass")) and not issues, "issues": issues, "evidence": result.get("evidence") if isinstance(result.get("evidence"), list) else [], "note": result.get("note") or "", "owner": result.get("owner") or "", "digest": digest}
        if kind == "dialogue":
            valid_ids = set(packet["dialogue_ids"])
            if not result.get("pass") and not issues:
                raise RuntimeError("人物交流复核判定失败但没有返回带编号的问题项")
            if any(not (set(re.findall(r"S\d{2}-L\d{2}", issue)) & valid_ids) for issue in issues):
                raise RuntimeError("人物交流问题项缺少当前台词包中的有效编号")
            review.update({"dialogue_sha256": packet["dialogue_sha256"], "dialogue_count": packet["dialogue_count"], "coverage": packet["coverage"]})
        return review

    def _repair(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], audit: dict[str, Any], layer: str) -> None:
        previous_script = str(node.get("script") or "")
        shared = "不得改拓扑、选项目标、结局属性、正式角色/场景/道具名，也不得提前使用未来事实。只修被定位的当前场次，只返回JSON。"
        if layer == "dialogue":
            system = "你是人物交流层定点返修器。输入剧本中的方括号编号只用于定位，输出时必须全部移除。只能修改问题编号对应的现有说话人台词；场次、出场、动作、选择和全部非台词文字必须逐字不变。不得用新对白补世界规则、证据来源或动作过程。" + shared
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
        system = f"你是 episode-generator {current_episode_skill_version()} 状态回写员。当前执行组全部节点已经通过同一正文指纹的机械、场面事实、人物交流和隔离观众复核。只从冻结正文提取实际发生的状态；禁止写作者计划、未来答案、路线评价或主题解释。只返回JSON。"
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
            "reviews": [reviews[name] for name in ("mechanical", "causal", "dialogue", "cold") if name in reviews],
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
        if data.get("__compactTopologyCards"):
            self._expand_group_cards(project_id, run_id, data, nodes, predecessors)
        for node in nodes:
            if not str(node.get("script") or "").strip():
                self._write_episode(project_id, run_id, data, node, predecessors, topology)
        self._group_state(group, "候选正文")

        for state, kind in (("机械预检通过", "mechanical"), ("场面事实通过", "causal"), ("人物交流通过", "dialogue"), ("隔离观众通过", "cold")):
            pipeline["scripts"] = {"pct": round(((index - 1) + GROUP_STATES.index(state) / 8) / max(total_groups, 1) * 100), "label": f"{group['id']} · {state} · {index}/{total_groups} 组", "state": "active"}
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)
            for node in nodes:
                try:
                    self._ensure_layer(project_id, run_id, data, node, predecessors, topology, kind)
                except ProductionStopped:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"{group['id']} · {node.get('id')} · {state}：{public_error_message(exc)}") from exc
                self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)
            self._group_state(group, state)
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)

        for node in nodes:
            self._ensure_current_reviews(project_id, run_id, data, node, predecessors, topology)
            node["episode_audit"]["locked"] = True
        self._group_state(group, "机械冻结")
        for node in nodes:
            self._freeze(project_id, run_id, data, node, predecessors)
        self._group_state(group, "状态已回写")
        self._sync_episodes(data, predecessors)
        self._log(project_id, run_id, f"  ✓ {group['id']} 全组冻结并完成状态回写")

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
        system = f"你是 episode-generator {current_episode_skill_version()} 全剧终检员。" + specs[kind] + ' 只判不改，只报告会破坏理解、连续性或人物成立的真实问题。返回JSON：{"pass":true或false,"issues":["问题"],"affected_ids":["节点id"]}。'
        result = self._json_chat(
            project_id,
            run_id,
            [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"topology": [{"id": n.get("id"), "next": n.get("next")} for n in data.get("nodes") or []], "episodes": episodes}, ensure_ascii=False)}],
            validator=True,
            max_tokens=6000,
            temperature=0.1,
            reference_phase="whole-play-review",
            reference_profiles=self._reference_profiles(data),
        )
        return {"name": kind, "pass": bool(result.get("pass")), "issues": result.get("issues") if isinstance(result.get("issues"), list) else [], "affected_ids": [str(item) for item in result.get("affected_ids") or []]}

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
        episodes = []
        for node in data.get("nodes") or []:
            next_items = node.get("next") or []
            interactive = node.get("kind") in ("choice", "interaction")
            node_id = str(node.get("id"))
            related_props = []
            for item in data.get("props") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                appearances = {part.strip() for part in re.split(r"[、,，]", str(item.get("appears") or "")) if part.strip()}
                if node_id in appearances or str(item.get("name")) in str(node.get("script") or ""):
                    related_props.append(item.get("name"))
            episodes.append({
                "分集编号": node.get("id"),
                "分集标题": node.get("node_title") or node.get("id"),
                "分集剧本": {"单集梗概": node.get("text") or "", "完整剧本": node.get("script") or ""},
                "剧本分析": {"本集冲突": node.get("dramatic_core") or "", "前置节点编号列表": predecessors.get(str(node.get("id")), []), "后续节点编号列表": [item.get("to") for item in next_items]},
                "关联角色": [item for item in re.split(r"[、,，]", str(node.get("cast") or "")) if item],
                "关联场景": [node.get("loc")] if node.get("loc") else [],
                "关联道具": related_props,
                "是否结局": node.get("kind") in ("major", "minor"),
                "互动节点": {"是否为分支节点": interactive, "是否有选择问题": interactive and bool(next_items), "选择问题": node.get("choice_question") or node.get("question") or "", "选项列表": [{"选项编号": str(index + 1), "选项文字": item.get("label") or "继续", "目标分集编号": item.get("to")} for index, item in enumerate(next_items)], "默认下一分集编号": "" if interactive else ((next_items[0].get("to") if next_items else "") or "")},
            })
        data["episodes"] = episodes

    @staticmethod
    def _is_locked(node: dict[str, Any]) -> bool:
        audit = node.get("episode_audit")
        if not isinstance(audit, dict) or not audit.get("locked"):
            return False
        digest = script_digest(node.get("script"))
        reviews = {str(item.get("name")): item for item in audit.get("reviews") or [] if isinstance(item, dict)}
        return audit.get("digest") == digest and all(
            reviews.get(name, {}).get("pass") and reviews.get(name, {}).get("digest") == digest
            for name in ("mechanical", "causal", "dialogue", "cold")
        )

    def _finish_from_checkpoint(self, project_id: str, run_id: str, data: dict[str, Any], pipeline: dict[str, Any]) -> None:
        """Run the v0.1.25 rolling execution-group pipeline from a checkpoint."""
        self._load_reference_context(
            project_id,
            run_id,
            "production-cards",
            self._reference_profiles(data),
        )
        order, predecessors, topology = self._graph(data)
        total = len(order)
        forge = data.get("__episodeForge") if isinstance(data.get("__episodeForge"), dict) else {}
        fresh_groups = self._execution_groups(data)
        old_groups = forge.get("execution_groups") if str(forge.get("version") or "").endswith("-backend") and isinstance(forge.get("execution_groups"), list) else []
        old_by_nodes = {tuple(group.get("nodes") or []): group for group in old_groups if isinstance(group, dict)}
        for group in fresh_groups:
            previous = old_by_nodes.get(tuple(group["nodes"]))
            if previous and previous.get("state") in GROUP_STATES:
                group["state"] = previous["state"]
        forge.update({
            "version": current_episode_skill_version() + "-backend",
            "cache_version": "episode-cache-" + current_episode_skill_version(),
            "policy_note": "全部模型阶段由装载器注入当前 reference bundle；装载凭证仅保存在执行器私有内存中",
            "topology_frozen": True,
            "status": "running",
            "total": total,
            "review_policy": "execution-group: mechanical → scene-facts → character-exchange → isolated-audience → freeze → state-writeback",
            "execution_groups": fresh_groups,
        })
        data["__episodeForge"] = forge

        groups = forge["execution_groups"]
        pipeline["topology"] = {"pct": 100, "label": f"全局规划通过 · {len(groups)} 个执行组", "state": "pass"}
        pipeline["overall"] = {"label": "按执行组滚动生产", "state": "active"}
        forge["phase"] = "execution_groups"
        for index, group in enumerate(groups, 1):
            self._check_stop(project_id, run_id)
            group_nodes = [next(node for node in order if str(node.get("id")) == node_id) for node_id in group["nodes"]]
            if group.get("state") == "状态已回写" and all(self._is_locked(node) for node in group_nodes):
                continue
            forge["current_group"] = group["id"]
            self._process_group(project_id, run_id, data, group, predecessors, topology, pipeline, index, len(groups))
            forge["generated"] = sum(1 for node in order if str(node.get("script") or "").strip())
            forge["locked"] = sum(1 for node in order if self._is_locked(node))
            forge["reviewed"] = forge["locked"]
            pipeline["scripts"] = {"pct": round(index / max(len(groups), 1) * 100), "label": f"已完成 {index}/{len(groups)} 个执行组", "state": "active" if index < len(groups) else "pass"}
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)

        forge["phase"] = "whole_play_validation"
        pipeline["scripts"] = {"pct": 100, "label": f"{len(groups)} 个执行组全部状态已回写", "state": "pass"}
        pipeline["overall"] = {"label": "全剧压缩校验", "state": "active"}
        while True:
            whole_reviews = []
            for index, kind in enumerate(("causal", "dialogue"), 1):
                pipeline["audit"] = {"pct": index * 45, "label": "全剧场面事实复检" if kind == "causal" else "全剧人物交流复检", "state": "active"}
                self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)
                review = self._whole_play_review(project_id, run_id, data, kind)
                whole_reviews.append(review)
                details = "；".join(review["issues"]) or "通过"
                self._log(project_id, run_id, f"  {'✓' if review['pass'] else '✗'} 全剧{'场面事实' if kind == 'causal' else '人物交流'}：{details}")
            forge["whole_play_reviews"] = whole_reviews
            failed = [review for review in whole_reviews if not review.get("pass")]
            if not failed:
                break
            affected = {node_id for review in failed for node_id in review.get("affected_ids") or []}
            if not affected:
                raise RuntimeError("全剧语义复检未通过，但校验模型没有定位受影响节点")
            by_id = {str(node.get("id")): node for node in order}
            self._log(project_id, run_id, "  ↳ 全剧复检只解冻受影响节点及依赖：" + "、".join(sorted(affected)))
            causal_ids = {node_id for review in failed if review.get("name") == "causal" for node_id in review.get("affected_ids") or []}
            impacted = self._descendants(data, causal_ids) | affected
            for review in failed:
                layer = "causal" if review.get("name") == "causal" else "dialogue"
                for node_id in review.get("affected_ids") or []:
                    node = by_id.get(node_id)
                    if not node:
                        continue
                    localized = {"issues": [issue for issue in review.get("issues") or [] if node_id in issue] or review.get("issues") or []}
                    try:
                        self._repair(project_id, run_id, data, node, predecessors, topology, localized, layer)
                    except RuntimeError as exc:
                        if str(exc) != "REPAIR_CARD_REQUIRED":
                            raise
                        self._repair_card(project_id, run_id, data, node, localized)
                        self._write_episode(project_id, run_id, data, node, predecessors, topology)
            for group_index, group in enumerate(groups, 1):
                if not impacted.intersection(group.get("nodes") or []):
                    continue
                group["state"] = "未开始"
                for node_id in group.get("nodes") or []:
                    if isinstance((by_id.get(node_id) or {}).get("episode_audit"), dict):
                        by_id[node_id]["episode_audit"]["locked"] = False
                self._process_group(project_id, run_id, data, group, predecessors, topology, pipeline, group_index, len(groups))

        forge.update({"status": "completed", "phase": "completed"})
        data["__forgeComplete"] = True
        self._sync_episodes(data, predecessors)
        self._assert_reference_receipts(project_id, run_id)
        pipeline["audit"] = {"pct": 100, "label": f"全剧校验通过 · {total} 集已冻结", "state": "pass"}
        pipeline["overall"] = {"label": "已交付", "state": "pass"}
        self._log(project_id, run_id, f"✓ {current_episode_skill_version()} 分集主体已完成。")
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
            compact_mode = duration >= 30 or endings >= 6
            topology_base = list(base_messages) + ([self._compact_topology_instruction()] if compact_mode else [])
            round_no, json_failures, messages = 1, 0, list(topology_base)
            while True:
                self._check_stop(project_id, run_id)
                pipeline["overall"] = {"label": "拓扑与情绪脊", "state": "active"}
                pipeline["topology"] = {"pct": min(90, 15 + round_no * 10), "label": f"第 {round_no} 轮生成与硬规则验证", "state": "active"}
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
                except (ValueError, json.JSONDecodeError) as exc:
                    json_failures += 1
                    errors = [f"JSON 无法解析：{exc}"]
                    if len(json_text) >= 18000 and not compact_mode:
                        compact_mode = True
                        json_failures = 0
                        topology_base = list(base_messages) + [self._compact_topology_instruction()]
                        self._log(project_id, run_id, f"  ↳ 检测到结构化输出在 {len(json_text)} 字符附近截断，切换长项目分段协议；这不是剧情返工")
                    elif compact_mode and len(json_text) >= 18000 and json_failures >= 2:
                        raise RuntimeError("长项目分段协议下 JSON 仍连续两次被截断，已停止以避免重复消耗") from exc
                    elif json_failures >= 3:
                        raise RuntimeError("拓扑 JSON 连续三次无法解析，已停止以避免重复消耗") from exc
                else:
                    json_failures = 0
                    errors = self._outline_errors(data, duration, endings)
                if not errors:
                    break
                self._log(project_id, run_id, "  ↳ 未通过：" + "；".join(errors))
                messages = list(topology_base) + [{"role": "user", "content": "上一版未通过硬规则，必须完整重做并修正：\n- " + "\n- ".join(errors)}]
                round_no += 1

            data["__compactTopologyCards"] = compact_mode
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
