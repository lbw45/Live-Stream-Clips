#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动编排器 —— 「双击 → 出现主窗口」的完整流程。

严格对应文档 §7.4 的 15 步：
    1  定位模式（installed.marker）
    2  定位工作区（JACK_WORKSPACE_ROOT 优先）
    3  单实例锁（按工作区路径哈希命名）
    4  复用已运行服务（探测 editor /api/editor/health **+ slicer /version**；
       版本不一致 ⇒ 说明代码更新过而旧服务没退，改为**重启服务**，见 reload_services）
    5  分配端口 + 生成 session token
    6  「重新连接」复用原端口+token
    7  收紧凭据文件权限（K9：用 icacls，不用 Get-Acl）
    8  选 Python（工作区便携运行时优先）
    9  选桌面宿主
   10  Whisper 专用 Python
   11 定位 ffmpeg / ffprobe
   12 先显示桌面窗口
   13 起两个服务（隐藏窗口 + 日志重定向 + pid 文件）
   14 健康检查（进程退出立即报错，不干等超时）
   15 错误提示

**与原版的实现差异（D22）**：原版用 PowerShell 脚本编排，本项目改用 Python 实现。
理由：逻辑可单测、可直接复用 `app.common.paths` 的路径解析、不依赖 PowerShell 版本差异
（原版自己就注释吐槽过 WinPS 5.1 的模块自动加载问题，见 K9）。`.runtime/*.ps1` 保留为薄封装。

用法：
    python launcher/orchestrator.py start            # 启动（含桌面窗口）
    python launcher/orchestrator.py start --services-only
    python launcher/orchestrator.py stop             # 停止全部服务
    python launcher/orchestrator.py restart          # 全部重启（含窗口）
    python launcher/orchestrator.py reload-services  # 只重启服务、保留窗口（改了后端代码用）
    python launcher/orchestrator.py status           # 查看状态（会提示「旧服务占着不放」）

**改了后端代码后为什么必须 reload/restart**：服务是**常驻后台进程**，窗口关掉它也不退。
第 4 步会复用它们 —— 于是只重开窗口的话，你看到的是**新窗口 + 旧后端**。v3.33 起
`start` 会自己比对运行版本与代码版本，不一致就自动重启服务（正在拉流时避让，只警告）。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.paths import APP_ID, APP_TITLE, APP_VERSION, ws  # noqa: E402
from app.common.runtime_probe import http_json, load_ports  # noqa: E402

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
IS_WINDOWS = os.name == "nt"

# 服务进程的创建标志（§7.4 第 13 步）：
#   必须脱离父进程的控制台/进程组，否则用户关掉启动它的终端窗口会发
#   CTRL_CLOSE_EVENT，把服务一起带走。
#   注意：DETACHED_PROCESS 与 CREATE_NO_WINDOW 互斥，不能同时指定；
#   已经完全没有控制台时，CREATE_NO_WINDOW 是多余的。
SERVICE_FLAGS = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if IS_WINDOWS else 0
)

# 默认端口（§7.4 第 5 步；实际以动态分配为准）
DEFAULT_SLICER_PORT = 7870
DEFAULT_EDITOR_PORT = 8140

MIN_TOKEN_LENGTH = 32


# ---------------------------------------------------------------- 基础工具

def log(message: str) -> None:
    print(f"[launcher] {message}", flush=True)


