#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「剪辑工作台」服务（editor）—— FastAPI 应用工厂。

对应文档 §6.1 工程结构、§7.4 启动编排：
- 由启动脚本以 ``python -m app.editor.backend`` 拉起，``-WindowStyle Hidden``；
- 端口来自环境变量 ``EDITOR_PORT``（由启动脚本分配空闲端口）；
- HOST 强制 ``127.0.0.1``，不对外暴露；
- 除健康检查外，所有接口都需要 session token（见 security.py）。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.common.paths import APP_TITLE, APP_VERSION, ws
from app.editor.backend import database
from app.editor.backend.api import health as health_api
from app.editor.backend.api import publish as publish_api
from app.editor.backend.api import library as library_api
from app.editor.backend.api import settings as settings_api
from app.editor.backend.security import require_token

API_PREFIX = "/api/editor"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建目录并执行 migration；关闭时无需特殊处理（连接按需开启）。"""
    summary = database.initialize(verbose=bool(os.environ.get("YJ_DEBUG")))
    app.state.db_migration = summary
    if summary["applied"]:
        print(f"[editor] 数据库已升级：v{summary['from']} → v{summary['to']}"
              f"（应用 {summary['applied']}）", flush=True)
    print(f"[editor] 工作区 {ws.root}", flush=True)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{APP_TITLE} · 剪辑工作台服务",
        version=APP_VERSION,
        lifespan=lifespan,
        # 本地服务，不暴露交互式文档入口以外的敏感信息
        docs_url="/docs",
        redoc_url=None,
    )

    # CORS：仅允许本机来源（端口动态，故放开 loopback 段）
    origins = os.environ.get("EDITOR_CORS_ORIGINS", "")
    allow_origins = [o.strip() for o in origins.split(",") if o.strip()] or []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 全局鉴权（健康检查等公共路径在 security 内部放行）
    app.include_router(
        health_api.router,
        prefix=API_PREFIX,
        tags=["health"],
        dependencies=[Depends(require_token)],
    )

    # M5-A 待发布队列（不包含任何「点发布」逻辑，见 R1 / D15）
    app.include_router(
        publish_api.router,
        prefix=f"{API_PREFIX}/publish",
        tags=["publish"],
        dependencies=[Depends(require_token)],
    )

    # 素材库（5 分类统一视图；删除走 file_ops，见 library.py 顶部说明）
    app.include_router(
        library_api.router,
        prefix=f"{API_PREFIX}/library",
        tags=["library"],
        dependencies=[Depends(require_token)],
    )

    # 设置（AI 服务 / 抖音账号 / 开机自启 / 评分阈值 的统一入口）
    app.include_router(
        settings_api.router,
        prefix=f"{API_PREFIX}/settings",
        tags=["settings"],
        dependencies=[Depends(require_token)],
    )

    @app.get("/", include_in_schema=False)
    def index() -> dict:
        return {
            "service": "editor",
            "app": APP_TITLE,
            "version": APP_VERSION,
            "health": f"{API_PREFIX}/health",
        }

    return app


app = create_app()


def main() -> int:
    import uvicorn

    port = int(os.environ.get("EDITOR_PORT", "8140"))
    host = "127.0.0.1"   # 强制本机，不对外暴露
    print(f"[editor] 监听 http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0
