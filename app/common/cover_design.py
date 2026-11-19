#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**发布封面设计渲染**（v4.76，方案《发布封面设计优化方案》）——纯函数，不联网。

四层结构（叠在 publish_cover 的首帧底图之上）：
  ④ emoji 图层（Noto Emoji 128px 透明 PNG，Apache 2.0，可多个）
  ③ 主文案（两行大字：双色、白描边、字号=画布宽百分比）
  ② 背景（视频帧：模糊 + 压暗，centering 偏上保人脸）
  ① 裁切（publish_cover.COVER_RATIOS，含 3:4/4:3/9:16/16:9 四档）

设计约定（方案 §四，前后端唯一接口是 `<stem>_cover.json`）：
  · 所有位置/尺寸都是**相对值**（0–1 小数），换比例自动等比；
  · `fill1/fill2` 分别对应第一行/第二行（双行双色）；
  · emoji 的 `code` 是 Unicode 码点字符串，文件名就是 `{code}.png`（不维护映射表，K44）；
  · **没有配置时走原路径**（publish_cover._derive），老素材行为不变。

emoji 三个坑（方案 §6.3，全部实测）：
  坑① Noto/Twemoji 的 PNG 是 mode=P ⇒ **必须 .convert("RGBA")**；
  坑② 丢 alpha 会出方块 ⇒ 只用 alpha_composite()；
  坑③ 72px 放大糊 ⇒ 本仓用 Noto Emoji 128px（assets/emoji/，Apache 2.0）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.common.paths import ws

EMOJI_DIR = Path(__file__).parent / "assets" / "emoji"
# 封面字体：**Noto Sans SC Bold（OFL 1.1 可商用）** —— 方案 §5.1 原定站酷快乐体，
# 实测该 ttf 对部分常用字（团/高/能）**缺字形（渲染成白方块）**，不能用。
# 思源黑体 Bold 覆盖全 CJK，圆润黑体抓眼，符合方案"必须要一个圆润的黑体"的意图。
# （站酷快乐体 ttf 仍留在 assets/fonts/ 备查，但不参与渲染。）
FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSansSC-Bold.otf"
FONT_NAME = "NotoSansSC"

#: 三套预设（方案 §八）——参数全部可调，但用户只选不调也能出效果
TEMPLATES: dict[str, dict] = {
    "A": {"label": "左对齐双色", "sizeRatio": 0.12, "lineHeight": 1.08, "strokeWidth": 10,
          "fill1": "#FF78C8", "fill2": "#FFBE28", "strokeFill": "#FFFFFF",
          "align": "left", "x": 0.07, "y": 0.10,
          "emojis": [{"code": "1f921", "x": 0.64, "y": 0.035, "size": 0.18},
                     {"code": "1f525", "x": 0.055, "y": 0.30, "size": 0.11}]},
    "B": {"label": "居中大字", "sizeRatio": 0.15, "lineHeight": 1.02, "strokeWidth": 12,
          "fill1": "#FFFFFF", "fill2": "#FFE55C", "strokeFill": "#5A3E28",
          "align": "center", "x": 0.5, "y": 0.12,
          "emojis": [{"code": "1f631", "x": 0.06, "y": 0.03, "size": 0.15}]},
    "C": {"label": "花字", "sizeRatio": 0.13, "lineHeight": 1.10, "strokeWidth": 14,
          "fill1": "#FF5FA2", "fill2": "#FFC107", "strokeFill": "#FFFFFF",
          "align": "left", "x": 0.06, "y": 0.09,
          "emojis": [{"code": "1f525", "x": 0.30, "y": 0.02, "size": 0.14}]},
}

