#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行环境体检报告（v3.176）：把「各功能能不能用、缺什么」一次讲清。

用户 2026-10-29：「运行环境里面优化一下展示，各个功能的准备状态也显示一下，
哪些模型准备中，哪些 CUDA 功能准备中，是否可用，哪些运行环境都展示一下」。

设计要点（都是本仓踩过的坑）：
1. **只查不加载**：这一份报告会被「服务状态」抽屉频繁拉取，所以
   —— 不 import torch（8 秒）、不跑 GPU 实测（要几秒且占显存）、不 ffprobe 视频。
   只查文件存在/大小、读已有探测结果。真跑能力仍由「实测」按钮负责（K144 同源）。
2. **口径唯一**：每项的判据都调用既有模块的探测函数（`gpu.probe` / `runtime_summary` /
   `jianying.status` / `net_gate` / `llm.load_config`），
   这里**不复制**任何阈值或路径（K44）。
3. **三态**：`ok`（可用）/ `degraded`（能跑但有降级）/ `missing`（缺东西，附怎么补）。
   不给"假绿"：依赖没装就说缺什么，不拿"文件存在"冒充"功能可用"。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.common.paths import ws
from app.common.runtime_probe import probe_system, runtime_summary


def _size_mb(p: Path) -> float:
    """目录/文件大小（MB）；拿不到就 0（不抛，K72）。"""
    try:
        if p.is_file():
            return round(p.stat().st_size / 2 ** 20, 1)
        if p.is_dir():
            total = 0
            for r, _d, fs in os.walk(p):
                for f in fs:
                    try:
                        total += os.path.getsize(os.path.join(r, f))
                    except OSError:
                        pass
            return round(total / 2 ** 20, 1)
    except OSError:
        pass
    return 0.0


def _dir_state(p: Path) -> tuple[bool, str]:
    if not p.exists():
        return False, "不存在"
    if p.is_dir() and not any(p.iterdir()):
        return False, "空目录"
    return True, ""


def _runtime_rows(rt: dict, gpu: dict) -> list[dict]:
    """运行环境各条。detail 尽量给**有用的具体值**（版本/大小/个数），别只说"存在"。"""
    cuda_rt = gpu.get("runtime") or {}
    whisper = gpu.get("whisper") or {}
    rows = []
    for it in rt.get("items") or []:
        label = it.get("label") or "—"
        detail = it.get("detail") or ""
        if not detail:
            if "Python" in label:
                detail = sys.version.split()[0]
            elif "CUDA" in label:
                detail = (f"{cuda_rt.get('requiredCount', 0)} 个 dll 齐全" if cuda_rt.get("ready")
                          else "缺 dll")
            elif "Whisper" in label:
                detail = "已就绪" if whisper.get("modelPresent") else "缺失"
            else:
                detail = "存在" if it.get("ok") else "缺失"
        rows.append({
            "label": label,
            "detail": detail if it.get("ok") else (detail or "缺失"),
            "path": it.get("path") or "",
            "ok": bool(it.get("ok")),
            "required": bool(it.get("required")),
        })
    return rows


def _model_rows(gpu: dict) -> list[dict]:
    rows: list[dict] = []
    # Whisper（转写用）—— 路径口径来自 gpu.probe()，别在这里再拼一次
    wdir = Path(gpu.get("whisper", {}).get("modelDir") or ws.models_dir / "small")
    rows.append({
        "label": "Whisper small（语音转写）",
        "detail": f"{_size_mb(wdir):.0f} MB · {wdir.name}",
        "path": str(wdir),
        "ok": bool(gpu.get("whisper", {}).get("modelPresent")),
        "hint": "" if gpu.get("whisper", {}).get("modelPresent") else "重装/复制 models\\small 到工作区",
    })
    # OCR（边界双重验证用；装在 runtime\ocrlib，没装就降级为纯文本验证）
    ocr_dir = ws.runtime / "ocrlib"
    ocr_ok = (ocr_dir / "rapidocr_onnxruntime").is_dir()
    rows.append({
        "label": "OCR（rapidocr，画面复核）",
        "detail": f"{_size_mb(ocr_dir):.0f} MB" if ocr_ok else "未安装（只做文本验证）",
        "path": str(ocr_dir), "ok": ocr_ok,
        "hint": "" if ocr_ok else "可选：装 rapidocr-onnxruntime 到 runtime\\ocrlib",
    })
    return rows


def _cuda_rows(gpu: dict) -> list[dict]:
    gpus = gpu.get("gpus") or []
    g0 = gpus[0] if gpus else {}
    rt = gpu.get("runtime") or {}
    nv = gpu.get("nvenc") or {}
    rows = [
        {"label": "显卡",
         "detail": (f"{g0.get('name')} · {round((g0.get('memoryTotalMb') or 0) / 1024)} GB · "
                    f"空闲 {round((g0.get('memoryFreeMb') or 0) / 1024, 1)} GB") if g0 else "未检测到 NVIDIA 显卡",
         "ok": bool(gpu.get("hasGpu")),
         "hint": "" if gpu.get("hasGpu") else "无独显也能跑，全部走 CPU（慢）"},
        {"label": "CUDA 运行时（Whisper 用）",
         "detail": (f"{rt.get('dir', '')} · {rt.get('requiredCount', 0)} 个 dll 齐全"
                    if rt.get("ready") else "缺 dll：" + "、".join(rt.get("missing") or [])[:80]),
         "ok": bool(rt.get("ready")),
         "hint": "" if rt.get("ready") else "设置页「显卡与加速」里可一键补"},
        {"label": "NVENC 硬件编码（渲染用）",
         "detail": "h264_nvenc 可用" if nv.get("supported") else "不可用（自动回退软编，慢）",
         "ok": bool(nv.get("supported")),
         "hint": "" if nv.get("supported") else "装新驱动或忽略（会走 libx264）"},
    ]
    return rows


