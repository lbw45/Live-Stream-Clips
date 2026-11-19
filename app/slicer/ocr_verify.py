#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视觉边界验证（v3.132）：用 **OCR 读画面上的结算/开局字样**来复核文本给的切点。

「双重验证」的口径（用户 2026-10-23 明确要）：
  · **第一重**：转写文本里的强信号词（:mod:`app.slicer.segment_rules`）—— 便宜、确定性；
  · **第二重**：边界时刻 ±2 秒抽两帧做 OCR，看屏幕上是不是真的写着「胜利 / 结算 / 第1名 /
    再来一局」—— 只有文本与画面**都指向同一处**，才标「双确认」。

⚠️ OCR 是**可选依赖**：没装就 ``available() == False``，调用方据此降级成"仅文本验证"，
   并在界面上如实写明（不许假装验过）。装法见 :func:`install_hint`。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from app.common import proc
from app.common.paths import ws
from app.slicer import notes as notes_mod

#: OCR 依赖装在运行时目录下（R2：不许落 C 盘、不许污染用户环境）
OCR_LIB_DIR = ws.runtime / "ocrlib"
FRAME_TOL_SEC = 2.0          # 边界前后各抽一帧的时间偏移
MAX_POINTS = 24
ERRORS: list[str] = []      # 最近一次抽帧/OCR 的错误（给界面显示，别静默丢）              # 一次最多验这么多个边界（每点 2 帧，别把 CPU 跑满）


class OcrError(Exception):
    """OCR 不可用或抽帧失败（message 面向用户）。"""


def install_hint() -> str:
    return f'pip install --target "{OCR_LIB_DIR}" rapidocr-onnxruntime'


def _engine():
    """按需加载 OCR 引擎（只试 rapidocr；没装就报不可用）。"""
    if str(OCR_LIB_DIR) not in sys.path and OCR_LIB_DIR.is_dir():
        sys.path.insert(0, str(OCR_LIB_DIR))
    try:
        from rapidocr_onnxruntime import RapidOCR       # noqa: PLC0415
    except Exception:                                   # noqa: BLE001
        return None
    try:
        return RapidOCR()
    except Exception:                                   # noqa: BLE001
        return None


_CACHE: dict = {}


def available() -> dict:
    """``{"ok": bool, "engine": str, "hint": str}`` —— 界面直接显示这个。"""
    if "avail" in _CACHE:
        return _CACHE["avail"]
    eng = _engine()
    _CACHE["engine"] = eng
    info = {"ok": eng is not None,
            "engine": "rapidocr-onnxruntime" if eng else "",
            "hint": "" if eng else install_hint()}
    _CACHE["avail"] = info
    return info


def _ffmpeg() -> str:
    # ⚠️ 唯一出处是 `ws.ffmpeg`（paths.py）；`app.common.proc` 里**没有** ffmpeg_path ——
    #    本轮一开始写错成 proc.ffmpeg_path()，AttributeError（抽帧必崩，但只在真调用时才炸）。
    return str(ws.ffmpeg)


def _frames(video: Path, times: list[float], out_dir: Path) -> list[Path]:
    """按时间点抽单帧（每点一张，JPEG 质量 80）—— 用 ffmpeg，不引新库。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for i, t in enumerate(times):
        f = out_dir / f"f{i}_{int(t)}.jpg"
        cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{max(t, 0):.2f}", "-i", str(video),
               "-frames:v", "1", "-q:v", "4", str(f)]
        try:
            proc.run(cmd, timeout=30)
        except Exception as exc:                         # noqa: BLE001
            # ⚠️ 以前这里是 `continue` —— 抽帧失败**一声不响**，最后表现为"0 帧 / 全部仅文本"，
            #    排查时看不出是 ffmpeg 挂了还是没装。改成记下来（K122：失败不能无声）。
            ERRORS.append(f"抽帧失败 @{t:.1f}s：{type(exc).__name__}: {exc}")
            continue
        if f.is_file() and f.stat().st_size > 0:
            files.append(f)
    return files


def _ocr_text(eng, img: Path) -> str:
    try:
        res, _ = eng(str(img))
    except Exception:                                    # noqa: BLE001
        return ""
    if not res:
        return ""
    parts = []
    for row in res:
        # rapidocr 返回 [[box, text, score], ...]
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            parts.append(str(row[1]))
    return " ".join(parts)


def _norm(t: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", str(t or "").casefold())


def verify_points(video: str | Path, points: list[dict], unit: str = "match",
                  *, ffmpeg_out: Path | None = None) -> dict:
    """对每个文本边界做 OCR 复核。

    返回 ``{"ok", "available", "engine", "points":[{t, kind, evidence, ocrText,
    matched, verdict}]}``；``verdict`` 取 ``双确认 / 仅文本 / 冲突 / 未知``。
    """
    info = available()
    preset = notes_mod.UNIT_PRESETS.get(unit or "match") or notes_mod.UNIT_PRESETS["match"]
    vocab = [_norm(w) for w in (preset.get("closeWords") or []) + (preset.get("openWords") or [])]
    pts = list(points or [])[:MAX_POINTS]
    if not info["ok"]:
        return {"ok": True, "available": False, "engine": "", "hint": info["hint"],
                "points": [{"t": p["t"], "kind": p.get("kind", ""),
                            "evidence": p.get("evidence", ""), "ocrText": "",
                            "matched": [], "verdict": "仅文本"} for p in pts],
                "note": "未安装 OCR，边界仅由转写文本判定（可在设置里看安装命令）"}

    video_p = Path(video)
    if not video_p.is_file():
        raise OcrError(f"素材不存在：{video_p.name}")
    eng = _CACHE.get("engine") or _engine()
    if eng is None:
        raise OcrError("OCR 引擎加载失败（依赖已装但初始化失败）")

    base = (ffmpeg_out or (ws.tmp_dir / "ocr_frames"))
    base = Path(base) / re.sub(r"[^\w.-]+", "_", video_p.stem)[:40]
    times: list[float] = []
    for p in pts:
        times.append(max(float(p["t"]) - FRAME_TOL_SEC, 0))
        times.append(float(p["t"]) + FRAME_TOL_SEC)
    ERRORS.clear()
    imgs = _frames(video_p, times, base)

    out = []
    for i, p in enumerate(pts):
        texts = []
        for k in (i * 2, i * 2 + 1):
            if k < len(imgs):
                texts.append(_ocr_text(eng, imgs[k]))
        ocr = " ".join(t for t in texts if t)
        n = _norm(ocr)
        matched = sorted({w for w in vocab if w and len(w) >= 2 and w in n})
        text_hit = bool(p.get("evidence"))
        if matched and text_hit:
            verdict = "双确认"
        elif matched and not text_hit:
            verdict = "仅画面"
        elif text_hit and not matched:
            verdict = "仅文本"
        else:
            verdict = "未知"
        out.append({"t": p["t"], "kind": p.get("kind", ""),
                    "evidence": p.get("evidence", ""), "ocrText": ocr[:120],
                    "matched": matched, "verdict": verdict})
    return {"ok": True, "available": True, "engine": info["engine"],
            "points": out, "errors": ERRORS[-5:],
            "summary": {v: sum(1 for x in out if x["verdict"] == v)
                        for v in ("双确认", "仅文本", "仅画面", "未知")}}
