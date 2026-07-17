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
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from backend.run_store import RunStore

ROOT = Path(__file__).resolve().parent.parent
H5_DIR = ROOT / "h5"
DATAPACKS_DIR = H5_DIR / "datapacks"
DATA_DIR = ROOT / "data" / "projects"
RUNTIME_DIR = ROOT / ".runtime"
SETTINGS_PATH = RUNTIME_DIR / "settings.json"
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
        "api_key": data.get("api_key", ""),
        "auto_commit": bool(data.get("auto_commit", True)),
        "auto_push": bool(data.get("auto_push", False)),
    }


def public_settings() -> dict[str, Any]:
    data = load_settings()
    return {
        "provider": data["provider"],
        "base_url": data["base_url"],
        "model": data["model"],
        "validator_model": data["validator_model"],
        "api_key_configured": bool(data["api_key"]),
        "auto_commit": data["auto_commit"],
        "auto_push": data["auto_push"],
    }


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
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
            self._json(200, {"ok": True, "service": "interactive-drama-backend", "root": str(ROOT)})
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
            if not project_id:
                self._json(400, {"ok": False, "error": "缺少 project_id"})
                return
            self._json(200, {"ok": True, "tasks": RUNS.list_tasks(project_id)})
            return
        task_match = re.fullmatch(r"/api/tasks/([^/]+)/([^/]+)", path)
        if task_match:
            project_id, run_id = map(urllib.parse.unquote, task_match.groups())
            record = RUNS.read(project_id, run_id)
            if record is None:
                self._json(404, {"ok": False, "error": "任务不存在"})
            else:
                self._json(200, {"ok": True, "task": record})
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
            for key in ("provider", "base_url", "model", "validator_model", "auto_commit", "auto_push"):
                if key in payload:
                    current[key] = payload[key]
            if payload.get("api_key"):
                current["api_key"] = str(payload["api_key"]).strip()
            if payload.get("clear_api_key"):
                current["api_key"] = ""
            atomic_json(SETTINGS_PATH, current)
            self._json(200, {"ok": True, "settings": public_settings()})
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(exc)})

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
                RUNS.request_stop(urllib.parse.unquote(project_id), urllib.parse.unquote(run_id))
                self._json(200, {"ok": True, "status": "stopping"})
            elif path == "/api/git/commit":
                self.git_commit("projects")
            elif path == "/api/git/push":
                self.git_push()
            elif path == "/api/llm/chat":
                self.llm_chat()
            elif path == "/api/llm/chat/stream":
                self.llm_chat_stream()
            else:
                self.send_error(404, "Not Found")
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": str(exc)})

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
            commit_result = self.commit_paths([str(target.relative_to(ROOT))], f"保存生成记录 {title}")
            if settings["auto_push"] and commit_result.get("status") == "committed":
                push = run_git("push", "origin", "HEAD")
                if push.returncode:
                    commit_result["push_error"] = push.stdout.strip()
                else:
                    commit_result["push"] = push.stdout.strip()
        self._json(200, {"ok": True, "project": meta, "run_id": run_id, "git": commit_result})

    def commit_paths(self, paths: list[str], message: str) -> dict[str, Any]:
        changed = run_git("status", "--porcelain", "--", *paths).stdout.strip()
        if not changed:
            return {"status": "clean", "message": "没有新的本机改动。"}
        add = run_git("add", "--", *paths)
        if add.returncode:
            raise RuntimeError(add.stdout.strip() or "无法暂存项目文件")
        commit = run_git("commit", "--only", "-m", message, "--", *paths)
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

    def _openrouter_request(self, payload: dict[str, Any], stream: bool) -> urllib.request.Request:
        settings = load_settings()
        if settings["provider"] != "openrouter":
            raise RuntimeError("当前仅启用 OpenRouter provider")
        api_key = settings["api_key"]
        if not api_key:
            raise RuntimeError("请先在生产后台配置 OpenRouter API Key")
        request_body = dict(payload)
        request_body.setdefault("model", settings["model"])
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


if __name__ == "__main__":
    import sys

    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
