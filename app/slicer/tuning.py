#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 切片质量调优：score_threshold 配置 + 人工修改率统计（评分闭环的度量基础）。

**不依赖 API Key**：Key 只用于「调 prompt」这一步（留待账号/Key 到位后实测）。
本模块落地的是评分闭环能离线做的部分：
- ``score_threshold``（默认 80，§6.2 tasks 表）可配 —— 自动化流水线只渲染 ≥ 阈值的候选；
- **人工修改率**统计 —— 记录每次人工干预（移除候选 / 改标题 / 改标签），
  为 M7 验收「人工修改率 ≤30%」提供度量数据。

阈值存 settings.json（键 ``scoreThreshold``）；反馈记 ``runtime_data/slicer/feedback.json``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.common.paths import ws

DEFAULT_THRESHOLD = 80.0
FEEDBACK_FILE = "feedback.json"
FEEDBACK_LIMIT = 500     # 只保留最近 N 条，避免无限增长


# ---------------------------------------------------------------- score_threshold

def get_score_threshold() -> float:
    try:
        data = json.loads(ws.settings_path.read_text(encoding="utf-8"))
        v = float(data.get("scoreThreshold", DEFAULT_THRESHOLD))
        return max(0.0, min(100.0, v))
    except (OSError, ValueError, TypeError):
        return DEFAULT_THRESHOLD


def set_score_threshold(value: float) -> dict:
    try:
        v = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "scoreThreshold 必须是 0–100 的数字"}
    try:
        path = ws.settings_path
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        data["scoreThreshold"] = v
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "scoreThreshold": v}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 人工修改率（评分闭环）

def _feedback_path() -> Path:
    return ws.slicer_runtime / FEEDBACK_FILE


def _load() -> list[dict]:
    try:
        data = json.loads(_feedback_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_feedback(action: str, detail: str = "") -> dict:
    """记录一次人工干预（供 M7 统计人工修改率）。幂等、失败不影响主流程。"""
    entry = {
        "action": action,
        "detail": str(detail)[:200],
        "at": round(time.time(), 1),
    }
    items = _load()
    items.append(entry)
    items = items[-FEEDBACK_LIMIT:]
    try:
        _feedback_path().parent.mkdir(parents=True, exist_ok=True)
        _feedback_path().write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    except OSError:
        pass
    return entry


def feedback_stats() -> dict:
    """人工干预统计：总量 + 按动作分类 + 最近几条。"""
    items = _load()
    by_action: dict[str, int] = {}
    for it in items:
        by_action[it.get("action", "unknown")] = by_action.get(it.get("action", "unknown"), 0) + 1
    return {
        "totalActions": len(items),
        "byAction": by_action,
        "recent": items[-10:][::-1],
    }
