#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live Stream Clips · 桌面主窗口入口

对应文档：
- §7.4 启动编排第 9 步「选桌面宿主」：优先编译好的 exe，开发时用 pythonw / python
- §7.7 交付验收：双击后应尽快出现主窗口，不要黑屏干等

本阶段（M1-A）职责：
1. 建立 pywebview 单窗口，加载启动状态页；
2. 通过 js_api 暴露环境自检结果，验证 Python ↔ JS 桥接可用；
3. 后续 M1-B 接入 editor/slicer 双服务后，改为加载 editor 前端页面。

用法：
    python desktop_entry.py              # 正常启动
    python desktop_entry.py --selftest   # 自检：5 秒后自动关窗，打印 WINDOW_OK
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.paths import (  # noqa: E402
    APP_TITLE,
    APP_VERSION,
    asset_root,
    ws,
)
from app.common.runtime_probe import (  # noqa: E402
    load_ports,
    probe_services,
    probe_system,
    service_request,
)

UI_INDEX = asset_root() / "launcher" / "ui" / "index.html"

# 前端可以按**逻辑名**打开工作区子目录（名字 → Workspace 属性名）。
# 存在的意义：转写产物住在 runtime_data\slicer\transcripts，前端不该硬编码这个路径链。
WORKSPACE_ALIASES = {
    "live_raw": "live_source_dir",
    "live": "live_source_dir",       # 素材库分类名叫 live（见 library.py KINDS）
    "records": "records_dir",
    "clips": "slice_dir",
    "edits": "editor_output_dir",
    "exports": "final_output_dir",
    "music": "music_dir",
    "backgrounds": "background_dir",
    "transcripts": "transcripts_dir",
    "candidates": "candidates_dir",
    "runtime_data": "runtime_data",
    # 素材库分类名 → 工作区目录（前端只传分类名，路径仍只从 paths.py 取）
    "uploads": "uploads_dir",
    # v3.105：账号数据看板的数据目录（放采集出来的数据表）。
    "board": "board_dir",
}


# ---------------------------------------------------------------- 环境自检

def collect_status(with_services: bool = True, with_timing: bool = False) -> dict:
    """收集环境就绪情况，供启动页展示。不抛异常，全部降级为布尔/文本。"""
    ws.ensure_dirs()

    def probe(label: str, path: Path, required: bool = True) -> dict:
        return {
            "label": label,
            "path": str(path),
            "ok": path.exists(),
            "required": required,
        }

    items = [
        probe("Python 运行时", ws.python_exe),
        probe("FFmpeg", ws.ffmpeg),
        probe("ffprobe", ws.ffprobe),
        probe("CUDA 运行时", ws.cuda_dir / "cublas64_12.dll"),
        probe("Whisper small", ws.models_dir / "small" / "model.bin"),
        probe("Whisper medium", ws.models_dir / "medium" / "model.bin", required=False),
    ]

    payload = {
        "app": APP_TITLE,
        "version": APP_VERSION,
        "workspace": str(ws.root),
        "runtime_root": str(ws.runtime),
        "runtime_data": str(ws.runtime_data),
        "webview2": _webview2_version(),
        "items": items,
        "system": probe_system(),
    }
    # 服务探测要发 HTTP，不能阻塞窗口显示 → 首次调用可由前端异步发起
    if with_services:
        payload["services"] = probe_services(with_timing=with_timing)
    return payload


