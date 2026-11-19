#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**封面文案生成**（v4.76，方案《发布封面设计优化方案》§7）——LLM 出 4 条候选。

约定（方案 §7.2）：**不让 LLM 输出 emoji 字符**（PIL 画不出彩色 emoji）——
让它从码点清单里选，返回 `"1f921"`，渲染时拼 `{code}.png`。可选范围可控。

失败返回空列表（K69：如实降级，**不阻断发布**——仍出无字封面）。
"""
from __future__ import annotations

from .cover_design import EMOJI_CATALOG

PROMPT_TMPL = """你是短视频封面文案编辑。下面是一条直播切片的标题和摘要，请写 4 条封面文案。

【硬性要求】
1. 每条**分两行**，第一行 4–6 字，第二行 4–7 字，总长 8–13 字；
2. 砍掉"主播坦言/他说/网友表示"这类第三人称前缀；
3. 必须包含**冲突双方**或**反差点**（谁和谁、什么和什么对立）；
4. 四条分别用四种语气：① 反问 ② 判断 ③ 惊讶 ④ 第一人称；
5. 不许编造标题和摘要里没有的事实、数字、人物；
6. 不要用书名号、感叹号堆叠、emoji 字符。

【输出 JSON，不要任何解释】
{{"candidates":[
  {{"line1":"","line2":"","tone":"反问","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"判断","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"惊讶","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"第一人称","emoji":"码点"}}
]}}

【emoji 只能从这些码点里选】
{codes}

标题：{title}
摘要：{summary}"""

#: v4.84「AI 简化文案」：以**用户当前文案**为源做压缩（用户 2026-11-14：
#: 「这个 AI 优化文案的按钮，是优化上方的文案内容的，因为是封面文案，
#: 所以优化后的文案字数可以少一点」）。之前的实现拿画布第一行 + 片名去**重新生成**，
#: 用户填在上面的文案根本没进模型 —— 改法见 :func:`build_simplify_prompt`。
SIMPLIFY_TMPL = """你是短视频封面文案编辑。下面是**当前封面文案**（可能偏长）。请把它**压缩得更短**，\
产出 4 条候选，让用户挑一条替换。

【当前文案】{text}
【片名/背景】{title}｜{summary}

【硬性要求】
1. 每条**分两行**：第一行 3–5 字，第二行 3–6 字，**总长不超过 10 字**（比原文短，宁短不长）；
2. **只做压缩，不做扩写**：保留原文里最抓人的冲突、数字或反差，不许新增原文没有的事实、\
人物、数字、事件；
3. 四条用四档不同力度：① 保留主信息 ② 只留冲突点 ③ 只留结果/结论 ④ 口语化更狠一点；
4. 统一成海报上能直接印的语气，去掉"主播/他说/网友"这类第三人称前缀；
5. 不要书名号、不要堆感叹号、不要 emoji 字符。

【输出 JSON，不要任何解释】
{{"candidates":[
  {{"line1":"","line2":"","tone":"保留主信息","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"只留冲突","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"只留结果","emoji":"码点"}},
  {{"line1":"","line2":"","tone":"极短口语","emoji":"码点"}}
]}}

【emoji 只能从这些码点里选】
{codes}

只输出 JSON。"""


def build_simplify_prompt(text: str, title: str, summary: str) -> str:
    """v4.84：把**用户当前文案**压短（而非拿标题重新生成）。"""
    codes = " / ".join(f"{c['code']} {c['name']}" for c in EMOJI_CATALOG)
    return SIMPLIFY_TMPL.format(codes=codes, text=(text or "").strip() or "（空）",
                                title=title or "（无）", summary=summary or "（无）")


def build_prompt(title: str, summary: str) -> str:
    codes = " / ".join(f"{c['code']} {c['name']}" for c in EMOJI_CATALOG)
    return PROMPT_TMPL.format(codes=codes, title=title or "（无）", summary=summary or "（无）")


def generate_candidates(title: str, summary: str, *, text: str | None = None,
                        config=None, transport=None) -> list[dict]:
    """调 LLM 出 4 条候选；**任何失败返回 []**（不阻断封面/发布）。

    ``text`` 非空（v4.84）：以它为用户当前文案做**压缩**（"AI 简化文案"按钮的正解）；
    为空则退回按标题/摘要**生成**（老行为，兼容没填文案的场景）。
    """
    # ⚠️ 配置唯一出处是 app.slicer.llm（设置页保存的 AI Key 就在那儿）。
    # 这里曾经写成 `from .llm import …`（app/common/llm.py 根本不存在）——
    # 于是「AI 简化文案」按钮点一次就 ModuleNotFoundError，静默不可用（v4.84 修）。
    from app.slicer.llm import LLMError, chat_json, extract_json, load_config

    cfg = config or load_config()
    if not cfg.configured:
        return []
    src = str(text or "").strip()
    prompt = build_simplify_prompt(src, title, summary) if src else build_prompt(title, summary)
    system = ("你是短视频封面文案编辑。只输出 JSON，不要任何解释。"
              if not src else "你是短视频封面文案编辑，擅长把长文案压成海报上的一句话。只输出 JSON。")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        content = chat_json(cfg, messages, transport=transport)
        data = extract_json(content)
        out: list[dict] = []
        allowed = {c["code"] for c in EMOJI_CATALOG}
        for it in (data.get("candidates") or [])[:4]:
            if not isinstance(it, dict):
                continue
            line1 = str(it.get("line1") or "").strip()[:10]
            line2 = str(it.get("line2") or "").strip()[:12]
            if not line1:
                continue
            code = str(it.get("emoji") or "").strip().lower()
            out.append({"line1": line1, "line2": line2,
                        "tone": str(it.get("tone") or "")[:8],
                        "emoji": code if code in allowed else ""})
        return out
    except (LLMError, Exception):                      # noqa: BLE001 — 失败如实降级
        return []
