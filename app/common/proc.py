"""子进程启动的**唯一入口**：把「不弹控制台窗口」这件事收在一个地方。

为什么必须集中（2026-09-22 实测踩坑）
------------------------------------
Windows 上，当一个**自己没有控制台**的进程（我们的窗口是 GUI 子系统；服务进程
也是带 ``CREATE_NO_WINDOW`` 起的，等于没有可见控制台）去启动一个**控制台程序**
（``nvidia-smi`` / ``ffmpeg`` / ``ffprobe`` / ``python``）时，系统会为子进程
**新建一个可见的控制台窗口**。子进程退出，窗口消失。

于是症状是「屏幕上有个小黑窗不停开关闪烁」。本项目的实例：

- ``/api/render/status`` 被前端每 4 秒轮询一次，每次都调 ``gpu_memory_used_mib()``
  → 每次都起一次 ``nvidia-smi`` → **黑窗每 4 秒闪一下**（用户 2026-09-22 报的 bug）。

传 ``CREATE_NO_WINDOW`` 就没有窗口，**且不改变子进程行为**（只是不给它窗口），
``capture_output`` 照常工作。

为什么不能各模块自己写：**必然漏**。当时全项目的情况是
``merge.py`` / ``recorder.py`` 写了，``render.py`` / ``transcriber.py`` /
``whisper_run.py`` 三处都漏了 —— 典型的「一处修了、另外几处忘了」。
所以统一走 :func:`run` / :func:`popen`。

用法::

    from app.common import proc

    r = proc.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    p = proc.popen(cmd, stdout=subprocess.PIPE, text=True)

若调用方自己要传 ``creationflags``（例如 ``DETACHED_PROCESS``），本模块**不覆盖**
（用 ``setdefault``），因为那种情况是有意为之。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

#: 不给子进程分配控制台窗口（Windows）。非 Windows 平台为 0，等价于不传。
CREATE_NO_WINDOW: int = 0x08000000 if os.name == "nt" else 0

#: ``CREATE_NEW_PROCESS_GROUP``：让 ``send_signal(CTRL_BREAK_EVENT)`` 能只打这一个子进程。
CREATE_NEW_PROCESS_GROUP: int = 0x00000200 if os.name == "nt" else 0


def no_window_kwargs() -> dict[str, Any]:
    """``subprocess`` 的「不弹窗」参数。非 Windows 返回空 dict。"""
    if not CREATE_NO_WINDOW:
        return {}
    return {"creationflags": CREATE_NO_WINDOW}


def run(cmd: list[str] | str, **kwargs: Any) -> subprocess.CompletedProcess:
    """:func:`subprocess.run` 的包装：默认不弹控制台窗口。"""
    if CREATE_NO_WINDOW:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def popen(cmd: list[str] | str, **kwargs: Any) -> subprocess.Popen:
    """:func:`subprocess.Popen` 的包装：默认不弹控制台窗口。"""
    if CREATE_NO_WINDOW:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)