#: emoji 目录（方案 §5.2 首批清单）——发给前端选择用（K44：一份）
EMOJI_CATALOG: tuple[dict, ...] = (
    {"code": "1f921", "name": "小丑"}, {"code": "1f631", "name": "尖叫"},
    {"code": "1f62d", "name": "大哭"}, {"code": "1f621", "name": "愤怒"},
    {"code": "1f633", "name": "脸红"}, {"code": "1f914", "name": "思考"},
    {"code": "1f644", "name": "无语"}, {"code": "1f602", "name": "笑哭"},
    {"code": "1f60f", "name": "得意"}, {"code": "1f97a", "name": "委屈"},
    {"code": "1f525", "name": "火"}, {"code": "26a1", "name": "闪电"},
    {"code": "2b50", "name": "星"}, {"code": "2757", "name": "感叹号"},
    {"code": "1f4a5", "name": "爆炸"}, {"code": "1f44d", "name": "赞"},
    {"code": "1f64c", "name": "举手"}, {"code": "1f440", "name": "眼睛"},
    {"code": "1f449", "name": "指右"}, {"code": "1f448", "name": "指左"},
    {"code": "1f4b0", "name": "钱"}, {"code": "1f3ae", "name": "手柄"},
    {"code": "1f3b5", "name": "音符"}, {"code": "1f4f1", "name": "手机"},
    {"code": "1f381", "name": "礼物"}, {"code": "1f929", "name": "星目"},
    {"code": "1f970", "name": "爱慕"}, {"code": "1f60d", "name": "花痴"},
    {"code": "1f62c", "name": "抿嘴"}, {"code": "1f910", "name": "封嘴"},
    {"code": "1f636", "name": "无脸"}, {"code": "1f925", "name": "长鼻"},
    {"code": "1f4a8", "name": "汗气"}, {"code": "1f4aa", "name": "肌肉"},
    {"code": "1f3af", "name": "靶心"}, {"code": "1f3c0", "name": "篮球"},
    {"code": "1f9e7", "name": "红包"}, {"code": "1f4b8", "name": "钱翼"},
    {"code": "1f6d1", "name": "停止"}, {"code": "2705", "name": "勾选"},
)


# v4.82（用户）：背景原来写死「模糊 22 + 压暗 47%」，用户要能自己调 ——
# 这里是**唯一出处**：`render()`、`render_base_jpeg()`、前端滑块默认值都用它。
DEFAULT_BACKDROP: dict = {"on": True, "blur": 22, "shade": 0.47}
#: 取景焦点默认值（v4.98）：原来是写死的 `centering=(0.5, 0.35)`（垂直偏上保人脸），
#: 现在用户可以在画布上拖动底图自选，默认值保持与旧出图**完全一致**。
DEFAULT_FOCUS: tuple[float, float] = (0.5, 0.35)
#: 取景缩放倍率（v4.99）：1.0 = cover 基准（与旧出图一致），最大 3.0（再大就是纯色块）
DEFAULT_ZOOM: float = 1.0
ZOOM_MAX: float = 3.0
BLUR_MAX = 40          # 超过 40 就是纯色块了，滑块给到这里就够
SHADE_MAX = 0.85       # 再高压暗就全黑，没有意义


def backdrop_params(cfg: dict) -> tuple[int, float]:
    """从 ``cfg["background"]`` 取 ``(blur, shade)``，缺字段/脏值一律回默认。

    · ``on=False`` ⇒ blur 归 0（"用原图"，但 shade 仍按用户设置，方便只去模糊不去压暗）；
    · 老配置没有 ``background`` 字段 ⇒ 22 / 0.47（与 v4.81 及以前完全一致，出图不变）。
    """
    bg = (cfg or {}).get("background") or {}
    if not isinstance(bg, dict):
        bg = {}
    try:
        blur = int(bg.get("blur", DEFAULT_BACKDROP["blur"]) or 0)
    except (TypeError, ValueError):
        blur = DEFAULT_BACKDROP["blur"]
    if bg.get("on") is False:
        blur = 0
    blur = max(0, min(BLUR_MAX, blur))
    try:
        shade = float(bg.get("shade", DEFAULT_BACKDROP["shade"]))
    except (TypeError, ValueError):
        shade = DEFAULT_BACKDROP["shade"]
    shade = max(0.0, min(SHADE_MAX, shade))
    return blur, shade


def template_cfg(template: str, line1: str = "", line2: str = "") -> dict:
    """取一套模板并填入文案，返回**方案契约结构**（§四：copy/style/emojis/background）。

    ⚠️ style 是**包裹层**（style.fill1/fill2/sizeRatio…）—— 之前模板字段平铺在顶层，
    渲染读 style.get(...) 拿到空 ⇒ 文字全白（本仓实测踩过，别改回平铺）。
    """
    import copy
    name = template if template in TEMPLATES else "A"
    tpl = TEMPLATES[name]
    style = {k: copy.deepcopy(v) for k, v in tpl.items() if k not in ("label", "emojis")}
    return {
        "version": 1,
        "template": name,
        "copy": {"line1": line1, "line2": line2},
        "style": style,
        "emojis": copy.deepcopy(tpl.get("emojis") or []),
        "background": dict(DEFAULT_BACKDROP),
    }


