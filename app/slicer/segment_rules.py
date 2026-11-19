#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单元边界检测（v3.132）：把「一场对局 / 一次连麦 / 一个商品」的起止**从转写里认出来**。

**为什么要有它**：切窗以前是「按固定 60 秒硬切」，窗口必然跨对局 —— 模型拿到的是
「半局 + 半局」，怎么提示都切不出完整一局（2026-10-23 用户反馈的核心）。

本模块只做三件事，全部**纯本地、确定性、不花 API 钱**：

1. :func:`detect_boundaries` —— 用 ``notes.UNIT_PRESETS[unit]`` 的强信号词找边界点
   （结算/开局/连麦/上下架），带证据词与置信度（证据不足就**不产出**边界，宁缺勿错）。
2. :func:`units_from` —— 把边界点连成单元（太短就合并、太长就按停顿/句子切开）。
3. :func:`snap_candidates` —— 把候选 clip 的起止**吸附到最近的单元边界**（严格模式用）。

视觉侧（OCR 读结算屏）在 :mod:`app.slicer.ocr_verify`，二者组成「双重验证」：
文本先给候选边界，OCR 再确认那几个时刻屏幕上确实写着「胜利/结算/名次」。
"""
from __future__ import annotations

import re

from app.slicer import notes as notes_mod

#: 边界点之间小于这个间隔就合并成一个（同一句话里同时出现"结束"和"胜利"）
MERGE_GAP_SEC = 4.0
#: 证据词长度 → 置信度（4 字以上的强词才给高置信）
_WORD_CONF = {2: 0.55, 3: 0.72, 4: 0.88, 5: 0.95}


def _norm(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", str(text or "").casefold())


def _conf_for(word: str) -> float:
    n = len(word)
    return _WORD_CONF.get(min(max(n, 2), 5), 0.9)


def detect_boundaries(segments: list[dict], unit: str = "match") -> list[dict]:
    """从转写里找单元边界。

    返回 ``[{t, kind, evidence, confidence}]``（``kind`` 为 ``open`` / ``close``），
    按时间升序。**只收置信度 ≥ 0.5 的点**；一个都没找到就返回空表（调用方走原逻辑）。
    """
    preset = notes_mod.UNIT_PRESETS.get(unit or "chapter") or notes_mod.UNIT_PRESETS["chapter"]
    opens = [w for w in preset.get("openWords") or [] if w]
    closes = [w for w in preset.get("closeWords") or [] if w]
    if not opens and not closes:
        return []

    pts: list[dict] = []
    for seg in segments or []:
        text = _norm(seg.get("text"))
        if not text:
            continue
        hit_open = [w for w in opens if _norm(w) and _norm(w) in text]
        hit_close = [w for w in closes if _norm(w) and _norm(w) in text]
        if hit_open:
            w = max(hit_open, key=len)
            pts.append({"t": float(seg.get("start") or 0), "kind": "open",
                        "evidence": w, "confidence": _conf_for(w)})
        if hit_close:
            w = max(hit_close, key=len)
            pts.append({"t": float(seg.get("end") or 0), "kind": "close",
                        "evidence": w, "confidence": _conf_for(w)})
    if not pts:
        return []

    pts.sort(key=lambda p: p["t"])
    merged: list[dict] = []
    for p in pts:
        if merged and p["t"] - merged[-1]["t"] < MERGE_GAP_SEC:
            # 同一处：保留置信度更高、词更长的那个，但 close 优先（结算才是真边界）
            prev = merged[-1]
            if (p["confidence"], p["kind"] == "close", len(p["evidence"])) > \
               (prev["confidence"], prev["kind"] == "close", len(prev["evidence"])):
                merged[-1] = p
            continue
        merged.append(p)
    return [p for p in merged if p["confidence"] >= 0.5]


def units_from(boundaries: list[dict], total: float, *, unit: str = "match",
               min_sec: float | None = None, max_sec: float | None = None) -> list[dict]:
    """把边界点连成单元 ``[{start, end, evidence, kind}]``。

    - 只有 ``close`` 点也能成单元（开场那段从 0 起算）；
    - 单元短于 ``min_sec`` 就与后一个合并（避免半局碎片）；
    - 单元长于 ``max_sec`` 就按等分切开（切点仍落在句子边界由上层 :func:`snap_candidates` 处理）。
    """
    preset = notes_mod.UNIT_PRESETS.get(unit or "match") or notes_mod.UNIT_PRESETS["chapter"]
    lo = float(min_sec if min_sec is not None else preset["minSec"])
    hi = float(max_sec if max_sec is not None else preset["maxSec"])
    # ⚠️ 边界点可能**正好落在素材末尾**（最后一局的结算就在最后一秒）。
    #    以前写的是 `0 < t < total`，把末尾那个点排除掉 ⇒ 只剩 1 个点 ⇒ 一个单元都划不出
    #    （本轮实测：1200 秒素材、600/1200 两个结算点，结果 unitSpans=[]）。
    cuts = sorted({round(min(float(p["t"]), total), 3)
                   for p in boundaries if 1e-6 < float(p["t"]) <= total + 1e-6})
    if len(cuts) < 2:
        return []

    spans: list[dict] = []
    prev = 0.0
    for c in cuts:
        if c - prev >= lo * 0.5:
            spans.append({"start": prev, "end": c})
            prev = c
    if total - prev >= lo * 0.5:
        spans.append({"start": prev, "end": total})

    # 太短 → 与后一个合并（最后一段与前一个合并）
    merged: list[dict] = []
    for sp in spans:
        if merged and (sp["end"] - sp["start"]) < lo:
            merged[-1]["end"] = sp["end"]
        else:
            merged.append(dict(sp))
    if len(merged) >= 2 and (merged[-1]["end"] - merged[-1]["start"]) < lo:
        merged[-2]["end"] = merged[-1]["end"]
        merged.pop()

    # 太长 → 等分（一局 40 分钟的 MOBA 不能整条出一段）
    out: list[dict] = []
    for sp in merged:
        dur = sp["end"] - sp["start"]
        if dur <= hi:
            out.append(sp)
            continue
        n = int(dur // hi) + (1 if dur % hi else 0)
        step = dur / n
        for i in range(n):
            out.append({"start": sp["start"] + i * step,
                        "end": sp["start"] + (i + 1) * step if i < n - 1 else sp["end"],
                        "splitFrom": True})
    for sp in out:
        sp["start"] = round(sp["start"], 3)
        sp["end"] = round(sp["end"], 3)
        sp["dur"] = round(sp["end"] - sp["start"], 3)
    return out


def snap_candidates(cands: list[dict], boundaries: list[dict],
                    tol: float = 8.0) -> int:
    """把候选的起止**吸到最近边界**（容差 ``tol`` 秒内才动），返回改动条数。

    严格模式下这能保证「切点正好落在结算/开局那一刻」，而不是切在半句话上。
    """
    if not boundaries:
        return 0
    pts = sorted(float(p["t"]) for p in boundaries)
    changed = 0
    for c in cands:
        for key in ("start", "end"):
            v = float(c.get(key) or 0)
            near = min(pts, key=lambda p: abs(p - v)) if pts else None
            if near is not None and abs(near - v) <= tol and abs(near - v) > 0.01:
                c[key] = round(near, 3)
                changed += 1
        if float(c.get("end") or 0) <= float(c.get("start") or 0):
            c["end"] = round(float(c["start"]) + 1.0, 3)
    return changed
