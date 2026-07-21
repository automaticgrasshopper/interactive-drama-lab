#!/usr/bin/env python3
"""Deterministic private task ledger for episode-generator v0.1.35."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


VERSION = "atomic-task-ledger-v1"
SKILL_VERSION = "v0.1.35"
STATUSES = {"pending", "running", "complete", "needs_fix", "blocked"}


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def task(task_id: str, kind: str, role: str, deps: list[str], scope_id: str = "project") -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": kind,
        "scope_id": scope_id,
        "role": role,
        "dependencies": deps,
        "allowed_inputs": [],
        "allowed_outputs": [],
        "status": "pending",
        "attempt": 0,
        "input_digest": "",
        "output_digest": "",
        "issues": [],
    }


def planning_tasks() -> list[dict[str, Any]]:
    specs = [
        ("upstream-translate", "upstream_translate", "author"),
        ("topology-write", "topology_write", "author"),
        ("topology-review", "topology_review", "reviewer"),
    ]
    result: list[dict[str, Any]] = []
    for index, (task_id, kind, role) in enumerate(specs):
        result.append(task(task_id, kind, role, [] if index == 0 else [specs[index - 1][0]]))
    return result


def validate_episode_plan(plan: dict[str, Any]) -> list[str]:
    episodes = plan.get("episodes") or plan.get("nodes") or []
    errors: list[str] = []
    for episode in episodes:
        if not isinstance(episode, dict) or not episode.get("id"):
            errors.append("分集流程存在缺少 id 的分集")
            continue
        if not str(episode.get("id")).startswith("episode-"):
            errors.append(f"{episode.get('id')} 不是 episode-NNN 分集节点")
    ids = [str(item.get("id")) for item in episodes if isinstance(item, dict)]
    if len(ids) != len(set(ids)):
        errors.append("分集 id 重复")
    known = set(ids)
    for episode in episodes:
        for edge in episode.get("next") or [] if isinstance(episode, dict) else []:
            if isinstance(edge, dict) and str(edge.get("to")) not in known:
                errors.append(f"{episode.get('id')} 指向未知分集 {edge.get('to')}")
    return errors


def production_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    errors = validate_episode_plan(plan)
    if errors:
        raise ValueError("；".join(errors))
    result: list[dict[str, Any]] = []
    prior = "topology-review"
    for episode in plan.get("episodes") or plan.get("nodes") or []:
        episode_id = str(episode["id"])
        chain = [
            ("packet", "episode_packet_and_profile", "author"),
            ("forge", "scene_forging", "author"),
            ("fact-review", "scene_fact_review", "reviewer"),
            ("dialogue-review", "dialogue_and_profile_review", "reviewer"),
            ("save", "episode_save", "controller"),
        ]
        for suffix, kind, role in chain:
            task_id = f"{episode_id}:{suffix}"
            result.append(task(task_id, kind, role, [prior], episode_id))
            prior = task_id
    for suffix, kind, role in (
        ("continuity-review", "continuity_review", "reviewer"),
        ("final-assembly", "final_assembly", "assembler"),
    ):
        result.append(task(suffix, kind, role, [prior]))
        prior = suffix
    return result


def validate_ledger(ledger: dict[str, Any]) -> None:
    tasks = ledger.get("tasks") or []
    ids = [str(item.get("id")) for item in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("任务 id 重复")
    known = set(ids)
    if sum(1 for item in tasks if item.get("status") == "running") > 1:
        raise ValueError("同时存在多个 running 任务")
    for item in tasks:
        if item.get("status") not in STATUSES:
            raise ValueError(f"{item.get('id')} 状态非法")
        missing = set(item.get("dependencies") or []) - known
        if missing:
            raise ValueError(f"{item.get('id')} 依赖不存在：{sorted(missing)}")


def next_task(ledger: dict[str, Any]) -> dict[str, Any] | None:
    validate_ledger(ledger)
    running = next((item for item in ledger["tasks"] if item["status"] == "running"), None)
    if running:
        return running
    states = {item["id"]: item["status"] for item in ledger["tasks"]}
    return next((item for item in ledger["tasks"] if item["status"] == "pending" and all(states.get(dep) == "complete" for dep in item["dependencies"])), None)


def claim(ledger: dict[str, Any]) -> dict[str, Any]:
    current = next_task(ledger)
    if current is None:
        raise ValueError("没有可领取任务")
    if current["status"] == "pending":
        current["status"] = "running"
        current["attempt"] += 1
    return current


def complete(ledger: dict[str, Any], task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    current = next_task(ledger)
    if current is None or current["id"] != task_id or current["status"] != "running":
        raise ValueError("只能完成当前 running 任务")
    current["input_digest"] = str(result.get("input_digest") or "")
    current["output_digest"] = str(result.get("output_digest") or digest(result.get("output")))
    if current["role"] == "reviewer":
        required = {str(item) for item in result.get("required_checks") or []}
        covered = {str(item) for item in result.get("covered_checks") or []}
        issues = [str(item) for item in result.get("issues") or [] if str(item).strip()]
        if not required or required - covered:
            issues.append("审查覆盖不完整")
        current["issues"] = issues
        if issues:
            current["status"] = "needs_fix"
            _insert_repair_cycle(ledger, current)
            return current
    current["issues"] = []
    current["status"] = "complete"
    return current


def _insert_repair_cycle(ledger: dict[str, Any], review: dict[str, Any]) -> None:
    tasks = ledger["tasks"]
    index = tasks.index(review)
    sequence = review["attempt"]
    repair_id = f"{review['id']}:fix-{sequence}"
    retry_id = f"{review['id']}:retry-{sequence}"
    repair = task(repair_id, "targeted_fix", "author", [review["id"]], review["scope_id"])
    retry = task(retry_id, review["kind"], "reviewer", [repair_id], review["scope_id"])
    repair["allowed_inputs"] = [review["output_digest"], *review["issues"]]
    for downstream in tasks:
        downstream["dependencies"] = [retry_id if dep == review["id"] else dep for dep in downstream.get("dependencies") or []]
    tasks[index + 1:index + 1] = [repair, retry]
    review["status"] = "complete"
    review["outcome"] = "issues_found"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, ledger: dict[str, Any]) -> None:
    validate_ledger(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "expand", "status", "claim", "complete"))
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()

    if args.command == "init":
        ledger = {"ledger_version": VERSION, "skill_version": SKILL_VERSION, "tasks": planning_tasks()}
    else:
        ledger = load(args.ledger)
    if args.command == "expand":
        if not args.plan:
            raise SystemExit("expand 需要 --plan")
        if any(item["id"] == "path-replay" for item in ledger["tasks"]):
            raise SystemExit("生产任务已展开")
        ledger["tasks"].extend(production_tasks(load(args.plan)))
    elif args.command == "claim":
        current = claim(ledger)
        print(json.dumps(current, ensure_ascii=False))
    elif args.command == "complete":
        if not args.task_id or not args.result:
            raise SystemExit("complete 需要 --task-id 和 --result")
        complete(ledger, args.task_id, load(args.result))
    elif args.command == "status":
        current = next_task(ledger)
        print(json.dumps({"current": current, "complete": sum(item["status"] == "complete" for item in ledger["tasks"]), "total": len(ledger["tasks"])}, ensure_ascii=False))
    save(args.ledger, ledger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