def load_cfg(video: Path) -> dict:
    """读一份成片的封面设计配置（没有就返回 {} —— 走原路径）。"""
    cfg_path = ws.cover_json_for(video)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) and cfg.get("copy") else {}
    except (OSError, ValueError):
        return {}


def save_cfg(video: Path, cfg: dict) -> Path:
    cfg_path = ws.cover_json_for(video)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cfg_path)
    return cfg_path


# ---------------------------------------------------------------- 渲染（纯函数）

def focus_params(cfg: dict, ratio_key: str | None = None) -> tuple[float, float]:
    """从 ``cfg["background"]["focus"]`` 取取景焦点 ``(fx, fy)``，都在 0–1。

    v4.98（用户）：「画布中的图片能不能改为选中的，由我调整图片的画面位置，画布固定比例就行，
    就是由我拖动图片调整到最佳位置」—— 语义与 ``ImageOps.fit(centering=(fx, fy))`` 一致：
    0=左/上，0.5=中，1=右/下。前端拖动时把「图片左上角」按同一公式摆，所以预览＝出图。
    缺字段/脏值 ⇒ :data:`DEFAULT_FOCUS`（与 v4.97 及以前的出图一模一样）。
    """
    bg = (cfg or {}).get("background") or {}
    if not isinstance(bg, dict):
        return DEFAULT_FOCUS
    # v5.0（用户：「切换其他比例操作时，这个图片位置调整有点不好用」）：
    # 取景**按比例各记一份**（`focusByRatio`）——换个宽高比，溢出量完全不同，
    # 沿用同一个百分比会落到完全不同的画面上（看起来"位置跳了"）。
    # 某比例没记录 ⇒ 用 DEFAULT_FOCUS（= 旧硬编码值 0.5/0.35，与老出图一致）。
    f = {}
    by = bg.get("focusByRatio")
    if ratio_key and isinstance(by, dict):
        cand = by.get(ratio_key)
        if isinstance(cand, dict):
            f = cand
    if not f:
        cand = bg.get("focus")
        f = cand if isinstance(cand, dict) else {}
    out = []
    for k, dflt in (("x", DEFAULT_FOCUS[0]), ("y", DEFAULT_FOCUS[1])):
        try:
            v = float(f.get(k, dflt))
        except (TypeError, ValueError):
            v = dflt
        out.append(max(0.0, min(1.0, v)))
    return (out[0], out[1])


def zoom_params(cfg: dict, ratio_key: str | None = None) -> float:
    """从 ``cfg["background"]["zoom"]`` 取取景缩放倍率，1.0–3.0（v4.99）。

    需求（2026-11-14 截图指「4:3 · 1440×1080」徽标）：「这个改为按钮状态，点开就是滑块，
    滑动修改图片的放大和缩小」。1.0 = cover 基准（与旧出图一致，走原 `ImageOps.fit`
    路径**逐像素不变**）；>1.0 走手动裁切路径。
    """
    bg = (cfg or {}).get("background") or {}
    if not isinstance(bg, dict):
        return DEFAULT_ZOOM
    raw = None
    by = bg.get("zoomByRatio")
    if ratio_key and isinstance(by, dict) and by.get(ratio_key) is not None:
        raw = by.get(ratio_key)
    if raw is None:
        raw = bg.get("zoom", DEFAULT_ZOOM)      # 老配置（单值）兜底
    try:
        z = float(raw)
    except (TypeError, ValueError):
        z = DEFAULT_ZOOM
    return max(1.0, min(ZOOM_MAX, z))


