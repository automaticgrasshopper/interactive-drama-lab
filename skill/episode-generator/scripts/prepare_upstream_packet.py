#!/usr/bin/env python3
"""Build the only allowed semantic-reading packet from raw episode input."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


INPUT_HEADINGS = ("游戏企划", "角色描述", "场景描述", "道具描述")
ROLE_FIELDS = ("角色名称", "身份定位", "人物驱动力", "人物弧光", "外貌特征", "人物关系")
SCENE_FIELDS = ("场景名称", "场景描述")
PROP_FIELDS = ("道具名称", "道具形象描述", "道具意义")


def sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    names = tuple(match.group(1) for match in matches)
    if names != INPUT_HEADINGS:
        raise SystemExit("input.md 必须且只能按顺序包含游戏企划、角色描述、场景描述、道具描述")
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if not value:
            raise SystemExit(f"input.md 的{match.group(1)}缺少实际描述")
        result[match.group(1)] = value
    return result


def value(block: str, field: str) -> str:
    match = re.search(
        rf"(?m)^\s*(?:[-*]\s*)?{re.escape(field)}\s*[:：]\s*(\S.*)$",
        block,
    )
    if not match:
        raise SystemExit(f"缺少字段：{field}")
    return match.group(1).strip()


def records(text: str, name_field: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
    starts = list(re.finditer(
        rf"(?m)^\s*(?:[-*]\s*)?{re.escape(name_field)}\s*[:：]\s*(\S.*)$",
        text,
    ))
    if not starts:
        if name_field == "道具名称" and re.fullmatch(r"\s*(?:无|0|无关键道具)[。.]?\s*", text):
            return []
        raise SystemExit(f"{name_field}未解析到任何正式记录")
    result = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start():end]
        result.append({field: value(block, field) for field in fields})
    return result


def render_records(title: str, items: list[dict[str, str]], fields: tuple[str, ...]) -> str:
    lines = [f"## {title}", ""]
    if not items:
        return "\n".join(lines + ["无", ""])
    for item in items:
        lines += [f"### {item[fields[0]]}", ""]
        lines += [f"- {field}：{item[field]}" for field in fields]
        lines.append("")
    return "\n".join(lines)


def build(input_text: str) -> str:
    source = sections(input_text)
    roles = records(source["角色描述"], "角色名称", ROLE_FIELDS)
    scenes = records(source["场景描述"], "场景名称", SCENE_FIELDS)
    props = records(source["道具描述"], "道具名称", PROP_FIELDS)
    chunks = [
        "# 上游转译阅读包\n",
        "## 游戏企划\n\n" + source["游戏企划"].strip() + "\n",
        render_records("人物白名单", roles, ROLE_FIELDS),
        render_records("场景白名单", scenes, SCENE_FIELDS),
        render_records("道具白名单", props, PROP_FIELDS),
    ]
    return "\n".join(chunk.rstrip() for chunk in chunks).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cache_root", type=Path)
    args = parser.parse_args()
    input_path = args.cache_root / "input.md"
    if not input_path.is_file():
        raise SystemExit(f"缺少 {input_path}")
    output = args.cache_root / "upstream-packet.md"
    output.write_text(build(input_path.read_text(encoding="utf-8")), encoding="utf-8")
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
