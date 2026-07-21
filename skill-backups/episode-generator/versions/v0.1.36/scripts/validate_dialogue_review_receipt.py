#!/usr/bin/env python3
"""Validate that dialogue review covers the current screenplay and required references."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from prepare_dialogue_review import build_dialogue_packet


PROFILE_CASE = {
    "ordinary": None,
    "suspense": None,
    "confrontation": "case-confrontation-liuxi.md",
    "power-reversal": "case-power-reversal-minguo.md",
    "evidence-verification": "case-evidence-verification.md",
}
REQUIRED_CHECKS = {
    "referents",
    "turn_response",
    "information_permission",
    "relationship_distance",
    "character_voice",
    "plain_chinese",
    "functional_information",
    "profile_progression",
}


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 不是 JSON 对象")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=tuple(PROFILE_CASE))
    args = parser.parse_args()

    current = build_dialogue_packet(args.script.read_text(encoding="utf-8"))
    packet = load_json(args.packet)
    review = load_json(args.review)
    issues: list[str] = []

    for key in (
        "packet_version",
        "script_sha256",
        "dialogue_sha256",
        "dialogue_count",
        "dialogue_ids",
        "coverage",
    ):
        if packet.get(key) != current.get(key):
            issues.append(f"台词包与当前正文不一致：{key}")

    for key in ("script_sha256", "dialogue_sha256", "dialogue_count", "coverage"):
        if review.get(key) != current.get(key):
            issues.append(f"复核凭证与当前正文不一致：{key}")

    if current["coverage"] != "COMPLETE":
        issues.append("台词抽取覆盖不是 COMPLETE")
    if review.get("profile") != args.profile:
        issues.append("复核场型与命令参数不一致")
    if review.get("issues") != []:
        issues.append("人物交流仍有未解决问题")

    references = {str(item) for item in review.get("references") or []}
    expected_references = {"chinese-dialogue-craft.md"}
    case = PROFILE_CASE[args.profile]
    if case:
        expected_references.add(case)
    if references != expected_references:
        issues.append(f"复核 reference 错误：{sorted(references)}")

    covered = {str(item) for item in review.get("covered_checks") or []}
    missing = REQUIRED_CHECKS - covered
    if missing:
        issues.append(f"人物交流检查覆盖不完整：{sorted(missing)}")

    if issues:
        print("FAIL")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print(
        "PASS: dialogue review receipt matches current script; "
        f"{current['dialogue_count']} lines, COMPLETE, profile={args.profile}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