def _features(gpu: dict, jy: dict, llm_cfg, net_ok: bool) -> list[dict]:
    """各功能的「能不能用」。state：ok / degraded / missing。"""
    out: list[dict] = []

    # 语音转写
    w_ok = bool(gpu.get("whisper", {}).get("modelPresent"))
    cuda_ok = bool((gpu.get("runtime") or {}).get("ready"))
    if w_ok and cuda_ok:
        out.append({"key": "transcribe", "label": "语音转写", "state": "ok",
                    "detail": "Whisper small + CUDA", "hint": ""})
    elif w_ok:
        out.append({"key": "transcribe", "label": "语音转写", "state": "degraded",
                    "detail": "Whisper small（CUDA 运行库缺，走 CPU 会很慢)", "hint": "补 CUDA 运行库"})
    else:
        out.append({"key": "transcribe", "label": "语音转写", "state": "missing",
                    "detail": "缺 Whisper 模型", "hint": "models\\small 放进工作区 runtime\\models"})

    # AI 切片（无 Key 也能用本地规则规划器 —— 那是降级不是不可用）
    if getattr(llm_cfg, "configured", False):
        out.append({"key": "slice", "label": "AI 切片", "state": "ok",
                    "detail": f"{llm_cfg.provider} / {llm_cfg.model}", "hint": ""})
    else:
        out.append({"key": "slice", "label": "AI 切片", "state": "degraded",
                    "detail": "未配 AI Key（用本地规则规划器，能切但不懂语义）",
                    "hint": "设置页填 AI Key"})

    # 剪辑渲染
    if (gpu.get("nvenc") or {}).get("supported"):
        out.append({"key": "render", "label": "剪辑渲染", "state": "ok",
                    "detail": "NVENC 硬编", "hint": ""})
    else:
        out.append({"key": "render", "label": "剪辑渲染", "state": "degraded",
                    "detail": "无 NVENC，回退软编（更慢）", "hint": ""})

    # 导入剪映
    jy_ok = bool(jy.get("ok"))
    out.append({"key": "jianying", "label": "导入剪映", "state": "ok" if jy_ok else "missing",
                "detail": (jy.get("draftDir") or "") if jy_ok else (jy.get("reason") or "缺 pyJianYingDraft"),
                "hint": "" if jy_ok else "pip 装 pyJianYingDraft 到项目 runtime"})

    # 外网（联网能力总闸）
    out.append({"key": "net", "label": "外网访问", "state": "ok" if net_ok else "degraded",
                "detail": "已开启" if net_ok else "已关闭（联网功能不可用）",
                "hint": "" if net_ok else "设置页或环境变量里打开外网总闸"})
    return out


def report() -> dict:
    """一次体检：功能 / 模型 / CUDA / 运行环境 四栏 + 汇总。"""
    rt = runtime_summary()
    sys_info = probe_system()
    try:
        from app.slicer import gpu as gpu_engine
        gpu = gpu_engine.probe()
    except Exception as exc:                                       # noqa: BLE001
        gpu = {"hasGpu": False, "gpus": [], "runtime": {}, "nvenc": {}, "whisper": {},
               "error": f"{type(exc).__name__}: {exc}"}
    try:
        from app.slicer import jianying as jianying_engine
        jy = jianying_engine.status()
    except Exception as exc:                                       # noqa: BLE001
        jy = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        from app.slicer.llm import load_config as load_llm_config
        llm_cfg = load_llm_config()
    except Exception:                                              # noqa: BLE001
        llm_cfg = None
    try:
        from app.common import net_gate
        net_ok = bool(net_gate.net_enabled())
        net_reason = net_gate.disabled_reason()
    except Exception:                                              # noqa: BLE001
        net_ok, net_reason = True, ""

    features = _features(gpu, jy, llm_cfg, net_ok)
    counts = {"ok": 0, "degraded": 0, "missing": 0}
    for f in features:
        counts[f["state"]] = counts.get(f["state"], 0) + 1

    return {
        "ok": counts["missing"] == 0,
        "summary": counts,
        "features": features,
        "models": _model_rows(gpu),
        "cuda": _cuda_rows(gpu),
        "runtime": _runtime_rows(rt, gpu),
        "system": sys_info,
        "netReason": net_reason if not net_ok else "",
        "python": sys.version.split()[0],
        "app": {"version": _app_version()},
    }


def _app_version() -> str:
    try:
        from app.common import paths as paths_mod
        return getattr(paths_mod, "APP_VERSION", "")
    except Exception:                                              # noqa: BLE001
        return ""
