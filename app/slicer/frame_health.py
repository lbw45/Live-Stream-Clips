#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制画面健康检测（v3.77，）。

**要解决的现象**：用户 2026-10-09「有个直播录屏为什么会画面抖动，能不能加个录制的过程中
检测画面是否正常，有没有黑屏画面抖动等等这些问题，检测到了就及时暂停录制，然后再又重新
开始录制直播」。

**做法**：不去解码整段（那要几分钟），只从**正在写的那个分片的尾部**抽几秒画面
（默认 3 秒、8fps、缩到 96×54 灰度），算几个统计量，再按阈值判三类故障：

| 判据 | 现象 | 靠什么量 |
|---|---|---|
| ``black`` | 黑屏 / 纯色无内容 | 平均亮度很低 **且** 空间标准差很小 |
| ``frozen`` | 画面卡住不动（解码器停了 / 流断了但 ffmpeg 还在写重复帧） | 相邻帧差近乎为 0 的帧占比极高 |
| ``jitter`` | 画面剧烈抖动 / 闪屏（帧序错乱、时间戳跳变、编码花屏） | 相邻帧差**很大**的帧占比极高 |

⚠️ 三个量都是**相对**判据，阈值是拿**本机真实录像**标定过的（见
``vid_analysis\\bench_frame_health.py``）—— 正常直播口播画面绝不能误判，
所以阈值取在「明显比正常画面更极端」的位置。判错的代价不对称：
误判会让一场好好的直播被反复重启（还会多敲抖音），漏判只是少救一段。
因此**要求连续两次都判坏**才真的动作（见 recorder 里的去抖），并加频率闸门。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..common import proc
from ..common.paths import ws

# ---------------------------------------------------------------- 采样参数
#: 每次看几秒（越长越准、越贵；3 秒足够分辨"卡住"与"正常说话"）
SAMPLE_SECONDS = 3.0
#: 采样帧率（8fps 足够判变化剧烈程度，解码也更省）
SAMPLE_FPS = 8.0
#: 缩放后的宽高（灰度图，越小越快；96×54 仍能看出画面有没有内容）
SAMPLE_W, SAMPLE_H = 96, 54

# ---------------------------------------------------------------- 判据阈值
#: 黑屏：亮度低于它 **且** 空间标准差低于它（纯色/无内容）
BLACK_LUMA = 16.0
BLACK_STD = 8.0
#: 卡死：相邻帧平均差低于它 **且**「几乎不动的帧」占比高于它
FROZEN_DIFF = 0.45
FROZEN_STATIC_RATIO = 0.85
#: 抖动：相邻帧差很大的帧占比高于它 **且** 90 分位差高于它
JITTER_JUMP_RATIO = 0.55
JITTER_P90 = 26.0
#: 抖动还会带「整体亮度剧烈跳变」（闪黑闪白），这一路单独兜底
JITTER_LUMA_JUMP = 26.0
#: 采样出来至少要有几帧才算数（少于它 = 读不出画面，也算异常）
MIN_FRAMES = 3

REASON_BLACK = "黑屏"
REASON_FROZEN = "画面卡住不动"
REASON_JITTER = "画面抖动/闪屏"
REASON_NOFRAME = "读不到画面"


def sample_gray(path: Path, *, seconds: float = SAMPLE_SECONDS,
                fps: float = SAMPLE_FPS,
                size: tuple[int, int] = (SAMPLE_W, SAMPLE_H)) -> np.ndarray | None:
    """从视频**尾部**抽 ``seconds`` 秒，返回 ``(n, h, w)`` 的灰度数组（0..255）。

    用 ``-sseof``（从结尾往回找），所以对**正在写**的分片同样有效 —— 这是"边录边检"的关键。
    任何一步失败都返回 None（调用方按「读不到画面」处理，不抛）。
    """
    w, h = size
    frame_bytes = w * h
    cmd = [str(ws.ffmpeg), "-v", "error", "-nostdin",
           "-sseof", f"-{float(seconds):.2f}", "-i", str(path),
           "-an", "-sn",
           "-vf", f"fps={fps:g},scale={w}:{h},format=gray",
           "-f", "rawvideo", "-"]
    try:
        r = proc.run(cmd, capture_output=True)
    except Exception:                                              # noqa: BLE001
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = len(buf) // frame_bytes
    if n <= 0:
        return None
    return buf[:n * frame_bytes].reshape(n, h, w).astype(np.float32)


