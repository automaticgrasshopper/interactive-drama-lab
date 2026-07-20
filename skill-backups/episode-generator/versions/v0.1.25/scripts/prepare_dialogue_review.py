#!/usr/bin/env python3
"""Build a compact, complete, numbered packet for whole-scene dialogue review."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SCENE_RE = re.compile(r"^【\s*(场[^·】]+?)\s*·")
DIALOGUE_RE = re.compile(r"^([^：:\n]{1,30})[：:]\s*(\S.*)$")
EXCLUDED_SPEAKERS = {"单集梗概", "出场", "选择", "互动选项"}
QUESTION_RE = re.compile(r"[？?]|(?:吗|呢|吧|怎么|为什么|哪|谁|什么|是否|有没有)[？?]?$" )
COMMAND_RE = re.compile(r"^(?:先|别|不要|给我|把|请|立刻|马上|停|放下|告诉|必须|跟我|过来|出去)")
CONNECTOR_RE = re.compile(r"^(?:所以|但是|但|可|那|因为|既然|不过|可是|否则)")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_dialogue_packet(script: str) -> dict[str, Any]:
    scene_index = 0
    line_index = 0
    current_scene = "未标注场次"
    numbered_lines: list[str] = []
    entries: list[dict[str, str]] = []

    for raw_line in str(script or "").strip().splitlines():
        scene = SCENE_RE.match(raw_line.strip())
        if scene:
            scene_index += 1
            line_index = 0
            current_scene = scene.group(1).strip()
            numbered_lines.append(raw_line)
            continue
        match = DIALOGUE_RE.match(raw_line.strip())
        if (
            match
            and match.group(1).strip() not in EXCLUDED_SPEAKERS
            and not raw_line.strip().startswith(("△", "#", "【", "-"))
        ):
            if scene_index == 0:
                scene_index = 1
            line_index += 1
            line_id = f"S{scene_index:02d}-L{line_index:02d}"
            speaker, text = match.group(1).strip(), match.group(2).strip()
            entries.append(
                {"id": line_id, "scene": current_scene, "speaker": speaker, "text": text}
            )
            indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
            numbered_lines.append(f"{indent}[{line_id}] {raw_line.strip()}")
        else:
            numbered_lines.append(raw_line)

    candidates: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        signals: list[str] = []
        text = entry["text"]
        if QUESTION_RE.search(text):
            signals.append("question")
        if COMMAND_RE.search(text):
            signals.append("request_or_command")
        if CONNECTOR_RE.search(text):
            signals.append("causal_or_turn_connector")
        if len(re.sub(r"\s+", "", text)) >= 36 or len(re.findall(r"[，；。！？!?]", text)) >= 4:
            signals.append("possible_overload")
        if signals:
            candidates.append(
                {
                    "id": entry["id"],
                    "signals": signals,
                    "previous": entries[index - 1]["id"] if index else None,
                    "next": entries[index + 1]["id"] if index + 1 < len(entries) else None,
                }
            )

    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return {
        "packet_version": "dialogue-review-v1",
        "script_sha256": _sha(str(script or "").strip()),
        "dialogue_sha256": _sha(canonical),
        "dialogue_count": len(entries),
        "dialogue_ids": [entry["id"] for entry in entries],
        "numbered_script": "\n".join(numbered_lines),
        "turn_candidates": candidates,
        "coverage": "COMPLETE",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("script_file", nargs="?", type=Path)
    args = parser.parse_args()
    if args.script_file:
        script = args.script_file.read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            script = str(payload.get("script") or "") if isinstance(payload, dict) else str(payload)
        except json.JSONDecodeError:
            script = raw
    print(json.dumps(build_dialogue_packet(script), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
