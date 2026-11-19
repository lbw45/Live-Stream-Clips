#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕生成：转写句 → ASS 字幕（§6.6 第 4 步）。

为什么要单独一个模块：字幕是**烧进画面**的（抖音风格硬字幕），一旦位置/字号/断行不对，
成片就废了；而且断句与行长是纯计算，值得单独测。

**字体：一个文档与现实的落差（实测 2026-09-22）**
§6.6 写「字体思源黑体」。但本机实测：`思源黑体` / `Source Han Sans SC` 这两个字面名字
**libass 解析不到**，会**静默回落**到 `sans-serif`（表现是字形变了但不报错）；
而 `Noto Sans SC` —— **思源黑体的同源字体**（Source Han Sans 与 Noto Sans CJK 是同一套设计，
Noto Sans SC 是其中文子集）—— 可以正常渲染中文。

所以本模块做**字体名归一**：把文档里的 `思源黑体` / `Source Han Sans SC` 映射到
实际可用的 `Noto Sans SC`，并保留 `Microsoft YaHei` 作备选（系统必有）。
判定「名字到底能不能用」的办法见 `tools\\smoke_render.py` 的字体断言（渲染一帧比字节数）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 文档里的名字 → 本机真正能解析的名字（见模块 docstring）
FONT_ALIASES = {
    "思源黑体": "Noto Sans SC",
    "Source Han Sans SC": "Noto Sans SC",
    "SourceHanSans": "Noto Sans SC",
    "source han sans": "Noto Sans SC",
}
# 优先级链：第一个通常是思源黑体同源，后面是系统保底
FONT_CHAIN = ("Noto Sans SC", "Microsoft YaHei", "DengXian", "SimHei")

_WS_RE = re.compile(r"\s+")
# 断行优先在标点后断：句末 > 句内
_HARD_BREAK = "。！？!?…；;\n"
_SOFT_BREAK = "，,、：:）)】」』"

# 「隐藏独立语气词」用到的语气字集合（高级剪辑页「字幕」工具）
# 只列**纯语气、不带语义**的字；删的是整句只有这些字构成的 cue。
_FILLER_CHARS = set("嗯啊呃哦噢哎唉额呀啦呐呗咯啧诶呣")


def normalize_font(name: str | None) -> str:
    """把字体名归一成**本机真能解析**的名字。空值取链首。"""
    raw = (name or "").strip()
    if not raw:
        return FONT_CHAIN[0]
    if raw in FONT_ALIASES:
        return FONT_ALIASES[raw]
    low = raw.lower()
    for alias, real in FONT_ALIASES.items():
        if alias.lower() == low:
            return real
    return raw


# 样式常量是按 **1080×1920（9:16）** 调出来的。成片换画幅（例如 1920×1080 横屏）
# 时字号/边距必须等比缩放，否则同样 72 px 的字在 1080 高的画面上会占掉 1/15 屏。
BASE_W, BASE_H = 1080, 1920