def free_port() -> int:
    """让系统分配一个空闲的 loopback 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def new_token() -> str:
    return secrets.token_hex(32)          # 64 位十六进制


def service_needs_reload(running_version: str,
                         current_version: str = APP_VERSION) -> bool:
    """运行中的服务版本与当前代码**是否不一致**（不一致 ⇒ 得重启服务，新代码才生效）。

    v3.33 为什么要这个判据：第 4 步「复用已运行服务」原本只探 editor 健康就复用，**不看版本**。
    于是「代码更新过、旧服务还活着」时，新窗口（HTML 从磁盘读，天然是新的）会连上**旧后端**，
    出现「界面是新的、行为是旧的」这种最迷惑的混合状态 —— 用户看到的是「功能明明改了却没用」
    （实测：窗口标题已是 v3.32，slicer 服务却还在 v3.28，解析抖音号报的还是老文案）。

    ⚠️ 探测失败（``running_version`` 为空）**不算陈旧**：宁可不重启，也不要因为一次探测抖动
    就把用户可能正在录的服务停掉。「探测不到」与「确实是旧版」是两回事。
    """
    return bool(running_version) and running_version != current_version


def process_create_time(pid: int) -> int | None:
    """取进程创建时间（Windows FILETIME 整数）。取不到返回 None。

    ⚠️ 为什么需要它：**Windows 会复用 PID**。只凭 `pid_alive()` 判断，
    一个已死的服务留下的旧 PID 很可能被系统分配给别的进程，于是
    `stop_all()` 会 `TerminateProcess` 掉一个**完全无关的进程**（可能是用户的
    编辑器、浏览器）。启动时记下创建时间，停止前比对，就能识破 PID 复用。
    """
    if pid <= 0 or not IS_WINDOWS:
        return None
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time))
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int, created: int | None = None) -> bool:
    """进程是否存活。给了 ``created`` 就同时校验创建时间，识破 PID 复用。"""
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    if created:
        actual = process_create_time(pid)
        if actual is not None and actual != created:
            return False          # 同号不同命 → 已被复用
    return True


def read_pid_record(path: Path) -> dict:
    """读 pid 文件。格式：第一行 pid，第二行创建时间（旧文件可能只有一行）。"""
    try:
        lines = path.read_text(encoding="ascii").strip().splitlines()
        pid = int(lines[0])
    except (OSError, ValueError, IndexError):
        return {"pid": 0, "created": None}
    created = None
    if len(lines) > 1:
        try:
            created = int(lines[1])
        except ValueError:
            created = None
    return {"pid": pid, "created": created}


def write_pid(path: Path, pid: int) -> None:
    """写 pid 文件，**同时记下创建时间**（见 process_create_time 的说明）。"""
    created = process_create_time(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = str(pid) if created is None else f"{pid}\n{created}"
    path.write_text(payload, encoding="ascii")


# ---------------------------------------------------------------- 单实例锁

_mutex_handle = None


def acquire_single_instance_lock() -> bool:
    """按**工作区路径哈希**命名互斥体（§7.4 第 3 步）。

    这样便携版与安装版（工作区不同）可以同时运行，而同一工作区只会有一个启动器。
    """
    if not IS_WINDOWS:
        return True
    digest = hashlib.sha256(str(ws.root).lower().encode("utf-8")).hexdigest()[:16]
    name = f"Local\\{APP_ID}.StartCore.{digest}"
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    already_exists = kernel32.GetLastError() == 183   # ERROR_ALREADY_EXISTS
    global _mutex_handle
    _mutex_handle = handle
    if already_exists:
        log("已有启动器在处理同一工作区，本次退出")
        return False
    return True


# ---------------------------------------------------------------- 端口与令牌

def write_ports(slicer_port: int, editor_port: int, token: str) -> None:
    ws.ensure_dirs()
    payload = {
        "slicerPort": slicer_port,
        "editorPort": editor_port,
        "sessionToken": token,
    }
    ws.ports_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    harden_permissions(ws.ports_path)


def harden_permissions(path: Path) -> None:
    """收紧凭据文件权限至「当前用户 + SYSTEM + Administrators」（§7.4 第 7 步 / K9）。

    注意：不用 PowerShell 的 Get-Acl/Set-Acl，直接调 icacls（原版同款做法）。
    """
    if not IS_WINDOWS:
        return
    try:
        current_sid = _current_user_sid()
    except Exception as exc:      # 拿不到 SID 就跳过，不阻塞启动
        log(f"跳过权限收紧（无法获取 SID）：{exc}")
        return
    icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    if not icacls.is_file():
        return
    args = [
        str(icacls), str(path),
        "/inheritance:r", "/grant:r",
        f"*{current_sid}:(F)", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)",
        "/Q",
    ]
    try:
        subprocess.run(args, capture_output=True, timeout=15, check=False,
                       creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"权限收紧失败（不影响运行）：{exc}")


def _current_user_sid() -> str:
    """取当前进程用户 SID（K15：**不依赖 pywin32**，便携运行时里没有这个包）。

    三条路径依次尝试：
        1. ctypes 直调 advapi32（首选，无外部依赖、无子进程开销）
        2. ``whoami /user`` 解析（ctypes 被安全软件拦时的兜底）
        3. 失败则抛异常，由调用方跳过权限收紧，不阻塞启动
    """
    try:
        return _sid_via_ctypes()
    except Exception as exc:              # noqa: BLE001 —— 逐级兜底
        log(f"ctypes 取 SID 失败（{exc}），改用 whoami")
    return _sid_via_whoami()


def _sid_via_ctypes() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    # x64 下必须声明 restype/argtypes，否则伪句柄会被截断成 32 位
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(f"OpenProcessToken 失败，err={ctypes.get_last_error()}")
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
        if size.value == 0:
            raise OSError("GetTokenInformation 未返回所需长度")
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
                token, TokenUser, buf, size.value, ctypes.byref(size)):
            raise OSError(f"GetTokenInformation 失败，err={ctypes.get_last_error()}")
        # TOKEN_USER 首字段为 SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid_ptr:
            raise OSError("TOKEN_USER.Sid 为空")
        str_ptr = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
            raise OSError(f"ConvertSidToStringSidW 失败，err={ctypes.get_last_error()}")
        try:
            sid = str_ptr.value or ""
        finally:
            kernel32.LocalFree(str_ptr)
        if not sid.startswith("S-"):
            raise OSError(f"SID 格式异常：{sid!r}")
        return sid
    finally:
        kernel32.CloseHandle(token)


def _sid_via_whoami() -> str:
    """兜底：``whoami /user`` 输出形如 ``desktop-cj686r1\\29257 S-1-5-21-...-1001``。"""
    whoami = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    if not whoami.is_file():
        raise FileNotFoundError("找不到 whoami.exe")
    result = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15, check=False, creationflags=CREATE_NO_WINDOW,
    )
    for chunk in result.stdout.replace('"', "").replace("'", "").split(","):
        chunk = chunk.strip()
        if chunk.startswith("S-1-"):
            return chunk
    raise OSError(f"whoami 输出中未找到 SID：{result.stdout.strip()!r}")


# ---------------------------------------------------------------- 运行时探测

def resolve_python() -> Path:
    """选 Python（§7.4 第 8 步）：工作区便携运行时优先，不逐个 import 验证。"""
    if ws.has_portable_runtime():
        return ws.python_exe
    if ws.python_exe.is_file():
        return ws.python_exe
    return Path(sys.executable)


def resolve_ffmpeg() -> tuple[Path | None, Path | None]:
    """定位 ffmpeg / ffprobe（§7.4 第 11 步）：工作区优先，其次 PATH。"""
    import shutil

    ffmpeg = ws.ffmpeg if ws.ffmpeg.is_file() else None
    ffprobe = ws.ffprobe if ws.ffprobe.is_file() else None
    if ffmpeg is None:
        found = shutil.which("ffmpeg")
        ffmpeg = Path(found) if found else None
    if ffprobe is None:
        found = shutil.which("ffprobe")
        ffprobe = Path(found) if found else None
    return ffmpeg, ffprobe


# ---------------------------------------------------------------- 服务

class ServiceSpec:
    def __init__(self, name: str, module: str, cwd: Path, port_env: str,
                 pid_path: Path, log_base: str) -> None:
        self.name = name
        self.module = module
        self.cwd = cwd
        self.port_env = port_env
        self.pid_path = pid_path
        self.log_base = log_base

    def out_log(self) -> Path:
        return ws.log_dir / f"{self.log_base}.out.log"

    def err_log(self) -> Path:
        return ws.log_dir / f"{self.log_base}.err.log"


class Orchestrator:
    def __init__(self, reuse_session: bool = False) -> None:
        self.reuse_session = reuse_session
        self.python = resolve_python()
        self.ffmpeg, self.ffprobe = resolve_ffmpeg()
        self.slicer = ServiceSpec(
            name="slicer",
            module="app.slicer.web_server",
            cwd=PROJECT_ROOT,
            port_env="SLICE_PORT",
            pid_path=ws.slicer_runtime / "slicer.pid",
            log_base="slicer",
        )
        self.editor = ServiceSpec(
            name="editor",
            module="app.editor.backend",
            cwd=PROJECT_ROOT,
            port_env="EDITOR_PORT",
            pid_path=ws.editor_runtime / "editor.pid",
            log_base="editor",
        )
        self.desktop_pid_path = ws.runtime_data / "desktop.pid"
        self.slicer_port = 0
        self.editor_port = 0
        self.token = ""
        self.processes: list[subprocess.Popen] = []

    # --- 步骤 4：复用已运行服务 ---

    def probe_existing(self) -> dict | None:
        """探测已运行的服务（第 4 步）。

        v3.33：**连在跑的 slicer 版本一起探**（``/version`` 免鉴权）。版本不一致的处置见
        :meth:`reload_services` —— 关键是要能区分「同一份代码的重复启动」与「旧服务占着不放」。
        """
        saved = load_ports()
        if not saved:
            return None
        url = f"http://127.0.0.1:{saved['editorPort']}/api/editor/health"
        payload = http_json(url)
        if not (payload and payload.get("ok")):
            return None
        info = http_json(f"http://127.0.0.1:{saved['slicerPort']}/version")
        saved["slicerVersion"] = str((info or {}).get("version") or "")
        return saved

    def recording_active(self, saved: dict) -> bool:
        """有没有录制器**此刻正在拉流**（重启服务会打断它 → 必须避让）。

        判据用 ``recording.pulling``（ffmpeg 进程活着），不用 ``state`` ——
        「监控中等开播」和「正在录制」在 state 里都是 ``waiting``，分不开（见
        :attr:`app.slicer.recorder.LiveRecorder.pulling`）。``state == "recording"``
        一并认（防御：万一将来真用了这个值）。探测失败按「没在录」处理（不阻塞更新）。
        """
        data = http_json(f"http://127.0.0.1:{saved['slicerPort']}/api/recorders",
                         token=saved.get("sessionToken"))
        for rec in (data or {}).get("recorders") or []:
            if str(rec.get("state")) == "recording":
                return True
            if (rec.get("recording") or {}).get("pulling"):
                return True
        return False

    def reload_services(self, saved: dict, *, reason: str) -> int:
        """停下旧服务、用**原端口与原令牌**起新服务（已开着的窗口不用重开）。

        对齐参考实现 ``launcher\\.runtime\\reload-services.ps1``
        （stop-core → start-core，且 ``JACK_REUSE_LOCAL_SESSION=1`` / ``JACK_KEEP_DESKTOP=1``）：
        **端口与令牌绝不能变**，否则已经开着的窗口会失联。窗口的 UI 是从磁盘读的
        （``desktop_entry.py`` 用 ``UI_INDEX.as_uri()``），所以重启服务后前端本来就是新的，
        只有**后端**需要这样换来生效。

        正在拉流时**不自动重启**（会打断录制）—— 只警告并给出用户能照做的命令。
        """
        log(f"⚠️ 检测到旧服务：{reason}（新代码尚未生效）")
        if self.recording_active(saved):
            log("   当前有录制器**正在拉流**，不自动重启（重启会打断这场录制）。")
            log("   请等这场录完后执行：python launcher\\orchestrator.py reload-services")
            return 0
        log("→ 重启服务以加载新代码（沿用原端口与原令牌，窗口无需重开）")
        stop_all(keep_desktop=True)
        for _ in range(25):                     # 最多等 5s 让旧服务真的退下去
            if not http_json(f"http://127.0.0.1:{saved['editorPort']}/api/editor/health"):
                break
            time.sleep(0.2)
        self.slicer_port = saved["slicerPort"]
        self.editor_port = saved["editorPort"]
        self.token = saved["sessionToken"]
        self.start_service(self.slicer, self.slicer_port)
        self.start_service(self.editor, self.editor_port)
        if not self.wait_healthy():
            log("服务重启失败，请手动执行：python launcher\\orchestrator.py restart")
            return 1
        log(f"服务已重启 → v{APP_VERSION}（新代码已生效）；"
            "若窗口停留在旧界面，点右上角「刷新」即可")
        return 0

    # --- 步骤 5/6：端口与令牌 ---

    def prepare_ports(self) -> None:
        if self.reuse_session:
            saved = load_ports()
            if saved:
                self.slicer_port = saved["slicerPort"]
                self.editor_port = saved["editorPort"]
                self.token = saved["sessionToken"]
                log(f"复用原会话：slicer={self.slicer_port} editor={self.editor_port}")
                return
        self.slicer_port = free_port()
        do_while_guard = 0
        self.editor_port = free_port()
        while self.editor_port == self.slicer_port and do_while_guard < 10:
            self.editor_port = free_port()
            do_while_guard += 1
        self.token = new_token()
        write_ports(self.slicer_port, self.editor_port, self.token)
        log(f"分配端口：slicer={self.slicer_port} editor={self.editor_port}")

    # --- 步骤 13：起服务 ---

    def _service_env(self, spec: ServiceSpec, port: int) -> dict:
        ws.ensure_dirs()
        extra = {
            "PYTHONPATH": str(PROJECT_ROOT),
            spec.port_env: str(port),
            "JACK_LOCAL_SESSION_TOKEN": self.token,
            "LIVE_SLICER_URL": f"http://127.0.0.1:{self.slicer_port}",
            "EDITOR_CORS_ORIGINS": (
                f"http://127.0.0.1:{self.editor_port},http://localhost:{self.editor_port}"
            ),
        }
        if self.ffmpeg:
            extra["FFMPEG_PATH"] = str(self.ffmpeg)
        if self.ffprobe:
            extra["FFPROBE_PATH"] = str(self.ffprobe)

        # 目录链（供服务读取；实际值后续由设置页覆盖）
        extra.update({
            "SLICE_VIDEO_DIR": str(ws.live_source_dir),
            "SLICE_OUTPUT_DIR": str(ws.slice_dir),
            "SLICE_DATA_DIR": str(ws.slicer_runtime),
            "SOURCE_SRT_DIR": str(ws.live_source_dir),
            "MUSIC_DIR": str(ws.music_dir),
            "BACKGROUND_VIDEO_DIR": str(ws.background_dir),
            "OUTPUT_DIR": str(ws.editor_output_dir),
            "RUNTIME_DIR": str(ws.editor_runtime),
            "JACK_SETTINGS_PATH": str(ws.settings_path),
            "PLAYWRIGHT_BROWSERS_PATH": str(ws.chromium_dir),
            "SLICE_WHISPER_PYTHON": str(self.python),
            "SLICE_CUDA_RUNTIME_DIR": str(ws.cuda_dir),
            "SUBTITLE_CUDA_RUNTIME_DIR": str(ws.cuda_dir),
            "SLICE_AUTO_WATCH": "1",
        })

        env = ws.subprocess_env(extra)
        # 开发模式下若已存在本地 whisper 模型，直接指定模型目录，避免联网下载
        if (ws.models_dir / "small" / "model.bin").is_file():
            env["SLICE_WHISPER_MODEL"] = str(ws.models_dir / "small")
        return env

    def start_service(self, spec: ServiceSpec, port: int) -> subprocess.Popen:
        spec.out_log().parent.mkdir(parents=True, exist_ok=True)
        env = self._service_env(spec, port)
        out = open(spec.out_log(), "w", encoding="utf-8")
        err = open(spec.err_log(), "w", encoding="utf-8")
        proc = subprocess.Popen(
            [str(self.python), "-m", spec.module],
            cwd=str(spec.cwd),
            env=env,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=SERVICE_FLAGS,
        )
        write_pid(spec.pid_path, proc.pid)
        log(f"启动 {spec.name} (pid={proc.pid}, port={port})")
        self.processes.append(proc)
        return proc

    # --- 步骤 14：健康检查 ---

    def wait_healthy(self) -> bool:
        checks = [
            (self.slicer, f"http://127.0.0.1:{self.slicer_port}/version", "素材切片服务"),
            (self.editor, f"http://127.0.0.1:{self.editor_port}/api/editor/health", "剪辑工作台"),
        ]
        deadline = time.time() + 60
        pending = dict(enumerate(checks))
        while pending and time.time() < deadline:
            for index in list(pending):
                spec, url, label = pending[index]
                if http_json(url, timeout=1.5):
                    log(f"{label} 已就绪")
                    pending.pop(index)
                    continue
                proc = self.processes[index] if index < len(self.processes) else None
                if proc is not None and proc.poll() is not None:
                    # 进程已退出 → 立即报错，不干等超时（§7.4 第 14 步）
                    log(f"{label} 启动失败（进程已退出，exit={proc.returncode}）")
                    self._dump_log(spec)
                    return False
            time.sleep(0.3)
        if pending:
            for index, (spec, _url, label) in pending.items():
                log(f"{label} 启动超时")
                self._dump_log(spec)
            return False
        return True

    @staticmethod
    def _dump_log(spec: ServiceSpec, lines: int = 20) -> None:
        for path in (spec.err_log(), spec.out_log()):
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if not content:
                continue
            log(f"--- {path.name} 末尾 {lines} 行 ---")
            for line in content[-lines:]:
                print(f"        {line}", flush=True)

    # --- 桌面窗口 ---

    def _desktop_hwnd(self) -> int:
        """找桌面窗口的 hwnd（0 = 没有窗口）。

        为什么不只看 pid：pywebview 的 WebView2 窗口在长时间挂机/显卡资源紧张时
        可能被系统回收，但**进程还活着**（用户实测：挂一会窗口消失，再点启动
        却提示"已在运行，不再重复打开" —— PID 骗人，窗口早没了）。
        """
        try:
            from app.common.window_focus import _user32
            u = _user32()
            if not u:
                return 0
            # build_window 的标题是 "{APP_TITLE} v{APP_VERSION}"；WebView2 偶尔会改写
            # 后缀，所以先用精确标题找，再退回"以 APP_TITLE 开头"的枚举。
            hwnd = int(u.FindWindowW(None, f"{APP_TITLE} v{APP_VERSION}") or 0)
            if hwnd:
                return hwnd
            found: list[int] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _cb(hwnd_, _lp):
                n = u.GetWindowTextLengthW(hwnd_)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    u.GetWindowTextW(hwnd_, buf, n + 1)
                    if (buf.value or "").startswith(APP_TITLE):
                        found.append(int(hwnd_))
                return True

            u.EnumWindows(WNDENUMPROC(_cb), 0)
            return found[0] if found else 0
        except Exception:
            return 0

    def desktop_running(self) -> int:
        record = read_pid_record(self.desktop_pid_path)
        pid = record["pid"]
        if not (pid and pid_alive(pid, record["created"])):
            return 0
        if not self._desktop_hwnd():
            # 进程在、窗口没了 → 僵尸，杀掉让上层重开（否则用户永远打不开）
            log(f"桌面进程 {pid} 还在但窗口已消失（僵尸），清理后重新打开")
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            try:
                self.desktop_pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        return pid

    def desktop_host(self) -> Path:
        """选桌面宿主（§7.4 第 9 步）：优先 pythonw.exe，避免闪出一个黑控制台。

        注意：**不能用 CREATE_NO_WINDOW**，那会让窗口也被隐藏；这里要的是
        「没有控制台」，而 pythonw 本身就不创建控制台。
        """
        pythonw = self.python.with_name("pythonw.exe")
        return pythonw if pythonw.is_file() else self.python

    def start_desktop(self) -> int:
        entry = PROJECT_ROOT / "launcher" / "desktop_entry.py"
        if not entry.is_file():
            log(f"找不到桌面入口：{entry}")
            return 0
        host = self.desktop_host()
        out = open(ws.log_dir / "desktop.out.log", "w", encoding="utf-8")
        err = open(ws.log_dir / "desktop.err.log", "w", encoding="utf-8")
        env = ws.subprocess_env({
            "PYTHONPATH": str(PROJECT_ROOT),
            "JACK_LOCAL_SESSION_TOKEN": self.token,
            "EDITOR_URL": f"http://127.0.0.1:{self.editor_port}",
        })
        # K17：必须脱离父进程。用户双击 .bat 后一定会顺手关掉那个 cmd 窗口，
        # 若不脱离，CTRL_CLOSE_EVENT 会把桌面窗口一起带走。
        proc = subprocess.Popen(
            [str(host), str(entry)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            creationflags=SERVICE_FLAGS,
        )
        write_pid(self.desktop_pid_path, proc.pid)
        log(f"启动桌面窗口 (pid={proc.pid}, host={host.name})")
        return proc.pid

    # --- 主流程 ---

    def run(self, services_only: bool = False) -> int:
        if not acquire_single_instance_lock():
            return 0

        ws.ensure_dirs()
        log(f"{APP_TITLE} v{APP_VERSION}")
        log(f"工作区 {ws.root}")
        log(f"Python  {self.python}")
        if not self.ffmpeg:
            log("警告：未找到 ffmpeg，录制与渲染功能不可用")
        if (ws.cuda_dir / "cublas64_12.dll").is_file():
            log(f"CUDA   {ws.cuda_dir}")
        else:
            log("警告：未找到 CUDA 运行时，转写将回退 CPU")

        # 步骤 4：已有服务在跑 → 只开窗口连上去
        existing = self.probe_existing()
        if existing:
            # v4.10：无论接下来是复用还是重载，都先恢复会话三元组。
            # 否则「版本不一致 → reload_services() → 有录制任务而跳过」时端口/token 仍是空，
            # 继续开窗会连到 port=0（K161：判据与初始化都要放在分叉之前）。
            self.slicer_port = existing["slicerPort"]
            self.editor_port = existing["editorPort"]
            self.token = existing["sessionToken"]
            running = existing.get("slicerVersion") or ""
            # v3.33：版本不一致 = 代码更新过但旧服务没退 → 必须先重启服务，
            # 否则新窗口会连上旧后端（「界面是新的、功能是旧的」，见 service_needs_reload）
            if service_needs_reload(running):
                rc = self.reload_services(
                    existing,
                    reason=f"运行中的服务是 v{running}，当前代码是 v{APP_VERSION}")
                if rc:
                    return rc
                # v4.10（用户 2026-10-31：「每次 start 都要点两三次，第一次卡住没反应」）：
                # 这里原来直接 return reload_services(...)，于是**每次版本更新后的第一次 start
                # 只重启服务、不开窗口**；第二次点版本一致了才开窗，看起来就像第一次卡死。
                # 现在重载完成（或因录制中安全跳过）后继续走下面的开窗步骤。
            else:
                log("检测到已在运行的服务"
                    f"（editor={existing['editorPort']}，v{running}），复用")
            if not services_only:
                pid = self.desktop_running()
                if pid:
                    # 已有窗口：**把它切到前台**（原来只打一行日志就退出 ——
                    # 用户点了启动却看不到窗口，以为坏了）。
                    hwnd = self._desktop_hwnd()
                    raised = False
                    if hwnd:
                        try:
                            from app.common.window_focus import bring_to_front
                            raised = bring_to_front(hwnd)
                        except Exception:
                            raised = False
                    if raised:
                        log(f"桌面窗口已在运行 (pid={pid})，已切到前台")
                    else:
                        log(f"桌面窗口已在运行 (pid={pid})，但切前台失败 —— "
                            "如果还是看不到窗口，结束该进程后重新启动即可"
                            "（任务管理器里找 Live Stream Clips）")
                else:
                    self.start_desktop()
            return 0

        self.prepare_ports()

        if not services_only:
            # 步骤 12：先显示桌面窗口，避免用户对着黑屏等
            self.start_desktop()

        # 步骤 13：并行起两个服务
        self.start_service(self.slicer, self.slicer_port)
        self.start_service(self.editor, self.editor_port)

        # 步骤 14
        if not self.wait_healthy():
            print(f"[launcher] 启动失败，请查看日志目录：{ws.log_dir}", flush=True)
            return 1

        log(f"就绪 → http://127.0.0.1:{self.editor_port}")
        log(f"运行数据 {ws.runtime_data}")
        if services_only:
            log("服务已在后台运行（--services-only）；用 orchestrator.py stop 停止")
        return 0


# ---------------------------------------------------------------- 停止

def stop_all(keep_desktop: bool = False) -> int:
    """停止全部服务（读 pid 文件）。

    ⚠️ 必须校验创建时间：Windows 会复用 PID，只看「这个号有进程在」就
    `TerminateProcess`，会杀掉无关的用户进程。见 process_create_time()。
    """
    targets = [
        ("slicer", ws.slicer_runtime / "slicer.pid"),
        ("editor", ws.editor_runtime / "editor.pid"),
    ]
    if not keep_desktop:
        targets.append(("desktop", ws.runtime_data / "desktop.pid"))

    stopped = 0
    for name, pid_path in targets:
        record = read_pid_record(pid_path)
        pid, created = record["pid"], record["created"]
        if not pid:
            continue
        if not pid_alive(pid, created):
            if created is not None and pid_alive(pid):
                log(f"{name} 的 pid={pid} 已被系统复用给其他进程，跳过（只清理 pid 文件）")
            else:
                log(f"{name} 未在运行（清理 pid 文件）")
            pid_path.unlink(missing_ok=True)
            continue
        log(f"停止 {name} (pid={pid})")
        _terminate(pid)
        pid_path.unlink(missing_ok=True)
        stopped += 1
    if stopped == 0:
        log("没有需要停止的进程")
    return 0


def _terminate(pid: int, timeout: float = 6.0) -> None:
    if not IS_WINDOWS:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        return
    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if handle:
        kernel32.TerminateProcess(handle, 0)
        kernel32.CloseHandle(handle)
        return
    # 兜底：taskkill
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ---------------------------------------------------------------- 状态

def show_status() -> int:
    saved = load_ports()
    print(f"{APP_TITLE} v{APP_VERSION}")
    print(f"工作区 {ws.root}")
    print(f"端口配置 {ws.ports_path} -> {'存在' if saved else '不存在'}")
    if saved:
        print(f"  slicer={saved['slicerPort']} editor={saved['editorPort']} "
              f"token=***{saved['sessionToken'][-6:]}")
    running_slicer_version = ""
    for name, module_url in (
        ("slicer", f"http://127.0.0.1:{saved['slicerPort']}/version" if saved else ""),
        ("editor", f"http://127.0.0.1:{saved['editorPort']}/api/editor/health" if saved else ""),
    ):
        pid_path = (ws.slicer_runtime / "slicer.pid") if name == "slicer" \
            else (ws.editor_runtime / "editor.pid")
        record = read_pid_record(pid_path)
        pid, created = record["pid"], record["created"]
        alive = pid_alive(pid, created)
        # PID 复用会让「进程=在」骗人，健康端点才是权威判据
        recycled = bool(pid and not alive and created is not None and pid_alive(pid))
        payload = http_json(module_url) if module_url else None
        healthy = bool(payload)
        if name == "slicer" and payload:
            running_slicer_version = str(payload.get("version") or "")
        state = "已被复用" if recycled else ("在" if alive else "不在")
        extra = f" 版本=v{running_slicer_version}" if (name == "slicer"
                                                and running_slicer_version) else ""
        print(f"  {name:<7} pid={pid or '-':<7} 进程={state:<5} "
              f"健康={'OK' if healthy else 'FAIL'}{extra}")
    # v3.33：把「旧服务还占着」这件事显式打出来 —— 它最容易被忽略，
    # 表现是「明明改了代码却像没生效」（v3.32 就栽在这上面）
    if service_needs_reload(running_slicer_version):
        print(f"  ⚠️ 运行中的服务是 v{running_slicer_version}，当前代码是 v{APP_VERSION} "
              f"→ 新代码未生效，请运行：orchestrator.py reload-services")
    record = read_pid_record(ws.runtime_data / "desktop.pid")
    pid, created = record["pid"], record["created"]
    alive = pid_alive(pid, created)
    recycled = bool(pid and not alive and created is not None and pid_alive(pid))
    state = "已被复用" if recycled else ("在" if alive else "不在")
    print(f"  desktop pid={pid or '-':<7} 进程={state}")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_TITLE} 启动编排器")
    parser.add_argument("action", nargs="?", default="start",
                        choices=["start", "stop", "status", "restart",
                                 "reload-services"])
    parser.add_argument("--services-only", action="store_true",
                        help="只起服务，不打开桌面窗口")
    parser.add_argument("--reuse-session", action="store_true",
                        help="复用 local-service-ports.json 中的原端口与令牌")
    args = parser.parse_args()

    if args.action == "start":
        return Orchestrator(reuse_session=args.reuse_session).run(
            services_only=args.services_only)
    if args.action == "stop":
        return stop_all()
    if args.action == "restart":
        stop_all()
        time.sleep(1.0)
        return Orchestrator().run(services_only=args.services_only)
    if args.action == "reload-services":
        # 对齐参考实现 reload-services.ps1：**只重启服务、保留窗口**，沿用原端口与原令牌。
        # 「改了后端代码但窗口开着不想关」时手动跑这条（start 也会自动检测版本不一致后走它）。
        saved = load_ports()
        alive = bool(saved) and bool(http_json(
            f"http://127.0.0.1:{saved['editorPort']}/api/editor/health"))
        if not alive:
            log("没有正在运行的服务，直接启动")
            return Orchestrator().run(services_only=args.services_only)
        return Orchestrator().reload_services(saved, reason="用户手动重载服务")
    return show_status()


if __name__ == "__main__":
    sys.exit(main())
