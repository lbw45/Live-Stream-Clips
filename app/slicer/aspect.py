#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制画幅（宽高比）—— 识别 / 目标尺寸 / 重编码滤镜的**唯一出处**。

需求来源（用户原话，v3.25）：
    「录制画幅可以识别一下，当直播内容是连线PK的时改为录制3:4的画幅，
      或者可以自动识别直播间画幅 16:9 / 9:16 / 3:4 / 4:3 这四种；
      当刚开始录制时的直播间画幅是 3:4 时就录制 3:4 的画幅，
      当画幅改变时就保存上一个画幅为一段录制素材，又继续录制新的画幅比例。」

实现口径（用户选定：**真重编码到目标画幅**，不是只做标记）：
- 识别：ffprobe 实读源流宽高 → 按**最接近**的标准比例归类（容差 2%），
  超出全部容差记 ``unknown``（此时不重编码，保持原样 —— 宁可不处理也不瞎裁）。
- 输出：以**源高度**为基准算目标尺寸（不放大、不缩分辨率），
  多出来的部分用 ``scale=...:increase`` + ``crop`` **居中裁掉**（不补黑边：
  短视频出片不要黑边；且 PK 场景下 3:4 本来就是主播画面的真实占比）。
- 音频**始终 copy**（重编码只碰视频，省算力且不损音质）。

⚠️ 画幅分类的容差是实测换来的妥协：抖音同一场直播的流会在
``1920x1080`` / ``1080x1920`` / ``1080x1440`` / ``544x960`` 之间跳，
比例并不精确等于标准值（如 544/960=0.5667 vs 9/16=0.5625）。
**不设容差会把同一个画幅判成两类 → 疯狂分段**（K56）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 四类标准画幅（用户指定）。ratio = 宽/高
ASPECTS: dict[str, float] = {
    "16:9": 16 / 9,     # 横屏直播
    "9:16": 9 / 16,     # 竖屏直播
    "3:4": 3 / 4,       # 连线 PK / 上下分屏
    "4:3": 4 / 3,       # 老式横屏
}
# 文件名后缀（v3.25 需求：「改动画幅的素材在命名上简单备注一下」）
# 用 ASCII：目录链必须英文（D25/K24），且冒号在 Windows 文件名里非法
ASPECT_TAGS: dict[str, str] = {
    "16:9": "169",
    "9:16": "916",
    "3:4": "34",
    "4:3": "43",
}
ASPECT_LABELS: dict[str, str] = {
    "16:9": "16:9 横屏",
    "9:16": "9:16 竖屏",
    "3:4": "3:4 竖屏（PK/连线常见）",
    "4:3": "4:3 横屏",
}
UNKNOWN = "unknown"

MODE_AUTO = "auto"      # 自动识别并按识别结果重编码
MODE_COPY = "copy"      # 原样录制（不识别、不重编码）
MODES: tuple[str, ...] = (MODE_AUTO, "16:9", "9:16", "3:4", "4:3", MODE_COPY)

# 比例容差：相对误差超过它就不归这一类（K56：太小会来回判变、疯狂分段）
CLASSIFY_TOL = 0.02
# 源尺寸与目标尺寸相差不超过这么多像素 → 认为无需重编码
TRANSFORM_EPS_PX = 2

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_encoder_cache: dict[str, str] = {}


def ratio_of(width: int, height: int) -> float:
    """宽高比；非法输入返回 0。"""
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return 0.0
    return (w / h) if w > 0 and h > 0 else 0.0


def classify(width: int, height: int, pk: bool = False,
             tol: float = CLASSIFY_TOL) -> str:
    """把实测宽高归到四类之一；都不像就返回 ``unknown``。

    ``pk=True``（房间信息显示正在连线/PK）时按用户口径**偏向 3:4**：
    PK 画面常被平台拼成接近 3:4 的区域，此时即使比例处在 9:16 与 3:4 之间
    的模糊地带，也判 3:4 —— 这正是用户要的那一条。
    """
    r = ratio_of(width, height)
    if r <= 0:
        return UNKNOWN
    if pk and 0.62 <= r <= 0.95:
        return "3:4"
    best, best_err = UNKNOWN, None
    for key, target in ASPECTS.items():
        err = abs(r - target) / target
        if err <= tol and (best_err is None or err < best_err):
            best, best_err = key, err
    return best


def tag_of(aspect: str) -> str:
    """文件名后缀（``916``）；unknown/空 → 空串（不加后缀）。"""
    return ASPECT_TAGS.get(aspect or "", "")


def aspect_of_tag(tag: str) -> str:
    """后缀 → 画幅 key（反查，给录制库展示用）。"""
    for key, t in ASPECT_TAGS.items():
        if t == tag:
            return key
    return UNKNOWN


