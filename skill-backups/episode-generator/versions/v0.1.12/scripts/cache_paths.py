#!/usr/bin/env python3
"""Resolve episode-generator's private cache outside the public Canvas tree."""

from __future__ import annotations

from pathlib import Path


PRIVATE_CACHE_DIR = ".episode-generator-cache"


def workspace_root_for(canvas_root: Path) -> Path:
    """Return the workspace that owns a conventional canvas/<slug> project."""
    root = canvas_root.resolve()
    if root.parent.name == "canvas":
        return root.parent.parent
    return root.parent


def cache_root_for(canvas_root: Path) -> Path:
    """Return the private cache paired with a public Canvas project."""
    root = canvas_root.resolve()
    return workspace_root_for(root) / PRIVATE_CACHE_DIR / root.name