def _backdrop(base: Image.Image, ratio: dict, blur: int, shade: float,
              focus: tuple[float, float] = DEFAULT_FOCUS,
              zoom: float = DEFAULT_ZOOM) -> Image.Image:
    """背景层：裁到目标比例（取景焦点/缩放可调）→ 高斯模糊 → 压暗（方案 §6.1）。

    v4.98：``focus`` 就是 ``ImageOps.fit`` 的 ``centering`` —— 前端拖动底图改的正是它，
    两边同一公式 ⇒ 拖动所见即出图。
    v4.99：``zoom``（≥1）在前端 coverLayout 与这里的**手动裁切**是同一公式
    （裁切尺寸 = 画布 / (cover基准 × zoom)，起点 = 溢出 × focus）。
    ⚠️ zoom == 1 时必须走原 ``ImageOps.fit`` —— 保证 v4.97 及以前的出图**逐像素不变**。
    """
    im = base.convert("RGB")
    W, H = int(ratio["width"]), int(ratio["height"])
    if abs(float(zoom) - 1.0) < 1e-6:
        fitted = ImageOps.fit(im, (W, H),
                              method=Image.Resampling.LANCZOS, centering=focus)
    else:
        z = max(1.0, min(ZOOM_MAX, float(zoom)))
        w0, h0 = im.size
        sc0 = max(W / w0, H / h0)                 # cover 基准
        sc = sc0 * z
        cw, ch = W / sc, H / sc                   # 裁切尺寸（原图坐标）
        cw, ch = min(w0, cw), min(h0, ch)         # zoom 极大时不超过原图
        fx, fy = focus
        x0 = (w0 - cw) * fx
        y0 = (h0 - ch) * fy
        fitted = im.crop((int(round(x0)), int(round(y0)),
                          int(round(x0 + cw)), int(round(y0 + ch)))).resize(
            (W, H), Image.Resampling.LANCZOS)
    if blur:
        fitted = fitted.filter(ImageFilter.GaussianBlur(radius=blur))
    layer = Image.new("RGBA", fitted.size, (0, 0, 0, int(255 * shade)))
    return Image.alpha_composite(fitted.convert("RGBA"), layer)


#: 手动换行模式最多几行（与前端 designer.html 的 MAX_LINES 同口径）
MAX_MANUAL_LINES = 5


def _copy_lines(copy: dict, canvas_w: int, size: int) -> list[str]:
    """把文案拆成**最多两行**（v4.80）。

    两种输入：
      · ``copy.text``（一段文案，AI 简化后的整段）⇒ 按**字宽实测**折行，最多 2 行；
      · 否则用 ``copy.line1/line2``（老契约，向后兼容）。
    折行宽度按 canvas_w × 0.86（与超框缩字号同口径）。

    v4.91（用户）：「不开启自动折两行就是默认一行的第一种样式，就算手动回车换行了
    也还是第一个花字样式」⇒ ``copy.twoLine is False`` 时**只出一行**：
    换行/宽度都不折，整段压成一行（第二行空），颜色自然只用 fill1。
    前端 ``paintText`` 用同一口径（含超框缩字号），保证"预览 = 出图"。
    """
    # v5.2（用户）：「文案输入框默认一个，新增一个就一行文案」——每条输入框＝一行。
    # `copy.lines` 是新的唯一真源（list[str]）；text / line1+line2 只作老配置兜底。
    lines_src = copy.get("lines")
    if isinstance(lines_src, list):
        rows = []
        for x in lines_src[:MAX_MANUAL_LINES]:
            s2 = re.sub(r"[\r\n]+", " ", str(x or "")).strip()
            if s2:
                rows.append(s2)
        return rows or [""]
    text = str(copy.get("text") or "").strip()
    single = copy.get("twoLine") is False
    if not text:
        l1 = str(copy.get("line1") or "").strip()
        l2 = str(copy.get("line2") or "").strip()
        if single:
            return [re.sub(r"[\r\n]+", "", (l1 + l2)).strip()]
        return [l1, l2]
    if single:
        # v4.92：**手动换行模式** —— 只在用户回车处断行（最多 MAX_MANUAL_LINES 行），
        # 不按宽度折；首尾空行丢掉、中间空行保留（用户可能就要一个空行做间隔）。
        rows = [r.strip() for r in re.split(r"\r?\n", text)]
        while rows and not rows[0]:
            rows.pop(0)
        while rows and not rows[-1]:
            rows.pop()
        return (rows or [""])[:MAX_MANUAL_LINES]
    try:
        font = ImageFont.truetype(str(FONT_PATH), max(8, int(size)))
    except Exception:                                  # noqa: BLE001
        return [text[:8], text[8:16]]
    max_w = int(canvas_w * 0.86)
    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        try:
            w = font.getbbox(cur + ch)[2]
        except Exception:                              # noqa: BLE001
            w = len(cur + ch) * size
        # 只在**第一行**按宽度断行；第二行装下剩余全部（超宽由字号自缩兜住，不丢字）
        if w > max_w and cur and not lines:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        if not lines:
            lines.append(cur)
        elif len(lines) == 1:
            lines.append(cur)
        else:
            lines[1] += cur
    return (lines + ["", ""])[:2]


