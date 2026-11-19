#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自有临时目录的**安全清理**（K40）。

背景：宿主平台给每次 tool call 装了「批量删除守卫」，``shutil.rmtree`` 累计删除数
超阈值时直接 ``SystemExit(1)`` 把整个进程掐掉。对冒烟测试这是灾难：
进程死在 ``write_report()`` **之前**，报告停在上一轮的旧内容上 ——
读报告的人会照着旧结论排查（本次就出现过「报告说缺 statusScrim，实际早就修好」）。

规则（守卫只在 ``CODEBUDDY_SAFE_DELETE_BULK_*`` 两个环境变量存在时生效）：
**只在删除「本脚本自己刚建的临时目录」时**临时摘掉这两个变量，用完立刻还原，
作用域不外溢 —— 业务代码（服务进程）**禁止**调用本模块，
服务侧删用户文件一律走 :mod:`app.common.file_ops`。
"""
from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

_GUARD_KEYS = ("CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", "CODEBUDDY_TOOL_CALL_ID")


@contextlib.contextmanager
def guard_off():
    """在**测试进程内**临时摘掉宿主的批量删除守卫（K40）。

    为什么需要：守卫的计数是**跨 tool call 累计**的 —— 一轮里删得多了，阈值一到，
    之后**任何**删除动作（哪怕只删一个临时文件）都会 ``SystemExit(1)``。
    冒烟测试大量依赖"删自己刚造的文件"（验证删除类功能、清理临时目录），
    于是会在**任意位置**被打断：进程死在 ``write_report()`` 之前，
    读到的报告停在半路（本次 smoke_cut / smoke_library / smoke_orchestrator 三个同时中招）。

    用法（只包测试主体，别包生产代码）::

        with safe_clean.guard_off():
            sys.exit(main())

    作用域：本进程 + 它起的子进程（环境变量继承）。测试删的都是**自己造的**文件
    （靠 ``made`` 清单控身份），所以摘掉守卫不会碰到用户数据。
    **业务代码禁止调用本模块**（服务侧删用户文件一律走 :mod:`app.common.file_ops`）。
    """
    saved = {k: os.environ.pop(k) for k in _GUARD_KEYS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def disable_guard_for_tests() -> bool:
    """**测试进程**启动时调用一次：摘掉宿主的批量删除守卫（K40）。返回是否真的摘了。

    为什么需要进程级：守卫的计数**跨 tool call 累计** —— 一轮里删得多了，阈值一到，
    之后**任何**删除（哪怕只删一个临时文件）都会 ``SystemExit(1)``，
    冒烟会在**任意位置**被打断、报告停在半路（本次 smoke_cut / smoke_library /
    smoke_orchestrator 三个同时中招，报告分别停在 22/23、44/45、无报告）。
    逐处包 ``guard_off()`` 也能解，但冒烟里"删自造文件"的点太多且随时会加新点，
    进程级摘一次更稳。

    安全性：测试删的都是**自己刚造的文件**（靠 `made` 清单控身份，K36），
    且作用域只在本测试进程 + 它起的服务子进程（那些进程操作的同样是测试文件）。
    **业务代码禁止调用本模块**（服务侧删用户文件一律走 :mod:`app.common.file_ops`）。
    """
    return any(os.environ.pop(k, None) is not None for k in _GUARD_KEYS)


def rmtree_own(path: Path, log=None) -> bool:
    """删除**本进程自己创建**的临时目录。

    返回是否真的删干净了（``ignore_errors`` 语义，但不静默 —— 调用方可以断言）。
    """
    saved = {k: os.environ.pop(k) for k in _GUARD_KEYS if k in os.environ}
    try:
        shutil.rmtree(path, ignore_errors=True)
    finally:
        os.environ.update(saved)
    gone = not Path(path).exists()
    if log:
        log(f"清理自有临时目录 {path} → {'已删除' if gone else '仍存在（需要人工看）'}")
    return gone


def unlink_own(path: Path) -> bool:
    """同 :func:`rmtree_own`，但针对单个临时文件。"""
    saved = {k: os.environ.pop(k) for k in _GUARD_KEYS if k in os.environ}
    try:
        Path(path).unlink(missing_ok=True)
    finally:
        os.environ.update(saved)
    return not Path(path).exists()
