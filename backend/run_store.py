#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持久化生成任务状态。任务文件写在 data/projects/<project>/tasks/。"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any


def safe_id(value: Any, fallback: str = "run") -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff\-.]", "_", str(value or fallback))
    return text[:100] or fallback


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class RunStore:
    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root

    def project_dir(self, project_id: str) -> Path:
        return self.projects_root / safe_id(project_id, "project")

    def task_path(self, project_id: str, run_id: str) -> Path:
        return self.project_dir(project_id) / "tasks" / f"{safe_id(run_id)}.json"

    def stop_path(self, project_id: str, run_id: str) -> Path:
        return self.project_dir(project_id) / "tasks" / f"{safe_id(run_id)}.stop"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def read(self, project_id: str, run_id: str) -> dict[str, Any] | None:
        path = self.task_path(project_id, run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def create(
        self,
        project_id: str,
        run_id: str,
        title: str,
        phase: str,
        request_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = safe_id(project_id, "project")
        run_id = safe_id(run_id, "run")
        stop = self.stop_path(project_id, run_id)
        if stop.exists():
            stop.unlink()
        record = {
            "project_id": project_id,
            "run_id": run_id,
            "title": title or project_id,
            "status": "running",
            "phase": phase,
            "progress": 0,
            "created_at": now(),
            "updated_at": now(),
            "request": request_summary or {},
            "response_text": "",
            "usage": None,
            "error": "",
        }
        self._atomic_json(self.task_path(project_id, run_id), record)
        return record

    def update(self, project_id: str, run_id: str, **changes: Any) -> dict[str, Any]:
        record = self.read(project_id, run_id) or self.create(project_id, run_id, project_id, "unknown")
        record.update(changes)
        record["updated_at"] = now()
        self._atomic_json(self.task_path(project_id, run_id), record)
        return record

    def append_text(self, project_id: str, run_id: str, text: str, progress: int | None = None) -> dict[str, Any]:
        record = self.read(project_id, run_id) or self.create(project_id, run_id, project_id, "generation")
        record["response_text"] = str(record.get("response_text") or "") + text
        if progress is not None:
            record["progress"] = max(0, min(100, int(progress)))
        record["updated_at"] = now()
        self._atomic_json(self.task_path(project_id, run_id), record)
        return record

    # 后台专用全流程生成日志：只留在后台记录里，供生产后台排查用，前端不公开。
    # 末尾保留上限，避免 task.json 无限膨胀。
    LOG_CAP = 400_000

    def append_log(self, project_id: str, run_id: str, text: str) -> dict[str, Any]:
        record = self.read(project_id, run_id) or self.create(project_id, run_id, project_id, "generation")
        merged = str(record.get("production_log") or "") + text
        if len(merged) > self.LOG_CAP:
            merged = "…（日志已截断，仅保留最近部分）\n" + merged[-self.LOG_CAP:]
        record["production_log"] = merged
        record["updated_at"] = now()
        self._atomic_json(self.task_path(project_id, run_id), record)
        return record

    def request_stop(self, project_id: str, run_id: str) -> None:
        record = self.read(project_id, run_id)
        if not record or record.get("status") in {"completed", "failed", "stopped"}:
            return
        path = self.stop_path(project_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now(), encoding="utf-8")
        self.update(project_id, run_id, status="stopping")

    def clear_stop(self, project_id: str, run_id: str) -> None:
        """Clear a persisted cancellation marker before resuming a task."""
        path = self.stop_path(project_id, run_id)
        if path.is_file():
            path.unlink()

    def should_stop(self, project_id: str, run_id: str) -> bool:
        return self.stop_path(project_id, run_id).exists()

    def list_tasks(self, project_id: str) -> list[dict[str, Any]]:
        directory = self.project_dir(project_id) / "tasks"
        if not directory.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if record.get("status") == "stopping":
                try:
                    updated = dt.datetime.fromisoformat(str(record.get("updated_at") or ""))
                    stale = (dt.datetime.now() - updated).total_seconds() >= 3
                except ValueError:
                    stale = True
                if stale:
                    record["status"] = "stopped"
                    record["phase"] = "stopped"
                    record["updated_at"] = now()
                    self._atomic_json(path, record)
            summary = {key: record.get(key) for key in ("run_id", "title", "status", "phase", "progress", "created_at", "updated_at", "error", "kind")}
            checkpoint = record.get("result_data")
            if isinstance(checkpoint, dict):
                summary["result_title"] = str(checkpoint.get("title") or checkpoint.get("logline") or "")
            summary["resumable"] = bool(
                record.get("kind") == "production"
                and record.get("status") in {"failed", "stopped", "disconnected"}
                and isinstance(checkpoint, dict)
                and isinstance(checkpoint.get("nodes"), list)
                and checkpoint.get("nodes")
            )
            records.append(summary)
        return records

    def list_all_tasks(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.projects_root.is_dir():
            return records
        for project in self.projects_root.iterdir():
            if not project.is_dir():
                continue
            for record in self.list_tasks(project.name):
                record["project_id"] = project.name
                records.append(record)
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records

    def delete(self, project_id: str, run_id: str) -> bool:
        """Delete a finished task and its stop marker.

        Active tasks must be stopped by the caller first so a worker cannot
        recreate the task file immediately after it is removed.
        """
        path = self.task_path(project_id, run_id)
        deleted = False
        if path.is_file():
            path.unlink()
            deleted = True
        stop = self.stop_path(project_id, run_id)
        if stop.is_file():
            stop.unlink()
        return deleted
