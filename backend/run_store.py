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

    def request_stop(self, project_id: str, run_id: str) -> None:
        path = self.stop_path(project_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now(), encoding="utf-8")
        self.update(project_id, run_id, status="stopping")

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
            records.append({key: record.get(key) for key in ("run_id", "title", "status", "phase", "progress", "created_at", "updated_at", "error")})
        return records
