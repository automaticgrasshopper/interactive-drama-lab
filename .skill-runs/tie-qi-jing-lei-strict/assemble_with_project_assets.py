#!/usr/bin/env python3
"""Run the formal v0.1.37 checks with this project's frozen asset registry."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skill" / "episode-generator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_and_assemble_scripts_v037  # noqa: F401,E402
import validate_and_assemble_scripts_v036 as base  # noqa: E402


base.CHARACTERS = {"霍峥", "玄甲-07", "陆骁", "沈砚秋", "赫连朔"}
base.SCENES = {"雷暴荒原", "雁回关城楼", "断烽峡谷", "裂隙核心区"}
base.PROPS = {"雁翎将军剑", "残破赤旌", "玄甲核心模块", "未来残像终端"}


if __name__ == "__main__":
    raise SystemExit(base.main())
