#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""互动影视节奏工作台本地后端。

职责：
- 提供现有 H5 静态文件；
- 保存数据包和后端项目；
- 管理仅保存在本机的模型设置；
- 代理 OpenRouter 请求，避免浏览器保存 API Key；
- 提供 Git 状态、里程碑提交和显式推送。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend.production_worker import ProductionManager
from backend.run_store import RunStore

ROOT = Path(__file__).resolve().parent.parent
H5_DIR = ROOT / "h5"
DATAPACKS_DIR = H5_DIR / "datapacks"
DATA_DIR = ROOT / "data" / "projects"
RUNTIME_DIR = ROOT / ".runtime"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"
EXECUTION_MANIFEST_PATH = ROOT / "backend" / "execution" / "execution_manifest.json"
RUNS = RunStore(DATA_DIR)


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def safe_name(value: Any, fallback: str = "item") -> str:
    text = str(value or fallback)
    text = re.sub(r"[^\w\u4e00-\u9fff\-.]", "_", text)
    return text[:100] or fallback


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def load_settings() -> dict[str, Any]:
    data = read_json(SETTINGS_PATH, {})
    return {
        "provider": data.get("provider", "openrouter"),
        "base_url": data.get("base_url", "https://openrouter.ai/api/v1"),
        "model": data.get("model", ""),
        "validator_model": data.get("validator_model", ""),
        "image_model": data.get("image_model") or "google/gemini-2.5-flash-image",
        "api_key": data.get("api_key", ""),
        "config_locked": bool(data.get("config_locked", False)),
        "auto_commit": bool(data.get("auto_commit", True)),
        # Remote pushes are intentionally reserved for the Codex-managed workflow.
        "auto_push": False,
    }


def public_settings() -> dict[str, Any]:
    data = load_settings()
    return {
        "provider": data["provider"],
        "base_url": data["base_url"],
        "model": data["model"],
        "validator_model": data["validator_model"],
        "image_model": data["image_model"],
        "api_key_configured": bool(data["api_key"]),
        "config_locked": data["config_locked"],
        "auto_commit": data["auto_commit"],
        "auto_push": data["auto_push"],
    }


def execution_schema_version() -> str:
    """Platform-owned execution schema version (internal; not a Skill version)."""
    try:
        manifest = json.loads(EXECUTION_MANIFEST_PATH.read_text(encoding="utf-8"))
        return str(manifest.get("execution_schema_version") or "v1")
    except (OSError, json.JSONDecodeError):
        return "v1"


PRODUCTION = ProductionManager(RUNS, load_settings)


def project_dirs() -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    projects: list[dict[str, Any]] = []
    for path in sorted(DATA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir():
            continue
        meta = read_json(path / "project.json", {})
        runs_dir = path / "runs"
        run_count = len(list(runs_dir.glob("*.json"))) if runs_dir.is_dir() else 0
        projects.append(
            {
                "id": path.name,
                "title": meta.get("title", path.name),
                "stage": meta.get("stage", "已保存"),
                "updated_at": meta.get(
                    "updated_at",
                    dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                ),
                "run_count": run_count,
            }
        )
    return projects


def datapack_stem(value: Any) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff\-]", "_", str(value or "rec"))[:40]


def git_file_state(path: Path) -> str:
    try:
        rel = str(path.relative_to(ROOT))
    except ValueError:
        return "未提交"
    if run_git("status", "--porcelain", "--", rel).stdout.strip():
        return "待提交"
    if run_git("ls-files", "--error-unmatch", "--", rel).returncode:
        return "未提交"
    remote = run_git("cat-file", "-e", f"@{{upstream}}:{rel}")
    return "已在 Git" if remote.returncode == 0 else "已本地提交"


