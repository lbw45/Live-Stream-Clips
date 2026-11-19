#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显卡 / CUDA 加速检测与修复（v3.81，）。

为什么单独开一个模块（而不是散在 render.py / aspect.py 里）
----------------------------------------------------------
在此之前，GPU 相关的三件事分别住在三处、各写各的：

- ``render.gpu_memory_used_mib()`` —— 轮询用的显存占用（带 3 秒 TTL）；
- ``aspect.pick_video_encoder()`` —— 查 ffmpeg ``-encoders`` 判 NVENC（带缓存）；
- ``paths.ws.register_cuda()`` —— 把便携 CUDA DLL 注册进进程（import ctranslate2 前必须调）。

**本模块不重写它们** —— 只import 复用（K44：三处写必漂移）。这里新增的是三件**别的
地方没有、且必须单独才有意义**的事：

1. **体检汇总**：显卡型号 / 驱动 / 支持的最高 CUDA / 显存 / 温度，加上本机**便携 CUDA DLL
   缺哪几个**，以及 NVENC 与 Whisper GPU **到底能不能用**（不是"应该能用"）。
2. **实测**（用户点出来的）：NVENC 真编 1 秒、Whisper 真加载一次 GPU 模型。
   ⚠️ Whisper 那次会占 ~1GB 显存十几秒，所以**绝不能在 HTTP 轮询路径上跑** ——
   走后台线程 + 轮询状态（与转写/渲染同一套 submit 模式）。
3. **一键修复**：缺 DLL 时自动从 PyPI 拉对应的 ``nvidia-*-cu12`` wheel 并把 DLL
   解到 ``runtime\\cuda12``。

关于「从哪下载 CUDA 组件」这个决定（用户没指定，我按实测拍的板）
------------------------------------------------------------------
工作区原有的这套 CUDA 是**便携 DLL 形式**（``hc\\runtime\\cuda12`` 下 cublas + cudnn9
共 11 个 DLL，约 1.5 GB），**不是**在系统里装了 CUDA Toolkit。它来源就是 PyPI 那几个
``nvidia-*-cu12`` wheel —— 这正是 NVIDIA 现在官方推荐的分发方式。

所以「下载 CUDA 组件」有两种做法，我选第二种：

- 引导用户去官网下 ``cuda_x.x.x_windows.exe``（约 3 GB，要管理员权限，会往系统里
  装一堆东西，**违反本仓 R2「一切落 G 盘」**，且装出来的版本未必是 ctranslate2 要的）；
- **自动拉 wheel 解 DLL**（几百 MB，纯解压，不动系统任何东西，版本号**从本机
  ctranslate2 的依赖声明里反推**，不可能装错版本）。

版本号怎么定的（这里最容易写错，写成硬编码必然过期）::

    hc\\runtime\\python\\Lib\\site-packages\\ctranslate2-*.dist-info\\METADATA
      → Requires-Dist: nvidia-cublas-cu12==12.6.4.1 ; extra == "cuda"

也就是说：**本机能跑 GPU 的前提，就是这台机器上那几行 Requires-Dist 所声明的版本**。
我们从 METADATA 里读出来当唯一出处，这个配置文件 anybody 都不用维护。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from ..common import proc
from ..common.paths import PROJECT_ROOT, ws

# nvidia-smi 的确定性输出字段（CSV、无表头、无单位 —— 省得再做单位解析）
_SMI_FIELDS = ("index", "name", "driver_version", "memory.total", "memory.used",
               "memory.free", "compute_cap", "temperature.gpu", "utilization.gpu")

#: 缺 DLL → 该下哪个 pip 包（wheel 名与本仓约定的目录名一致，见 :func:`required_wheels`）
PKG_CUBLAS = "nvidia-cublas-cu12"
PKG_CUDNN = "nvidia-cudnn-cu12"

