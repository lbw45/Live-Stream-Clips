#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地服务鉴权：session token 校验。

对应文档 §7.4 第 5/6 步：token 由启动脚本生成（32 字节随机数转 hex64），
通过环境变量 ``JACK_LOCAL_SESSION_TOKEN`` 注入，写入 ``local-service-ports.json``。
两个本地服务互访时携带该 token，防止同机其他进程随意调用。

**例外**：``/api/editor/health`` 等健康检查端点不校验 token —— 启动编排脚本要在
服务刚起来时探测它（见 §7.4 第 14 步），此时尚未读取 token 文件。
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Request, status

TOKEN_ENV = "JACK_LOCAL_SESSION_TOKEN"
TOKEN_HEADER = "X-Session-Token"
TOKEN_QUERY = "token"

# 这些路径前缀不做 token 校验
# 只放行启动编排真正需要的探测端点（§7.4 第 14 步 editor 探 /health）
# 注意：不要放行 /version —— 那是 slicer 的探测端点，editor 侧无需公开
PUBLIC_PREFIXES = (
    "/api/editor/health",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def expected_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def token_required() -> bool:
    """开发模式下未注入 token 时自动放行，方便直接跑服务调试。"""
    return bool(expected_token())


def is_public(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def extract(request: Request) -> str:
    value = request.headers.get(TOKEN_HEADER)
    if value:
        return value.strip()
    return (request.query_params.get(TOKEN_QUERY) or "").strip()


async def require_token(request: Request) -> None:
    """FastAPI 依赖：校验调用方携带的 session token。"""
    if is_public(request.url.path):
        return
    expected = expected_token()
    if not expected:
        return   # 开发模式：未配置即不校验
    if extract(request) != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="本地会话令牌无效，请从桌面窗口重新打开页面",
        )
