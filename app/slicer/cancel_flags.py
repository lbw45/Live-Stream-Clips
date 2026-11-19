# -*- coding: utf-8 -*-
"""v3.159：同步长任务（切片）的「停止」开关。

背景：转写/渲染本来就是**子进程**，能 kill；而 `/api/slice` 是**同步长任务**
（对每个转写窗口各调一次 AI），中间没法杀 —— 只能靠协作式取消：
    前端点「停止」→ `POST /api/slice/cancel` → 这里置位
    切片主流程在「开窗口 / 进模型 / 落盘之前」各查一次 → 抛 ``SliceCancelled``
这样**不会写出半成品**（候选产物只在最后一步 atomic replace 落盘）。
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()
_FLAGS: dict[str, threading.Event] = {}


def begin(kind: str) -> threading.Event:
    """开始一次任务：换一个全新的、未置位的令牌（旧令牌自动作废）。"""
    ev = threading.Event()
    with _LOCK:
        _FLAGS[kind] = ev
    return ev


def request(kind: str) -> bool:
    """请求停止；返回「当前确实有一个在跑的任务」而不是空点。"""
    with _LOCK:
        ev = _FLAGS.get(kind)
    if ev is None:
        return False
    ev.set()
    return True


def cancelled(kind: str) -> bool:
    with _LOCK:
        ev = _FLAGS.get(kind)
    return bool(ev and ev.is_set())


def end(kind: str) -> None:
    with _LOCK:
        _FLAGS.pop(kind, None)