#: ctranslate2 加载 GPU 时真正会去找的 DLL（按名字前缀归属包）
_REQUIRED_DLLS: tuple[tuple[str, str], ...] = (
    ("cublas64_12.dll", PKG_CUBLAS),
    ("cublasLt64_12.dll", PKG_CUBLAS),
    ("cudnn64_9.dll", PKG_CUDNN),
    ("cudnn_ops64_9.dll", PKG_CUDNN),
    ("cudnn_heuristic64_9.dll", PKG_CUDNN),
    ("cudnn_cnn64_9.dll", PKG_CUDNN),
    ("cudnn_adv64_9.dll", PKG_CUDNN),
    ("cudnn_graph64_9.dll", PKG_CUDNN),
    ("cudnn_engines_precompiled64_9.dll", PKG_CUDNN),
    ("cudnn_engines_runtime_compiled64_9.dll", PKG_CUDNN),
)

#: wheel 里 DLL 的子目录 → 目标目录（直接拷到 runtime\\cuda12 根）
_WHEEL_DLL_SUBDIRS = ("bin", "lib", "lib/x64")

_PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"


# ---------------------------------------------------------------- nvidia-smi

def smi_exe() -> str | None:
    """``nvidia-smi.exe`` 的路径；找不到返回 ``None``（没装驱动或没 N 卡）。

    ⚠️ 必须给绝对路径兜底：某些 shell 里 PATH 是坏的（本仓 2026-10 就遇到过），
    ``which`` 拿不到不代表机器没显卡。
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for base in (os.environ.get("SystemRoot", r"C:\Windows"),
                 os.environ.get("WinDir", r"C:\Windows")):
        cand = Path(str(base)) / "System32" / "nvidia-smi.exe"
        if cand.is_file():
            return str(cand)
    return None


def _smi_csv() -> list[dict]:
    """跑一次 nvidia-smi CSV 查询；任何失败都返回空列表（不抛、不假装有数据）。"""
    exe = smi_exe()
    if not exe:
        return []
    cmd = [exe, "--query-gpu=" + ",".join(_SMI_FIELDS),
           "--format=csv,noheader,nounits"]
    try:
        r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out: list[dict] = []
    for line in (r.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < len(_SMI_FIELDS):
            continue
        rec = dict(zip(_SMI_FIELDS, parts))

        def _int(key: str) -> int | None:
            try:
                return int(float(rec.get(key) or ""))
            except (TypeError, ValueError):
                return None

        out.append({
            "index": _int("index"),
            "name": rec.get("name") or "未知显卡",
            "driverVersion": rec.get("driver_version") or "",
            "memoryTotalMb": _int("memory.total"),
            "memoryUsedMb": _int("memory.used"),
            "memoryFreeMb": _int("memory.free"),
            "computeCap": rec.get("compute_cap") or "",
            "temperatureC": _int("temperature.gpu"),
            "utilPercent": _int("utilization.gpu"),
        })
    return out


def _cuda_umd_version() -> str:
    """驱动自带的 CUDA UMD 版本（如 ``13.4``）—— 决定我们最多能用多新的 CUDA DLL。

    nvidia-smi 的 CSV 查询不支持这个字段（实测报 "not a valid field"），
    只能从**完整输出的表头**里正则抠。抠不到返回空串（不当成致命）。
    """
    exe = smi_exe()
    if not exe:
        return ""
    try:
        r = proc.run([exe], capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"CUDA\s+(?:UMD\s+)?Version:\s*([\d.]+)", r.stdout or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 本机 CUDA 运行时

def missing_runtime_dlls() -> list[str]:
    """``runtime\\cuda12`` 里缺哪几个 DLL（全在就返回空列表）。"""
    return [name for name, _pkg in _REQUIRED_DLLS
            if not (ws.cuda_dir / name).is_file()]


def _wheel_dir() -> Path:
    return ws.runtime / "_cuda_wheels"


def required_wheels(driver_cuda: str | None = None) -> tuple[list[dict], str]:
    """**该下哪几个 wheel、以及精确版本** —— 从本机 ctranslate2 的依赖声明里读。

    返回 ``([{"package","version","reason"}], error)``。

    为什么不写死版本号：ctranslate2 升级后要求的 cublas/cudnn 版本会变，
    硬编码必然过期（K44 同源）。而 `pip download nvidia-cublas-cu12`（不带版本）
    拉回最新的，也可能与 ctranslate2 不兼容 —— 只会让人更难判断问题在哪。

    开源版 2026-11-19（用户：先检测 GPU 再按版本下载）：Windows 的 ctranslate2
    wheel **不在 METADATA 里声明 nvidia 依赖**（Linux 才有 cuda extra），声明读不到
    时回退默认表（ct2 4.x → CUDA 12.x + cuDNN 9）；且先校验驱动支持的 CUDA 版本，
    低于 12 就不列下载清单，直接提示升级驱动 —— 别人的机器不是我们这台。
    """
    meta = _ct2_metadata()
    pairs: list[tuple[str, str]] = []
    if meta:
        pairs = re.findall(
            r"^Requires-Dist:\s*(nvidia-[\w-]+)\s*==\s*([\w.]+)\s*;.*extra\s*==\s*[\"']cuda[\"']",
            meta, re.M | re.I)
        if not pairs:
            pairs = re.findall(
                r"^Requires-Dist:\s*(nvidia-[\w-]+)\s*==\s*([\w.]+)",
                meta, re.M | re.I)
    if not pairs:
        pairs = [("nvidia-cublas-cu12", ""), ("nvidia-cudnn-cu12", "")]
    dc = None
    try:
        dc = float(str(driver_cuda).strip()) if str(driver_cuda or "").strip() else None
    except ValueError:
        dc = None
    if dc is not None and dc < 12:
        return [], (f"驱动支持的 CUDA 版本是 {driver_cuda}，低于本程序需要的 CUDA 12 —— "
                    "请先升级 NVIDIA 驱动再下载组件")
    missing = {pkg for name, pkg in _REQUIRED_DLLS
               if not (ws.cuda_dir / name).is_file()}
    out: list[dict] = []
    for pkg, ver in pairs:
        if pkg == PKG_CUBLAS and PKG_CUBLAS in missing:
            out.append({"package": pkg, "version": ver,
                        "reason": "缺 cublas DLL（whisper 做矩阵乘要它）"})
        elif pkg == PKG_CUDNN and PKG_CUDNN in missing:
            out.append({"package": pkg, "version": ver,
                        "reason": "缺 cuDNN DLL（whisper 的卷积算子要它）"})
    return out, ""


def _ct2_metadata() -> str:
    """读 ctranslate2 的 dist-info METADATA（Windows 工作区运行时）。"""
    root = ws.python_dir / "Lib" / "site-packages"
    if not root.is_dir():
        return ""
    hits = sorted(root.glob("ctranslate2-*.dist-info"))
    for d in hits:
        p = d / "METADATA"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------- 汇总体检

def probe() -> dict:
    """一次体检汇总（**不实测**，全部毫秒级，可以放心轮询）。

    字段说明给前端用：
    ``gpu`` 是显卡本体信息；``runtime`` 是便携 CUDA DLL；``nvenc`` / ``whisper``
    是**推断**可用性（fast path 的依据是历史上真的能用吗？不 —— 见下）。

    ⚠️ 刻意区分「**支持**」与「**实测通过**」：
    ``nvencSupported`` 只表示 ffmpeg 里编出了 h264_nvenc 这一项、**around 4GB 显存**
    究竟够不够要真跑才知道；``whisperReady`` 同理。所以要回答"能不能用"就看
    ``selftest`` —— 那是 :func:`start_selftest` 跑出来的硬证据。
    """
    gpus = _smi_csv()
    missing = missing_runtime_dlls()
    wheels, werr = required_wheels(_cuda_umd_version())

    nvenc = _nvenc_supported()
    out = {
        "hasGpu": bool(gpus),
        "gpus": gpus,
        "cudaUmdVersion": _cuda_umd_version(),
        "smiPath": smi_exe() or "",
        "runtime": {
            "dir": str(ws.cuda_dir),
            "requiredCount": len(_REQUIRED_DLLS),
            "missing": missing,
            "ready": not missing,
            "sizeMb": round(_dir_size_mb(ws.cuda_dir), 1),
        },
        "nvenc": nvenc,
        "whisper": {
            "ready": not missing and bool(gpus),
            "reason": ("显卡与 CUDA 运行时都在（能否真的用上看「实测」）" if (not missing and gpus)
                       else ("缺 CUDA 运行时 DLL：" + "、".join(missing) if missing
                             else "没检测到 NVIDIA 显卡")),
            "modelDir": str(ws.models_dir / "small"),
            "modelPresent": (ws.models_dir / "small" / "model.bin").is_file(),
        },
        "repair": {
            "available": bool(wheels),
            "wheels": wheels,
            "wheelDir": str(_wheel_dir()),
            "error": werr,
            "note": ("只下载缺失的那几个 nvidia wheel 并把 DLL 解进 runtime\\cuda12，"      # 「自备运行时」
                     "不动系统、不需要管理员权限、不装 CUDA Toolkit。" if wheels
                     else ("本机不缺 CUDA 运行时，不需要下载" if not werr else "")),
        },
        "probeAt": round(time.time(), 1),
    }
    return out


def _nvenc_supported() -> dict:
    """ffmpeg 里有没有 h264_nvenc（**只查表**，非常快）。复用 aspect 的唯一实现。"""
    from .aspect import pick_video_encoder

    try:
        enc = pick_video_encoder(ws.ffmpeg)
    except Exception as exc:                                   # noqa: BLE001
        return {"supported": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"supported": enc == "nvenc", "encoder": enc}


def _dir_size_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    total = 0
    try:
        for f in path.iterdir():
            try:
                total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0.0
    return total / 1024 / 1024


# ---------------------------------------------------------------- 实测（后台线程）

_lock = threading.Lock()
_state: dict = {"running": False, "startedAt": 0.0, "steps": [], "done": False,
                "ok": None, "error": ""}


def selftest_state() -> dict:
    """实测任务状态（前端轮询）。"""
    with _lock:
        snap = dict(_state)
        snap["steps"] = list(_state["steps"])
        snap["elapsedSec"] = (round(time.time() - _state["startedAt"], 1)
                              if _state["startedAt"] else 0)
        return snap


def start_selftest() -> dict:
    """启动一次实测（NVENC 编码 + Whisper GPU 加载转录）。

    幂等：正在跑就返回当前状态，不会起第二个线程（GPU 受不了两个同时加载模型）。

    ⚠️ **锁的纪律**：快照必须在**锁外**取。``selftest_state()`` 自己也要拿这把锁，
    而 ``threading.Lock`` 不可重入 —— 在 ``with _lock`` 里调它就**死锁**，
    表现是「第二次点『实测一次』之后那个请求永远不返回」，而且因为服务是线程池，
    它会把一个工作线程彻底占住（本仓实测踩到过：冒烟的幂等断言直接把进程挂住）。
    """
    with _lock:
        busy = _state["running"]
        if not busy:
            _state.update({"running": True, "startedAt": time.time(), "steps": [],
                           "done": False, "ok": None, "error": ""})
    if busy:
        return {"started": False, "busy": True, "state": selftest_state()}
    threading.Thread(target=_run_selftest, name="gpu-selftest", daemon=True).start()
    return {"started": True, "busy": False, "state": selftest_state()}


def _step(name: str, ok: bool | None, detail: str = "") -> None:
    with _lock:
        _state["steps"].append({"name": name, "ok": ok, "detail": detail,
                                "at": round(time.time() - _state["startedAt"], 1)})


def _run_selftest() -> None:
    try:
        _nvenc_step()
        _whisper_step()
        ok = all(s.get("ok") for s in _state["steps"] if s.get("ok") is not None)
        with _lock:
            _state["ok"] = bool(ok)
    except Exception as exc:                                    # noqa: BLE001
        with _lock:
            _state["ok"] = False
            _state["error"] = f"{type(exc).__name__}: {exc}"
            _step("实测中断", False, _state["error"])
    finally:
        with _lock:
            _state["running"] = False
            _state["done"] = True


def _nvenc_step() -> None:
    """真编 1 秒 320x240 视频（走 NVENC）。不是查表，是动手。"""
    if not ws.ffmpeg.is_file():
        _step("NVENC 编码实测", False, f"ffmpeg 不在：{ws.ffmpeg}")
        return
    cmd = [str(ws.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
           "-c:v", "h264_nvenc", "-preset", "p4", "-f", "null", "-"]
    t0 = time.time()
    try:
        r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=60, env=ws.subprocess_env())
    except subprocess.TimeoutExpired:
        _step("NVENC 编码实测", False, "编码 1 秒测试帧超时（60s）")
        return
    except OSError as exc:
        _step("NVENC 编码实测", False, f"{type(exc).__name__}: {exc}")
        return
    cost = round(time.time() - t0, 1)
    if r.returncode == 0:
        _step("NVENC 编码实测", True, f"1 秒测试帧编码成功，用时 {cost}s")
    else:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["（无输出）"]
        _step("NVENC 编码实测", False,
              f"ffmpeg 返回 {r.returncode}：{tail[0][:200]}（用时 {cost}s）")


def _whisper_step() -> None:
    """**子进程里**加载一次 GPU whisper 模型并转录 2 秒音频。

    为什么不开在我们自己的进程里：加载 faster-whisper 会把 ctranslate2 连同 CUDA
    context 一起拽进来，占 1GB 级显存，且失败时常常是**原生层崩溃**（ct2 的 CUDA 报错
    有时直接 abort）。把它关在子进程里，崩了也只是那个进程没了 —— 服务照常活着，
    我们还能拿到它最后吐出的错误信息（K118 同源：以此为依据判断，不猜）。
    """
    script = ws.tmp_dir / "gpu_whisper_selftest.py"
    try:
        ws.tmp_dir.mkdir(parents=True, exist_ok=True)
        script.write_text(_WHISPER_PROBE_SCRIPT.format(project_app=str(PROJECT_ROOT)),
                          encoding="utf-8")
    except OSError as exc:
        _step("Whisper GPU 实测", False, f"写自检脚本失败：{exc}")
        return
    if not ws.python_exe.is_file():
        _step("Whisper GPU 实测", False, f"Python 运行时不在：{ws.python_exe}")
        return
    cmd = [str(ws.python_exe), str(script)]
    t0 = time.time()
    try:
        r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=300, env=ws.subprocess_env())
    except subprocess.TimeoutExpired:
        _step("Whisper GPU 实测", False, "加载/转录超时（300s）")
        return
    except OSError as exc:
        _step("Whisper GPU 实测", False, f"{type(exc).__name__}: {exc}")
        return
    cost = round(time.time() - t0, 1)
    text = (r.stdout or "").strip()
    marker = "__RESULT__"
    if marker not in text:
        tail = (text or (r.stderr or "")).strip().splitlines()[-3:]
        _step("Whisper GPU 实测", False,
              "子进程没有返回结果（可能在原生层崩了）—— " + " ｜ ".join(tail)[:300])
        return
    try:
        payload = json.loads(text.split(marker, 1)[1].strip())
    except ValueError as exc:
        _step("Whisper GPU 实测", False, f"结果不是 JSON：{exc}｜{text[-200:]}")
        return
    if payload.get("ok"):
        _step("Whisper GPU 实测", True,
              f"device={payload.get('device')} 加载 {payload.get('loadSec')}s、"
              f"转录 {payload.get('transcribeSec')}s（共 {cost}s）")
    else:
        _step("Whisper GPU 实测", False, str(payload.get("error") or "未知原因")[:300])


_WHISPER_PROBE_SCRIPT = '''# -*- coding: utf-8 -*-
"""GPU 自检子进程：加载 whisper GPU 模型 + 转录 2 秒音频。"""
import json, sys, time
from pathlib import Path

try:
    sys.path.insert(0, r"{project_app}")
    from app.common.paths import ws
    ok = ws.register_cuda()
    if not ok:
        print("__RESULT__" + json.dumps({{"ok": False,
            "error": "runtime\\\\cuda12 里没有 cublas64_12.dll，CUDA 运行时缺失"}}))
        sys.exit(0)
    # ⚠️ 子进程一律走 app.common.proc（唯一入口，自带 CREATE_NO_WINDOW）。
    #    这里虽然自己已经在无窗子进程里，但它再起 ffmpeg 仍会新建可见控制台（K：黑窗闪烁）。
    from app.common import proc as _proc
    wav = ws.tmp_dir / "selftest.wav"
    ws.tmp_dir.mkdir(parents=True, exist_ok=True)
    _proc.run([str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
               "-ar", "16000", "-ac", "1", str(wav)], check=False)
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(str(ws.models_dir / "small"), device="cuda",
                         compute_type="int8_float16", cpu_threads=2, num_workers=1)
    load = round(time.time() - t0, 1)
    t1 = time.time()
    segs, info = model.transcribe(str(wav), beam_size=1, vad_filter=False)
    _ = [s for s in segs]
    tr = round(time.time() - t1, 1)
    print("__RESULT__" + json.dumps({{"ok": True, "device": "cuda",
        "loadSec": load, "transcribeSec": tr, "lang": getattr(info, "language", "")}}))
except Exception as exc:
    import traceback
    print("__RESULT__" + json.dumps({{"ok": False,
        "error": "{{type(exc).__name__}}: {{exc}}",
        "trace": traceback.format_exc().strip().splitlines()[-1][:200]}}))
'''


# ---------------------------------------------------------------- 一键修复（拉 wheel 解 DLL）

_repair_lock = threading.Lock()
_repair: dict = {"running": False, "startedAt": 0.0, "log": [], "done": False,
                 "ok": None, "error": "", "downloadedKb": 0}


def repair_state() -> dict:
    with _repair_lock:
        snap = dict(_repair)
        snap["log"] = list(_repair["log"])
        snap["elapsedSec"] = (round(time.time() - _repair["startedAt"], 1)
                              if _repair["startedAt"] else 0)
        return snap


def start_repair() -> dict:
    """缺 DLL 时的一键修复：``pip download`` → 解 DLL → 校验。

    ⚠️ 这是**人点了按钮才跑**的动作（不是周期性后台行为），所以不归 net_gate 管，
    但每次启动都会先把「要下什么、下到哪」写进日志，让用户看得见自己在下载什么。

    ⚠️ 同 :func:`start_selftest`：快照在**锁外**取（锁不可重入，锁内调 repair_state 会死锁）。
    """
    with _repair_lock:
        busy = _repair["running"]
        if not busy:
            _repair.update({"running": True, "startedAt": time.time(), "log": [],
                            "done": False, "ok": None, "error": "", "downloadedKb": 0})
    if busy:
        return {"started": False, "busy": True, "state": repair_state()}
    threading.Thread(target=_run_repair, name="cuda-repair", daemon=True).start()
    return {"started": True, "busy": False, "state": repair_state()}


def _rlog(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _repair_lock:
        _repair["log"].append(line)
    print(f"[gpu-repair] {msg}", flush=True)


def _run_repair() -> None:
    try:
        wheels, err = required_wheels()
        if not wheels:
            with _repair_lock:
                _repair["ok"] = True
                _repair["error"] = err or "没有需要下载的组件"
            _rlog("没有需要下载的组件")
            return
        if not ws.python_exe.is_file():
            with _repair_lock:
                _repair["ok"] = False
                _repair["error"] = f"Python 运行时不在：{ws.python_exe}"
            _rlog(_repair["error"])
            return
        wdir = _wheel_dir()
        wdir.mkdir(parents=True, exist_ok=True)
        ws.cuda_dir.mkdir(parents=True, exist_ok=True)
        for item in wheels:
            spec = (item['package'] + ('==' + item['version'] if item['version'] else ''))
            _rlog(f"开始下载 {spec}（{item['reason']}）…")
            whl = _pip_download(spec, wdir)
            if whl is None:
                _rlog("pip 不可用或下载失败，改走 PyPI 直连 wheel（不需要 pip）")
                whl = _direct_wheel_download(spec, wdir)
            if whl is None:
                with _repair_lock:
                    _repair["ok"] = False
                    _repair["error"] = f"下载失败：{spec}"
                _rlog(f"下载失败：{spec}（网络不通就先把 {wdir} 里手动放好的 whl 留下再重试）")
                return
            n = _extract_dlls(whl, ws.cuda_dir)
            _rlog(f"{item['package']} 已解出 {n} 个 DLL 到 {ws.cuda_dir}")
        rest = missing_runtime_dlls()
        with _repair_lock:
            _repair["ok"] = not rest
            _repair["error"] = ("还缺：" + "、".join(rest)) if rest else ""
        _rlog("校验通过，CUDA 运行时已补齐" if not rest else f"仍然缺失：{'、'.join(rest)}")
    except Exception as exc:                                    # noqa: BLE001
        with _repair_lock:
            _repair["ok"] = False
            _repair["error"] = f"{type(exc).__name__}: {exc}"
        _rlog(_repair["error"])
    finally:
        with _repair_lock:
            _repair["running"] = False
            _repair["done"] = True


def _pip_download(spec: str, dest: Path) -> Path | None:
    """``pip download`` 一个 wheel 到 ``dest``。返回 wheel 路径；失败返回 ``None``。

    先试阿里云镜像（国内快），失败再走官方源 —— 两处都失败才算失败。
    """
    base = [str(ws.python_exe), "-m", "pip", "download", "--no-deps", "-d", str(dest)]
    for attempt in (("镜像源", base + ["-i", _PIP_INDEX]), ("官方源", base)):
        label, cmd = attempt
        try:
            r = proc.run(cmd + [spec], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=1800,
                         env=ws.subprocess_env())
        except subprocess.TimeoutExpired:
            _rlog(f"{label}超时（30 分钟），换下一个源")
            continue
        except OSError as exc:
            _rlog(f"{label}启动失败：{exc}")
            continue
        if r.returncode == 0:
            hits = sorted(dest.glob("*.whl"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
            if hits:
                with _repair_lock:
                    _repair["downloadedKb"] += round(hits[0].stat().st_size / 1024)
                _rlog(f"{label}下载完成：{hits[0].name} "
                      f"（{round(hits[0].stat().st_size / 1024 / 1024, 1)} MB）")
                return hits[0]
            _rlog(f"{label}报成功但没找到 whl 文件")
        else:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
            _rlog(f"{label}失败：{tail[0][:200] if tail else '（无输出）'}")
    return None


def _extract_dlls(whl: Path, target: Path) -> int:
    """从 wheel 里把 DLL 解到 ``target`` 根目录；返回解出的个数。

    只解 :data:`_REQUIRED_DLLS` 里登记的名字 —— wheel 里还有 license、头文件和
    一堆用不上的库，**全解会把目录搞脏几百 MB，还难判断到底缺什么**（K114 同源）。
    """
    wanted = {name for name, _p in _REQUIRED_DLLS}
    n = 0
    with zipfile.ZipFile(whl) as z:
        for info in z.infolist():
            base = Path(info.filename).name
            if base not in wanted:
                continue
            if not ("/lib/" in info.filename or "/bin/" in info.filename):
                continue
            dst = target / base
            with z.open(info) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    return n

# ----------------------------------------------------------------------------
# v4.61：**显卡并发提示**（方案文档《显卡并发冲突_提示与排队方案》第二章 B）
#
# 先说清这个函数**不做**什么：
# 不做互斥（不抢锁、不阻塞别人）—— 只回答"现在有没有人占着显卡"，让界面提示用户；
# 不查显存（nvidia-smi 几十到几百 ms，还会和真任务抢 GPU 查询）；
# 不 import torch（服务每几秒轮询一次，一 import 首屏就要白等 2 秒+，见 status() 的警告）。
#
# 只读内存里的任务表 ⇒ 单次 < 1ms。探测失败一律按"空闲"处理：宁可漏提示，
# 也绝不因为探测出错把正常业务流程挡住（K69 的反面用法：不知道就别猜）。
# ----------------------------------------------------------------------------

def busy_owner() -> dict:
    """显卡当前被谁占着（只读、极轻、无副作用）。

    返回 {"busy": bool, "owner": str, "label": str, "detail": str, "since": float}

    本版没有常驻显存的额外产线（转写/渲染各自有显存守卫），因此恒返回空闲；
    保留该接口是为了让服务端与界面的契约保持稳定。
    """
    return {"busy": False, "owner": "", "label": "", "detail": "", "since": 0.0}
