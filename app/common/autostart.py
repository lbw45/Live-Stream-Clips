#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机自启（Windows HKCU Run 键）。用户可在「服务状态 / 设置」页自主开关。

对应文档 §5.2「自动化」里的「开机启动」、§7.1 交付目标。
写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run（**当前用户**，无需管理员），
值指向 ``pythonw.exe <orchestrator.py> start``（无控制台窗；服务内部 DETACHED 脱离）。

纯逻辑（winreg 操作），便于单测；桌面桥暴露 get/set 两个方法。
**这里是唯一的写入实现** —— 设置页的状态展示也复用它，避免出现第二份注册表逻辑。
"""
from __future__ import annotations

from pathlib import Path

from app.common.paths import APP_ID, ws

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _pythonw() -> Path:
    """启动用的 Python：优先 pythonw（无控制台），不存在回退 python。"""
    pyw = ws.python_exe.with_name("pythonw.exe")
    return pyw if pyw.is_file() else ws.python_exe


def command_for_display() -> str:
    """注册表里将要写入的启动命令（不硬编码路径，从 paths 解析）。

    本文件 ``app/common/autostart.py`` → ``parents[2]`` 即源码根 ``slicer-workbench/``。
    """
    src_root = Path(__file__).resolve().parents[2]
    orchestrator = src_root / "launcher" / "orchestrator.py"
    return f'"{_pythonw()}" "{orchestrator}" start'


def is_enabled() -> bool:
    """当前是否已开机自启。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_ID)
            return bool(str(value).strip())
    except OSError:
        return False


def set_enabled(enabled: bool) -> dict:
    """开启/关闭开机自启。返回 ``{"ok": .., "enabled": .., "command": ..}`` 或错误。

    幂等：关闭时若本就没有该值，按成功处理（FileNotFoundError 不算失败）。
    """
    import winreg
    try:
        cmd = command_for_display()
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, cmd)
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass   # 本来就没开，幂等
        return {"ok": True, "enabled": enabled, "command": cmd}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
