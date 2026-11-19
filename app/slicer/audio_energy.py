#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音频能量雷达（v4.2）：给切片链路补上「模型看不到的那一半信号」。

**为什么要有这个模块**（用户 2026-10-31「切片内容没有亮点 / 很生硬」）：

切片送审只有**转写文本**。直播真正的亮点 —— 破防大喊、爆笑、欢呼、音效爆发 ——
在转写里要么是一句平淡的字，要么**根本没被转出来**（Whisper 对喊叫/笑声经常直接跳过）。
模型对着干文本猜"哪里精彩"，切出来的自然不是最炸的那几段。

做法（全部本地、不花钱、不加依赖）：

1. :func:`rms_curve` —— ffmpeg 解 8kHz 单声道 PCM，按 0.5s 窗算 RMS（响度包络）；
   一次解码全场，**按源视频 mtime+size 缓存**（runtime_data/slicer/energy/），
   重切/复核时不重复解码；
2. :func:`find_peaks` —— 全场 q90 以上的连续窗合并成「峰值段」＝疑似高光时刻；
3. :func:`annotate` —— 每句转写标注 ``_loud``（句内峰值能量的全场分位 0–1）与
   ``_rate``（语速的全场分位 0–1），送审时渲染成 ``[高能]``/``[语速↑]`` 标记；
4. :func:`enrich` —— 一站式入口：算/读缓存 → 标注句 → 返回峰段。**任何失败都静默降级**
   （返回无标注的原始句）—— 能量是加分信号，绝不能因为它把切片搞挂。

