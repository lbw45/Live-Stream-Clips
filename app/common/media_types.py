#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体扩展名分类 + 扩展名 → MIME 的**唯一出处**（K44）。

为什么必须只有一份：**「素材库允许列出的」与「媒体流端点允许播的」是同一件事**。
两边各写一份必然漂移，而漂移的表现是**静默无声音**（不是报错）。

实测（v3.34，用户报「音乐素材试听不可用，没声音」）：素材库的「我的上传」分类
按 ``IMPORT_EXT = 视频 | 音频 | 图片`` 列文件（列表里有 mp3），而媒体流端点
``STREAM_ROOTS["uploads"]`` 只放视频扩展名 —— 于是试听那条 mp3 时端点回
``400 文件名或类型不合法``，前端拿不到流，点了没声音。同类的未来风险：
任何一边新增/收紧扩展名，另一边不会跟着动。

所以这里集中定义，三处都从这里取：
1. ``app.editor.backend.api.library`` —— 素材库分类的「允许扩展名」；
2. ``app.slicer.cut.STREAM_ROOTS`` —— 媒体流端点的「可播扩展名」；
3. ``app.slicer.web_server`` —— 流式响应的 ``Content-Type``。
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------- 扩展名分类

VIDEO_EXT = frozenset({".mp4", ".mov", ".mkv", ".avi", ".flv",
                       ".wmv", ".ts", ".m4v", ".webm"})
AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"})
IMAGE_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})

# 「我的上传」允许的扩展名（素材向，排除可执行文件）
IMPORT_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT

# 能被流式端点播放/显示的扩展名（= 上面三类；`.json` 之类只作元数据，不走流）
STREAMABLE_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT


# ---------------------------------------------------------------- 扩展名 → MIME
# ⚠️ K54：流式端点早先一律写 video/mp4，于是 mp3 试听被浏览器按 mp4 解码（没声音）。
#    后来改成按扩展名查表，但表在 web_server 里；这里收到唯一出处，避免再分叉。

MEDIA_TYPES: dict[str, str] = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".ts": "video/mp2t", ".flv": "video/x-flv", ".webm": "video/webm",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

DEFAULT_MEDIA_TYPE = "application/octet-stream"


def ext_of(name: str) -> str:
    """取小写扩展名（含点）。``a.MP3`` → ``.mp3``；无扩展名 → 空串。"""
    return Path(str(name or "")).suffix.lower()


def content_type_for(name: str) -> str:
    """按扩展名给 Content-Type。查不到就 ``application/octet-stream``（不猜）。"""
    return MEDIA_TYPES.get(ext_of(name), DEFAULT_MEDIA_TYPE)


def is_audio(name: str) -> bool:
    """是不是音频（前端决定按钮写「试听」还是「预览」用同一判据）。"""
    return ext_of(name) in AUDIO_EXT
