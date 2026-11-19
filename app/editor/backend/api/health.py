#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康与版本端点。

启动编排脚本依赖这两个端点判断服务是否就绪（§7.4 第 14 步）：
    slicer → GET /version
    editor → GET /api/editor/health
"""
from __future__ import annotations

import os
import sys
import time

from fastapi import APIRouter

from app.common.paths import APP_TITLE, APP_VERSION, ws
from app.common.runtime_probe import probe_system, runtime_summary
from app.editor.backend import database

router = APIRouter()

_STARTED_AT = time.time()


@router.get("/health")
def health() -> dict:
    """服务健康快照。**不校验 token**（启动编排要探测它）。"""
    db = database.health()
    return {
        "ok": db.get("error") is None,
        "service": "editor",
        "app": APP_TITLE,
        "version": APP_VERSION,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "uptimeSec": round(time.time() - _STARTED_AT, 1),
        "workspace": str(ws.root),
        "runtimeData": str(ws.runtime_data),
        "port": os.environ.get("EDITOR_PORT", ""),
        "tokenProtected": bool(os.environ.get("JACK_LOCAL_SESSION_TOKEN", "").strip()),
        "db": db,
    }


@router.get("/version")
def version() -> dict:
    return {"service": "editor", "version": APP_VERSION, "app": APP_TITLE}


@router.get("/runtime")
def runtime() -> dict:
    """运行时依赖就绪情况（桌面启动页与设置页用）。"""
    return runtime_summary()


@router.get("/system")
def system() -> dict:
    """系统级信息：磁盘水位（A6 告警用）、工作区路径。"""
    return probe_system()