def _webview2_version() -> str:
    """WebView2 运行时版本（桌面壳依赖，缺失时窗口无法创建）。空串表示未安装。"""
    keys = (
        r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
        r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    )
    try:
        import winreg  # type: ignore
    except ImportError:
        return ""
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                if version:
                    return str(version)
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------- 服务请求超时（按端点分级）
# 为什么必须分级：``/api/slice`` 是**服务端同步跑完才回包**的 —— 它要对每个转写窗口
# 各调一次 LLM（``slicing.DEFAULT_PARAMS["windowSec"]=720``，所以 59 分钟的素材约 6 个
# 窗口），总耗时是**分钟级**。其余端点全是「入队即返回」（转写/渲染/重新识别都是
# submit 模式），短超时就够，短超时的好处是「服务真挂死时用户不用干等」。
#
# 2026-09-24 报障「12 分钟素材能切、1 小时素材必报『切片失败：连接失败：timed out』」
# 的真因就在这里：1 小时的素材 6 个窗口，35s 远远不够；12 分钟的只有 1 个窗口，
# 刚好压在 35s 之内 —— 所以看起来像"素材太大/太长有问题"，其实是**桥的超时**。
SLICER_SHORT_TIMEOUT = 35.0
# v3.69 新增**中档**：有些端点虽然也是"同步跑完才回包"，但最长只是几十秒 ——
# 典型就是 ``/api/download/probe``：抖音链接要先开一次无头浏览器取 Cookie
# （``dy_browser.BROWSER_TIMEOUT_SEC = 90``）再解析，正常 10~40 秒，最坏 2 分钟。
# 给它 35s（短档）会重演 K76 的老伤 —— 明明在跑，却报「连接失败：timed out」；
# 给它 1800s（长档）又太浪费（服务真挂死时要干等半小时）。所以单独一档。
SLICER_MID_TIMEOUT = 180.0
# 30 分钟：6 窗口 × (90s × 最多 3 次重试) ≈ 27 分钟，之上再留余量。
# 取这么长是因为宁可等到真结果，也不要拿一个假的"连接失败"骗用户重试。
SLICER_LONG_TIMEOUT = 1800.0
# 只列「同步跑完才回包」的端点；前缀匹配（``/api/slice`` 带 query 也算）
SLICER_LONG_PATHS: tuple[str, ...] = ("/api/slice",)
# 中档：要临时开一次浏览器的端点（v3.69）＋ v3.120 选题库 AI 分析（同步跑 LLM，几十秒）
SLICER_MID_PATHS: tuple[str, ...] = ()


#: `DesktopApi.open_external` 放行的协议（只有 http/https；见该方法的说明）
_OPEN_SCHEMES = ("http://", "https://")


