#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
互动影视节奏工作台 · 本地前台服务器（带数据包保存接口）
- 静态文件服务（同 python -m http.server）
- 额外提供 POST /__save_datapack ：把 H5 导出的数据包直接写进 h5/datapacks/，
  免去浏览器 showDirectoryPicker 手动选文件夹。拉下仓库后数据就在 h5/datapacks/。
仅本机访问，关窗即停。
"""
import json, os, re, sys, subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATAPACKS_DIR = os.path.join(ROOT, "h5", "datapacks")

def run_git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )

def safe_name(s):
    s = str(s or "rec")
    s = re.sub(r"[^\w\u4e00-\u9fff\-.]", "_", s)
    return s[:80] or "rec"

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # 本地工作台是频繁直接改 HTML 的单文件应用；禁用浏览器缓存，保证重启后必定看到最新界面。
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/__git_sync_datapacks":
            self.sync_datapacks()
            return
        if path != "/__save_datapack":
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            # 支持单个 pack 或 {files:[{name,content}]} 批量
            files = payload.get("files")
            if files is None:
                name = safe_name(payload.get("name") or payload.get("title") or "datapack")
                if not name.endswith(".json"):
                    name += ".sb.json"
                files = [{"name": name, "content": payload.get("content", payload)}]
            os.makedirs(DATAPACKS_DIR, exist_ok=True)
            written = []
            for f in files:
                nm = safe_name(f.get("name") or "datapack.sb.json")
                content = f.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                # 连续补图时也不会让读取端碰到写到一半的 JSON。
                target = os.path.join(DATAPACKS_DIR, nm)
                temp = target + ".tmp"
                with open(temp, "w", encoding="utf-8") as fp:
                    fp.write(text)
                os.replace(temp, target)
                written.append(nm)
            body = json.dumps({"ok": True, "written": written, "dir": "h5/datapacks/"}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def sync_datapacks(self):
        """只提交并推送数据包，绝不带上工作区里的其他修改。"""
        try:
            changed = run_git("status", "--porcelain", "--", "h5/datapacks").stdout.strip()
            if not changed:
                result = {"ok": True, "status": "clean", "message": "数据包已经在仓库中，没有需要推送的更新。"}
            else:
                add = run_git("add", "--", "h5/datapacks")
                if add.returncode:
                    raise RuntimeError(add.stdout.strip() or "无法暂存数据包")
                stamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
                # --only 会忽略用户已经暂存、但不属于数据包的任何工作。
                commit = run_git("commit", "--only", "-m", "保存工作台数据包 " + stamp, "--", "h5/datapacks")
                if commit.returncode:
                    raise RuntimeError(commit.stdout.strip() or "无法提交数据包")
                push = run_git("push", "origin", "HEAD")
                if push.returncode:
                    raise RuntimeError("数据包已保存在本机提交中，但推送失败：\n" + push.stdout.strip())
                result = {"ok": True, "status": "pushed", "message": "数据包已提交并推送到 GitHub。", "commit": commit.stdout.strip()}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # 降低日志噪音
    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(DATAPACKS_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("serving %s at http://localhost:%d/ (datapacks 直写已开启)" % (ROOT, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