def _text_layer(canvas: Image.Image, copy: dict, style: dict) -> Image.Image:
    """文字层：两行大字，双色 + 白描边，**字号 = 画布宽百分比**（方案 §6.2）。"""
    d = ImageDraw.Draw(canvas)
    size_ratio = float(style.get("sizeRatio") or 0.12)
    size = int(canvas.width * size_ratio)
    font = ImageFont.truetype(str(FONT_PATH), size)
    line_h = float(style.get("lineHeight") or 1.08)
    stroke_w = int(style.get("strokeWidth") or 10)
    align = style.get("align") or "left"
    x_ratio = float(style.get("x") or 0.07)
    y = int(canvas.height * float(style.get("y") or 0.10))
    max_w = int(canvas.width * 0.86)               # 字数超框自动缩（方案 §十二）
    lines = _copy_lines(copy, int(canvas.width), size)
    # v4.92：手动换行模式（twoLine=False）—— 逐行画、**所有行统一用 fill1**，
    # 并按**最长一行**统一缩字号（前端 paintText 同口径，保证预览 = 出图）。
    single = copy.get("twoLine") is False
    # v5.2：copy.lines 有几条画几条（最多 MAX_MANUAL_LINES）；老配置仍是最多 2 行
    if isinstance(copy.get("lines"), list):
        n_max = MAX_MANUAL_LINES
    else:
        n_max = MAX_MANUAL_LINES if single else 2
    if single:
        widest = 0
        for line in lines[:n_max]:
            if not line:
                continue
            try:
                bb = d.textbbox((0, 0), line, font=font, stroke_width=stroke_w)
                widest = max(widest, bb[2] - bb[0])
            except Exception:                      # noqa: BLE001
                pass
        if widest > max_w > 0:
            size = max(24, int(size * max(0.5, max_w / widest)))
            font = ImageFont.truetype(str(FONT_PATH), size)
    for i, line in enumerate(lines[:n_max]):
        if not line:
            continue
        f = font
        # 超框自动缩：量宽 >86% 画布就按比例缩字号（不缩行距，保持节奏）；
        # 手动换行模式已经统一缩过，这里不再逐行缩（否则几行大小不一）
        if not single:
            try:
                bbox = d.textbbox((0, 0), line, font=f, stroke_width=stroke_w)
                w = bbox[2] - bbox[0]
                if w > max_w and w > 0:
                    shrink = max(0.5, max_w / w)
                    f = ImageFont.truetype(str(FONT_PATH), max(24, int(size * shrink)))
            except Exception:                      # noqa: BLE001
                pass
        # v5.2：多行时第 1 行用 fill1、其余统一 fill2（单行模式全部 fill1，沿用 v4.92）
        if single:
            fill = style.get("fill1") or "#FFFFFF"
        else:
            fill = style.get("fill1" if i == 0 else "fill2") or "#FFFFFF"
        if align == "center":
            d.text((int(canvas.width * x_ratio), y), line, font=f,
                   fill=fill, stroke_width=stroke_w,
                   stroke_fill=style.get("strokeFill") or "#FFFFFF", anchor="ma")
        else:
            d.text((int(canvas.width * x_ratio), y), line, font=f,
                   fill=fill, stroke_width=stroke_w,
                   stroke_fill=style.get("strokeFill") or "#FFFFFF")
        y += int(size * line_h)
    return canvas


