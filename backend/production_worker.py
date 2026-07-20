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
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from backend.run_store import RunStore


def visible_count(value: Any) -> int:
    return len(re.sub(r"\s", "", str(value or "")))


def action_count(value: Any) -> int:
    return len(re.findall(r"^\s*△", str(value or ""), flags=re.MULTILINE))


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


class ProductionStopped(RuntimeError):
    pass


class ProductionManager:
    def __init__(self, runs: RunStore, settings_loader: Callable[[], dict[str, Any]]) -> None:
        self.runs = runs
        self.settings_loader = settings_loader
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        self._responses: dict[tuple[str, str], Any] = {}
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
            self.runs.update(project_id, run_id, status="running", error="")
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
            "overall": {"label": "准备中", "state": "active"},
            "topology": {"pct": 0, "label": "等待拓扑与情绪脊生成", "state": "active"},
            "scripts": {"pct": 0, "label": "拓扑通过后生成全量剧本", "state": ""},
            "audit": {"pct": 0, "label": "全量剧本完成后开始验收", "state": ""},
        }

    def _check_stop(self, project_id: str, run_id: str) -> None:
        if self.runs.should_stop(project_id, run_id):
            raise ProductionStopped("用户已停止")

    def _update_pipeline(self, project_id: str, run_id: str, pipeline: dict[str, Any], **changes: Any) -> None:
        overall = pipeline.get("overall") or {}
        phase = changes.pop("phase", None) or overall.get("label") or "production"
        percentages = [int((pipeline.get(k) or {}).get("pct") or 0) for k in ("topology", "scripts", "audit")]
        progress = round(sum(percentages) / 3)
        self.runs.update(project_id, run_id, pipeline=pipeline, phase=phase, progress=progress, **changes)

    def _log(self, project_id: str, run_id: str, message: str) -> None:
        record = self.runs.read(project_id, run_id) or {}
        old = str(record.get("response_text") or "")
        self.runs.update(project_id, run_id, response_text=old + message.rstrip() + "\n")

    def _log_chunk(self, project_id: str, run_id: str, chunk: str) -> None:
        if not chunk:
            return
        record = self.runs.read(project_id, run_id) or {}
        old = str(record.get("response_text") or "")
        self.runs.update(project_id, run_id, response_text=old + chunk)

    def _chat(self, project_id: str, run_id: str, messages: list[dict[str, Any]], *, validator: bool = False, max_tokens: int = 9000, temperature: float = 0.72, stream_log: bool = False) -> str:
        self._check_stop(project_id, run_id)
        settings = self.settings_loader()
        if not settings.get("config_locked") or not settings.get("api_key"):
            raise RuntimeError("生产后台未锁定或 Key 未配置")
        model = (settings.get("validator_model") or settings.get("model")) if validator else settings.get("model")
        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
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

    def _json_chat(self, project_id: str, run_id: str, messages: list[dict[str, Any]], *, validator: bool = False, max_tokens: int = 9000, temperature: float = 0.72) -> dict[str, Any]:
        current = list(messages)
        round_no = 1
        while True:
            text = self._chat(project_id, run_id, current, validator=validator, max_tokens=max_tokens, temperature=temperature)
            try:
                return parse_json_text(text)
            except (ValueError, json.JSONDecodeError):
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
        }

    def _write_episode(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]]) -> None:
        system = "你是新版分集剧本锻造器。拓扑和制作卡已冻结，只能写当前一集，禁止改变节点编号、前后连接、选项目标、结局属性或资产身份。剧本必须是自然中文影视剧本，850–1300个可见非空白字符，16–24个以“△”开头且可直接拍摄的动作段落；台词可说、人物可区分。每集只推进一个主事件，必须形成触发→反应→行动→阻力→调整→可见结果→反应→下一步。只返回JSON。"
        prompt = "根据冻结上下文写当前集。production_card 的 must_payoff 必须兑现，must_seed 必须埋下，forbidden 不得触碰。working_summary 仅供下游写作使用，不是锁稿摘要。\n" + json.dumps(self._context(data, node, predecessors, topology), ensure_ascii=False) + '\n返回：{"script":"完整剧本","entry_state":"300–600字入口硬状态","exit_state":"出口硬状态","working_summary":"60–100字临时连续性摘要","dramatic_core":"唯一戏剧变化","power_shift":"权力变化","information_gap":"知情差","dialogue_intent":"对抗/交代/关系"}'
        result = self._json_chat(project_id, run_id, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        node.update(result)
        self._log(project_id, run_id, f"  ↳ {node.get('id')} 初稿完成：{visible_count(node.get('script'))} 可见字 · {action_count(node.get('script'))} 个动作段")

    @staticmethod
    def _mechanical(node: dict[str, Any], digest: str) -> dict[str, Any]:
        length, actions, issues = visible_count(node.get("script")), action_count(node.get("script")), []
        if length < 850 or length > 1300:
            issues.append(f"可见字数 {length}，要求 850–1300")
        if actions < 16 or actions > 24:
            issues.append(f"△动作段 {actions}，要求 16–24")
        entry_length = visible_count(node.get("entry_state"))
        if entry_length < 300 or entry_length > 600:
            issues.append("入口硬状态须为 300–600 字")
        return {"name": "mechanical", "pass": not issues, "issues": issues, "digest": digest, "stats": {"visible_chars": length, "action_paragraphs": actions}}

    def _review(self, project_id: str, run_id: str, kind: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], digest: str) -> dict[str, Any]:
        specs = {
            "causal": "只检查地点与出入、人物权限、事件感知与反应、道具路径、入口出口连续性、分支与汇合一致性。",
            "dialogue": "只检查中文台词是否自然可说、角色声音能否区分、是否翻译腔或AI腔、是否过度解释设定。",
            "cold": "你是与创作上下文隔离的首次观众。只根据入口硬状态、前集观众摘要和当前剧本检查六问：我在哪里、谁在场、现在要什么、阻力是什么、刚发生了什么变化、下一步为什么成立；另检查动作可拍、台词可说。",
        }
        context = self._context(data, node, predecessors, topology)
        payload = {"prior_audience_summaries": context["incoming_memories"], "entry_state": node.get("entry_state"), "script": node.get("script")} if kind == "cold" else {"episode_id": node.get("id"), "predecessors": predecessors.get(str(node.get("id")), []), "topology": topology, "current_script": node.get("script"), "entry_state": node.get("entry_state"), "exit_state": node.get("exit_state")}
        system = "你是分集锁稿验收员。" + specs[kind] + '必须对给定 SHA-256 对应的当前版本作结论。只返回JSON：{"pass":true或false,"issues":["可定位问题"],"note":"简短结论"}。没有问题时 issues 为空；不得因为偏好提出改写。'
        result = self._json_chat(project_id, run_id, [{"role": "system", "content": system}, {"role": "user", "content": "digest=" + digest + "\n" + json.dumps(payload, ensure_ascii=False)}], validator=True, max_tokens=3500, temperature=0.15)
        return {"name": kind, "pass": bool(result.get("pass")), "issues": result.get("issues") if isinstance(result.get("issues"), list) else [], "note": result.get("note") or "", "digest": digest}

    def _repair(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], audits: list[dict[str, Any]]) -> None:
        issues = [f"{audit['name']}：{issue}" for audit in audits for issue in audit.get("issues") or []]
        system = "你是分集剧本定点返修器。只能修当前集剧本与本集记忆字段，不得改拓扑、选项目标、结局属性、角色/场景/道具身份，也不得提前使用未来事实。修复列出的验收问题，同时满足850–1300可见字、16–24个△动作段、自然可说中文。只返回JSON。"
        prompt = json.dumps({"context": self._context(data, node, predecessors, topology), "current": {"script": node.get("script"), "entry_state": node.get("entry_state"), "exit_state": node.get("exit_state")}, "issues": issues}, ensure_ascii=False) + '\n返回：{"script":"返修后完整剧本","entry_state":"300–600字","exit_state":"出口硬状态"}'
        result = self._json_chat(project_id, run_id, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        for key in ("script", "entry_state", "exit_state"):
            if result.get(key):
                node[key] = result[key]

    def _freeze(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]]) -> None:
        system = "你是分集冻结记录员。正文已经通过同一 SHA-256 的机械、因果、对白与隔离冷读验收。只记录观众在本集实际看见并确定知道的事实；禁止写作者计划、未来答案、路线评价或主题解释。只返回JSON。"
        prompt = json.dumps({"episode_id": node.get("id"), "predecessors": predecessors.get(str(node.get("id")), []), "entry_state": node.get("entry_state"), "script": node.get("script")}, ensure_ascii=False) + '\n返回：{"audience_summary":"60–100个汉字","exit_state":"人物位置、关系、知情、道具归属、未决行动"}'
        while True:
            result = self._json_chat(project_id, run_id, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            if 60 <= visible_count(result.get("audience_summary")) <= 100:
                node["audience_summary"] = result.get("audience_summary") or ""
                if result.get("exit_state"):
                    node["exit_state"] = result["exit_state"]
                return
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 锁稿摘要长度不合规，后台自动重做")

    def _audit_until_pass(self, project_id: str, run_id: str, data: dict[str, Any], node: dict[str, Any], predecessors: dict[str, list[str]], topology: list[dict[str, Any]], on_round: Callable[[int, str], None]) -> None:
        round_no, previous = 1, None
        while True:
            self._check_stop(project_id, run_id)
            on_round(round_no, f"第 {round_no} 轮验收")
            digest = hashlib.sha256(str(node.get("script") or "").encode("utf-8")).hexdigest()
            audits = [self._mechanical(node, digest)]
            mechanical = audits[0]
            self._log(project_id, run_id, f"  {'✓' if mechanical['pass'] else '✗'} {node.get('id')} 机械检查：{mechanical['stats']['visible_chars']} 字 · {mechanical['stats']['action_paragraphs']} 动作段" + (("｜" + "；".join(mechanical["issues"])) if mechanical["issues"] else ""))
            for kind in ("causal", "dialogue", "cold"):
                label = {"causal": "因果", "dialogue": "对白", "cold": "冷读"}[kind]
                self._log(project_id, run_id, f"  … {node.get('id')} {label}验收中")
                review = self._review(project_id, run_id, kind, data, node, predecessors, topology, digest)
                audits.append(review)
                details = "；".join(str(item) for item in review.get("issues") or []) or str(review.get("note") or "通过")
                self._log(project_id, run_id, f"  {'✓' if review['pass'] else '✗'} {node.get('id')} {label}：{details}")
            node["episode_audit"] = {"digest": digest, "locked": False, "attempts": round_no, "reviews": audits, "previous": previous}
            if all(bool(item.get("pass")) and item.get("digest") == digest for item in audits):
                self._log(project_id, run_id, f"  … {node.get('id')} 四项验收通过，正在生成锁稿摘要")
                self._freeze(project_id, run_id, data, node, predecessors)
                node["episode_audit"] = {"digest": digest, "locked": True, "attempts": round_no, "reviews": audits, "previous": previous}
                return
            self._log(project_id, run_id, f"  ↳ {node.get('id')} 第 {round_no} 轮未通过，后台按问题自动返工")
            on_round(round_no, f"第 {round_no} 轮未过 · 自动返工")
            self._repair(project_id, run_id, data, node, predecessors, topology, audits)
            previous, round_no = audits, round_no + 1

    @staticmethod
    def _sync_episodes(data: dict[str, Any], predecessors: dict[str, list[str]]) -> None:
        episodes = []
        for node in data.get("nodes") or []:
            next_items = node.get("next") or []
            interactive = node.get("kind") in ("choice", "interaction")
            episodes.append({
                "分集编号": node.get("id"),
                "分集标题": node.get("node_title") or node.get("id"),
                "分集剧本": {"单集梗概": node.get("text") or "", "完整剧本": node.get("script") or ""},
                "剧本分析": {"本集冲突": node.get("dramatic_core") or "", "前置节点编号列表": predecessors.get(str(node.get("id")), []), "后续节点编号列表": [item.get("to") for item in next_items]},
                "关联角色": [item for item in re.split(r"[、,，]", str(node.get("cast") or "")) if item],
                "关联场景": [node.get("loc")] if node.get("loc") else [],
                "是否结局": node.get("kind") in ("major", "minor"),
                "互动节点": {"是否为分支节点": interactive, "是否有选择问题": interactive and bool(next_items), "选择问题": node.get("choice_question") or node.get("question") or "", "选项列表": [{"选项编号": str(index + 1), "选项文字": item.get("label") or "继续", "目标分集编号": item.get("to")} for index, item in enumerate(next_items)]},
                "_工作台验收": node.get("episode_audit"),
            })
        data["episodes"] = episodes

    @staticmethod
    def _is_locked(node: dict[str, Any]) -> bool:
        audit = node.get("episode_audit")
        if not isinstance(audit, dict) or not audit.get("locked"):
            return False
        digest = hashlib.sha256(str(node.get("script") or "").encode("utf-8")).hexdigest()
        return audit.get("digest") == digest

    def _finish_from_checkpoint(self, project_id: str, run_id: str, data: dict[str, Any], pipeline: dict[str, Any]) -> None:
        """Generate and audit only unfinished episodes in a persisted topology."""
        order, predecessors, topology = self._graph(data)
        total = len(order)
        forge = data.get("__episodeForge") if isinstance(data.get("__episodeForge"), dict) else {}
        forge.update({
            "version": "0.1.16-backend",
            "topology_frozen": True,
            "status": "running",
            "total": total,
            "review_policy": "same-digest mechanical + causal + dialogue + isolated-cold-read; repair-until-pass",
        })
        data["__episodeForge"] = forge

        generated = sum(1 for node in order if str(node.get("script") or "").strip())
        forge.update({"phase": "scripts", "generated": generated})
        pipeline["topology"] = {"pct": 100, "label": "拓扑、流程图与情绪脊验证通过", "state": "pass"}
        pipeline["scripts"] = {"pct": round(generated / max(total, 1) * 100), "label": f"已保留 {generated}/{total} 集断点", "state": "active" if generated < total else "pass"}
        pipeline["overall"] = {"label": "继续生成全量剧本", "state": "active"}
        self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)
        for index, node in enumerate(order, 1):
            self._check_stop(project_id, run_id)
            if str(node.get("script") or "").strip():
                continue
            pipeline["scripts"] = {"pct": round((index - 1) / max(total, 1) * 100), "label": f"正在写第 {index}/{total} 集 · {node.get('id')}", "state": "active"}
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)
            self._log(project_id, run_id, f"▸ 断点续写 {index}/{total}｜{node.get('id')}")
            self._write_episode(project_id, run_id, data, node, predecessors, topology)
            forge["generated"] = sum(1 for item in order if str(item.get("script") or "").strip())
            self._sync_episodes(data, predecessors)
            pipeline["scripts"] = {"pct": round(forge["generated"] / max(total, 1) * 100), "label": f"已生成 {forge['generated']}/{total} 集", "state": "active" if forge["generated"] < total else "pass"}
            self._update_pipeline(project_id, run_id, pipeline, phase="scripts", result_data=data)

        forge["phase"] = "audit"
        locked = sum(1 for node in order if self._is_locked(node))
        forge.update({"reviewed": locked, "locked": locked})
        pipeline["scripts"] = {"pct": 100, "label": f"{total} 集完整剧本已全部生成", "state": "pass"}
        pipeline["audit"] = {"pct": round(locked / max(total, 1) * 100), "label": f"已保留 {locked}/{total} 集锁稿断点", "state": "active" if locked < total else "pass"}
        pipeline["overall"] = {"label": "继续校验与自动返工", "state": "active"}
        self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)
        for index, node in enumerate(order, 1):
            self._check_stop(project_id, run_id)
            if self._is_locked(node):
                continue

            def on_round(audit_round: int, label: str, *, _index: int = index, _node: dict[str, Any] = node) -> None:
                pipeline["audit"] = {"pct": round((_index - 1) / max(total, 1) * 100), "label": f"{_node.get('id')} · {label} · 剩余 {total - _index + 1} 集", "state": "active"}
                self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)

            self._log(project_id, run_id, f"▸ 断点续验 {index}/{total}｜{node.get('id')}")
            self._audit_until_pass(project_id, run_id, data, node, predecessors, topology, on_round)
            locked = sum(1 for item in order if self._is_locked(item))
            forge.update({"reviewed": locked, "locked": locked})
            self._sync_episodes(data, predecessors)
            pipeline["audit"] = {"pct": round(locked / max(total, 1) * 100), "label": f"已锁稿 {locked}/{total} 集", "state": "active" if locked < total else "pass"}
            self._update_pipeline(project_id, run_id, pipeline, phase="audit", result_data=data)
            self._log(project_id, run_id, f"  ✓ {node.get('id')} 第 {node['episode_audit']['attempts']} 轮通过并锁稿")

        forge.update({"status": "completed", "phase": "completed"})
        data["__forgeComplete"] = True
        self._sync_episodes(data, predecessors)
        pipeline["audit"] = {"pct": 100, "label": f"{total} 集全部通过并锁稿", "state": "pass"}
        pipeline["overall"] = {"label": "已交付", "state": "pass"}
        self._log(project_id, run_id, "✓ 后台生产、验收与自动返工全部完成，已交付。")
        self._update_pipeline(project_id, run_id, pipeline, phase="completed", status="completed", result_data=data)

    def _resume(self, project_id: str, run_id: str, data: dict[str, Any]) -> None:
        record = self.runs.read(project_id, run_id) or {}
        pipeline = record.get("pipeline") if isinstance(record.get("pipeline"), dict) else self._pipeline()
        try:
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

    def _run(self, project_id: str, run_id: str, payload: dict[str, Any]) -> None:
        pipeline = self._pipeline()
        try:
            outline = payload.get("outline") if isinstance(payload.get("outline"), dict) else {}
            base_messages = outline.get("messages") if isinstance(outline.get("messages"), list) else []
            if not base_messages:
                raise RuntimeError("缺少大纲生成 messages")
            duration, endings = int(payload.get("duration") or 20), int(payload.get("endings") or 3)
            round_no, messages = 1, list(base_messages)
            while True:
                self._check_stop(project_id, run_id)
                pipeline["overall"] = {"label": "拓扑与情绪脊", "state": "active"}
                pipeline["topology"] = {"pct": min(90, 15 + round_no * 10), "label": f"第 {round_no} 轮生成与硬规则验证", "state": "active"}
                self._update_pipeline(project_id, run_id, pipeline, phase="topology")
                self._log(project_id, run_id, f"▸ 后台拓扑生成与校验｜第 {round_no} 轮")
                text = self._chat(project_id, run_id, messages, max_tokens=int(outline.get("max_tokens") or 20000), temperature=float(outline.get("temperature") or 0.8), stream_log=True)
                try:
                    data = parse_json_text(text.split("===JSON===", 1)[-1])
                except (ValueError, json.JSONDecodeError) as exc:
                    errors = [f"JSON 无法解析：{exc}"]
                else:
                    errors = self._outline_errors(data, duration, endings)
                if not errors:
                    break
                self._log(project_id, run_id, "  ↳ 未通过：" + "；".join(errors))
                messages = list(base_messages) + [{"role": "user", "content": "上一版未通过硬规则，必须完整重做并修正：\n- " + "\n- ".join(errors)}]
                round_no += 1

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
