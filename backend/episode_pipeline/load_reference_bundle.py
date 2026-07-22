#!/usr/bin/env python3
"""Deterministically load mandatory phase references and issue private receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[2] / "skill" / "episode-generator"
REFERENCE_ROOT = SKILL_ROOT / "references"
MANIFEST_PATH = SKILL_ROOT / "reference-manifest.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_manifest() -> tuple[dict[str, Any], str]:
    raw = MANIFEST_PATH.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("phases"), dict):
        raise ValueError("reference-manifest.json 缺少 phases")
    return manifest, sha256_text(raw)


def reference_names(
    manifest: dict[str, Any], phase: str, profiles: tuple[str, ...]
) -> list[str]:
    phases = manifest["phases"]
    if phase not in phases or not isinstance(phases[phase], list):
        raise ValueError(f"未知 reference 阶段：{phase}")
    names = list(phases[phase])
    profile_map = manifest.get("profiles") or {}
    for profile in profiles:
        if profile not in profile_map:
            raise ValueError(f"未知 reference profile：{profile}")
        additions = (profile_map[profile] or {}).get(phase) or []
        if not isinstance(additions, list):
            raise ValueError(f"reference profile 配置无效：{profile}/{phase}")
        names.extend(additions)
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".md"):
            raise ValueError(f"非法 reference 文件名：{name!r}")
        if name not in result:
            result.append(name)
    return result


def load_bundle(phase: str, profiles: tuple[str, ...] = ()) -> dict[str, Any]:
    profiles = tuple(sorted(set(profiles)))
    manifest, manifest_sha = read_manifest()
    files = []
    sections = []
    for name in reference_names(manifest, phase, profiles):
        path = (REFERENCE_ROOT / name).resolve()
        if path.parent != REFERENCE_ROOT.resolve() or not path.is_file():
            raise ValueError(f"reference 不存在或越界：{name}")
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"reference 为空：{name}")
        digest = sha256_text(content)
        files.append({"name": name, "sha256": digest, "chars": len(content)})
        sections.append(f"<reference name=\"{name}\" sha256=\"{digest}\">\n{content}\n</reference>")
    receipt_core = {
        "skill_version": manifest.get("skill_version"),
        "phase": phase,
        "profiles": list(profiles),
        "manifest_sha256": manifest_sha,
        "files": files,
    }
    receipt_sha = sha256_text(
        json.dumps(receipt_core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    bundle = (
        f"<episode-generator-reference-bundle version=\"{manifest.get('skill_version')}\" "
        f"phase=\"{phase}\" receipt=\"{receipt_sha}\">\n"
        + "\n\n".join(sections)
        + "\n</episode-generator-reference-bundle>"
    )
    return receipt_core | {"receipt_sha256": receipt_sha, "bundle": bundle}


def receipt_filename(phase: str, profiles: tuple[str, ...]) -> str:
    suffix = "-".join(sorted(set(profiles))) or "base"
    return f"{phase}--{suffix}.json"


def write_receipt(directory: Path, result: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / receipt_filename(
        str(result["phase"]), tuple(str(item) for item in result.get("profiles") or [])
    )
    payload = {key: value for key, value in result.items() if key != "bundle"}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def verify_receipts(directory: Path, required: tuple[str, ...]) -> list[str]:
    errors: list[str] = []
    for phase in required:
        candidates = sorted(directory.glob(f"{phase}--*.json")) if directory.is_dir() else []
        valid = False
        for path in candidates:
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
                current = load_bundle(
                    phase, tuple(str(item) for item in stored.get("profiles") or [])
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            expected = {key: value for key, value in current.items() if key != "bundle"}
            if stored == expected:
                valid = True
                break
        if not valid:
            errors.append(f"缺少当前有效的 reference 装载凭证：{phase}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--format", choices=("prompt", "json", "receipt"), default="prompt")
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--verify-dir", type=Path)
    parser.add_argument("--require", help="逗号分隔的必需阶段")
    args = parser.parse_args()

    if args.verify_dir:
        required = tuple(
            item.strip() for item in str(args.require or "").split(",") if item.strip()
        )
        if not required:
            raise SystemExit("--verify-dir 必须同时提供 --require")
        errors = verify_receipts(args.verify_dir, required)
        if errors:
            raise SystemExit("\n".join(errors))
        print("reference receipt validation: PASS")
        return 0

    if not args.phase:
        raise SystemExit("必须提供 --phase")
    if not re.fullmatch(r"[a-z][a-z-]*", args.phase):
        raise SystemExit("非法阶段名")
    try:
        result = load_bundle(args.phase, tuple(args.profile))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.receipt_dir:
        write_receipt(args.receipt_dir, result)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False))
    elif args.format == "receipt":
        print(json.dumps({key: value for key, value in result.items() if key != "bundle"}, ensure_ascii=False))
    else:
        print(result["bundle"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