def enrich_task(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    item.pop("diagnostic_error", None)
    project_id = safe_name(item.get("project_id"), "")
    project_meta = read_json(DATA_DIR / project_id / "project.json", {})
    project_title = str(project_meta.get("title") or project_id)
    item["project_title"] = project_title
    candidates = {datapack_stem(item.get("title")), datapack_stem(project_title), datapack_stem(project_id)}
    item["has_cg"] = bool(item.get("has_cg")) or any((DATAPACKS_DIR / f"{stem}.sb.json").is_file() for stem in candidates if stem)
    if not item.get("read_only"):
        item["git_state"] = git_file_state(RUNS.task_path(project_id, str(item.get("run_id") or "")))
    return item


def git_tree_paths(ref: str, prefix: str) -> list[str]:
    result = run_git("-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", ref, "--", prefix)
    return result.stdout.splitlines() if result.returncode == 0 else []


def git_blob_json(ref: str, path: str) -> dict[str, Any]:
    result = run_git("show", f"{ref}:{path}")
    if result.returncode:
        return {}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def archive_time(value: Any) -> str:
    text = str(value or "")
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    return text


def archive_dedupe_key(path: str, payload: dict[str, Any], *, kind: str, project_id: str) -> str:
    """Return a stable archive identity without treating asset timestamps as separate work."""
    if kind in {"script", "storyboard"}:
        name = Path(path).name
        for suffix in (".script.json", ".sb.json"):
            if name.endswith(suffix):
                return "datapack|" + name.removesuffix(suffix)
        return "datapack|" + datapack_stem(payload.get("boundStory") or payload.get("title"))
    run_id = str(payload.get("run_id") or Path(path).stem)
    return f"run|{project_id}|{run_id}"


def git_archived_tasks(include_data: bool = False) -> list[dict[str, Any]]:
    refs: list[tuple[str, str]] = [("HEAD", "已本地提交")]
    if run_git("rev-parse", "--verify", "@{upstream}").returncode == 0:
        refs.append(("@{upstream}", "已在 Git"))
    selected: dict[str, tuple[str, str]] = {}
    for ref, state in refs:
        for prefix in ("data/projects", "h5/datapacks"):
            for path in git_tree_paths(ref, prefix):
                if path.endswith(".json"):
                    selected[path] = (ref, state)

    cg_stems = {Path(path).name.removesuffix(".sb.json") for path in selected if path.endswith(".sb.json")}
    archives: dict[str, dict[str, Any]] = {}
    raw_by_key: dict[str, dict[str, Any]] = {}

    def upsert(path: str, ref: str, state: str, payload: dict[str, Any], *, kind: str) -> None:
        title = str(payload.get("title") or payload.get("boundStory") or Path(path).stem)
        when = str(payload.get("time") or payload.get("updated_at") or payload.get("created_at") or "")
        project_match = re.match(r"data/projects/([^/]+)/", path)
        project_id = project_match.group(1) if project_match else safe_name(payload.get("project_id") or title, "git-archive")
        dedupe = archive_dedupe_key(path, payload, kind=kind, project_id=project_id)
        record = archives.get(dedupe)
        has_cg = datapack_stem(title) in cg_stems or datapack_stem(payload.get("boundStory")) in cg_stems or kind == "storyboard"
        if record is None:
            archive_id = "git-" + hashlib.sha1(dedupe.encode("utf-8")).hexdigest()[:14]
            record = {
                "project_id": project_id,
                "run_id": archive_id,
                "title": title,
                "status": "archived",
                "phase": "CG / 分镜归档" if kind == "storyboard" else "生成记录归档",
                "progress": 100,
                "created_at": archive_time(when),
                "updated_at": archive_time(when),
                "error": "",
                "read_only": True,
                "git_state": state,
                "has_cg": has_cg,
                "source_ref": ref,
                "source_path": path,
                "source_paths": [path],
            }
            archives[dedupe] = record
        else:
            record["has_cg"] = bool(record.get("has_cg")) or has_cg
            candidate_time = archive_time(when)
            if candidate_time and candidate_time > str(record.get("updated_at") or ""):
                record["updated_at"] = candidate_time
            if candidate_time and (not record.get("created_at") or candidate_time < str(record["created_at"])):
                record["created_at"] = candidate_time
            if state == "已在 Git":
                record["git_state"] = state
            if kind == "storyboard":
                record["phase"] = "CG / 分镜归档"
            if path not in record["source_paths"]:
                record["source_paths"].append(path)
        raw_by_key[dedupe] = payload

    for path, (ref, state) in selected.items():
        # tasks/ 是后台运行时状态/日志，不是项目归档；Git 归档只认正式 runs/ 与 datapacks。
        match = re.match(r"data/projects/[^/]+/runs/[^/]+\.json$", path)
        is_pack = path.startswith("h5/datapacks/") and (path.endswith(".script.json") or path.endswith(".sb.json"))
        if not match and not is_pack:
            continue
        payload = git_blob_json(ref, path)
        if not payload:
            continue
        if path.endswith(".sb.json"):
            kind = "storyboard"
        elif path.endswith(".script.json"):
            kind = "script"
        else:
            kind = "task" if match and match.group(1) == "tasks" else "run"
        upsert(path, ref, state, payload, kind=kind)

    result = list(archives.values())
    if include_data:
        for key, record in archives.items():
            raw = dict(raw_by_key.get(key, {}))
            images = raw.pop("images", None)
            if isinstance(images, dict):
                raw["image_summary"] = {"count": len(images), "note": "图片数据已省略，避免详情页加载大段 base64"}
            record["archive_data"] = raw
    result.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return result


def all_tasks() -> list[dict[str, Any]]:
    local = [enrich_task(task) for task in RUNS.list_all_tasks()]
    seen = {(str(task.get("project_id")), str(task.get("run_id"))) for task in local}
    for task in git_archived_tasks():
        key = (str(task.get("project_id")), str(task.get("run_id")))
        if key not in seen:
            local.append(enrich_task(task))
            seen.add(key)
    local.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return local


def find_git_archived_task(project_id: str, run_id: str) -> dict[str, Any] | None:
    for task in git_archived_tasks(include_data=True):
        if str(task.get("project_id")) == project_id and str(task.get("run_id")) == run_id:
            return enrich_task(task)
    return None


def git_status() -> dict[str, Any]:
    branch = run_git("branch", "--show-current").stdout.strip()
    status = run_git("status", "--porcelain").stdout.splitlines()
    ahead = behind = 0
    counts = run_git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts.returncode == 0:
        parts = counts.stdout.strip().split()
        if len(parts) == 2:
            ahead, behind = map(int, parts)
    return {
        "branch": branch,
        "changed": len(status),
        "changes": status[:80],
        "ahead": ahead,
        "behind": behind,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "http://localhost:8000")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> Any:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json(200, {"ok": True, "service": "interactive-drama-backend", "api_version": 8, "execution_schema_version": execution_schema_version(), "system": platform.system(), "platform": platform.platform(), "capabilities": ["all_tasks", "git_archive_tasks", "task_delete", "git_task_delete", "project_delete", "production_resume", "execution_group_pipeline", "reference_bundle_gate", "precise_restart", "force_task_stop", "safe_git_sync"], "root": str(ROOT)})
            return
        if path == "/api/settings":
            self._json(200, {"ok": True, "settings": public_settings()})
            return
        if path == "/api/projects":
            self._json(200, {"ok": True, "projects": project_dirs()})
            return
        if path == "/api/runs":
            query = urllib.parse.parse_qs(parsed.query)
            project_id = safe_name((query.get("project_id") or [""])[0], "")
            if not project_id:
                self._json(400, {"ok": False, "error": "缺少 project_id"})
                return
            runs_dir = DATA_DIR / project_id / "runs"
            runs = []
            if runs_dir.is_dir():
                for item in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                    record = read_json(item, {})
                    runs.append({"id": item.stem, "title": record.get("title", ""), "time": record.get("time", ""), "pass": record.get("pass"), "attempts": record.get("attempts")})
            self._json(200, {"ok": True, "runs": runs})
            return
        if path == "/api/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            project_id = safe_name((query.get("project_id") or [""])[0], "")
            tasks = RUNS.list_tasks(project_id) if project_id else all_tasks()
            if project_id:
                for task in tasks:
                    task["project_id"] = project_id
                tasks = [enrich_task(task) for task in tasks]
            self._json(200, {"ok": True, "tasks": tasks})
            return
        task_match = re.fullmatch(r"/api/tasks/([^/]+)/([^/]+)", path)
        if task_match:
            project_id, run_id = map(urllib.parse.unquote, task_match.groups())
            record = RUNS.read(project_id, run_id) or find_git_archived_task(project_id, run_id)
            if record is None:
                self._json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._json(200, {"ok": True, "task": enrich_task(record)})
            return
        if path == "/api/git/status":
            self._json(200, {"ok": True, "git": git_status()})
            return
        super().do_GET()

    def do_PUT(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/settings":
            self.send_error(404, "Not Found")
            return
        try:
            payload = self._payload()
            current = load_settings()
            if current["config_locked"] and not payload.get("unlock"):
                raise RuntimeError("生产后台配置已锁定，请先解锁")
            if payload.get("unlock"):
                current["config_locked"] = False
            for key in ("provider", "base_url", "model", "validator_model", "image_model", "auto_commit", "auto_push"):
                if key in payload:
                    current[key] = payload[key]
            if payload.get("api_key"):
                current["api_key"] = str(payload["api_key"]).strip()
            if payload.get("clear_api_key"):
                current["api_key"] = ""
            if payload.get("lock"):
                if not current["api_key"] or not current["model"]:
                    raise RuntimeError("锁定前必须配置 API Key 和生成模型")
                current["config_locked"] = True
            atomic_json(SETTINGS_PATH, current)
            self._json(200, {"ok": True, "settings": public_settings()})
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            task_match = re.fullmatch(r"/api/tasks/([^/]+)/([^/]+)", path)
            if task_match:
                project_id, run_id = map(urllib.parse.unquote, task_match.groups())
                local_record = RUNS.read(project_id, run_id)
                archive_record = find_git_archived_task(project_id, run_id)
                record = local_record or archive_record
                if record is None:
                    self._json(404, {"ok": False, "error": "任务不存在"})
                    return
                if record and record.get("status") in {"running", "stopping"}:
                    self._json(409, {"ok": False, "error": "运行中的任务不能直接删除，请先停止并等待状态变为已停止"})
                    return
                query = urllib.parse.parse_qs(parsed.query)
                delete_git = (query.get("delete_git") or [""])[0] in {"1", "true", "yes"}
                title = str(record.get("title") or run_id)
                paths: set[str] = set(archive_record.get("source_paths") or []) if archive_record else set()
                task_path = RUNS.task_path(project_id, run_id)
                run_file = DATA_DIR / safe_name(project_id) / "runs" / f"{safe_name(run_id)}.json"
                for candidate in (task_path, run_file):
                    try:
                        paths.add(str(candidate.relative_to(ROOT)))
                    except ValueError:
                        pass
                stem = datapack_stem(title)
                for suffix in (".script.json", ".sb.json"):
                    candidate = DATAPACKS_DIR / f"{stem}{suffix}"
                    if candidate.is_file() or run_git("ls-files", "--error-unmatch", "--", str(candidate.relative_to(ROOT))).returncode == 0:
                        paths.add(str(candidate.relative_to(ROOT)))
                deleted = RUNS.delete(project_id, run_id)
                if run_file.is_file():
                    run_file.unlink()
                for rel in paths:
                    if not (rel.startswith("data/projects/") or rel.startswith("h5/datapacks/")):
                        continue
                    target = ROOT / rel
                    if target.is_file():
                        target.unlink()
                        deleted = True
                git_result = None
                if delete_git:
                    git_paths = [rel for rel in sorted(paths) if run_git("ls-files", "--error-unmatch", "--", rel).returncode == 0]
                    git_result = self.commit_paths(git_paths, f"删除任务 {title}") if git_paths else {"status": "clean", "message": "该任务没有 Git 文件"}
                    if git_result.get("status") == "committed":
                        push = run_git("push", "origin", "HEAD")
                        if push.returncode:
                            raise RuntimeError("删除已在本机提交，但推送 Git 失败：" + (push.stdout.strip() or "未知错误"))
                        git_result["push"] = push.stdout.strip() or "已推送删除提交"
                    elif archive_record:
                        raise RuntimeError("未能生成 Git 删除提交；归档仍会保留，请检查仓库状态")
                self._json(200, {"ok": True, "deleted": deleted, "run_id": safe_name(run_id), "git": git_result})
                return

            project_match = re.fullmatch(r"/api/projects/([^/]+)", path)
            if project_match:
                project_id = safe_name(urllib.parse.unquote(project_match.group(1)), "")
                target = DATA_DIR / project_id
                if not project_id or not target.is_dir():
                    self._json(404, {"ok": False, "error": "项目不存在"})
                    return
                active = [t for t in RUNS.list_tasks(project_id) if t.get("status") in {"running", "stopping"}]
                if active:
                    self._json(409, {"ok": False, "error": "项目仍有运行中的任务，请先停止任务再删除项目"})
                    return
                meta = read_json(target / "project.json", {})
                title = str(meta.get("title") or project_id)
                shutil.rmtree(target)
                removed_packs: list[str] = []
                if DATAPACKS_DIR.is_dir():
                    for pack_path in DATAPACKS_DIR.glob("*.script.json"):
                        pack = read_json(pack_path, {})
                        if str(pack.get("title") or "") == title:
                            pack_path.unlink()
                            removed_packs.append(pack_path.name)
                self._json(200, {"ok": True, "deleted": True, "project_id": project_id, "removed_datapacks": removed_packs})
                return

            run_match = re.fullmatch(r"/api/runs/([^/]+)/([^/]+)", path)
            if run_match:
                project_id, run_id = map(urllib.parse.unquote, run_match.groups())
                project_id = safe_name(project_id, "")
                run_id = safe_name(run_id, "")
                if not project_id or not run_id:
                    self._json(400, {"ok": False, "error": "缺少项目或记录标识"})
                    return
                target = DATA_DIR / project_id / "runs" / f"{run_id}.json"
                deleted = False
                if target.is_file():
                    target.unlink()
                    deleted = True
                self._json(200, {"ok": True, "deleted": deleted, "run_id": run_id})
                return

            pack_match = re.fullmatch(r"/api/datapacks/([^/]+)", path)
            if pack_match:
                name = safe_name(urllib.parse.unquote(pack_match.group(1)), "")
                if not name or not name.endswith(".script.json"):
                    self._json(400, {"ok": False, "error": "数据包名称无效"})
                    return
                target = DATAPACKS_DIR / name
                expected_time = (urllib.parse.parse_qs(parsed.query).get("expected_time") or [""])[0]
                if target.is_file() and expected_time:
                    pack = read_json(target, {})
                    if str(pack.get("time") or "") != expected_time:
                        self._json(200, {"ok": True, "deleted": False, "reason": "数据包已属于较新的同名记录"})
                        return
                deleted = False
                if target.is_file():
                    target.unlink()
                    deleted = True
                self._json(200, {"ok": True, "deleted": deleted, "name": name})
                return

            self.send_error(404, "Not Found")
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/__save_datapack":
                self.save_datapack()
            elif path == "/__git_sync_datapacks":
                self.git_commit("datapacks")
            elif path == "/api/projects":
                self.create_or_update_project()
            elif path == "/api/runs":
                self.save_run()
            elif re.fullmatch(r"/api/tasks/[^/]+/[^/]+/stop", path):
                _, _, _, project_id, run_id, _ = path.split("/")
                task = PRODUCTION.stop(urllib.parse.unquote(project_id), urllib.parse.unquote(run_id))
                self._json(200, {"ok": True, "status": "stopping", "task": task})
            elif re.fullmatch(r"/api/tasks/[^/]+/[^/]+/force-stop", path):
                _, _, _, project_id, run_id, _ = path.split("/")
                task = PRODUCTION.force_stop(urllib.parse.unquote(project_id), urllib.parse.unquote(run_id))
                self._json(200, {"ok": True, "status": "stopped", "task": task})
            elif re.fullmatch(r"/api/tasks/[^/]+/[^/]+/resume", path):
                _, _, _, project_id, run_id, _ = path.split("/")
                task = PRODUCTION.resume(urllib.parse.unquote(project_id), urllib.parse.unquote(run_id))
                self._json(202, {"ok": True, "status": "running", "task": task})
            elif path == "/api/production/start":
                self.start_production()
            elif path == "/api/system/restart":
                self.restart_backend()
            elif path == "/api/git/commit":
                self.git_commit("projects")
            elif path == "/api/git/push":
                self.git_push()
            elif path == "/api/git/sync":
                self.git_sync()
            elif path == "/api/llm/chat":
                self.llm_chat()
            elif path == "/api/llm/chat/stream":
                self.llm_chat_stream()
            else:
                self.send_error(404, "Not Found")
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})

    def start_production(self) -> None:
        payload = self._payload()
        project_id = safe_name(payload.get("project_id"), "")
        run_id = safe_name(payload.get("run_id"), "")
        if not project_id or not run_id:
            self._json(400, {"ok": False, "error": "缺少 project_id 或 run_id"})
            return
        title = str(payload.get("title") or project_id)
        task = PRODUCTION.start(project_id, run_id, title, payload)
        self._json(202, {"ok": True, "task": task})

    def restart_backend(self) -> None:
        port = int(self.server.server_address[1])
        system = platform.system()
        self._json(202, {"ok": True, "status": "restarting", "system": system, "port": port})
        threading.Thread(target=restart_server_process, args=(self.server, port), name="backend-restart", daemon=False).start()

    def save_datapack(self) -> None:
        payload = self._payload()
        files = payload.get("files")
        if files is None:
            name = safe_name(payload.get("name") or payload.get("title"), "datapack")
            if not name.endswith(".json"):
                name += ".sb.json"
            files = [{"name": name, "content": payload.get("content", payload)}]
        DATAPACKS_DIR.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for item in files:
            name = safe_name(item.get("name"), "datapack.sb.json")
            content = item.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            atomic_write(DATAPACKS_DIR / name, text)
            written.append(name)
        self._json(200, {"ok": True, "written": written, "dir": "h5/datapacks/"})

    def create_or_update_project(self) -> None:
        payload = self._payload()
        title = str(payload.get("title") or "未命名项目").strip()
        project_id = safe_name(payload.get("id") or title, "project")
        target = DATA_DIR / project_id
        target.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now().isoformat(timespec="seconds")
        meta = read_json(target / "project.json", {})
        meta.update({"id": project_id, "title": title, "stage": payload.get("stage", meta.get("stage", "已保存")), "updated_at": now})
        atomic_json(target / "project.json", meta)
        if "content" in payload:
            content = payload["content"]
            atomic_write(target / "content.json", content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2))
        settings = load_settings()
        commit_result = None
        if settings["auto_commit"]:
            commit_result = self.commit_paths([str(target.relative_to(ROOT))], f"保存项目 {title}")
            if settings["auto_push"] and commit_result.get("status") == "committed":
                push = run_git("push", "origin", "HEAD")
                commit_result["push"] = push.stdout.strip()
        self._json(200, {"ok": True, "project": meta, "git": commit_result})

    def save_run(self) -> None:
        payload = self._payload()
        title = str(payload.get("title") or "未命名项目").strip()
        project_id = safe_name(payload.get("project_id") or title, "project")
        run = payload.get("run")
        if not isinstance(run, dict):
            raise RuntimeError("run 必须是对象")
        target = DATA_DIR / project_id
        runs_dir = target / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        raw_id = payload.get("run_id") or run.get("runId") or run.get("time") or dt.datetime.now().isoformat()
        run_id = safe_name(raw_id, dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        atomic_json(runs_dir / f"{run_id}.json", run)
        now = dt.datetime.now().isoformat(timespec="seconds")
        meta = read_json(target / "project.json", {})
        meta.update({"id": project_id, "title": title, "stage": payload.get("stage", "生成记录已保存"), "updated_at": now, "latest_run": run_id})
        atomic_json(target / "project.json", meta)
        settings = load_settings()
        commit_result = None
        if settings["auto_commit"]:
            # 只提交正式项目元数据和本次 run；tasks/ 是运行时状态/日志，禁止顺手带入 Git。
            commit_result = self.commit_paths([
                str((target / "project.json").relative_to(ROOT)),
                str((runs_dir / f"{run_id}.json").relative_to(ROOT)),
            ], f"保存生成记录 {title}")
            if settings["auto_push"] and commit_result.get("status") == "committed":
                push = run_git("push", "origin", "HEAD")
                if push.returncode:
                    commit_result["push_error"] = push.stdout.strip()
                else:
                    commit_result["push"] = push.stdout.strip()
        self._json(200, {"ok": True, "project": meta, "run_id": run_id, "git": commit_result})

    def commit_paths(self, paths: list[str], message: str) -> dict[str, Any]:
        active_paths = [path for path in paths if run_git("status", "--porcelain", "--", path).stdout.strip()]
        if not active_paths:
            return {"status": "clean", "message": "没有新的本机改动。"}
        add = run_git("add", "--", *active_paths)
        if add.returncode:
            raise RuntimeError(add.stdout.strip() or "无法暂存项目文件")
        commit = run_git("commit", "--only", "-m", message, "--", *active_paths)
        if commit.returncode:
            raise RuntimeError(commit.stdout.strip() or "无法创建本地提交")
        return {"status": "committed", "message": commit.stdout.strip()}

    def git_commit(self, scope: str) -> None:
        if scope == "datapacks":
            paths = ["h5/datapacks"]
            message = "保存工作台数据包 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        else:
            paths = ["data/projects"]
            message = "保存工作台项目 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        self._json(200, {"ok": True, **self.commit_paths(paths, message)})

    def git_push(self) -> None:
        push = run_git("push", "origin", "HEAD")
        if push.returncode:
            raise RuntimeError(push.stdout.strip() or "推送失败")
        self._json(200, {"ok": True, "message": push.stdout.strip() or "已推送到远端。"})

    def git_sync(self) -> None:
        fetch = run_git("fetch", "origin")
        if fetch.returncode:
            raise RuntimeError(fetch.stdout.strip() or "无法获取远端状态")
        commit = self.commit_paths(["data/projects", "h5/datapacks"], "同步工作台项目 " + dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        before = git_status()
        merge_result = None
        if before["behind"] > 0:
            merge = run_git("merge", "--no-edit", "@{upstream}")
            if merge.returncode:
                conflicts = [line.strip() for line in run_git("diff", "--name-only", "--diff-filter=U").stdout.splitlines() if line.strip()]
                if not conflicts:
                    run_git("merge", "--abort")
                    raise RuntimeError("远端有更新，但当前工作区无法安全合并：" + (merge.stdout.strip() or "未知原因"))
                try:
                    resolved = self.resolve_git_conflicts(conflicts)
                    merge_commit = run_git("commit", "--no-edit")
                    if merge_commit.returncode:
                        raise RuntimeError(merge_commit.stdout.strip() or "冲突解决后无法创建合并提交")
                    merge_result = {"status": "model-resolved", "files": resolved, "message": merge_commit.stdout.strip()}
                except Exception as exc:  # noqa: BLE001
                    run_git("merge", "--abort")
                    raise RuntimeError(f"有冲突，自动解决失败，提交失败：{exc}") from exc
            else:
                merge_result = {"status": "merged", "message": merge.stdout.strip()}
        after_commit = git_status()
        if after_commit["ahead"] == 0:
            self._json(200, {"ok": True, "message": "远端已是最新，项目数据也没有新的改动。", "commit": commit, "merge": merge_result, "git": after_commit})
            return
        push = run_git("push", "origin", "HEAD")
        if push.returncode:
            raise RuntimeError("本地提交已创建，但推送失败：" + (push.stdout.strip() or "未知错误"))
        final = git_status()
        self._json(200, {"ok": True, "message": "已检查远端、提交项目数据并推送到 Git。", "commit": commit, "merge": merge_result, "push": push.stdout.strip(), "git": final})

    def resolve_git_conflicts(self, paths: list[str]) -> list[str]:
        settings = load_settings()
        if not settings.get("config_locked") or not settings.get("api_key") or not settings.get("model"):
            raise RuntimeError("生产后台没有已锁定的模型和 Key，无法调用 OpenRouter 解决冲突")
        allowed = {".json", ".txt", ".md", ".markdown", ".html", ".css", ".js", ".py", ".yml", ".yaml"}
        resolved: list[str] = []
        for rel in paths:
            target = ROOT / rel
            if target.resolve().is_relative_to(RUNTIME_DIR.resolve()) or target.suffix.lower() not in allowed:
                raise RuntimeError(f"冲突文件 {rel} 不是可安全自动合并的文本文件")
            versions = []
            for stage in (1, 2, 3):
                result = run_git("show", f":{stage}:{rel}")
                versions.append(result.stdout if result.returncode == 0 else "")
            if sum(len(text) for text in versions) > 240_000:
                raise RuntimeError(f"冲突文件 {rel} 过大，未调用模型以避免额度失控")
            prompt = (
                "你是 Git 三方合并器。请合并同一文件的 BASE、OURS、THEIRS。"
                "保留双方所有不冲突修改；冲突时优先保持当前工作台功能完整和数据不丢失。"
                "不得解释，不得使用 Markdown 代码围栏，只输出 ===MERGED=== 后的完整最终文件。\n\n"
                f"文件：{rel}\n===BASE===\n{versions[0]}\n===OURS===\n{versions[1]}\n===THEIRS===\n{versions[2]}\n"
            )
            req = self._openrouter_request(
                {"temperature": 0, "max_tokens": 20000, "messages": [{"role": "system", "content": "精确执行 Git 三方合并，输出完整文件。"}, {"role": "user", "content": prompt}]},
                stream=False,
            )
            try:
                with urllib.request.urlopen(req, timeout=360) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                quota = "额度不足或限额" if exc.code in {402, 429} else "模型请求失败"
                raise RuntimeError(f"{quota}（HTTP {exc.code}）：{detail[:500]}") from exc
            try:
                text = str(data["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("模型没有返回可用的冲突合并结果") from exc
            marker = "===MERGED==="
            merged = text.split(marker, 1)[1].lstrip("\r\n") if marker in text else text.strip()
            if target.suffix.lower() == ".json":
                try:
                    json.loads(merged)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"模型合并后的 {rel} 不是合法 JSON：{exc}") from exc
            atomic_write(target, merged)
            added = run_git("add", "--", rel)
            if added.returncode:
                raise RuntimeError(added.stdout.strip() or f"无法暂存已解决的冲突 {rel}")
            resolved.append(rel)
        remaining = run_git("diff", "--name-only", "--diff-filter=U").stdout.strip()
        if remaining:
            raise RuntimeError("仍有未解决冲突：" + remaining.replace("\n", "、"))
        return resolved

    def _openrouter_request(self, payload: dict[str, Any], stream: bool) -> urllib.request.Request:
        settings = load_settings()
        if not settings["config_locked"]:
            raise RuntimeError("生产后台配置尚未锁定")
        if settings["provider"] != "openrouter":
            raise RuntimeError("当前仅启用 OpenRouter provider")
        api_key = settings["api_key"]
        if not api_key:
            raise RuntimeError("请先在生产后台配置 OpenRouter API Key")
        request_body = dict(payload)
        use_image_model = bool(request_body.pop("_use_image_model", False))
        use_validator_model = bool(request_body.pop("_use_validator_model", False))
        selected_model = settings["model"]
        if use_image_model:
            selected_model = settings["image_model"]
        elif use_validator_model:
            selected_model = settings["validator_model"] or settings["model"]
        request_body.setdefault("model", selected_model)
        if not request_body.get("model"):
            raise RuntimeError("请先配置默认生成模型")
        request_body["stream"] = stream
        if stream:
            request_body.setdefault("usage", {"include": True})
        url = settings["base_url"].rstrip("/") + "/chat/completions"
        return urllib.request.Request(
            url,
            data=json.dumps(request_body).encode("utf-8"),
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            method="POST",
        )

    def llm_chat(self) -> None:
        payload = self._payload()
        req = self._openrouter_request(payload, stream=False)
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenRouter 请求失败：{exc.code} {detail}") from exc
        self._json(200, {"ok": True, "data": data})

    def llm_chat_stream(self) -> None:
        payload = self._payload()
        task_meta = payload.pop("_task", {}) if isinstance(payload.get("_task"), dict) else {}
        project_id = safe_name(task_meta.get("project_id"), "")
        run_id = safe_name(task_meta.get("run_id"), "")
        phase = str(task_meta.get("phase") or "generation")
        title = str(task_meta.get("title") or project_id or "生成任务")
        if project_id and run_id:
            RUNS.create(project_id, run_id, title, phase, {"model": payload.get("model", ""), "message_count": len(payload.get("messages", [])), "attempt": task_meta.get("attempt", 1)})
        req = self._openrouter_request(payload, stream=True)
        try:
            response = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if project_id and run_id:
                RUNS.update(project_id, run_id, status="failed", error=f"{exc.code} {detail}")
            raise RuntimeError(f"OpenRouter 请求失败：{exc.code} {detail}") from exc
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        received = 0
        response_text = ""
        usage: Any = None
        try:
            while True:
                if project_id and run_id and RUNS.should_stop(project_id, run_id):
                    RUNS.update(project_id, run_id, status="stopped", progress=min(99, received // 120))
                    break
                line = response.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded.startswith("data:"):
                    raw = decoded[5:].strip()
                    if raw and raw != "[DONE]":
                        try:
                            event = json.loads(raw)
                            delta = event.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                            response_text += delta
                            received += len(delta)
                            if event.get("usage"):
                                usage = event["usage"]
                            if project_id and run_id and delta:
                                RUNS.update(project_id, run_id, response_text=response_text, usage=usage, progress=min(95, max(1, received // 120)))
                        except (json.JSONDecodeError, IndexError, TypeError):
                            pass
        except (BrokenPipeError, ConnectionResetError):
            if project_id and run_id:
                RUNS.update(project_id, run_id, status="disconnected", response_text=response_text, usage=usage)
        finally:
            response.close()
        if project_id and run_id and not RUNS.should_stop(project_id, run_id):
            RUNS.update(project_id, run_id, status="completed", progress=100, response_text=response_text, usage=usage)

    def log_message(self, _format: str, *args: Any) -> None:
        return


def restart_server_process(server: ThreadingHTTPServer, port: int) -> None:
    """Restart exactly this workspace backend with the same interpreter and port."""
    time.sleep(0.35)
    server.shutdown()
    server.server_close()
    command = [sys.executable, str(ROOT / "serve.py"), str(port)]
    options: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": os.environ.copy(),
    }
    if platform.system() == "Windows":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)


def run(port: int = 8000) -> None:
    DATAPACKS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving {ROOT} at http://localhost:{port}/")
    print("backend settings and project storage enabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import sys

    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