诚实原则：峰段只是"疑似高光"，标注里也只说「能量高」，不假装知道"这是击杀还是爆笑"。
"""
from __future__ import annotations

import json
import time
from array import array
from bisect import bisect_left
from pathlib import Path

from app.common.paths import ws

#: 采样率：8kHz 对"响度包络"绰绰有余（人声能量集中在 300–3400Hz），解码最快
_SR = 8000
#: 能量窗（秒）—— 0.5s 足够分辨"一句喊"，再细就全是抖动
_WIN = 0.5
#: 峰值段检测：能量高于全场这个分位数才算峰
PEAK_QUANTILE = 0.90
#: 相邻峰窗间隔 ≤ 这个秒数就合并成同一段
PEAK_MERGE_SEC = 3.0
#: 最多保留多少段（直播录屏 BGM/音效常驻，q90 以上往往一大把 —— 只取**最强 Top N**
#: 才有"高光种子"的聚焦意义，撒一把种子等于没有种子）
TOP_PEAKS = 12
#: 语速突增的判定分位（句内字/秒高于全场 90% 的句才算「语速↑」）
RATE_QUANTILE = 0.90
#: 句级「高能」判定分位
LOUD_QUANTILE = 0.90
#: 次级「情绪↑」分位
WARM_QUANTILE = 0.75

try:                                    # C 实现，快 ~50 倍；Python 3.13 起被移除
    import audioop as _audioop

    def _rms(chunk: bytes) -> float:
        return _audioop.rms(chunk, 2) / 32768.0
except ImportError:                     # 兜底：纯 Python（慢但正确）
    def _rms(chunk: bytes) -> float:
        buf = array("h")
        buf.frombytes(chunk)
        if not buf:
            return 0.0
        s = 0
        for v in buf:
            s += v * v
        return (s / len(buf)) ** 0.5 / 32768.0


def cache_path(video: str | Path) -> Path:
    """能量缓存放哪：``runtime_data/slicer/energy/<源视频 stem>.json``。"""
    stem = Path(str(video)).stem
    return ws.slicer_runtime / "energy" / f"{stem}.json"


def rms_curve(video: str | Path, *, ffmpeg=None, force: bool = False) -> dict:
    """整段音频的响度包络（带缓存）。

    返回 ``{"rms": [...], "win": 0.5, "quantiles": {...}, "peaks": [...],
            "fromCache": bool}``。解码/分析失败抛 ``RuntimeError``（调用方决定降级）。
    """
    video = Path(str(video))
    if not video.is_file():
        raise RuntimeError(f"源视频不在：{video}")
    cp = cache_path(video)
    if not force and cp.is_file():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if (data.get("mtime") == round(video.stat().st_mtime, 3)
                    and data.get("size") == video.stat().st_size
                    and isinstance(data.get("rms"), list)):
                data["fromCache"] = True
                return data
        except (OSError, ValueError):
            pass

    ffmpeg = str(ffmpeg or ws.ffmpeg)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-i", str(video), "-vn", "-ac", "1", "-ar", str(_SR), "-f", "s16le", "-"]
    # 走 proc.run（唯一出处）：桌面壳里起子进程必须带 CREATE_NO_WINDOW，
    # 否则每次解码 PCM 都会在屏幕上闪一个黑框（K：creationflags 全项目一处管）。
    from app.common import proc
    raw = proc.run(cmd, capture_output=True, timeout=1800).stdout
    if not raw:
        raise RuntimeError("ffmpeg 没解出音频（可能没有音轨）")
    step = int(_SR * _WIN)
    curve: list[float] = []
    for i in range(0, len(raw) - step + 1, step):
        curve.append(round(_rms(raw[i:i + step]), 5))
    if not curve:
        raise RuntimeError("音频太短，算不出能量曲线")

    st = sorted(curve)
    n = len(st)

    def q(p: float) -> float:
        return st[min(n - 1, int(n * p))]

    quantiles = {f"q{int(p * 100)}": round(q(p), 5)
                 for p in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95)}
    peaks = find_peaks(curve)
    data = {"version": 1, "video": str(video), "mtime": round(video.stat().st_mtime, 3),
            "size": video.stat().st_size, "win": _WIN, "sr": _SR,
            "rms": curve, "quantiles": quantiles, "peaks": peaks,
            "generatedAt": round(time.time(), 1), "fromCache": False}
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(cp)
    except OSError:
        pass                                    # 缓存写不进去就当场用，下次再算
    return data


def find_peaks(curve: list[float]) -> list[dict]:
    """全场能量 q90 以上的连续窗 → 合并成峰值段（疑似高光时刻），按强度降序。"""
    if not curve:
        return []
    st = sorted(curve)
    thr = st[min(len(st) - 1, int(len(st) * PEAK_QUANTILE))]
    hot = [(i * _WIN, v) for i, v in enumerate(curve) if v >= thr and v > 0]
    if not hot:
        return []
    merged: list[list] = []
    for t, v in hot:
        if merged and t - merged[-1][1] <= PEAK_MERGE_SEC:
            merged[-1][1] = t + _WIN
            merged[-1][2] = max(merged[-1][2], v)
        else:
            merged.append([t, t + _WIN, v])
    mx = max(v for _, _, v in merged) or 1.0
    peaks = [{"startSec": round(a, 2), "endSec": round(b, 2),
              "level": round(v / mx, 3)}          # 0–1，全场最强峰 = 1
             for a, b, v in merged]
    peaks.sort(key=lambda p: -p["level"])
    return peaks[:TOP_PEAKS]


def _pct_of(sorted_vals: list[float], v: float) -> float:
    """v 在已升序数组里的分位（0–1）。"""
    if not sorted_vals:
        return 0.0
    return min(1.0, bisect_left(sorted_vals, v) / len(sorted_vals))


def annotate(segments: list[dict], curve_data: dict) -> list[dict]:
    """给每句转写打 ``_loud``（能量分位）与 ``_rate``（语速分位），返回新列表。

    纯函数：不改传入的 segments（调用方拿到的是带标注的副本）。
    """
    curve = curve_data.get("rms") or []
    if not curve:
        return segments
    st = sorted(curve)
    win = float(curve_data.get("win") or _WIN)
    out: list[dict] = []
    rates: list[float] = []
    rows: list[tuple[dict, float]] = []
    for s in segments:
        try:
            a, b = float(s.get("start") or 0), float(s.get("end") or 0)
        except (TypeError, ValueError):
            out.append(s)
            continue
        i0, i1 = int(a / win), min(int(b / win), len(curve) - 1)
        peak = max(curve[i0:i1 + 1]) if i1 >= i0 else 0.0
        dur = max(b - a, 0.4)
        rate = len(str(s.get("text") or "")) / dur
        rows.append((s, peak))
        rates.append(rate)
    rates_sorted = sorted(rates)
    for (s, peak), rate in zip(rows, rates):
        s2 = dict(s)
        s2["_loud"] = round(_pct_of(st, peak), 3)
        s2["_rate"] = round(_pct_of(rates_sorted, rate), 3)
        out.append(s2)
    return out


def enrich(transcript_video: str | Path | None, segments: list[dict]) -> tuple[list[dict], dict | None]:
    """一站式：算/读能量缓存 → 标注句子。**失败一律静默降级**（返回原句 + None）。"""
    if not transcript_video or not segments:
        return segments, None
    try:
        data = rms_curve(transcript_video)
    except Exception:                                   # noqa: BLE001 — 信号缺了就缺了
        return segments, None
    marked = annotate(segments, data)
    return marked, {"peaks": data.get("peaks") or [],
                    "quantiles": data.get("quantiles") or {},
                    "fromCache": bool(data.get("fromCache"))}


def _selftest() -> int:
    """对 10-23 陈伯录屏实测：曲线、峰段、句级标注都跑一遍。"""
    video = ws.live_source_dir / "陈伯(全能王)_20261023-001651.mp4"
    tp = ws.transcripts_dir / "陈伯(全能王)_20261023-001651.json"
    ok = True

    def ck(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(("PASS " if cond else "FAIL ") + name + (f"  —— {detail}" if detail else ""))

    data = rms_curve(video)
    again = rms_curve(video)
    ck("能量曲线非空且二次调用命中缓存", len(data["rms"]) > 500 and again.get("fromCache") is True,
       f"{len(data['rms'])} 窗 / 二次 fromCache={again.get('fromCache')}")
    peaks = data["peaks"]
    ck("峰段 ≤ 12、都在界内、level 单调", 1 <= len(peaks) <= 12
       and all(p["startSec"] < p["endSec"] for p in peaks)
       and all(peaks[i]["level"] >= peaks[i + 1]["level"] for i in range(len(peaks) - 1)),
       f"{len(peaks)} 段，最强 {peaks[0] if peaks else '-'}")
    segs = json.loads(tp.read_text(encoding="utf-8"))["segments"]
    marked, info = enrich(str(video), segs)
    loud = [s for s in marked if s.get("_loud", 0) >= LOUD_QUANTILE]
    ck("句级标注：高能句占 10% 上下、峰段带回", 2 <= len(loud) <= len(marked) // 3
       and info is not None and len(info["peaks"]) == len(peaks),
       f"{len(loud)}/{len(marked)} 句 [高能]")
    # 最高能的一句，文本应该是情绪话（不许是空文本还标高能——空文本没有句可标）
    if loud:
        top = max(loud, key=lambda s: s["_loud"])
        ck("最强句有文字且能量分位最高档", bool(top.get("text")) and top["_loud"] >= 0.9,
           f"{top['start']:.0f}s {top['text'][:24]!r} loud={top['_loud']}")
    print("\n结果：", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
