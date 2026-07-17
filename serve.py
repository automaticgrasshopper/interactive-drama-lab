#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容启动入口：实际后端实现位于 backend/server.py。"""

import sys

from backend.server import run


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
