#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面提示词（v3.37）：给**外部 AI 生图工具**用的可复制提示词 + 参考帧说明。

对照参考实现（`editor_app/backend/services/cover_generator.py` 的
``prepare_manual_cover_kit`` / ``_manual_cover_prompt``）：参考实现的封面**不在本地生成**，
而是——
1. 从素材里挑/截两张「人物参考帧」（A 近景 / B 中景）；
2. 生成 **3:4 竖版**与 **4:3 横版**两份**可复制的提示词**；
3. 用户把「参考帧图 + 提示词」带到任意生图工具（参考实现指向 gemini.google.com）出封面。

我们的对齐口径一致：**本地只截参考帧，提示词交给人带走用**（用户 2026-09-24 明确要求）。

为什么提示词必须在 Python 侧（而不是前端拼字符串）：
- 它是"内容契约"：标题逐字、身份保真、不要文字/水印、标题安全区、画幅要求 ——
  改口径要能一处改全生效（K44）；
- 前端只负责展示与复制，不持有文案。
"""
from __future__ import annotations

from typing import Any

# 生图工具入口（与参考实现一样给个直达链接；用户也可用任何其它工具）
#
# ⚠️ **唯一出处**（K44）：这一份同时决定三处 ——
#   ① 剪辑页封面工具里「可用工具」那个链接显示成什么；
#   ② `desktop_entry.open_tool_url()` 的**主机白名单**（只放行这里登记过的 host，
#      不放行就成了「页面能开任意外链」的口子）；
#   ③ `build_cover_kit()` 下发给前端的 `tools` 与 `note` 里的工具名。
#   ⇒ 换工具只改这一处，**不要**在前端再写一份名字。
#
# v3.72（用户 2026-09-28）：用户「封面设计的可用工具当前是 Gemini，
# 帮我改成豆包的网页」⇒ 换成豆包网页版（www.doubao.com；网页版走「创作 → AI 绘画/图像生成」，
# 支持上传参考图 + 自定义比例，正好对上本页给的「参考帧图 + 提示词」）。
# 参考实现指向的是 gemini.google.com（那是**原版**的事实，记在上面的模块注释里，别改）。
IMAGE_TOOLS: list[dict] = [
    {"name": "豆包", "url": "https://www.doubao.com"},
]

_HONESTY = (
    "不要编造素材里没有的人物、事件、品牌、平台元素；"
    "不要生成任何文字、字母、数字、Logo 或水印（文字由你自己在排版时加，或交给工具出图后再加）。"
)


def _metaphor(title: str, summary: str, category: str, keywords: str, hook_text: str) -> str:
    """按内容关键词挑**一个**视觉隐喻（照原版 ``_cover_visual_metaphor`` 的思路）。

    只给一个线索 —— 多线索会让生图模型堆道具、出"拼贴海报"，与原版刻意保持克制一致。
    """
    subject = " ".join((title, summary, category, keywords, hook_text)).lower()

    if any(w in subject for w in ("执行", "行动", "开始", "拖延", "坚持", "自律")):
        return "用一条由近及远、略微向上的光线或桌面动线表达「立刻行动」，人物自然前倾或正在讲解，不摆拍。"
    if any(w in subject for w in ("钱", "收入", "财富", "赚钱", "创业", "生意", "商业", "变现")):
        return "用整洁的工作台、笔记本或向上延展的窗景暗示「积累与增长」；不要钞票雨、夸张金额或虚假成功场景。"
    if any(w in subject for w in ("选择", "人生", "成长", "认知", "关系", "家庭", "父母", "焦虑", "迷茫")):
        return "用房间里的明暗分界、门框或窗光表达「看清选择」；环境有层次但真实安静，人物始终是第一焦点。"
    if any(w in subject for w in ("故事", "经历", "案例", "真相", "为什么", "揭秘", "避坑")):
        return "用一处真实、有方向感的环境细节（桌面笔记、门口、窗边）建立叙事悬念；只留一个线索。"
    if any(w in subject for w in ("ai", "科技", "工具", "方法", "课程", "知识", "学习")):
        return "用简洁的桌面、纸张或屏幕微光表达「讲方法、讲知识」；不要赛博霓虹、悬浮 UI 或代码雨。"
    if any(w in subject for w in ("游戏", "打架", "枪", "战术", "攻略")):
        return "用真实的游戏直播环境（屏幕微光、耳机、桌面）承接气氛；不要贴图化的游戏元素或夸张特效。"
    return "从标题里提炼一个真实、一眼能读懂的环境线索来支持观点；只用这一个线索，保持直播空间的真实感。"


def _ratio_block(aspect: str) -> str:
    if aspect == "3:4":
        return ("画幅严格为 3:4 竖版。人物放在中下部，脸部靠近视觉中心；"
                "标题区留在中上部，绝不压住脸、眼睛或手势。")
    return ("画幅严格为 4:3 横版。人物放在中间偏右，左侧留出干净的标题安全区；"
            "绝不压住脸、眼睛或手势。")


def _variant_block(variant: str) -> str:
    if variant == "A":
        return ("方案 A（人物冲击感）：近景或半身近景，让眼神、自然表情和一处讲解手势成为第一焦点。")
    return ("方案 B（场景故事感）：稍宽的中景，保留并轻度整理真实直播环境，让环境线索支持主题；"
            "人物仍是绝对主体，环境不能抢脸。")


def build_prompt(*, title: str, summary: str = "", category: str = "",
                 keywords: str = "", hook_text: str = "", aspect: str = "3:4",
                 variant: str = "A", scores: dict | None = None,
                 strategy: str = "") -> str:
    """生成一份可复制的封面提示词（``aspect`` 取 ``3:4`` / ``4:3``）。"""
    t = " ".join(str(title or "直播高光").split()) or "直播高光"
    metaphor = _metaphor(t, summary, category, keywords, hook_text)
    parts = [p for p in (
        f"内容摘要：{summary}" if summary else "",
        f"内容类型：{category}" if category else "",
        f"关键词：{keywords}" if keywords else "",
        f"重点原话：{hook_text}" if hook_text else "",
    ) if p]
    info = "；".join(parts) or "根据标题和参考截图提炼一个真实、清晰的核心观点。"
    sc = scores or {}
    try:
        score_line = (f"参考评分：价值 {float(sc.get('value') or 0):.0f}、"
                      f"笑点 {float(sc.get('comedy') or 0):.0f}、"
                      f"完整度 {float(sc.get('completeness') or 0):.0f}")
    except (TypeError, ValueError):
        score_line = ""

    return (
        "请使用我上传的直播截图作为**唯一**人物身份、服装与原始环境参考，直接生成一张最终封面图。\n"
        "保持人物身份、脸型、五官、发型、表情、服装、体型真实一致：不要换人、不要复制脸、"
        "不要美颜换年龄、不要凭空增加人物或事件。可以为了构图轻度整理原有环境、改变景别、"
        "改善光线和人物与背景的分离；要像成熟知识博主/主播的真人摄影封面，不要插画、"
        "也不要直接用直播截图。\n\n"
        f"封面主标题（必须逐字正确）：{t}\n"
        f"内容信息：{info}\n"
        f"视觉隐喻：{metaphor}\n"
        + (f"剪辑口径：{strategy}；{score_line}\n" if strategy or score_line else "") +
        "\n"
        "画面品质：真实摄影质感、清楚的主体分离、自然皮肤纹理、干净的方向光、适度景深。"
        "底色可用沉稳深色、暖灰、米白，按主题给一小块亮黄或暖橙点睛；"
        "对比要适合手机小图看清，但禁止霓虹/赛博特效、红黄渐变、HDR、塑料磨皮、过度炫光、"
        "爆炸元素、表情包、贴纸墙。\n\n"
        + _variant_block(variant) + "\n"
        + _ratio_block(aspect) + "\n\n"
        f"排版：在标题安全区加入大标题「{t}」，占画面约 24%～34%，最多 2～3 行；"
        "用成熟、厚重但不笨拙的中文无衬线字体，白色为主，只把 1～2 个核心词改成亮黄或暖橙，"
        "配细描边与很轻的阴影，保证小图清楚。标题以外不要出现任何文字。\n\n"
        f"最终检查：人物脸清晰、视线与手势自然；标题不遮脸；环境只服务主题。{_HONESTY}"
    )


def build_cover_kit(picked: dict[str, Any], *, strategy: str = "",
                    reference_rel: str = "", cover_ms: float = -1) -> dict:
    """候选 → 封面参考包（标题 + 两版提示词 + 参考帧信息）。纯计算，不碰文件。"""
    title = " ".join(str(picked.get("title") or "").split()) or "直播高光"
    summary = " ".join(str(picked.get("summary") or "").split())
    category = " ".join(str(picked.get("category") or "").split())
    keywords = picked.get("keywords")
    if isinstance(keywords, (list, tuple)):
        keywords = "、".join(str(k) for k in keywords)
    keywords = " ".join(str(keywords or "").split())
    hook = picked.get("hook") or {}
    hook_text = " ".join(str(hook.get("sourceText") or hook.get("text") or "").split())
    scores = picked.get("scores") or {}

    common = {"title": title, "summary": summary, "category": category,
              "keywords": keywords, "hook_text": hook_text, "scores": scores,
              "strategy": strategy}
    return {
        "title": title,
        "referenceRel": reference_rel,
        "coverMs": cover_ms,
        "prompts": {
            "portrait3x4": build_prompt(aspect="3:4", variant="A", **common),
            "landscape4x3": build_prompt(aspect="4:3", variant="B", **common),
        },
        "tools": IMAGE_TOOLS,
        "note": ("本软件**不在本地生成封面**：点「复制提示词」把它连同上面的参考帧图"
                 "一起交给任意生图工具（如豆包），出图后按需自己加标题。"),
    }
