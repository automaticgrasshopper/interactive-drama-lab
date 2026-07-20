#!/usr/bin/env python3
"""Create immutable episode-generator snapshots and restore them safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
LIVE_SKILL = LAB_ROOT / "skill" / "episode-generator"
ARCHIVE_ROOT = LAB_ROOT / "skill-backups" / "episode-generator" / "versions"
HISTORY = ARCHIVE_ROOT / "history.jsonl"
IGNORED = {".DS_Store", "__pycache__"}


def files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and not any(part in IGNORED for part in p.relative_to(root).parts)
    )


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): file_hash(p) for p in files(root)}


def detected_version(root: Path) -> str:
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(r"当前规范版本：`([^`]+)`", text)
    if not match:
        raise SystemExit(f"无法从 {root / 'SKILL.md'} 识别版本")
    return match.group(1)


def append_history(event: dict[str, object]) -> None:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def snapshot(
    source: Path,
    version: str,
    summary: str,
    parent: str | None,
    allow_unmarked: bool = False,
) -> Path:
    source = source.resolve()
    try:
        actual = detected_version(source)
    except SystemExit:
        if not allow_unmarked:
            raise
        actual = None
    if actual is not None and actual != version:
        raise SystemExit(f"版本标记不一致：参数 {version}，SKILL.md 为 {actual}")
    target = ARCHIVE_ROOT / version
    source_inventory = inventory(source)
    if target.exists():
        target_inventory = inventory(target)
        target_inventory.pop("VERSION.json", None)
        if target_inventory != source_inventory:
            raise SystemExit(f"不可覆盖已有不同快照：{target}")
        print(f"snapshot already verified: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED))
    manifest = {
        "version": version,
        "parent": parent,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "source": str(source),
        "skill_marker": actual,
        "files": source_inventory,
    }
    (target / "VERSION.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    append_history({k: v for k, v in manifest.items() if k != "files"} | {"event": "snapshot"})
    print(f"snapshot created: {target}")
    return target


def verify(version: str) -> None:
    target = ARCHIVE_ROOT / version
    manifest_path = target / "VERSION.json"
    if not manifest_path.is_file():
        raise SystemExit(f"缺少版本清单：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = inventory(target)
    actual.pop("VERSION.json", None)
    if actual != manifest["files"]:
        raise SystemExit(f"版本快照校验失败：{version}")
    marker = manifest.get("skill_marker")
    if marker is not None and detected_version(target) != version:
        raise SystemExit(f"版本标记校验失败：{version}")
    print(f"snapshot verification: PASS ({version})")


def list_versions() -> None:
    if not ARCHIVE_ROOT.is_dir():
        return
    for path in sorted(p for p in ARCHIVE_ROOT.iterdir() if p.is_dir()):
        manifest = path / "VERSION.json"
        status = "verified" if manifest.is_file() else "unmanaged"
        print(f"{path.name}\t{status}\t{path}")


def restore(version: str) -> None:
    verify(version)
    target = ARCHIVE_ROOT / version
    live_version = detected_version(LIVE_SKILL)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety_version = f"{live_version}-pre-restore-{stamp}"
    safety = ARCHIVE_ROOT / "safety" / safety_version
    safety.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(LIVE_SKILL, safety, ignore=shutil.ignore_patterns(*IGNORED))
    for child in LIVE_SKILL.iterdir():
        if child.name in IGNORED:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in target.iterdir():
        if child.name == "VERSION.json":
            continue
        destination = LIVE_SKILL / child.name
        if child.is_dir():
            shutil.copytree(child, destination)
        else:
            shutil.copy2(child, destination)
    append_history({
        "event": "restore",
        "restored_version": version,
        "replaced_version": live_version,
        "safety_snapshot": str(safety),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    print(f"restored {version}; safety snapshot: {safety}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--source", type=Path, default=LIVE_SKILL)
    snap.add_argument("--version", required=True)
    snap.add_argument("--summary", required=True)
    snap.add_argument("--parent")
    snap.add_argument("--allow-unmarked", action="store_true")
    check = sub.add_parser("verify")
    check.add_argument("version")
    sub.add_parser("list")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("version")
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(
            args.source,
            args.version,
            args.summary,
            args.parent,
            args.allow_unmarked,
        )
    elif args.command == "verify":
        verify(args.version)
    elif args.command == "list":
        list_versions()
    else:
        restore(args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
