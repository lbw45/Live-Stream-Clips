# -*- coding: utf-8 -*-
"""FFmpeg 一键下载（开源版 2026-11-20）。

为什么需要：ffmpeg / ffprobe 是录制、转写、渲染的硬依赖，但它是外部二进制、
不能随仓库分发（体积 + 许可）。安装器里做了可选的 FFmpeg 下载 Section，
网络不通或用户取消了勾选时，应用内必须有一条自愈路径 —— 否则录制直接失败。

状态结构与 gpu.repair 同一套口径（running/log/done/ok/error），前端 pollGpuTask 复用。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from app.common import proc
from app.common.paths import ws

#: 下载源（主源 gyan.dev 是 ffmpeg 官方推荐 Windows 构建；备源 GitHub BtbN）
SOURCES: tuple[tuple[str, str], ...] = (
    ("gyan.dev", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("BtbN/GitHub", "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                    "ffmpeg-master-latest-win64-gpl.zip"),
)

_lock = threading.Lock()
_state: dict = {"running": False, "startedAt": 0.0, "log": [], "done": False,
                "ok": None, "error": "", "downloadedMb": 0.0}


def _log(msg: str) -> None:
    with _lock:
        _state["log"].append(msg)
    print(f"[ffmpeg-dl] {msg}", flush=True)


def version_text() -> str:
    """返回本机 ffmpeg 的版本行（缺文件/跑不起来时返回空串）。"""
    exe = ws.ffmpeg
    if not exe.is_file():
        return ""
    try:
        r = proc.run([str(exe), "-version"], capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=20)
        first = (r.stdout or "").strip().splitlines()[:1]
        return first[0][:120] if first else ""
    except Exception:
        return ""


def state() -> dict:
    with _lock:
        snap = dict(_state)
        snap["log"] = list(_state["log"])
    ver = version_text()
    snap["present"] = bool(ver)
    snap["version"] = ver
    snap["path"] = str(ws.ffmpeg)
    snap["binDir"] = str(ws.bin_dir)
    snap["sizeMb"] = round(
        (ws.ffmpeg.stat().st_size + ws.ffprobe.stat().st_size) / 1024 / 1024, 1
    ) if ws.ffmpeg.is_file() and ws.ffprobe.is_file() else 0.0
    snap["elapsedSec"] = (round(time.time() - _state["startedAt"], 1)
                          if _state["startedAt"] else 0)
    return snap


def start_download() -> dict:
    """后台下载并解出 ffmpeg.exe / ffprobe.exe 到 workspace\\bin。"""
    with _lock:
        if _state["running"]:
            return {"started": False, "busy": True, "state": state()}
        if version_text():
            return {"started": False, "alreadyPresent": True, "state": state()}
        _state.update({"running": True, "startedAt": time.time(), "log": [],
                       "done": False, "ok": None, "error": "", "downloadedMb": 0.0})
    threading.Thread(target=_run, daemon=True, name="ffmpeg-dl").start()
    return {"started": True, "state": state()}


def _download(url: str, dst: Path, label: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LiveStreamClips/ffmpeg-setup"})
        with urllib.request.urlopen(req, timeout=90) as resp, open(dst, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got, last = 0, 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if got - last >= 8 * 1024 * 1024:
                    last = got
                    with _lock:
                        _state["downloadedMb"] = round(got / 1024 / 1024, 1)
                    _log(f"{label} 已下载 {got / 1024 / 1024:.0f} MB"
                         + (f" / {total / 1024 / 1024:.0f} MB" if total else ""))
        return dst.stat().st_size > 10 * 1024 * 1024
    except Exception as exc:                       # noqa: BLE001
        _log(f"{label} 下载失败：{type(exc).__name__}: {exc}")
        return False


def _extract(zp: Path) -> int:
    """从 zip 里只挑 ffmpeg.exe / ffprobe.exe（其余几十 MB 不解）。"""
    ws.bin_dir.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe": ws.ffmpeg, "ffprobe.exe": ws.ffprobe}
    got = 0
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if base not in wanted:
                continue
            if "/bin/" not in info.filename.replace("\\", "/"):
                continue
            dst = wanted[base]
            with z.open(info) as src, open(dst, "wb") as out:
                while True:
                    b = src.read(256 * 1024)
                    if not b:
                        break
                    out.write(b)
            got += 1
            _log(f"已解出 {base}（{dst.stat().st_size / 1024 / 1024:.0f} MB）")
    return got


def _run() -> None:
    tmp = ws.runtime_data / "ffmpeg_dl"
    tmp.mkdir(parents=True, exist_ok=True)
    zp = tmp / "ffmpeg.zip"
    ok = False
    err = ""
    try:
        for label, url in SOURCES:
            _log(f"尝试从 {label} 下载 FFmpeg（约 90~120 MB）…")
            if not _download(url, zp, label):
                continue
            n = _extract(zp)
            if n < 2:
                _log(f"{label} 的包里没找齐 ffmpeg/ffprobe（解出 {n} 个），换下一个源")
                continue
            ver = version_text()
            if ver:
                _log(f"校验通过：{ver}")
                ok = True
                break
            _log("解出的文件跑不起来（可能是 32/64 位不匹配或被杀软拦截）")
        if not ok:
            err = ("FFmpeg 下载失败。可在浏览器打开 https://www.gyan.dev/ffmpeg/builds/ "
                   "下载 ffmpeg-release-essentials.zip，解压后把 ffmpeg.exe 和 ffprobe.exe "
                   f"放进 {ws.bin_dir}，再回来点「重新检测」。")
            _log(err)
    except BaseException as exc:                   # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        _log(err)
    finally:
        try:
            zp.unlink(missing_ok=True)
        except Exception:
            pass
        with _lock:
            _state["running"] = False
            _state["done"] = True
            _state["ok"] = ok
            _state["error"] = "" if ok else (err or "未知错误")
