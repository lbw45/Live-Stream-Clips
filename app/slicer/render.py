#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FFmpeg 渲染管线（M4，§6.6 顺序固定）。

    1) 源片段裁剪        —— 每个保留区间一个输入（用 `-ss/-t` **快进**，不从 0 解码）
    2) 竖屏处理（D18）    —— 源是竖屏 → 只缩放不裁剪；横屏 → 居中裁剪 `crop=ih*9/16:ih`
    3) 画中画             —— D10 跳过
    4) 字幕               —— 生成 ASS 走 `subtitles=` 滤镜（§6.6 第 4 步）
    5) 音频混音           —— BGM 22% + 人声优先**自动闪避**（`sidechaincompress`）
    6) 编码输出           —— `h264_nvenc`（p4 / CQ 23）→ **失败自动回退** `libx264`

三个刻意的实现选择：

1. **多输入 + concat，而不是单输入 + trim**。`trim` 会让 ffmpeg 从头解码整段素材
   （1 小时的录像只取 30 秒也要读 1 小时）；而每个区间单独 `-ss` 是**按时间跳读**，
   实测差距是数量级。多输入再用 `concat` 拼起来，时间轴与 `keepRanges` 一一对应。
2. **BGM 用 `-stream_loop -1` + `atrim` 到成片时长**，而不是要求 BGM 比成片长 —— 否则
   短 BGM 会让输出被截短，或音乐中途断掉。
3. **NVENC 失败就整体回退 libx264**，不是「换几个参数再试」。显存被占满时 NVENC 会
   直接开不了会话，此时唯一有意义的动作就是换软编。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..common import proc
from ..common.paths import ws
from .subtitles import SubtitleStyle, total_duration_ms

# ---------------------------------------------------------------- 参数


# ---------------------------------------------------------------- 画幅（D30）
# 成片的**画幅跟着录制的画幅走**：直播间是 16:9 横屏，成片就出 16:9，**不裁剪**；
# 直播间是 9:16 竖屏，成片就出 9:16。除非用户显式指定另一种画幅（那时才居中裁剪）。
ORIENT_AUTO = "auto"            # 跟随源（默认）
ORIENT_PORTRAIT = "portrait"    # 强制 9:16
ORIENT_LANDSCAPE = "landscape"  # 强制 16:9
ORIENT_CHOICES = (ORIENT_AUTO, ORIENT_PORTRAIT, ORIENT_LANDSCAPE)

TARGET_PORTRAIT = (1080, 1920)      # 9:16
TARGET_LANDSCAPE = (1920, 1080)     # 16:9
# v3.137（用户 2026-10-24「录制的比例是多少，成片比例就是多少」）：
# 跟随源时**不再把 4:3 / 3:4 硬塞进 16:9 / 9:16 两档**，而是保持源的宽高比；
# 只在长边超过 1920 时等比缩小（与 1080p / 1080×1920 两档的长边一致，避免 4K 源出 4K 片）。
MAX_LONG_SIDE = 1920