@dataclass
class SubtitleStyle:
    """抖音风格硬字幕的默认样式（白字 + 黑描边 + 阴影，画面偏下居中）。

    所有长度类字段都以 **1080×1920 竖屏**为基准；实际渲染前请用
    :func:`style_for` 按成片尺寸缩放一遍。
    """

    font: str = FONT_CHAIN[0]
    font_size: int = 72
    primary_colour: str = "&H00FFFFFF"     # 白（ASS 是 &HAABBGGRR）
    outline_colour: str = "&H00000000"     # 黑描边
    back_colour: str = "&H80000000"
    bold: int = -1                         # -1 = 粗体
    outline: int = 3
    shadow: int = 1
    alignment: int = 2                     # 2 = 底部居中（ASS align）
    margin_l: int = 60
    margin_r: int = 60
    margin_v: int = 380                    # 距底 380px（1080×1920 下约在下方 1/5 处）
    max_chars_per_line: int = 15
    max_lines: int = 2
    max_cue_sec: float = 8.0               # 单条字幕最长显示时长，超了按比例拆
    # v3.41：手动定位（剪辑页「字幕调整」的大小/XY 滑杆）。None = 不干预（走边距默认）。
    # pos_x：锚点在画面宽度上的百分比（0–100，50=居中）；pos_y：距底部百分比（0–100）。
    # 两者任一给出时，每条 Dialogue 前缀 {\an2\pos(x,y)} —— an2 锚点 = 字幕块底边中点。
    pos_x: float | None = None
    pos_y: float | None = None

    def scaled(self, width: int, height: int) -> "SubtitleStyle":
        """按成片尺寸等比缩放（返回**新对象**，不改自己）。

        纵向（字号 / 上下边距 / 描边）按**高度**比值缩，横向（左右边距 / 每行字数）
        按**宽度**比值缩 —— 这样竖屏横屏看起来的"占比"一致。
        """
        kh = (height / BASE_H) if height else 1.0
        kw = (width / BASE_W) if width else 1.0
        return SubtitleStyle(
            font=self.font,
            font_size=max(12, round(self.font_size * kh)),
            primary_colour=self.primary_colour,
            outline_colour=self.outline_colour,
            back_colour=self.back_colour,
            bold=self.bold,
            outline=max(1, round(self.outline * kh)),
            shadow=self.shadow,
            alignment=self.alignment,
            margin_l=max(8, round(self.margin_l * kw)),
            margin_r=max(8, round(self.margin_r * kw)),
            margin_v=max(8, round(self.margin_v * kh)),
            # 画面变宽 → 每行能多放几个字，但别太宽（上限 24 字）
            max_chars_per_line=max(10, min(24, round(self.max_chars_per_line * kw))),
            max_lines=self.max_lines,
            max_cue_sec=self.max_cue_sec,
            pos_x=self.pos_x,
            pos_y=self.pos_y,
        )

    def ass_style_line(self, name: str = "Default") -> str:
        return (
            f"Style: {name},{self.font},{self.font_size},{self.primary_colour},"
            f"&H000000FF,{self.outline_colour},{self.back_colour},"
            f"{self.bold},0,0,0,100,100,0,0,1,{self.outline},{self.shadow},"
            f"{self.alignment},{self.margin_l},{self.margin_r},{self.margin_v},1"
        )