def target_size(width: int, height: int, aspect: str) -> tuple[int, int]:
    """目标尺寸：**以源高度为基准**（不放大分辨率），宽按画幅算，取偶数。

    返回 ``(0, 0)`` 表示算不出来（画幅未知或尺寸非法）。
    """
    ratio = ASPECTS.get(aspect or "")
    try:
        h = int(height)
    except (TypeError, ValueError):
        return (0, 0)
    if not ratio or h <= 0:
        return (0, 0)
    w = int(round(h * ratio))
    return (_even(w), _even(h))


def needs_transform(width: int, height: int, aspect: str) -> bool:
    """源尺寸是否已等于目标尺寸（等于就不用重编码）。"""
    W, H = target_size(width, height, aspect)
    if not W or not H:
        return False
    return abs(W - int(width)) > TRANSFORM_EPS_PX or abs(H - int(height)) > TRANSFORM_EPS_PX


def build_vf(width: int, height: int, aspect: str) -> str:
    """构造 ``-vf``：缩放填满 + 居中裁到目标尺寸。无需变换时返回空串。

    ``increase``（不是 ``decrease``）：先把画面放大到**覆盖**目标框，再 crop ——
    这样画面是满的、不出现黑边；代价是边缘会被裁掉一点（auto 模式下通常只有几像素）。
    """
    if not needs_transform(width, height, aspect):
        return ""
    W, H = target_size(width, height, aspect)
    return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"


def plan(width: int, height: int, mode: str, pk: bool = False) -> dict:
    """给出一个录制段的完整画幅方案（录制器与 UI 共用同一份结论）。

    返回 ``{mode, aspect, width, height, srcWidth, srcHeight, vf, encode, tag}``；
    ``encode`` 为 ``copy`` / ``nvenc`` / ``x264`` / ``""``（无法处理）。
    """
    mode = normalize_mode(mode)
    out = {
        "mode": mode,
        "aspect": UNKNOWN,
        "width": int(width or 0), "height": int(height or 0),
        "srcWidth": int(width or 0), "srcHeight": int(height or 0),
        "vf": "", "encode": "copy", "tag": "",
    }
    if mode == MODE_COPY or not width or not height:
        return out
    aspect = mode if mode in ASPECTS else classify(width, height, pk=pk)
    out["aspect"] = aspect
    if aspect == UNKNOWN:
        return out                       # 认不出来 → 保持原样（不瞎裁）
    W, H = target_size(width, height, aspect)
    out["width"], out["height"] = W, H
    out["tag"] = tag_of(aspect)
    vf = build_vf(width, height, aspect)
    if not vf:
        return out                       # 已经是这个画幅了，copy 即可
    out["vf"] = vf
    out["encode"] = pick_video_encoder()
    return out


def normalize_mode(value: str, default: str = MODE_AUTO) -> str:
    """把任意入参收敛到 :data:`MODES` 之一（大小写/空格/别名都容错）。"""
    s = str(value or "").strip().lower().replace("：", ":")
    if s in ("", "auto", "自动", "自动识别"):
        return MODE_AUTO
    if s in ("copy", "原样", "不处理", "原始"):
        return MODE_COPY
    for key in ASPECTS:
        if s == key or s == key.replace(":", ""):
            return key
    return default


def mode_label(mode: str) -> str:
    m = normalize_mode(mode)
    if m == MODE_AUTO:
        return "自动识别"
    if m == MODE_COPY:
        return "原样录制（不重编码）"
    return ASPECT_LABELS.get(m, m)


# ---------------------------------------------------------------- 编码器

def pick_video_encoder(ffmpeg: Path | str | None = None) -> str:
    """选视频编码器：``nvenc`` 优先，不可用回落 ``x264``。

    结果**缓存**（一场录制会起好几次 ffmpeg，没必要每次都查）。
    消费级 N 卡对并发 NVENC 会话有限制（3050 Ti 约 3–5 路），
    多路同时录时 nvenc 可能起不来 → 那时只能靠上层报错后回落，这里不猜。
    """
    if "enc" in _encoder_cache:
        return _encoder_cache["enc"]
    from ..common.paths import ws
    exe = str(ffmpeg or ws.ffmpeg)
    enc = "x264"
    try:
        r = subprocess.run([exe, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20,
                           creationflags=_CREATE_NO_WINDOW)
        if r.returncode == 0 and "h264_nvenc" in (r.stdout or ""):
            enc = "nvenc"
    except (OSError, subprocess.SubprocessError):
        enc = "x264"
    _encoder_cache["enc"] = enc
    return enc


def encoder_args(kind: str) -> list[str]:
    """编码器对应的 ffmpeg 参数（重编码只碰视频，音频始终 copy）。"""
    if kind == "nvenc":
        # p4 = 质量/速度平衡点；vbr 让码率跟着画面复杂度走（直播画面差异大）
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
                "-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M",
                "-pix_fmt", "yuv420p"]
    # CPU 兜底：veryfast + crf，长时间录制不能再慢
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-max_muxing_queue_size", "1024"]


def _even(n: int) -> int:
    n = int(n)
    return n if n % 2 == 0 else n + 1