def _paste_emoji(canvas: Image.Image, code: str, x: float, y: float, size: float,
                 data: str | None = None) -> Image.Image:
    """emoji / 用户图片层（方案 §6.3 的三个坑全部处理）：缺图跳过不报错（K69）。

    v4.94：``data``（data URI）非空 ⇒ 画的是**用户自己选的图片**（前端已压到最长边 512），
    尺寸口径与 emoji 相同 —— 宽 = 画布宽 × size，高按原图比例（emoji 是正方形所以两者等价）。
    超过 4MB 的 data 直接跳过（防一条配置把渲染拖死）。
    """
    try:
        if data:
            if len(data) > 4 * 1024 * 1024:
                return canvas
            import base64
            from io import BytesIO
            _head, _sep, b64 = str(data).partition(",")
            if not b64:
                return canvas
            emo = Image.open(BytesIO(base64.b64decode(b64))).convert("RGBA")
        else:
            path = EMOJI_DIR / f"{code}.png"
            if not path.is_file():
                return canvas                          # 缺图跳过（K69：如实不报错）
            emo = Image.open(path).convert("RGBA")     # 坑①：mode=P 必须 convert RGBA
        s = max(8, int(canvas.width * float(size or 0.12)))
        # 正方形 emoji 与"按宽等比"的图片在这里统一：宽=size，高按原图比例
        h = s if not emo.width else max(8, int(round(s * emo.height / emo.width)))
        emo = emo.resize((s, h), Image.Resampling.LANCZOS)     # 坑③：LANCZOS 缩放
        canvas.alpha_composite(emo, (int(canvas.width * float(x or 0)),
                                     int(canvas.height * float(y or 0))))   # 坑②：alpha 合成
    except Exception:                              # noqa: BLE001
        pass                                       # 单个元素失败不拖垮整张封面
    return canvas


def render_base_jpeg(base_path: Path, ratio: dict, quality: int = 85,
                     blur: int = DEFAULT_BACKDROP["blur"],
                     shade: float = DEFAULT_BACKDROP["shade"],
                     focus: tuple[float, float] = DEFAULT_FOCUS,
                     zoom: float = DEFAULT_ZOOM) -> bytes:
    """**只出无字底图**（= 真出图的背景层：按比例裁切 + 模糊 + 压暗），返回 JPEG 字节。

    用途：封面设计子窗的画布底图 —— 之前拿的是**已烧字的四档封面**，
    画布上再叠一层可拖字就会出现"两份文案"（用户实测反馈）。

    v4.82：``blur`` / ``shade`` 由前端滑块传入（"背景模糊"开关关掉时 blur=0 即原图），
    这样**画布预览与最终出图用同一套参数**，不会出现「预览清楚、出图模糊」。
    """
    import io as _io

    blur = max(0, min(BLUR_MAX, int(blur)))
    shade = max(0.0, min(SHADE_MAX, float(shade)))
    with Image.open(str(base_path)) as im:
        canvas = _backdrop(im, ratio, blur, shade, focus, zoom)
    buf = _io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def render(base_path: Path, cfg: dict, ratio: dict, out: Path) -> bool:
    """把底图 + 配置渲染成一张封面。纯函数：不联网、不碰 src 之外的文件。

    没有配置（或没写 line1）⇒ 返回 False，调用方走原路径（老素材行为不变）。
    """
    if not cfg or not (cfg.get("copy") or {}).get("line1"):
        return False
    bg_blur, bg_shade = backdrop_params(cfg)
    _rk = str(ratio.get("key") or "") or None          # v5.0：取景按比例各存一份
    bg_focus = focus_params(cfg, _rk)                  # v4.98：取景焦点（拖动底图改的就是它）
    bg_zoom = zoom_params(cfg, _rk)                    # v4.99：取景缩放（徽标滑块）
    with Image.open(str(base_path)) as im:
        canvas = _backdrop(im, ratio, bg_blur, bg_shade, bg_focus, bg_zoom)
    canvas = _text_layer(canvas, cfg.get("copy") or {}, cfg.get("style") or {})
    for e in (cfg.get("emojis") or []):
        if not isinstance(e, dict):
            continue
        # v4.94：用户选的图片（type=image + data URI）与 emoji 走同一条粘贴管线
        if e.get("type") == "image" and e.get("data"):
            canvas = _paste_emoji(canvas, "", float(e.get("x") or 0),
                                  float(e.get("y") or 0), float(e.get("size") or 0.12),
                                  data=str(e["data"]))
        elif e.get("code"):
            canvas = _paste_emoji(canvas, str(e["code"]), float(e.get("x") or 0),
                                  float(e.get("y") or 0), float(e.get("size") or 0.12))
    from app.common.publish_cover import _save_jpeg
    _save_jpeg(canvas, out)
    return True
