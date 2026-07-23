#!/usr/bin/env python3
"""Prepare and verify mandatory per-episode quality-review receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_NAME = "episode-quality-review.md"
REFERENCE_PATH = SKILL_ROOT / "references" / REFERENCE_NAME
MANIFEST_PATH = SKILL_ROOT / "reference-manifest.json"
REVIEW_DIR = "quality-reviews"
PLAIN_LANGUAGE_CHECK = "普通话表达与叙述可读"
REQUIRED_CHECKS = [
    "事实来源与知情",
    "首次出现与必要交代",
    "因果相邻与可见后果",
    "话茬与现场目的",
    "口语组织与反过度压缩",
    PLAIN_LANGUAGE_CHECK,
    "结尾画面与下一入口",
]
COMPREHENSION_FIELDS = {
    "character_task": "人物任务",
    "trigger_cost": "触发代价",
    "action_result": "行动结果",
    "next_entry": "下一入口",
}
COMPREHENSION_FAILURE_MARKERS = ("正文不清楚", "无法判断", "无法确认", "信息不足", "未说明")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_episode(cache_root: Path, episode_id: str) -> tuple[Path, str]:
    if not re.fullmatch(r"episode-\d{3}", episode_id):
        raise ValueError(f"非法分集编号：{episode_id}")
    path = cache_root / "episodes" / f"{episode_id}.md"
    if not path.is_file():
        raise ValueError(f"缺少分集正文：{episode_id}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"分集正文为空：{episode_id}")
    return path, text


def current_reference() -> tuple[str, str, str]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    version = str(manifest.get("skill_version") or "")
    names = (manifest.get("phases") or {}).get("episode-quality-review") or []
    if names != [REFERENCE_NAME]:
        raise ValueError("episode-quality-review 阶段必须且只能装载独立复检 reference")
    text = REFERENCE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("逐集独立复检 reference 为空")
    return version, text, sha256(text)


def build_packet(cache_root: Path, episode_id: str) -> dict[str, Any]:
    path, script = read_episode(cache_root, episode_id)
    version, reference, reference_sha = current_reference()
    return {
        "packet_version": "episode-quality-gate-v1",
        "skill_version": version,
        "episode_id": episode_id,
        "script_path": str(path),
        "script_sha256": sha256(script),
        "reference_name": REFERENCE_NAME,
        "reference_sha256": reference_sha,
        "required_checks": REQUIRED_CHECKS,
        "reference": reference,
        "script": script,
    }


def validate_review(packet: dict[str, Any], review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    comprehension = review.get("comprehension")
    if not isinstance(comprehension, dict):
        errors.append("复检缺少结构化理解门")
        comprehension = {}
    for key, label in COMPREHENSION_FIELDS.items():
        item = comprehension.get(key)
        if not isinstance(item, dict):
            errors.append(f"理解门缺少：{label}")
            continue
        answer = str(item.get("answer") or "").strip()
        proof = str(item.get("proof") or "").strip()
        if len(answer) < 4 or len(proof) < 6:
            errors.append(f"理解门证据不完整：{label}")
        elif any(marker in answer or marker in proof for marker in COMPREHENSION_FAILURE_MARKERS):
            errors.append(f"理解门未通过：{label}")
    covered = [str(item) for item in review.get("covered_checks") or []]
    missing = [item for item in REQUIRED_CHECKS if item not in covered]
    if missing:
        errors.append("复检覆盖不完整：" + "、".join(missing))
    issues = review.get("issues")
    if issues != []:
        errors.append("复检仍有未解决问题")
    evidence = review.get("evidence")
    if not isinstance(evidence, list):
        errors.append("复检缺少逐项证据")
        evidence = []
    evidence_by_check: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if isinstance(item, dict) and str(item.get("check") or "") in REQUIRED_CHECKS:
            evidence_by_check[str(item["check"])] = item
    for check in REQUIRED_CHECKS:
        item = evidence_by_check.get(check) or {}
        if check == PLAIN_LANGUAGE_CHECK:
            fields = ("dialogue_location", "dialogue_proof", "narration_location", "narration_proof")
            if any(len(str(item.get(field) or "").strip()) < (2 if field.endswith("location") else 6) for field in fields):
                errors.append(f"复检证据不完整：{check}/台词与描述必须分别举证")
        elif len(str(item.get("location") or "").strip()) < 2 or len(str(item.get("proof") or "").strip()) < 6:
            errors.append(f"复检证据不完整：{check}")
    if review.get("script_sha256") != packet["script_sha256"]:
        errors.append("复检正文指纹与当前复检包不一致")
    if review.get("reference_sha256") != packet["reference_sha256"]:
        errors.append("复检 reference 指纹与当前复检包不一致")
    return errors


def receipt_path(cache_root: Path, episode_id: str) -> Path:
    return cache_root / REVIEW_DIR / f"{episode_id}.json"


def seal(cache_root: Path, episode_id: str, review_path: Path) -> Path:
    packet = build_packet(cache_root, episode_id)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    errors = validate_review(packet, review)
    if errors:
        raise ValueError("；".join(errors))
    receipt = {
        "packet_version": packet["packet_version"],
        "skill_version": packet["skill_version"],
        "episode_id": episode_id,
        "script_sha256": packet["script_sha256"],
        "reference_name": packet["reference_name"],
        "reference_sha256": packet["reference_sha256"],
        "required_checks": REQUIRED_CHECKS,
        "comprehension": review["comprehension"],
        "covered_checks": review["covered_checks"],
        "evidence": review["evidence"],
        "issues": [],
        "note": str(review.get("note") or ""),
    }
    output = receipt_path(cache_root, episode_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def verify_project(cache_root: Path) -> list[str]:
    errors: list[str] = []
    episode_ids = sorted(path.stem for path in (cache_root / "episodes").glob("episode-*.md"))
    if not episode_ids:
        return ["没有可复检的分集正文"]
    for episode_id in episode_ids:
        packet = build_packet(cache_root, episode_id)
        path = receipt_path(cache_root, episode_id)
        if not path.is_file():
            errors.append(f"缺少当前有效的逐集复检回执：{episode_id}")
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"逐集复检回执不可读取：{episode_id}")
            continue
        for key in ("packet_version", "skill_version", "episode_id", "script_sha256", "reference_name", "reference_sha256", "required_checks"):
            if receipt.get(key) != packet.get(key):
                errors.append(f"逐集复检回执已失效：{episode_id}/{key}")
        if receipt.get("covered_checks") is None or any(check not in receipt.get("covered_checks", []) for check in REQUIRED_CHECKS):
            errors.append(f"逐集复检覆盖不完整：{episode_id}")
        if receipt.get("issues") != []:
            errors.append(f"逐集复检仍有问题：{episode_id}")
        if validate_review(packet, receipt):
            errors.append(f"逐集复检证据无效：{episode_id}")
    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    packet_parser = subparsers.add_parser("packet")
    packet_parser.add_argument("cache_root", type=Path)
    packet_parser.add_argument("episode_id")
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("cache_root", type=Path)
    seal_parser.add_argument("episode_id")
    seal_parser.add_argument("review", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("cache_root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "packet":
            print(json.dumps(build_packet(args.cache_root, args.episode_id), ensure_ascii=False, indent=2))
        elif args.command == "seal":
            print(f"PASS: {seal(args.cache_root, args.episode_id, args.review)}")
        else:
            errors = verify_project(args.cache_root)
            if errors:
                for error in errors:
                    print(f"FAIL: {error}")
                return 1
            print("PASS: every episode has a current reference-bound quality-review receipt")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