@dataclass
class RenderParams:
    """渲染参数。刻意做成可 JSON 序列化（要塞进决策指纹）。"""

    orientation: str = ORIENT_AUTO   # auto / portrait / landscape（见 D30）
    width: int = 0                   # 目标尺寸覆盖；0 = 按 orientation 取默认档位
    height: int = 0
    fps: float = 0.0                     # 0 = 跟随源
    subtitle_font: str = "Noto Sans SC"
    subtitle_font_size: int = 72
    burn_subtitles: bool = True
    voice_volume: float = 1.0            # §5.2：人声 100%
    bgm_volume: float = 0.22             # §5.2：BGM 22%
    # 「钩子衔接淡化」（对齐参考实现高级剪辑「淡化」工具，0–1 秒）：给**每个保留段**
    # 的首尾加淡化，多段拼起来就是衔接过渡；单段时表现为成片首尾淡入淡出。
    # 0 = 不淡化（默认，保持既有产物不变）。
    # v3.156（用户 2026-10-27）：「开头钩子和后面的正文没有转场，看完钩子很生硬地接正文」
    # ⇒ 第 0→1 段之间插一个**短叠化**（xfade/acrossfade，默认 0.4 秒）。
    # 只作用于「钩子 → 正文」这一刀：后面的正文内部是同一口气的连续内容，叠化反而晕。
    # 置 0 即完全关闭（滤镜串回到纯 concat，既有产物字节不变）。
    # v3.157（用户 2026-10-28）：「做三个可选的转场效果，默认叠化，可选闪黑和闪白」
    #   fade  = 叠化（两段重叠交叉淡化，成片短 boundary_transition_sec 秒）
    #   black = 闪黑（接缝两侧各淡出/淡入到黑，时长不变）
    #   white = 闪白（同上，颜色换成白）
    #   none  = 不转场（等价于 boundary_transition_sec = 0）
    boundary_transition: str = "fade"
    boundary_transition_sec: float = 0.4
    hook_fade_sec: float = 0.0
    hide_fillers: bool = False           # 字幕「隐藏独立语气词」（参考实现「字幕」工具）
    bgm_ducking: bool = True             # 人声优先自动闪避
    duck_threshold: float = 0.05
    duck_ratio: float = 8.0
    duck_attack: int = 20
    duck_release: int = 400
    nvenc_preset: str = "p4"
    nvenc_cq: int = 23
    x264_crf: int = 20
    x264_preset: str = "veryfast"
    audio_bitrate: str = "192k"
    audio_rate: int = 48000
    # v4.3（方案 §3.5）：响度归一 —— 抖音口径 I=-14 LUFS / TP=-1.5。
    # 直播素材响度忽大忽小（对着麦喊 vs 转头说话），归一后刷抖音不用手动调音量。
    # loudnorm 动态模式单遍完成；false = 关闭（音频链与旧版逐字节一致）。
    loudness_normalize: bool = True

    def fingerprint_material(self) -> dict:
        """进指纹的字段：**只含影响画面/声音的**（改字幕字号要重渲染，改文件名不用）。"""
        d = asdict(self)
        return d