def metrics(gray: np.ndarray) -> dict:
    """把灰度帧序列算成一组统计量（**纯函数**，可离线拿录像标定/回归）。"""
    n = int(gray.shape[0])
    per_frame_luma = gray.reshape(n, -1).mean(axis=1)
    per_frame_std = gray.reshape(n, -1).std(axis=1)
    if n >= 2:
        d = np.abs(np.diff(gray, axis=0)).reshape(n - 1, -1).mean(axis=1)
        luma_jump = np.abs(np.diff(per_frame_luma))
    else:
        d = np.zeros(1, dtype=np.float32)
        luma_jump = np.zeros(1, dtype=np.float32)
    return {
        "frames": n,
        "lumaMean": float(per_frame_luma.mean()),
        "lumaStd": float(per_frame_std.mean()),
        "diffMean": float(d.mean()),
        "diffP90": float(np.percentile(d, 90)),
        "staticRatio": float((d < 0.35).mean()),       # 几乎不动
        "jumpRatio": float((d > 30.0).mean()),         # 变化剧烈
        "lumaJumpMax": float(luma_jump.max()),
    }


def judge(m: dict) -> list[str]:
    """按阈值给出故障原因列表（空列表 = 正常）。**纯函数**。"""
    if int(m.get("frames") or 0) < MIN_FRAMES:
        return [REASON_NOFRAME]
    out: list[str] = []
    luma, std = float(m["lumaMean"]), float(m["lumaStd"])
    if luma <= BLACK_LUMA and std <= BLACK_STD:
        out.append(REASON_BLACK)
    if m["diffMean"] <= FROZEN_DIFF and m["staticRatio"] >= FROZEN_STATIC_RATIO:
        out.append(REASON_FROZEN)
    if m["jumpRatio"] >= JITTER_JUMP_RATIO and m["diffP90"] >= JITTER_P90:
        out.append(REASON_JITTER)
    elif float(m.get("lumaJumpMax") or 0.0) >= JITTER_LUMA_JUMP and m["jumpRatio"] >= 0.3:
        # 闪屏另一路：整幅亮度大起大落（闪黑/闪白），但帧差没到极端
        out.append(REASON_JITTER)
    return out


def probe(path: Path, *, seconds: float = SAMPLE_SECONDS,
          fps: float = SAMPLE_FPS) -> dict:
    """抽帧 + 判据一次做完。返回 ``{"ok": bool, "reasons": [...], "metrics": {...}}``。

    ``ok=False`` 且 ``reasons`` 非空 = 判到异常；采样失败也返回 ok=False（读不到画面）。
    """
    gray = sample_gray(path, seconds=seconds, fps=fps)
    if gray is None:
        return {"ok": False, "reasons": [REASON_NOFRAME], "metrics": {"frames": 0}}
    m = metrics(gray)
    reasons = judge(m)
    return {"ok": not reasons, "reasons": reasons, "metrics": m}


def describe(result: dict) -> str:
    """一行中文说明（日志/界面用）—— 带上关键数字，别只报「异常」。"""
    m = result.get("metrics") or {}
    if not result.get("reasons"):
        return "正常"
    return ("%s（亮度 %.1f/标准差 %.1f；帧差 均 %.1f / P90 %.1f；不动帧 %.0f%%；"
            "剧变帧 %.0f%%）" % (
                "、".join(result["reasons"]),
                float(m.get("lumaMean") or 0), float(m.get("lumaStd") or 0),
                float(m.get("diffMean") or 0), float(m.get("diffP90") or 0),
                float(m.get("staticRatio") or 0) * 100,
                float(m.get("jumpRatio") or 0) * 100))
