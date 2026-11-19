#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``python -m app.editor.backend`` 入口。

启动脚本按此方式拉起服务（§7.4 第 13 步）：
    python -m app.editor.backend        # cwd = slicer-workbench/
"""
from __future__ import annotations

import sys

from app.editor.backend.main import main

if __name__ == "__main__":
    sys.exit(main())