class DesktopApi:
    """暴露给前端的 Python 接口（pywebview js_api）。"""

    def get_status(self, with_services: bool = True) -> dict:
        started = time.perf_counter()
        payload = collect_status(with_services=with_services)
        cost = (time.perf_counter() - started) * 1000
        if cost > 300 or os.environ.get("YJ_DEBUG"):
            detail = collect_status(with_services=with_services, with_timing=True)
            timing = (detail.get("services") or {}).get("timing", {})
            print(f"[ui] get_status 耗时 {cost:.0f}ms；分段 {timing}", flush=True)
        return payload

    def open_workspace(self, sub: str = "") -> bool:
        """在资源管理器中打开工作区（或其中子目录）。

        ``sub`` 三选一：**逻辑名**（见 :data:`WORKSPACE_ALIASES`，如 ``live_raw`` /
        ``transcripts``）、相对工作区的路径、绝对路径。

        逻辑名存在的意义：像转写产物这种住在 ``runtime_data\\slicer\\transcripts`` 的目录，
        让前端写全路径就违反「路径只从 paths.py 取」，所以在这里做一次映射。
        """
        target = ws.root
        if sub:
            attr = WORKSPACE_ALIASES.get(sub)
            candidate = getattr(ws, attr) if attr else Path(sub)
            if attr is None and not candidate.is_absolute():
                candidate = ws.root / candidate
            target = candidate
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))  # noqa: S606  (Windows 专用)
            return True
        except OSError:
            return False

    def open_external(self, url: str) -> bool:
        """用系统默认浏览器打开链接。

        v3.99：**放行外网链接**。这里原来卡着一道
        ``if not url.startswith(("http://127.0.0.1", "http://localhost")): return False``
        —— 那是当初只为「服务出错时的诊断地址」写的，结果**所有外网链接被静默拒绝**：
        函数返回 False、界面什么都不做（用户 2026-10-14：「打开链接的按钮点击了没有反应，
        没有正确跳转到浏览器打开链接」）。中招的有下载器搜索结果的「打开链接」、
        录制页的「打开直播间」、几条登录入口。

        现在只做**协议白名单**：``http`` / ``https`` 交给系统默认浏览器；其余
        （``file:`` / ``javascript:`` / 自定义协议…）一律拒绝 ——
        页面是本地页面，不给它「打开本机文件 / 触发任意协议处理器」的口子。
        """
        u = str(url or "").strip()
        if not u.lower().startswith(_OPEN_SCHEMES):
            return False
        try:
            os.startfile(u)  # noqa: S606
            return True
        except OSError:
            return False

    def slicer_api(self, method: str, path: str, body: dict | None = None) -> dict:
        """直播录制页调 slicer 服务的**代理**。

        前端页面是 file:// 加载的，直接 fetch 会撞 CORS 且拿不到 token ——
        所以所有对 slicer 的调用都走这里：token 不出 Python 层。
        返回 ``{"status": int, "data": dict}``，status=0 表示连接层失败（K26）。
        """
        creds = load_ports()
        if creds is None:
            return {"status": 0, "data": {
                "error": "本地服务未启动（无凭据文件）。请先用 start.bat 启动后再试。"}}
        if not path.startswith("/"):
            path = "/" + path
        # 超时按端点分级（见文件里 SLICER_LONG_PATHS 的说明）：切片是同步长任务，
        # 用短超时会把「服务正在跑」误报成「连接失败」。
        # 三档（v3.69 起）：长档 = 同步跑分钟级（切片）；中档 = 同步但要开一次浏览器
        # （下载解析）；其余 = 入队即返回，短档。
        if any(path.startswith(p) for p in SLICER_LONG_PATHS):
            timeout = SLICER_LONG_TIMEOUT
        elif any(path.startswith(p) for p in SLICER_MID_PATHS):
            timeout = SLICER_MID_TIMEOUT
        else:
            timeout = SLICER_SHORT_TIMEOUT
        return service_request(creds["slicerPort"], creds["sessionToken"],
                               method, path, body, timeout=timeout)

    def editor_api(self, method: str, path: str, body: dict | None = None) -> dict:
        """发布页调 **editor 服务**（FastAPI）的代理，规则同 :meth:`slicer_api`。

        path 传 ``/publish/jobs`` 这种（前缀 ``/api/editor`` 由这里补全）。
        """
        creds = load_ports()
        if creds is None:
            return {"status": 0, "data": {
                "error": "本地服务未启动（无凭据文件）。请先用 start.bat 启动后再试。"}}
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api/editor"):
            path = "/api/editor" + path
        return service_request(creds["editorPort"], creds["sessionToken"],
                               method, path, body, timeout=15.0)

    def slicer_media_url(self, rel: str) -> dict:
        """给 ``<video>`` 标签用的**媒体流地址**（预览面板 / 剪辑页）。

        video 元素带不了自定义 header，token 走 query 参数（服务端已支持，
        且只对本机 127.0.0.1 有效）。rel 形如 ``live/<文件名>``，服务端有白名单。
        """
        from urllib.parse import quote
        creds = load_ports()
        if creds is None:
            return {"ok": False, "error": "本地服务未启动（无凭据文件）"}
        return {"ok": True, "url":
                f"http://127.0.0.1:{creds['slicerPort']}/api/media/stream"
                f"?rel={quote(str(rel or ''), safe='')}"
                f"&token={quote(creds['sessionToken'], safe='')}"}

    def cover_emoji_url(self, code: str) -> dict:
        """v4.78：子窗口用 —— emoji PNG 的 data URI（assets/emoji 不在 ws 下）。"""
        from app.common import cover_designer_window
        return cover_designer_window.cover_emoji_url(code)

    def close_cover_designer(self) -> dict:
        """v4.78：子窗口「取消」——销毁标题以「封面设计」开头的窗口。"""
        from app.common import cover_designer_window
        return cover_designer_window.close_cover_designer()

    def set_designer_theme(self, theme: str) -> dict:
        """v5.1：主窗切主题时同步封面子窗（子窗已开着也不会重载，必须推一次）。"""
        from app.common import cover_designer_window
        return cover_designer_window.sync_theme(theme)

    def cover_designer_target(self) -> dict:
        """v4.78：子窗口用 —— 返回最近一次打开的成片名（file:// query 会丢，只能走桥）。"""
        from app.common import cover_designer_window
        return cover_designer_window.cover_designer_target()

    def designer_move_window(self, dx: float, dy: float) -> dict:
        """v4.87：无边框子窗拖动把手 —— 顶栏按住时前端按增量调这里（窗口无系统标题栏）。"""
        from app.common import cover_designer_window
        return cover_designer_window.designer_move_window(dx, dy)

    def open_cover_designer(self, film_name: str) -> dict:
        """v4.78 P-C：打开**封面设计子窗口**（pywebview 原生多窗口，非抽屉）。

        实现在 :mod:`app.common.cover_designer_window`（register 时挂到本类上）。
        子线程里 create_window —— 主窗口的事件循环正在跑，不能阻塞。
        """
        from app.common import cover_designer_window
        return cover_designer_window.open_cover_designer(self, film_name)

    def autostart_get(self) -> dict:
        """开机自启状态（M6：用户自主开关，见 app/common/autostart.py）。"""
        from app.common import autostart
        return {"enabled": autostart.is_enabled(),
                "command": autostart.command_for_display()}

    def autostart_set(self, enabled: bool) -> dict:
        from app.common import autostart
        return autostart.set_enabled(bool(enabled))

    def pick_library_files(self) -> tuple | list:
        """打开文件选择框挑素材（素材库「导入素材」用）。

        为什么必须走桥：页面是 file:// 加载的，``<input type=file>`` 拿不到可信路径，
        而导入动作由服务进程复制，需要**真实绝对路径**。
        用户取消时 pywebview 返回 ``None`` —— 这里统一收敛成空 list，
        免得每个调用方都要判空。
        """
        import webview
        try:
            picked = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("媒体文件 (*.mp4;*.mov;*.mkv;*.avi;*.mp3;*.wav;*.m4a;*.png;*.jpg;*.jpeg;*.webp)",
                            "所有文件 (*.*)"),
            )
        except Exception as exc:                                   # noqa: BLE001
            print(f"[ui] 打开文件选择框失败：{exc}", file=sys.stderr)
            return []
        return list(picked) if picked else []

    def pick_audio_files(self) -> list:
        """挑**歌**的文件选择框（文件类型只列音频 —— 视频不能当歌）。

        用户 2026-10-09：「选择歌曲方法应该是选择文件或者文件夹的」。
        与 :meth:`pick_library_files` 分开写，是为了让文件类型过滤器说实话：
        只列音频，用户就不会误挑到 mp4 再被后端拒绝。
        """
        import webview
        try:
            picked = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("音频文件 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.wma)",
                            "所有文件 (*.*)"),
            )
        except Exception as exc:                                   # noqa: BLE001
            print(f"[ui] 打开音频选择框失败：{exc}", file=sys.stderr)
            return []
        return list(picked) if picked else []

    def pick_any_dir(self) -> list:
        """挑一个**文件夹**（选草稿目录 / 整个素材目录用）。

        这一条只负责「把用户指的路径拿回来」；真正怎么扫、怎么复制由服务端决定 ——
        扫描与复制的规则只有**一处**（K44）。
        """
        import webview
        try:
            picked = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:                                   # noqa: BLE001
            print(f"[ui] 打开文件夹选择框失败：{exc}", file=sys.stderr)
            return []
        return list(picked) if picked else []

    def open_tool_url(self, url: str) -> bool:
        """用系统默认浏览器打开**生图工具**的官网（剪辑页封面工具的「可用工具」链接）。

        为什么不复用 :meth:`open_external`：那条只允许回环地址（诊断用），
        而这里要开的是外网站点。为了不变成"页面能打开任意 URL"的口子，只放行
        ``app.slicer.cover_prompt.IMAGE_TOOLS`` 里登记过的主机（**唯一出处**）。
        """
        from urllib.parse import urlparse

        from app.slicer.cover_prompt import IMAGE_TOOLS
        raw = str(url or "").strip()
        if not raw.startswith("https://"):
            return False
        host = (urlparse(raw).hostname or "").lower()
        allowed = {(urlparse(t.get("url", "")).hostname or "").lower()
                   for t in IMAGE_TOOLS}
        if not host or host not in allowed:
            return False
        try:
            os.startfile(raw)  # noqa: S606
            return True
        except OSError:
            return False

    def open_provider_url(self, url: str) -> bool:
        """用系统默认浏览器打开**AI 厂商的「去拿 Key」页面**（设置页 v3.81）。

        白名单来自 :func:`app.slicer.llm.allowed_key_hosts`，而那个集合是从
        ``PROVIDER_PRESETS`` 里读出来的 —— 也就是说**厂商表只有一处**（K44），
        这扇"能开外链"的门宽度跟着厂商表走，加厂商自动生效、不必动桥代码。
        与 :meth:`open_tool_url` 同样的考虑：这里要开的是外网站点，不复用
        只允许回环地址的 :meth:`open_external`。
        """
        from urllib.parse import urlparse

        from app.slicer.llm import allowed_key_hosts

        raw = str(url or "").strip()
        if not raw.startswith("https://"):
            return False
        host = (urlparse(raw).hostname or "").lower()
        if not host or host not in allowed_key_hosts():
            return False
        try:
            os.startfile(raw)  # noqa: S606
            return True
        except OSError:
            return False

    def copy_text(self, text: str) -> dict:
        """把文本写进系统剪贴板（v3.37，剪辑页「复制封面提示词」用）。

        为什么不直接用 ``navigator.clipboard``：页面是 **file://** 加载的，
        不是安全上下文，WebView2 里 writeText 可能直接不可用（会让用户点了没反应）。
        走 Python 侧 Win32 剪贴板最稳（不引入任何依赖，也不弹窗）。
        """
        payload = str(text or "")
        if not payload:
            return {"ok": False, "error": "没有可复制的内容"}
        if os.name != "nt":
            return {"ok": False, "error": "当前系统不支持该复制方式"}
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # v3.41 修复：**必须显式声明 64 位安全签名**。ctypes 不设 argtypes 时
        # 默认把整数参数按 c_int（32 位）传，而 GlobalAlloc 返回的 HGLOBAL 在
        # 64 位系统上是超过 2^31 的值 → SetClipboardData / GlobalFree 直接抛
        # ``OverflowError: int too long to convert`` → 复制必现失败，且异常从
        # finally 里二次抛出、桥 promise reject，前端连错误提示都收不到
        # （用户报「点了复制提示词没反应」的根因，probe_clipboard.py 实测复现）。
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE

        data = payload.encode("utf-16-le") + b"\x00\x00"
        try:
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return {"ok": False, "error": "剪贴板内存分配失败"}
            try:
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return {"ok": False, "error": "剪贴板内存锁定失败"}
                ctypes.memmove(ptr, data, len(data))
                kernel32.GlobalUnlock(handle)
                for _ in range(5):                  # 剪贴板可能被别的程序临时占用
                    if user32.OpenClipboard(None):
                        try:
                            user32.EmptyClipboard()
                            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                                return {"ok": False, "error": "写入剪贴板失败"}
                            handle = None           # 所有权已交给系统，不能再释放
                            return {"ok": True, "chars": len(payload)}
                        finally:
                            user32.CloseClipboard()
                    time.sleep(0.12)
            finally:
                if handle:
                    kernel32.GlobalFree(handle)
        except Exception as exc:                        # noqa: BLE001
            # v3.41：桥方法**绝不向外抛异常** —— 抛出去就是 JS 侧 promise reject，
            # 前端 await 直接中断、连错误提示都拿不到（上一版就是这么哑掉的）。
            return {"ok": False, "error": f"复制失败：{exc}"}
        return {"ok": False, "error": "剪贴板被其它程序占用，请稍后重试"}

    def read_clipboard_text(self) -> dict:
        """读系统剪贴板里的**最新一条文字**（v3.189，下载页「粘贴」用）。

        为什么不走 ``navigator.clipboard.readText()``：页面是 ``file://``，
        不是安全上下文，WebView2 里读剪贴板经常直接 reject；而且它会弹权限提示。
        这里跟 :meth:`copy_text` 一样走 Win32，零依赖、不弹窗。
        剪贴板里是图片/文件时如实说"没有文字"，不要返回空串让人以为按钮坏了。
        """
        if os.name != "nt":
            return {"ok": False, "error": "当前系统不支持该粘贴方式"}
        import ctypes
        from ctypes import wintypes

        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 64 位签名必须显式声明（与 copy_text 同一个坑：HGLOBAL 超过 2^31）
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE

        for _ in range(5):                      # 剪贴板可能被别的程序临时占用
            if not user32.OpenClipboard(None):
                time.sleep(0.12)
                continue
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return {"ok": False,
                            "error": "剪贴板里没有文字（可能是图片或文件），请先复制链接文本"}
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return {"ok": False, "error": "剪贴板里没有可读的文字"}
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return {"ok": False, "error": "剪贴板内存锁定失败"}
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
                return {"ok": True, "text": text, "chars": len(text)}
            except Exception as exc:                            # noqa: BLE001
                return {"ok": False, "error": f"读取剪贴板失败：{exc}"}
            finally:
                user32.CloseClipboard()
        return {"ok": False, "error": "剪贴板被其它程序占用，请稍后重试"}

    def log(self, message: str) -> None:
        print(f"[ui] {message}", flush=True)