def ts(ms: float) -> str:
    """毫秒 → ASS 时间戳 ``H:MM:SS.CC``（注意是**厘秒**，不是毫秒）。"""
    ms = max(0.0, float(ms))
    total_cs = int(round(ms / 10.0))
    cs = total_cs % 100
    s = (total_cs // 100) % 60
    m = (total_cs // 6000) % 60
    h = total_cs // 360000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def wrap_text(text: str, max_chars: int = 15, max_lines: int = 2) -> str:
    """按标点优先断行，返回带 ``\\N`` 的 ASS 文本。

    规则：先按句末标点断，再按句内标点断，最后硬切；每行 ≤ ``max_chars``；
    超过 ``max_lines`` 行的溢出内容**并到最后一行**（宁可行长超一点，也不丢字）。
    """
    clean = _WS_RE.sub("", text or "").strip()
    if not clean:
        return ""
    if len(clean) <= max_chars:
        return clean

    lines: list[str] = []
    rest = clean
    while rest and len(lines) < max_lines:
        if len(rest) <= max_chars:
            lines.append(rest)
            rest = ""
            break
        window = rest[:max_chars + 1]
        cut = 0
        for i in range(len(window) - 1, 0, -1):
            if window[i - 1] in _HARD_BREAK:
                cut = i
                break
        if not cut:
            for i in range(len(window) - 1, 0, -1):
                if window[i - 1] in _SOFT_BREAK:
                    cut = i
                    break
        if not cut:
            cut = max_chars
        lines.append(rest[:cut])
        rest = rest[cut:]
    if rest:                      # 溢出部分并进最后一行，不丢字
        lines[-1] = lines[-1] + rest
    return "\\N".join(lines)


def split_long_cue(text: str, start_ms: float, end_ms: float,
                   style: SubtitleStyle) -> list[dict]:
    """把「一条太长的字幕」按时间**均分**成多条（按字数占比分配时长）。

    抖音硬字幕一次只显示 1–2 行；一句 20 秒的长句必须拆开，否则字幕挂太久、
    而且换行会挤成一大坨。
    """
    clean = _WS_RE.sub("", text or "").strip()
    span = end_ms - start_ms
    if not clean or span <= 0:
        return []
    max_chars = style.max_chars_per_line * style.max_lines
    if span <= style.max_cue_sec * 1000 and len(clean) <= max_chars:
        return [{"startMs": start_ms, "endMs": end_ms, "text": clean}]

    # 先按标点切成「意群」，再把意群按时间比例装进每条 cue
    pieces = [p for p in re.split(r"(?<=[。！？!?…；;，,、])", clean) if p]
    if not pieces:
        pieces = [clean]
    total_chars = sum(len(p) for p in pieces)
    cues: list[dict] = []
    buf, buf_start = "", start_ms
    cursor = start_ms
    for p in pieces:
        share = span * (len(p) / max(total_chars, 1))
        piece_end = cursor + share
        if buf and (len(buf) + len(p) > max_chars
                    or piece_end - buf_start > style.max_cue_sec * 1000):
            cues.append({"startMs": buf_start, "endMs": cursor, "text": buf})
            buf, buf_start = p, cursor
        else:
            buf += p
        cursor = piece_end
    if buf:
        cues.append({"startMs": buf_start, "endMs": max(cursor, buf_start + 300), "text": buf})
    return cues


def is_filler_text(text: str) -> bool:
    """「隐藏独立语气词」判定：整句去掉标点/空白后**全是**语气词才算。

    只对纯语气词句生效（"嗯""啊""呃""嗯嗯啊"），带信息的句子即便开头有语气词也
    必须保留 —— 参考实现的描述就是"隐藏**独立**语气词"，不是把语气词所在的句子删掉。
    """
    t = re.sub(r"[\s，。！？、,.!?~…—\-·\"'“”‘’]", "", str(text or ""))
    return bool(t) and set(t) <= _FILLER_CHARS


# ---------------------------------------------------------------- 字幕挪动（v3.37）
# 剪辑页「字幕调整」工具：解锁轨道后可以把某一条字幕左右拖动（人工对齐语速/口型）。
# **这里是唯一实现** —— 前端显示用的偏移与渲染取字幕用的偏移必须照同一套规则算，
# 否则会出现「界面上挪过去了、成片里没挪」（K44/K53 同一类缺陷）。
#
# 身份 = **该条字幕的原始起点毫秒**（不是数组下标）：
# 前端展示的 cue 列表是「按候选区间过滤过」的，而渲染层拿到的是整份转写的段列表，
# 两边下标并不一致 —— 用下标做身份会静默错位到别的句子上。
CUE_SHIFT_MAX_MS = 30000.0


def cue_shift_key(start_ms) -> str:
    """一条字幕的身份键：原始起点毫秒（整数，字符串形式）。"""
    try:
        return str(int(round(float(start_ms))))
    except (TypeError, ValueError):
        return ""


def normalize_cue_shifts(raw) -> dict:
    """把任意来源的偏移收敛成 ``{"<原始起点ms>": <偏移ms>}``。

    接受两种形态（草稿里存 dict；前端也可传 ``[{"startMs":…, "deltaMs":…}]``）：
    - ``{"12345": -300.0, ...}``
    - ``[{"startMs": 12345, "deltaMs": -300}, ...]``

    规则：偏移夹在 ±30s（防手抖/脏数据）、非数字丢弃、**0 偏移不存**（等于没挪）。
    """
    items: list[tuple] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        for it in raw:
            if isinstance(it, dict):
                items.append((it.get("startMs"), it.get("deltaMs")))
    out: dict = {}
    for key, delta in items:
        k = cue_shift_key(key)
        if not k:
            continue
        try:
            d = float(delta)
        except (TypeError, ValueError):
            continue
        if d == 0:
            continue
        out[k] = round(max(-CUE_SHIFT_MAX_MS, min(CUE_SHIFT_MAX_MS, d)), 1)
    return out


def shift_cues(cues: list[dict], shifts, *, lo_ms=None, hi_ms=None) -> list[dict]:
    """按偏移返回**新的一份** cue 列表（不改原对象）。

    - 命中规则：cue 的**原始**起点毫秒命中 ``shifts`` 的键，整条前后平移；
    - 平移后整体夹在 ``[lo_ms, hi_ms]``（候选区间）内 —— 越界的部分推回来，
      而不是让字幕跑到片段外面（跑出去的字幕在成片里会消失，用户会以为"删掉了"）；
    - 输出带 ``shiftedFrom``（原始起点），前端据此显示"已挪动"。
    """
    norm = normalize_cue_shifts(shifts)
    out: list[dict] = []
    for c in (cues or []):
        try:
            s = float(c.get("startMs") or 0.0)
            e = float(c.get("endMs") or 0.0)
        except (TypeError, ValueError):
            continue
        key = cue_shift_key(s)
        delta = float(norm.get(key, 0.0))
        s2, e2 = s + delta, e + delta
        if lo_ms is not None:
            lo = float(lo_ms)
            if s2 < lo:
                e2 += lo - s2
                s2 = lo
        if hi_ms is not None:
            hi = float(hi_ms)
            if e2 > hi:
                s2 -= e2 - hi
                e2 = hi
            s2 = max(s2, float(lo_ms) if lo_ms is not None else s2)
        if e2 <= s2:                      # 退化（被夹没了）：保持最短 120ms，不漏字也空转
            e2 = s2 + 120.0
        item = dict(c)
        item["startMs"] = round(s2, 1)
        item["endMs"] = round(e2, 1)
        if delta:
            item["shiftedFrom"] = round(s, 1)
        out.append(item)
    return out


def shift_segments(segments: list[dict], shifts, *, lo_ms=None, hi_ms=None) -> list[dict]:
    """**渲染入口**：把偏移作用到「秒制」的转写段上（``[{"start","end","text"}]``）。"""
    norm = normalize_cue_shifts(shifts)
    if not norm:
        return list(segments or [])
    cues = []
    for s in (segments or []):
        try:
            start_ms = float(s.get("start") or 0.0) * 1000.0
            end_ms = float(s.get("end") or 0.0) * 1000.0
        except (TypeError, ValueError):
            continue
        cues.append({"startMs": start_ms, "endMs": end_ms,
                     "text": str(s.get("text") or "")})
    shifted = shift_cues(cues, norm, lo_ms=lo_ms, hi_ms=hi_ms)
    return [{"start": round(c["startMs"] / 1000.0, 3),
             "end": round(c["endMs"] / 1000.0, 3),
             "text": c["text"]} for c in shifted]


def cues_from_segments(segments: list[dict], keep_ranges: list[dict],
                       style: SubtitleStyle | None = None,
                       hide_fillers: bool = False) -> list[dict]:
    """§6.5 的 ``subtitleCues``：**把转写句按 ``keepRanges`` 重映射到成片时间轴**。

    这是「钩子前置」的关键 —— 一旦把 hook 区间复制到开头，后面所有内容都整体后移，
    字幕时间必须跟着重算，否则字幕与语音全部错位。

    ``hide_fillers`` = 高级剪辑页「字幕」工具的「隐藏独立语气词」开关。

    输出时间轴 = 依次拼接 ``keep_ranges`` 的时长；每个 cue 的 ``startMs/endMs``
    是**成片内**的相对时间。
    """
    style = style or SubtitleStyle()
    cues: list[dict] = []
    offset = 0.0
    for rng in keep_ranges:
        rs, re_ = float(rng["startMs"]), float(rng["endMs"])
        if re_ <= rs:
            continue
        for s in segments:
            s_start, s_end = s["start"] * 1000.0, s["end"] * 1000.0
            if s_end <= rs or s_start >= re_:          # 不相交
                continue
            if hide_fillers and is_filler_text(s.get("text") or ""):
                continue
            # 与保留区间求交（切头切尾），再平移到成片时间轴
            a = max(s_start, rs) - rs + offset
            b = min(s_end, re_) - rs + offset
            if b - a < 120:                            # 短于 120ms 的碎片丢弃
                continue
            for c in split_long_cue(s["text"], a, b, style):
                cues.append(c)
        offset += re_ - rs
    cues.sort(key=lambda c: c["startMs"])
    return cues


def build_ass(cues: list[dict], style: SubtitleStyle | None = None,
              width: int = BASE_W, height: int = BASE_H,
              keywords: list[str] | None = None) -> str:
    """生成完整 ASS 文件内容。

    **``width``/``height`` 必须传成片的真实分辨率**：ASS 的 ``PlayResX/Y`` 决定
    libass 的渲染基准，若与画面尺寸不一致，字幕会被**非等比拉伸**（16:9 画面配
    9:16 的 PlayRes → 字被压扁拉宽）。所以这里会自动按尺寸缩放样式
    （见 :meth:`SubtitleStyle.scaled`），调用方不必自己算。

    ``keywords``（v4.3，可选）：规划器给的关键词 —— 普通字幕里命中的词用**亮黄 + 放大**
    强调（大花字同款配色，视觉上与钩子呼应）。没给/没命中就原样，零风险。
    """
    style = style or SubtitleStyle()
    if (width, height) != (BASE_W, BASE_H):
        style = style.scaled(width, height)
    style.font = normalize_font(style.font)
    kw_list = [str(k) for k in (keywords or []) if str(k).strip()]
    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",                 # 2 = 不自动换行（我们自己断行）
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        style.ass_style_line(),
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    events = []
    for c in cues:
        text = wrap_text(c["text"], style.max_chars_per_line, style.max_lines)
        if not text:
            continue
        override = ""
        if c.get("style") == "hookBig":
            # v4.2（用户「没有互联网的那种镜头感」）：开头大花字 —— 钩子原话
            # 大号黄字、黑描边、顶部居中、淡入淡出。ASS 颜色是 &HBBGGRR&：
            # 亮黄 RGB(255,215,0) ⇒ B=00 / G=D7 / R=FF ⇒ &H00D7FF&。
            big_size = int(round(style.font_size * 1.9))
            override = (f"{{\\an8\\pos({width // 2},{round(height * 0.14)})}}"
                        f"{{\\fs{big_size}\\c&H00D7FF&\\3c&H000000&\\bord4\\b1"
                        f"\\fad(140,240)}}")
        elif style.pos_x is not None or style.pos_y is not None:
            # v3.41：手动定位。x = 宽度百分比；y = 自底部算起换到自顶部算（ASS \pos 的
            # y 轴向下）。an2 锚点 = 字幕块底边中点 → x=50 时与"底部居中+边距"同观感。
            fx = float(style.pos_x if style.pos_x is not None else 50.0)
            fy = float(style.pos_y if style.pos_y is not None else 20.0)
            x = max(0, min(width, round(width * fx / 100.0)))
            y = max(0, min(height, round(height * (1.0 - fy / 100.0))))
            override = "{\\an2\\pos(%d,%d)}" % (x, y)
        if kw_list and c.get("style") != "hookBig":
            # v4.3：关键词强调 —— 命中的词亮黄 + 1.15 倍（与开头大花字同色系，视觉呼应）。
            # 在 wrap 之后替换（\N 断行不影响词内子串匹配）；一处命中只替换一次即可。
            for kw in kw_list:
                if kw and kw in text:
                    text = text.replace(
                        kw, f"{{\\c&H00D7FF&\\fs{int(round(style.font_size * 1.15))}}}{kw}"
                            f"{{\\c&HFFFFFF&\\fs{style.font_size}}}", 1)
                    break
        events.append(
            f"Dialogue: 0,{ts(c['startMs'])},{ts(c['endMs'])},Default,,0,0,0,,{override}{text}")
    return "\n".join(head + events) + "\n"


def total_duration_ms(keep_ranges: list[dict]) -> float:
    """成片总时长 = 各保留区间时长之和。"""
    return sum(max(0.0, float(r["endMs"]) - float(r["startMs"])) for r in keep_ranges)
