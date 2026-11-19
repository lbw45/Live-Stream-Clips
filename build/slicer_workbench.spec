# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包桌面主窗口（§7.2）。

产物：`launcher\LiveStreamClips.exe` + `launcher\_internal\`。
只打包**桌面壳**依赖（webview / pywebview / pythonnet / clr_loader），
业务与重依赖（fastapi / faster-whisper / ffmpeg / cuda）走便携 runtime `hc\`，
不打包进来 —— 这是本项目与原版「runtime 全打进包」的刻意差异（§7.6）。

用法：
    pyinstaller build\slicer_workbench.spec --clean --noconfirm
    （用工作区里的便携 Python 执行 PyInstaller，见 README 的「快速开始」）
"""
from pathlib import Path

ROOT = Path(SPECPATH).parent  # spec 所在目录（build\），源码根是上一级
SRC = ROOT.parent

a = Analysis(
    [str(SRC / "launcher" / "desktop_entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        # 前端页面（pywebview 用 file:// 加载）
        (str(SRC / "launcher" / "ui"), "ui"),
    ],
    hiddenimports=[
        "webview",
        "clr_loader",
        "pythonnet",
        "webview.platforms.winforms",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 桌面壳不需要的（走便携 runtime）
        "fastapi", "uvicorn", "ctranslate2", "faster_whisper",
        "numpy", "torch", "playwright",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveStreamClips",
    icon=str(SRC / "launcher" / "ui" / "assets" / "app.ico"),  # 窗口/任务栏/资源管理器图标
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 桌面窗口（GUI 子系统，不弹控制台）
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LiveStreamClips",
)