# ---------------------------------------------------------------- 入口

def build_window(api: DesktopApi, hidden: bool = False):
    import webview  # 延迟导入：便于在无 GUI 环境给出友好报错

    return webview.create_window(
        title=f"{APP_TITLE} v{APP_VERSION}",
        url=UI_INDEX.as_uri(),
        js_api=api,
        width=1360,
        height=860,
        min_size=(1100, 700),
        hidden=hidden,
        text_select=True,
    )


def _webview2_proxy_guard() -> None:
    """给 WebView2 追加「回环直连」参数（K16）。"""
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    needed = "--proxy-bypass-list=127.0.0.1;localhost"
    current = os.environ.get(key, "").strip()
    if needed in current:
        return
    os.environ[key] = f"{current} {needed}".strip() if current else needed


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Stream Clips · 桌面主窗口")
    parser.add_argument("--selftest", action="store_true",
                        help="自检模式：N 秒后自动关窗并打印 WINDOW_OK")
    parser.add_argument("--selftest-seconds", type=float, default=5.0)
    parser.add_argument("--hidden", action="store_true", help="后台启动（不显示窗口）")
    args = parser.parse_args()

    # K16 保险：让 WebView2 明确绕过代理访问回环地址。
    # Chromium 默认就直连 localhost，但机器上装了抓包工具/企业代理时可能被改写，
    # 而前端（M1-D 起）要通过 http://127.0.0.1:port 调本地服务。
    # 追加而非覆盖：用户/企业可能已经设了这个变量。
    # 必须在 create_window / webview.start 之前设置 —— WebView2 环境一旦创建
    # 就不再读取该变量。
    _webview2_proxy_guard()

    try:
        import webview  # noqa: F401
    except ImportError as exc:
        print(f"[ERROR] 缺少 pywebview：{exc}", file=sys.stderr)
        print("        请确认使用的是工作区运行时："
              f"{ws.python_exe}", file=sys.stderr)
        return 3

    if not UI_INDEX.is_file():
        print(f"[ERROR] 找不到启动页：{UI_INDEX}", file=sys.stderr)
        return 4

    api = DesktopApi()
    try:
        window = build_window(api, hidden=args.hidden)
    except Exception as exc:  # WebView2 缺失等
        print(f"[ERROR] 创建窗口失败：{exc}", file=sys.stderr)
        print("        请安装 Microsoft Edge WebView2 Runtime 后重试。", file=sys.stderr)
        return 5

    if args.selftest:
        dom: dict = {}

        def _close_later() -> None:
            time.sleep(max(1.0, args.selftest_seconds))
            # 真窗口 DOM 断言（K42）：`.hidden` 若不是**通用**工具类，浮层会开机常显且关不掉。
            # 这类缺陷静态检查与语法检查都抓不到，只有读运行时 computed style 才知道。
            try:
                raw = window.evaluate_js(
                    "(function(){"
                    "  function d(id){var e=document.getElementById(id);"
                    "    return e?getComputedStyle(e).display:'MISSING';}"
                    "  return JSON.stringify({pop:d('statusPop'),scrim:d('statusScrim'),"
                    "                        record:d('page-record')});"
                    "})()")
                dom.update(json.loads(raw) if isinstance(raw, str) else (raw or {}))
            except Exception as exc:                                  # noqa: BLE001
                dom["error"] = f"{type(exc).__name__}: {exc}"
            try:
                window.destroy()
            except Exception:  # 窗口可能已关闭
                pass

        threading.Thread(target=_close_later, daemon=True).start()

    started = time.perf_counter()
    timings: dict[str, float] = {}

    def _mark(name: str) -> None:
        timings[name] = time.perf_counter() - started

    try:
        window.events.shown += lambda: _mark("shown")
        # storage_path 必须显式指定：否则 pywebview 会把 WebView2 用户数据
        # 写到 %LOCALAPPDATA%\Temp，违反 R2（K12）
        # private_mode=False 让登录态等数据持久化到工作区，发布模块需要
        from app.common import cover_designer_window
        cover_designer_window.register(api)  # v4.78 P-C：封面设计子窗口方法

        # v4.88（用户：关主程序时封面设计子窗不跟随关闭，桌面上留个孤儿窗）：
        # 主窗开始关闭时先把工具子窗收掉。closing 在 winforms 后端是"关前"钩子，
        # 返回 None/True 不拦截关闭；这里只做事、不改关闭流程。
        def _on_main_closing() -> None:
            try:
                cover_designer_window.close_cover_designer()
            except Exception:            # noqa: BLE001 — 收子窗失败也不能挡住主窗关闭
                pass

        for _ev in ("closing", "closed"):
            try:
                if _ev == "closing":
                    window.events.closing += _on_main_closing
                else:
                    window.events.closed += _on_main_closing
            except Exception:            # noqa: BLE001 — 老版本 pywebview 没有该事件
                pass

        def _cover_designer_loop():          # v4.78 P-C：事件循环就绪后消费待建子窗口
            import time as _time
            while True:
                try:
                    cover_designer_window.drain_pending()
                except Exception:            # noqa: BLE001
                    pass
                _time.sleep(0.3)

        webview.start(func=_cover_designer_loop,
                      debug=bool(os.environ.get("YJ_DEBUG")),
                      storage_path=str(ws.webview_data),
                      private_mode=False,
        )
    except Exception as exc:
        print(f"[ERROR] 窗口启动失败：{exc}", file=sys.stderr)
        return 6


    if args.selftest:
        shown = timings.get("shown")
        loaded = timings.get("loaded")
        parts = ["WINDOW_OK",
                 f"shown={shown:.2f}s" if shown else "shown=n/a",
                 f"loaded={loaded:.2f}s" if loaded else "loaded=n/a",
                 f"webview_data={ws.webview_data}"]
        if dom:
            ok_pop = dom.get("pop") == "none"
            ok_scrim = dom.get("scrim") == "none"
            ok_rec = dom.get("record") not in ("none", None, "MISSING")
            parts.append("dom="
                         + ("OK" if (ok_pop and ok_scrim and ok_rec) else "BAD")
                         + f"(pop={dom.get('pop')},scrim={dom.get('scrim')},"
                           f"record={dom.get('record')})")
            if not (ok_pop and ok_scrim and ok_rec):
                parts.append("HINT:.hidden 不是通用工具类？抽屉会开机常显且关不掉（K42）")
        print(" ".join(parts), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