@dataclass
class RenderResult:
    ok: bool = False
    output: str = ""
    encoder: str = ""
    fallback_used: bool = False
    duration_sec: float = 0.0
    expected_sec: float = 0.0
    width: int = 0
    height: int = 0
    orientation: str = ""       # 实际成片画幅（portrait / landscape）
    cropped: bool = False       # 是否做了裁剪（auto 跟随源时一般为 False）
    has_audio: bool = False
    vram_used_mib: int | None = None
    elapsed_sec: float = 0.0
    commands: list[list[str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 探测

def ffprobe_json(path: Path) -> dict:
    # 走 proc.run：从无控制台的进程起 ffprobe 会**弹出黑窗**，必须抑制（见 proc.py）
    r = proc.run(
        [str(ws.ffprobe), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{(r.stderr or '').strip()[:200]}")
    return json.loads(r.stdout or "{}")


def probe_media(path: Path) -> dict:
    """读回媒资信息：分辨率/时长/有无音轨/帧率。"""
    data = ffprobe_json(path)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = 0.0
    for src in ((data.get("format") or {}).get("duration"),
                (v or {}).get("duration")):
        try:
            dur = float(src)
            break
        except (TypeError, ValueError):
            continue
    fps = 0.0
    if v and v.get("avg_frame_rate") and "/" in v["avg_frame_rate"]:
        num, den = v["avg_frame_rate"].split("/")[:2]
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return {
        "path": str(path),
        "durationSec": round(dur, 3),
        "width": int((v or {}).get("width") or 0),
        "height": int((v or {}).get("height") or 0),
        "vcodec": (v or {}).get("codec_name", ""),
        "fps": round(fps, 3),
        "hasAudio": a is not None,
        "acodec": (a or {}).get("codec_name", ""),
        "sizeBytes": int((data.get("format") or {}).get("size") or 0),
    }


def is_portrait(info: dict) -> bool:
    """D18：`h > w` 即为竖屏（竖屏不裁剪，横屏才居中裁剪）。"""
    return info.get("height", 0) > info.get("width", 0)


def _even(n: float) -> int:
    """取偶数（编码器要求宽高为偶数；同时避免 0）。"""
    v = int(round(n))
    return max(2, v - (v % 2))


def resolve_output_size(info: dict, params: RenderParams) -> tuple[int, int, str | None]:
    """算出成片尺寸 + 是否需要裁剪（D30：**画幅跟随源**）。

    返回 ``(宽, 高, crop 表达式 or None)``。

    规则：
      - ``orientation=auto``（默认）→ **成片比例 = 录制比例**（v3.137 修正）：
        16:9 → 1920×1080、9:16 → 1080×1920、**4:3 → 1440×1080、3:4 → 1080×1440**，
        一律**不裁剪**；长边超过 1920 的源等比缩到长边 1920（4K 不直接出 4K）。
        旧行为是只分「竖屏/横屏」两档，把 4:3 当横屏塞进 16:9 并裁掉上下 ——
        用户 2026-10-24 报的就是这个（「被强制裁切成十六比九」）。
      - 显式指定画幅 → 按指定画幅出；**只有源与目标的画幅比值不同才居中裁剪**
        （横屏源强制竖屏 → 裁左右；竖屏源强制横屏 → 裁上下）。
      - 比值差 ≤1% 视为同一画幅，**直接缩放不裁剪**。
      - **不放大**：源比目标档位小就沿用源的尺寸（偶数化），免得白涨体积。
    """
    sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
    want = str(params.orientation or ORIENT_AUTO).strip().lower()
    explicit = want in (ORIENT_PORTRAIT, ORIENT_LANDSCAPE)

    if not explicit:
        # auto：**保持源的宽高比**。源未知时保守取竖屏（抖音主画幅）
        if sw <= 0 or sh <= 0:
            return TARGET_PORTRAIT[0], TARGET_PORTRAIT[1], None
        long_side = max(sw, sh)
        if long_side > MAX_LONG_SIDE:
            k = MAX_LONG_SIDE / float(long_side)
            return _even(sw * k), _even(sh * k), None
        return _even(sw), _even(sh), None

    tw, th = TARGET_PORTRAIT if want == ORIENT_PORTRAIT else TARGET_LANDSCAPE
    if params.width and params.height:      # 显式覆盖
        tw, th = int(params.width), int(params.height)

    crop: str | None = None
    if sw > 0 and sh > 0:
        if sw < tw and sh < th:             # 源比目标小：不放大，沿用源尺寸
            tw, th = sw - (sw % 2), sh - (sh % 2)
        else:
            src_a, tgt_a = sw / sh, tw / th
            if abs(src_a - tgt_a) / max(tgt_a, 1e-6) > 0.01:
                # 源比目标"宽" → 裁左右；源比目标"高" → 裁上下
                crop = (f"crop=ih*{tgt_a:.6f}:ih" if src_a > tgt_a
                        else f"crop=iw:iw/{tgt_a:.6f}")
    return tw, th, crop


# 显存查询缓存：这个函数在 **HTTP 请求路径**上（/api/render/status 每次轮询都会问），
# 而 nvidia-smi 一次要几十毫秒、还要起一个进程。给个短 TTL 就够用：
# 渲染守卫那边是每 5 s 问一次（> TTL，拿到的总是新鲜值）。
_VRAM_TTL_SEC = 3.0
_vram_cache: dict = {"t": 0.0, "v": None}


def gpu_memory_used_mib(*, force: bool = False) -> int | None:
    """当前显存占用（MiB）。查不到返回 ``None``（不假装是 0）。

    ``force=True`` 绕过缓存（调试用）。
    """
    now = time.time()
    if not force and (now - _vram_cache["t"]) < _VRAM_TTL_SEC:
        return _vram_cache["v"]

    exe = Path(r"C:\Windows\System32\nvidia-smi.exe")
    if not exe.is_file():
        exe = Path("nvidia-smi")
    value: int | None = None
    try:
        # 必须走 proc.run：否则每轮询一次就闪一个黑窗（见 proc.py 的说明）
        r = proc.run(
            [str(exe), "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        if r.returncode == 0 and (r.stdout or "").strip():
            value = int(float(r.stdout.strip().splitlines()[0]))
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None
    _vram_cache["t"] = now
    _vram_cache["v"] = value
    return value


# ---------------------------------------------------------------- 滤镜串

def _escape_filter_path(p: Path) -> str:
    """ffmpeg 滤镜里的路径转义：反斜杠→斜杠，盘符冒号要转义，单引号也要转义。

    （这块最容易踩：写错的表现是 `subtitles=...` 报 `Unable to open` ，
    但路径肉眼看着完全正确。）
    """
    s = str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return s


def per_range_fade(params: "RenderParams", range_ms: list[tuple[float, float]]) -> list[float]:
    """每个保留段各自的淡化秒数（对应渲染参数 ``hook_fade_sec``）。

    ⚠️ **必须逐段各算一次**：段很薄（短于 2×淡化时长）时若还照抄设定值，
    ``fade=t=out`` 的起点会落到负区间，ffmpeg 直接报 "Value out of range"
    并把整个任务挂掉（K：默认 0 秒时完全不插滤镜，既有产物字节不变）。
    """
    want = float(getattr(params, "hook_fade_sec", 0.0) or 0.0)
    out: list[float] = []
    for rs, re_ in range_ms:
        dur = (re_ - rs) / 1000.0
        if want <= 0 or dur <= 0:
            out.append(0.0)
            continue
        out.append(min(want, max(dur / 2.0 - 0.02, 0.0)))
    return out


TRANSITION_MODES: tuple[str, ...] = ("fade", "black", "white")


def transition_mode(params: "RenderParams") -> str:
    """转场类型归一：不认识的值一律按 fade（默认就是叠化）；空串/none 表示不转场。"""
    raw = str(getattr(params, "boundary_transition", "fade") or "").strip().lower()
    if raw in ("", "none", "off", "no"):
        return "none"
    return raw if raw in TRANSITION_MODES else "fade"


def boundary_transition(params: "RenderParams", range_ms: list[tuple[float, float]]) -> float:
    """钩子→正文那一刀的叠化秒数（0 = 不做）。

    三条夹紧规则（都来自实测会炸/会难看的情形）：
      · 少于 2 段 ⇒ 没有"接缝"，返回 0（单段时表现为整片首尾，不在这里做）；
      · 任一段短于 2×叠化 ⇒ 叠化会把半个段吃掉，按 ``min(d0,d1)/2 - 0.02`` 收窄；
      · 再封顶 0.8 秒（再长就不像转场、像卡住了），小于 0.08 秒干脆不做（看着像闪一下）。
    """
    want = float(getattr(params, "boundary_transition_sec", 0.0) or 0.0)
    if want <= 0 or len(range_ms) < 2 or transition_mode(params) == "none":
        return 0.0
    d0 = (range_ms[0][1] - range_ms[0][0]) / 1000.0
    d1 = (range_ms[1][1] - range_ms[1][0]) / 1000.0
    cap = min(want, 0.8, max(min(d0, d1) / 2.0 - 0.02, 0.0))
    return round(cap, 3) if cap >= 0.08 else 0.0


def build_filter_complex(n_ranges: int, range_ms: list[tuple[float, float]], info: dict,
                         params: RenderParams, ass_path: Path | None,
                         bgm_input_index: int | None, out_duration: float,
                         hook_zoom: bool = False) -> str:
    """拼 §6.6 的 filter_complex。顺序严格照文档：淡化→裁剪→竖屏→字幕→音频。

    ``range_ms`` 是各保留区间的毫秒时长 —— 无音轨时要**按各区间分别补静音**，
    补成总时长会让 concat 后的音轨比画面长，成片时长就错了。
    """
    parts: list[str] = []

    # 1) 各区间各自 resetpts（已经用 -ss/-t 在输入端裁好了）
    fades = per_range_fade(params, range_ms)
    mode = transition_mode(params)
    xf = boundary_transition(params, range_ms)
    # v3.157：闪黑/闪白不做重叠 —— 在接缝两侧各淡出/淡入 xf/2 秒到指定颜色（时长不变）
    dip_color = {"black": "black", "white": "white"}.get(mode, "")
    dip = (xf / 2.0) if (dip_color and xf > 0) else 0.0
    for i, (rs, re_) in enumerate(range_ms):
        seg_sec = max((re_ - rs) / 1000.0, 0.1)
        f = fades[i]
        vfilter = "setpts=PTS-STARTPTS"
        afilter = "asetpts=PTS-STARTPTS"
        if f > 0:
            vfilter += (f",fade=t=in:st=0:d={f:.3f},"
                        f"fade=t=out:st={max(seg_sec - f, 0.0):.3f}:d={f:.3f}")
            afilter += (f",afade=t=in:st=0:d={f:.3f},"
                        f"afade=t=out:st={max(seg_sec - f, 0.0):.3f}:d={f:.3f}")
        if dip > 0 and i == 0:
            vfilter += (f",fade=t=out:st={max(seg_sec - dip, 0.0):.3f}:d={dip:.3f}"
                        f":color={dip_color}")
            afilter += f",afade=t=out:st={max(seg_sec - dip, 0.0):.3f}:d={dip:.3f}"
        elif dip > 0 and i == 1:
            vfilter += f",fade=t=in:st=0:d={dip:.3f}:color={dip_color}"
            afilter += f",afade=t=in:st=0:d={dip:.3f}"
        if hook_zoom and i == 0:
            # v4.2（用户「没有互联网的那种镜头感」）：钩子段缓推 1.0→1.10 ——
            # 抖音切片的标志性处理：开头镜头慢慢压近主体，注意力被"吸"进去。
            # zoompan 必须显式 s=源尺寸（表达式里拿不到 iw）、d=1（一进一出）、
            # fps=源帧率（否则输出变 25fps 变速）。`on` 是输出帧号，0 起步。
            zw, zh = int(info.get("width") or 0), int(info.get("height") or 0)
            fps = float(info.get("fps") or 0)
            if zw > 0 and zh > 0 and fps > 1:
                frames = max(int(round(seg_sec * fps)), 2)
                vfilter += (f",zoompan=z='1+0.10*on/{frames}'"
                            f":d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                            f":s={zw}x{zh}:fps={fps:.4f}")
        if hook_zoom:
            # zoompan 把第一段的 timebase 变成 1/fps，而直通段的 tb 是容器原生
            # （H.264 常见 1/15360）—— xfade/concat 直接报 "timebase do not match"
            # （smoke_render 真机抓的）。zoom 启用时**所有视频段**统一 settb=AVTB；
            # 不启用时链路与旧版逐字节一致（零回归面）。
            vfilter += ",settb=AVTB"
        parts.append(f"[{i}:v]{vfilter}[v{i}]")
        if info["hasAudio"]:
            parts.append(f"[{i}:a]{afilter}[a{i}]")
        else:
            # 源没有音轨 → 按**该区间自己的时长**补静音，保证 concat 的 a=1 成立
            parts.append(f"anullsrc=r={params.audio_rate}:cl=stereo,"
                         f"atrim=0:{seg_sec:.3f},{afilter}[a{i}]")

    n = n_ranges
    if xf > 0 and mode == "fade":
        # v3.156：钩子（第 0 段）→ 正文（第 1 段）用**叠化**接；两段在时间轴上重叠 xf 秒。
        dur0 = (range_ms[0][1] - range_ms[0][0]) / 1000.0
        off = max(dur0 - xf, 0.0)
        # v4.13：xfade 要求两路**帧率严格一致**。钩子缓推的 zoompan 输出 fps=源帧率
        # （如 59999/1000），直通段保留容器帧率（60/1）⇒
        # "First input link main frame rate (59999/1000) do not match the corresponding
        #  second input link xfade frame rate (60/1)"，nvenc → libx264 两个编码器
        # 都死在滤镜初始化（rc=4294967274；旧链必炸、新链通过）。
        # xfade 前两路统一 fps（用源帧率；fps 滤镜只复制/丢帧，**不改时长/速度**）。
        _fpsx = float(info.get("fps") or 0)
        _fpsx = f"fps=fps={_fpsx:.4f}" if _fpsx > 1 else "fps=fps=30"
        parts.append(f"[v0]{_fpsx}[v0x]")
        parts.append(f"[v1]{_fpsx}[v1x]")
        parts.append(f"[v0x][v1x]xfade=transition=fade:duration={xf:.3f}:offset={off:.3f}[vx]")
        parts.append(f"[a0][a1]acrossfade=d={xf:.3f}:c1=tri:c2=tri[ax]")
        if n > 2:
            ins = "[vx][ax]" + "".join(f"[v{i}][a{i}]" for i in range(2, n))
            parts.append(f"{ins}concat=n={n - 1}:v=1:a=1[cv][ca]")
        else:
            # 只有两段：叠化结果直接当合并流（标号统一成 [cv]/[ca]，下游不用改）
            parts.append("[vx]null[cv]")
            parts.append("[ax]anull[ca]")
    else:
        ins = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{ins}concat=n={n}:v=1:a=1[cv][ca]")

    # 2) 画幅处理（D30：跟随源画幅，只有显式指定另一种画幅时才裁剪）
    tw, th, crop = resolve_output_size(info, params)
    vf = f"{crop}," if crop else ""
    parts.append(f"[cv]{vf}scale={tw}:{th},setsar=1[sv]")

    # 3) 字幕（§6.6 第 4 步）
    cur = "sv"
    if ass_path is not None:
        esc = _escape_filter_path(ass_path)
        parts.append(f"[{cur}]subtitles=filename='{esc}'[suv]")
        cur = "suv"

    # 4) 音频：BGM 22% + 人声优先自动闪避（§6.6 第 5 步）
    if bgm_input_index is not None:
        parts.append(f"[{bgm_input_index}:a]atrim=0:{max(out_duration, 0.1):.3f},"
                     f"asetpts=PTS-STARTPTS,volume={params.bgm_volume}[bgm]")
        if params.bgm_ducking:
            parts.append("[ca]asplit=2[voice][sc]")
            parts.append(f"[bgm][sc]sidechaincompress=threshold={params.duck_threshold}:"
                         f"ratio={params.duck_ratio}:attack={params.duck_attack}:"
                         f"release={params.duck_release}[bgmduck]")
            parts.append("[voice][bgmduck]amix=inputs=2:duration=first:normalize=0[apre]")
        else:
            parts.append(f"[ca]volume={params.voice_volume}[voice]")
            parts.append("[voice][bgm]amix=inputs=2:duration=first:normalize=0[apre]")
    else:
        # 没有 BGM：人声原样（音量 100%）
        parts.append(f"[ca]volume={params.voice_volume},"
                     f"aresample={params.audio_rate}[apre]")

    # v4.3（方案 §3.5）：响度归一 —— 抖音口径 I=-14 LUFS / TP=-1.5 / LRA=11。
    # 直播素材响度忽大忽小，归一后观众刷到不用手动调音量。loudnorm 动态模式单遍完成；
    # 关闭时 anull 直通（音频链与旧版逐字节一致）。
    if params.loudness_normalize:
        # ⚠️ loudnorm 内部恒定上采样到 192kHz —— 之后必须 aresample 拉回目标采样率，
        # 否则成片音轨是 192kHz（体积翻倍、部分播放器不认）。
        parts.append(f"[apre]loudnorm=I=-14:TP=-1.5:LRA=11,"
                     f"aresample={params.audio_rate}[aout]")
    else:
        parts.append("[apre]anull[aout]")

    parts.append(f"[{cur}]format=yuv420p[vout]")
    return ";".join(parts)


def encoder_args(encoder: str, params: RenderParams) -> list[str]:
    if encoder == "h264_nvenc":
        # -rc vbr + -cq 是 NVENC 的恒定质量模式；-b:v 0 表示不限码率
        return ["-c:v", "h264_nvenc", "-preset", params.nvenc_preset,
                "-rc", "vbr", "-cq", str(params.nvenc_cq), "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", params.x264_preset, "-crf", str(params.x264_crf)]


# ---------------------------------------------------------------- 渲染

def _run_encoder(cmd: list[str], *, expected_sec: float, timeout: float,
                 on_progress: Callable[[float], None] | None,
                 should_cancel: Callable[[], bool] | None) -> tuple[int, str, bool]:
    """跑一次编码，边跑边解析 ffmpeg 的 ``-progress`` 输出。

    为什么不用 ``subprocess.run``：那样**既报不出进度，也没法中途取消**——
    渲染一条几十秒的片子要好几秒到几十秒，界面看起来像卡死。
    这里用 Popen + 逐行读 ``out_time_ms=``（单位**微秒**，见下方注释）。

    返回 ``(returncode, stderr 文本, 是否被取消)``。
    """
    err_buf: list[str] = []
    cancelled = [False]

    # ffmpeg 的 -progress pipe:1 写 stdout；stderr 仍走 warning/error。
    # 两个管道都要有人读，否则任一方写满都会把进程堵死（K27 同类问题）。
    # 走 proc.popen：否则渲染全程会挂着一个黑窗（见 proc.py）。
    proc_handle = proc.popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        env=ws.subprocess_env(),
    )

    def _drain_stderr() -> None:
        try:
            assert proc_handle.stderr is not None
            err_buf.append(proc_handle.stderr.read())
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain_stderr, daemon=True)
    reader.start()
    try:
        assert proc_handle.stdout is not None
        for line in proc_handle.stdout:
            line = line.strip()
            # 注意：键名虽带 _ms，实测给出的是**微秒**（ffmpeg 的历史遗留），
            # 所以除以 1e6 才是秒。两种键名都兼容一下。
            if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
                try:
                    us = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                if on_progress and expected_sec > 0:
                    # 上限卡到 99：100 留给「渲染成功且复检通过」的时刻
                    on_progress(min(us / 1e6 / expected_sec * 100.0, 99.0))
            if should_cancel is not None and should_cancel():
                cancelled[0] = True
                break
        if cancelled[0] and proc_handle.poll() is None:
            proc_handle.terminate()
        try:
            proc_handle.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc_handle.kill()
            proc_handle.wait(timeout=10)
            raise
    finally:
        reader.join(timeout=5)
        try:
            if proc_handle.stdout is not None:
                proc_handle.stdout.close()
        except OSError:
            pass
    return proc_handle.returncode or 0, (err_buf[0] if err_buf else ""), cancelled[0]


def render(source: Path, keep_ranges: list[dict], output: Path, *,
           style: SubtitleStyle | None = None, ass_path: Path | None = None,
           bgm: Path | None = None, params: RenderParams | None = None,
           encoders: tuple[str, ...] = ("h264_nvenc", "libx264"),
           timeout: float = 1800.0,
           on_progress: Callable[[float], None] | None = None,
           should_cancel: Callable[[], bool] | None = None,
           hook_zoom: bool = False) -> RenderResult:
    """按 §6.6 渲染。返回 :class:`RenderResult`（失败也**不抛**，把原因写进 ``errors``）。

    ``on_progress`` 收 0–99 的百分比；``should_cancel`` 返回 True 时立即终止 ffmpeg。
    """
    params = params or RenderParams()
    res = RenderResult()
    started = time.time()
    source, output = Path(source), Path(output)
    rngs = [r for r in (keep_ranges or []) if r["endMs"] > r["startMs"]]
    if not rngs:
        res.errors.append("keepRanges 为空，没有可渲染的内容")
        return res
    # v3.156：叠化会让两段重叠 xf 秒 ⇒ 成片比"区间总和"短，进度条/时长都按扣完的算
    _rng_ms = [(r["startMs"], r["endMs"]) for r in rngs]
    _xf = boundary_transition(params, _rng_ms)
    # 只有「叠化」是两段重叠（时长变短）；闪黑/闪白是在接缝上淡到纯色，时长不变
    _cut = _xf if (transition_mode(params) == "fade") else 0.0
    res.expected_sec = round(total_duration_ms(rngs) / 1000.0 - _cut, 3)

    try:
        info = probe_media(source)
    except (RuntimeError, ValueError, OSError) as exc:
        res.errors.append(f"素材探测失败：{exc}")
        return res

    output.parent.mkdir(parents=True, exist_ok=True)
    # 每个保留区间一个输入：-ss 定位 + -t 限长（跳读，不解码整段）
    inputs: list[str] = []
    for r in rngs:
        inputs += ["-ss", f"{r['startMs'] / 1000.0:.3f}",
                   "-t", f"{(r['endMs'] - r['startMs']) / 1000.0:.3f}",
                   "-i", str(source)]
    n_ranges = len(rngs)

    # 画幅（D30）：先算出来，回写给调用方 —— 字幕的 ASS 基准分辨率必须与它一致，
    # 否则 libass 会把字幕按 PlayRes 渲完再拉伸到画面，横竖比例一变字就变形。
    out_w, out_h, _crop = resolve_output_size(info, params)
    # v3.137：auto 时不再谎报成「竖屏/横屏」——4:3 成片的真画幅是 4:3，
    # 报「横屏 16:9」会让界面上的「画幅」与「成片 1440×1080」自相矛盾。
    _want = str(params.orientation or ORIENT_AUTO).strip().lower()
    res.orientation = _want if _want in (ORIENT_PORTRAIT, ORIENT_LANDSCAPE) else ORIENT_AUTO
    res.cropped = _crop is not None

    bgm_index = None
    if bgm is not None and Path(bgm).is_file():
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]
        bgm_index = n_ranges

    fc = build_filter_complex(
        n_ranges,
        [(r["startMs"], r["endMs"]) for r in rngs],
        info, params, ass_path, bgm_index, res.expected_sec,
        hook_zoom=hook_zoom)

    base = [str(ws.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            *inputs, "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
            "-c:a", "aac", "-b:a", params.audio_bitrate, "-ar", str(params.audio_rate)]
    if params.fps:
        base += ["-r", str(params.fps)]
    base += ["-movflags", "+faststart"]

    for encoder in encoders:
        if should_cancel is not None and should_cancel():
            res.errors.append("渲染前收到取消请求，未启动编码")
            output.unlink(missing_ok=True)
            break
        cmd = base + ["-progress", "pipe:1", "-nostats"] + \
            encoder_args(encoder, params) + [str(output)]
        res.commands.append(cmd)
        try:
            rc, err_text, cancelled = _run_encoder(
                cmd, expected_sec=res.expected_sec, timeout=timeout,
                on_progress=on_progress, should_cancel=should_cancel)
        except subprocess.TimeoutExpired:
            res.errors.append(f"{encoder} 超时（>{timeout:.0f}s）")
            output.unlink(missing_ok=True)
            continue
        if cancelled:
            res.errors.append("已取消")
            output.unlink(missing_ok=True)
            break
        if rc == 0 and output.is_file() and output.stat().st_size > 0:
            res.ok = True
            res.encoder = encoder
            res.fallback_used = encoder != encoders[0]
            break
        res.errors.append(f"{encoder} 失败 rc={rc}：{err_text.strip()[:300]}")
        output.unlink(missing_ok=True)

    res.elapsed_sec = round(time.time() - started, 2)
    if res.ok:
        try:
            got = probe_media(output)
            res.duration_sec = got["durationSec"]
            res.width, res.height = got["width"], got["height"]
            res.has_audio = got["hasAudio"]
        except (RuntimeError, ValueError, OSError) as exc:
            res.errors.append(f"产物复检失败：{exc}")
        res.output = str(output)
    return res
