#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪辑决策生成：候选片段 → §6.5 的剪辑决策结构（纯计算，无 IO）。

§6.5 的核心设计是 **「剪辑 = 保留区间模式」**（不是时间轴上加效果）：

    keepRanges   保留哪些区间、按顺序拼起来
    hook.copiedToStart   把 3–12s 高能钩子**复制到开头**（留住完播率的核心手法）

**最容易做错的一点**：`hook.copiedToStart` 会把成片时间轴整体后移。
如果只写了 `keepRanges` 而忘了重算 `subtitleCues`，字幕就会与语音**整体错位**
（错位时长正好等于钩子长度）—— 而且画面正常、只是字幕对不上，
属于最难肉眼发现的那类 bug。所以这里把「重映射」固化在 :func:`build_edit_decision` 里，
字幕**只能**由 :func:`app.slicer.subtitles.cues_from_segments` 依据最终的 keepRanges 生成。

两种策略（`tasks.strategy`）：
- ``conservative``：不剪中间内容 —— keepRanges = [钩子?] + [整段]
- ``aggressive``：按停顿把低信息量的空档剪掉 —— keepRanges 会是多段
"""
from __future__ import annotations

import time

from .subtitles import SubtitleStyle, cues_from_segments, total_duration_ms

STRATEGY_CONSERVATIVE = "conservative"
STRATEGY_AGGRESSIVE = "aggressive"
STRATEGY_MANUAL = "manual"          # 预览剪辑页：用户自己分割/删除分段

DEFAULT_PARAMS: dict = {
    "aggressiveMinGapMs": 1500.0,     # aggressive：≥ 此停顿视为可剪空档
    "aggressiveMinKeepRatio": 0.55,   # 剪完至少要留下的比例（否则干脆不剪）
    "aggressiveMinKeptSec": 30.0,     # 剪完不得短于此时长
    "hookMinMs": 3000.0,
    "hookMaxMs": 12000.0,
    # v4.2（用户「切出来的内容很生硬，没有互联网的那种镜头感」）：
    # **保守策略也收短长停顿** —— 直播说话节奏慢，整段直出必然拖沓。
    # 只动「转写盖章的空白」（相邻两句之间的空档），**绝不动有话的地方**。
    "silenceTrimMs": 1500.0,          # ≥ 此时长的纯停顿才收短（1.5s 以下的自然停顿不动）
    "silenceKeepMs": 400.0,           # 收短后保留的呼吸感（居中留这么多）
    "silenceMinRatio": 0.75,          # 剪完至少要留下的比例（极端情况宁可拖沓不能剪碎）
    "hookBigMs": 2600.0,              # 开头大花字（钩子原话）压屏时长
}


def _clamp_hook(hook: dict | None, cand_start_ms: float, cand_end_ms: float,
                params: dict) -> dict | None:
    """校验并夹取钩子区间：必须落在候选内、时长在 3–12s。"""
    if not isinstance(hook, dict):
        return None
    try:
        hs = float(hook.get("startMs") or 0)
        he = float(hook.get("endMs") or 0)
    except (TypeError, ValueError):
        return None
    hs = max(hs, cand_start_ms)
    he = min(he, cand_end_ms)
    dur = he - hs
    if dur < params["hookMinMs"] - 1e-6 or dur > params["hookMaxMs"] + 1e-6:
        return None
    out = {"startMs": round(hs, 1), "endMs": round(he, 1),
           "copiedToStart": bool(hook.get("copiedToStart", True))}
    if hook.get("text"):
        out["text"] = str(hook["text"])[:80]
    return out


def _prune_ranges(segments: list[dict], start_ms: float, end_ms: float,
                  params: dict) -> list[dict]:
    """``aggressive``：按停顿切段，丢掉低信息量的空档（保守兜底：剪太狠就不剪）。

    信息量用**语速**（字/秒）近似 —— 和 M3-B 本地评分的口径一致。
    """
    inside = [s for s in segments if s["end"] * 1000 > start_ms and s["start"] * 1000 < end_ms]
    if len(inside) < 3:
        return [{"startMs": start_ms, "endMs": end_ms}]

    # 1) 按 ≥ minGap 的停顿切成若干「连续段」
    runs: list[list[dict]] = [[inside[0]]]
    for a, b in zip(inside, inside[1:]):
        if (b["start"] - a["end"]) * 1000 >= params["aggressiveMinGapMs"]:
            runs.append([b])
        else:
            runs[-1].append(b)

    # 2) 每段算语速，低于总均值一定比例的判为「空档」
    def rate(run: list[dict]) -> float:
        dur = max((run[-1]["end"] - run[0]["start"]), 1e-6)
        chars = sum(len(s.get("text") or "") for s in run)
        return chars / dur

    rates = [rate(r) for r in runs]
    avg = sum(rates) / max(len(rates), 1)
    keep = [r for r, rt in zip(runs, rates) if rt >= avg * 0.6]
    if not keep:
        return [{"startMs": start_ms, "endMs": end_ms}]

    kept = [{"startMs": max(r[0]["start"] * 1000, start_ms),
             "endMs": min(r[-1]["end"] * 1000, end_ms)} for r in keep]
    # 合并相邻（间隔 < minGap 就连起来）
    merged: list[dict] = []
    for r in kept:
        if merged and r["startMs"] - merged[-1]["endMs"] < params["aggressiveMinGapMs"]:
            merged[-1]["endMs"] = r["endMs"]
        else:
            merged.append(r)

    # 3) 兜底：剪得太狠（留下的比例太低或总时长太短）就放弃剪，退回整段
    total = end_ms - start_ms
    kept_len = sum(r["endMs"] - r["startMs"] for r in merged)
    if (kept_len < total * params["aggressiveMinKeepRatio"]
            or kept_len < params["aggressiveMinKeptSec"] * 1000):
        return [{"startMs": start_ms, "endMs": end_ms}]
    return merged


def _trim_silences(ranges: list[dict], segments: list[dict],
                   params: dict) -> list[dict]:
    """v4.2：把**纯停顿**收短（保守策略也用 —— 这是「生硬/拖沓」的最大来源）。

    只动「转写盖章的空白」：相邻两句之间 ≥ ``silenceTrimMs`` 的空档，
    居中保留 ``silenceKeepMs`` 的呼吸感，其余剪掉。**有话的地方一毫秒不动** ——
    丢了内容是事故，丢了 2 秒冷场是节奏。
    兜底：剪完总长 < ``silenceMinRatio`` 或 < 3s 就整体放弃（宁可拖沓，不能剪碎）。
    每个碎段仍 ≥ 400ms（与手动剪辑的单段下限同一口径）。
    """
    if not segments:
        return ranges
    out: list[dict] = []
    for rng in ranges:
        rs, re_ = float(rng["startMs"]), float(rng["endMs"])
        inside = [s for s in segments
                  if s["end"] * 1000.0 > rs and s["start"] * 1000.0 < re_]
        if len(inside) < 2:
            out.append(rng)
            continue
        cuts: list[tuple[float, float]] = []
        for a, b in zip(inside, inside[1:]):
            g0, g1 = a["end"] * 1000.0, b["start"] * 1000.0
            gap = g1 - g0
            if gap >= params["silenceTrimMs"]:
                keep = min(float(params["silenceKeepMs"]), gap)
                mid = (g0 + g1) / 2.0
                cuts.append((mid - keep / 2.0, mid + keep / 2.0))
        if not cuts:
            out.append(rng)
            continue
        pieces: list[dict] = []
        cur = rs
        for g0, g1 in cuts:
            if g0 > cur:
                pieces.append({"startMs": cur, "endMs": g0})
            cur = max(cur, g1)
        if cur < re_:
            pieces.append({"startMs": cur, "endMs": re_})
        total = sum(r["endMs"] - r["startMs"] for r in pieces)
        if total < (re_ - rs) * params["silenceMinRatio"] or total < 3000.0:
            out.append(rng)                       # 剪得太狠 → 整段放弃
        else:
            out.extend(r for r in pieces
                       if r["endMs"] - r["startMs"] >= 400.0)
    return out or ranges


def build_edit_decision(candidate: dict, transcript: dict, *,
                        strategy: str = STRATEGY_CONSERVATIVE,
                        planner: str = "local_fallback",
                        style: SubtitleStyle | None = None,
                        params: dict | None = None,
                        with_subtitles: bool = True,
                        hide_fillers: bool = False,
                        include_hook: bool = True) -> dict:
    """候选 + 转写 → §6.5 剪辑决策。"""
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})

    segments = list(transcript.get("segments") or [])
    start_ms = float(candidate["start"]) * 1000.0
    end_ms = float(candidate["end"]) * 1000.0
    if end_ms <= start_ms:
        raise ValueError(f"候选区间非法：{start_ms} → {end_ms}")

    # --- keepRanges：先按策略决定「正文区间」，再决定要不要把钩子复制到开头
    body = ([{"startMs": start_ms, "endMs": end_ms}]
            if strategy != STRATEGY_AGGRESSIVE
            else _prune_ranges(segments, start_ms, end_ms, p))
    # v4.2：保守策略也收长停顿（aggressive 有自己的整段重剪逻辑，不走这里）
    if strategy != STRATEGY_AGGRESSIVE:
        body = _trim_silences(body, segments, p)

    hook = _clamp_hook(candidate.get("hook"), start_ms, end_ms, p)
    keep_ranges: list[dict] = []
    # v3.168（用户 2026-10-28）：「控制成片是否有开头钩子内容」——
    # include_hook=False 时不把钩子**复制**到开头（钩子原始位置的内容仍在正片里，
    # 只是开头不再高能前置）；此时没有「钩子→正文」这一刀，转场自然不生效。
    if hook and hook.get("copiedToStart") and include_hook:
        keep_ranges.append({"startMs": hook["startMs"], "endMs": hook["endMs"]})
    keep_ranges.extend(body)

    # --- 字幕：**必须**基于最终 keepRanges 重映射（钩子前置会整体后移时间轴）
    cues = (cues_from_segments(segments, keep_ranges, style, hide_fillers)
            if with_subtitles else [])

    # v4.2：开头**大花字** —— 把钩子原话用大号黄字压在画面上部（抖音切片的标志性镜头感）。
    # hook 段固定是 keep_ranges[0]（成片时间轴 0 起步），压 0–hookBigMs。
    if (hook and include_hook and hook.get("text") and cues is not None
            and keep_ranges and keep_ranges[0].get("startMs") == hook["startMs"]):
        big_ms = min(float(p["hookBigMs"]), float(keep_ranges[0]["endMs"] - keep_ranges[0]["startMs"]))
        if big_ms >= 800.0:
            cues.insert(0, {"startMs": 0.0, "endMs": round(big_ms, 1),
                            "text": str(hook["text"])[:24], "style": "hookBig"})

    overall = float((candidate.get("scores") or {}).get("overall") or 0)
    return {
        "strategy": strategy,
        "planner": planner,
        "plannerConfidence": round(min(max(overall / 100.0, 0.0), 1.0), 3),
        "hook": hook,
        "keepRanges": [{"startMs": round(r["startMs"], 1), "endMs": round(r["endMs"], 1)}
                       for r in keep_ranges],
        "subtitleCues": cues,
        "keywords": [str(k)[:16] for k in (candidate.get("keywords") or [])
                     if str(k).strip()][:4],          # v4.3：字幕强调词（空=不强调）
        "visualMode": "original",              # D10：画面替换移出第一版
        "coverTitle": str(candidate.get("title") or "")[:60],
        "sourceCandidateIdx": candidate.get("idx"),
        "sourceStartSec": round(start_ms / 1000.0, 3),
        "sourceEndSec": round(end_ms / 1000.0, 3),
        "outputDurationMs": round(total_duration_ms(keep_ranges), 1),
        "generatedAt": round(time.time(), 2),
    }


def normalize_keep_ranges(ranges, start_ms: float, end_ms: float,
                          *, min_seg_ms: float = 400.0,
                          min_total_ms: float = 3000.0) -> list[dict]:
    """手动剪辑的 keep 段校验 + 归一化（纯函数，cut.save 与渲染共用同一套判据）。

    规则：排序、两两不重叠、每段落在候选界内（容差 1ms）、单段 ≥ min_seg_ms、
    总长 ≥ min_total_ms。任何一条不满足就抛 ``ValueError``（消息可直接给用户看）。
    """
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("keep 段不能为空")
    out: list[dict] = []
    tol = 1.0
    for r in ranges:
        if not isinstance(r, dict):
            raise ValueError("keep 段格式不合法（需要 {startMs, endMs} 对象数组）")
        try:
            s = float(r["startMs"])
            e = float(r["endMs"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("keep 段缺少 startMs / endMs")
        if not (s < e):
            raise ValueError(f"keep 段起点必须小于终点：{s} → {e}")
        if s < start_ms - tol or e > end_ms + tol:
            raise ValueError(f"keep 段越出候选区间 [{start_ms:.0f}, {end_ms:.0f}]："
                             f"{s:.0f} → {e:.0f}")
        out.append({"startMs": s, "endMs": e})
    out.sort(key=lambda r: r["startMs"])
    for a, b in zip(out, out[1:]):
        if b["startMs"] < a["endMs"] - tol:
            raise ValueError("keep 段之间不能重叠（请先分割，再删除不要的段）")
    for r in out:
        if r["endMs"] - r["startMs"] < min_seg_ms:
            raise ValueError("单段太短（不足 0.4 秒），请用「分割」后删除整段")
    total = sum(r["endMs"] - r["startMs"] for r in out)
    if total < min_total_ms:
        raise ValueError(f"留下的总时长太短（{total / 1000:.1f}s < 3s），不成片")
    return [{"startMs": round(r["startMs"], 1), "endMs": round(r["endMs"], 1)}
            for r in out]


def build_manual_decision(candidate: dict, transcript: dict,
                          keep_ranges: list[dict], *,
                          style: SubtitleStyle | None = None,
                          with_subtitles: bool = True,
                          hide_fillers: bool = False) -> dict:
    """预览剪辑页（用户手动分割/删除分段）→ 决策结构。

    **与 :func:`build_edit_decision` 同一条铁律**：字幕必须基于最终 keepRanges
    由 ``cues_from_segments`` 重映射 —— 用户裁掉多少，声音随 ffmpeg 按段裁切、
    字幕时间轴跟着前移，三者永远自动对齐（这正是参考实现"删除分段，原声音和字幕
    时间同步修剪"的对齐）。
    """
    start_ms = float(candidate["start"]) * 1000.0
    end_ms = float(candidate["end"]) * 1000.0
    if end_ms <= start_ms:
        raise ValueError(f"候选区间非法：{start_ms} → {end_ms}")
    rs = normalize_keep_ranges(keep_ranges, start_ms, end_ms)
    segments = list(transcript.get("segments") or [])
    cues = cues_from_segments(segments, rs, style, hide_fillers) if with_subtitles else []
    overall = float((candidate.get("scores") or {}).get("overall") or 0)
    return {
        "strategy": STRATEGY_MANUAL,
        "planner": "manual",
        "plannerConfidence": round(min(max(overall / 100.0, 0.0), 1.0), 3),
        "hook": None,                      # 手动剪辑不自动前置钩子（用户自己决定留哪段）
        "keepRanges": rs,
        "subtitleCues": cues,
        "visualMode": "original",
        "coverTitle": str(candidate.get("title") or "")[:60],
        "sourceCandidateIdx": candidate.get("idx"),
        "sourceStartSec": round(start_ms / 1000.0, 3),
        "sourceEndSec": round(end_ms / 1000.0, 3),
        "outputDurationMs": round(total_duration_ms(rs), 1),
        "generatedAt": round(time.time(), 2),
    }


def decision_fingerprint(decision: dict) -> str:
    """决策指纹：**只跟「会影响画面/声音」的字段有关**，用于判断能否复用渲染产物（§6.5）。

    刻意**不含** coverTitle / planner 这类不影响成片的字段 —— 否则改个标题就要重渲染。
    """
    import hashlib
    import json

    material = {
        "keepRanges": decision.get("keepRanges"),
        "visualMode": decision.get("visualMode"),
        "subtitles": [
            (round(c["startMs"]), round(c["endMs"]), c["text"])
            for c in (decision.get("subtitleCues") or [])
        ],
        "render": decision.get("_renderParams") or {},
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
