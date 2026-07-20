#!/usr/bin/env python3
"""Resolve episode-generator's private cache outside the public Canvas tree."""

from __future__ import annotations

import re
from pathlib import Path


PRIVATE_CACHE_DIR = ".episode-generator-cache"


def workspace_root_for(canvas_root: Path) -> Path:
    """Return the workspace that owns a Canvas root or legacy canvas/<slug>."""
    root = canvas_root.resolve()
    if root.name == "canvas":
        return root.parent
    if root.parent.name == "canvas":
        return root.parent.parent
    return root.parent


def declared_canvas(manifest: Path) -> Path | None:
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^## 公开 Canvas\s*$\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    value = match.group(1).strip().splitlines()[0].strip()
    return Path(value).expanduser().resolve() if value else None


def cache_root_for(canvas_root: Path) -> Path:
    """Return the private cache paired with a public Canvas project."""
    root = canvas_root.resolve()
    workspace = workspace_root_for(root)
    cache_parent = workspace / PRIVATE_CACHE_DIR
    if root.name == "canvas" and cache_parent.is_dir():
        matches = [
            manifest.parent
            for manifest in cache_parent.glob("*/manifest.md")
            if declared_canvas(manifest) == root
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("多个私有项目声明同一 Canvas 根目录，无法唯一定位")
    return cache_parent / root.name
