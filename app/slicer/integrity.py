#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切完之后的「完整性复核」（v3.132）：让 AI 再看一遍每条候选**是不是一条能独立发的片**。

为什么要第三道：边界能靠规则卡住，但"这条切出来能不能看懂"只有读一遍才知道。
本模块把每条候选自己的字幕（不含全片上下文）交给已配置的 LLM，只问四件事：

  · 开头是不是半句话（缺主语/承接上一段）
  · 结尾是不是断在半句（没有收束）
  · 有没有跨单元（一条里出现了两个结算/两个商品/两个人）
  · 这条整体能不能独立发布

输出 ``verdict``：``完整`` / ``开头突兀`` / ``结尾突兀`` / ``疑似跨单元`` / ``看不懂``。
**只用于提示与排序，不自动删候选**（删不删由人定，见 §0 的"AI 只建议不裁决"）。
"""
from __future__ import annotations

import json
import re

#: 一次最多复核多少条（再多就分批复核，别把一次请求撑爆）
MAX_PER_CALL = 12

_VERDICTS = ("完整", "开头突兀", "结尾突兀", "疑似跨单元", "看不懂")

PROMPT = (
    "你是短视频成片审片编辑。下面每条候选都附了它自己的**全部字幕**（只有这条，没有前后文）。"
    "请判断它作为一条独立发布的竖屏短视频是否成立，并按下面的 JSON 回答。\n"
    "判定口径：\n"
    "- 开头突兀：第一句像承接上文（「然后」「所以」「这个人」没有指代对象）、缺主语、像半句；\n"
    "- 结尾突兀：最后一句没讲完、没有收束、明显在等下一句；\n"
    "- 疑似跨单元：一条里出现了两次「结束/结算/胜利」或两个不同商品/两个不同的人；\n"
    "- 看不懂：全条没有可独立理解的信息（全是操作声/口头禅）。\n"
    "只输出 JSON：{\"items\":[{\"id\":\"原样回传\",\"verdict\":\"完整|开头突兀|结尾突兀|疑似跨单元|看不懂\","
    "\"reason\":\"一句话理由\",\"fix\":\"建议（如：起点提前 3 秒 / 终点后延 2 秒 / 不用改）\"}]}\n\n"
)


def _text_of(segments: list[dict], start: float, end: float, limit: int = 1600) -> str:
    parts = []
    for s in segments or []:
        if float(s.get("end") or 0) <= start or float(s.get("start") or 0) >= end:
            continue
        parts.append(str(s.get("text") or "").strip())
    text = " ".join(p for p in parts if p)
    return text[:limit]


def judge(transcript: dict, candidates: list[dict], *, llm_config=None,
          transport=None) -> dict:
    """逐条复核；返回 ``{"ok", "items":[{start,end,role,verdict,reason,fix}]}``。

    AI 没配置 / 调用失败都**不抛异常**（复核是锦上添花，不能把切片结果弄没）。
    """
    from app.slicer import llm

    segs = list(transcript.get("segments") or [])
    cands = [c for c in (candidates or []) if c.get("start") is not None]
    if not segs or not cands:
        return {"ok": False, "error": "没有可复核的候选"}

    try:
        cfg = llm_config or llm.load_config()
    except llm.LLMError as exc:
        return {"ok": False, "error": f"AI 未就绪：{exc}"}

    out: list[dict] = []
    for base in range(0, len(cands), MAX_PER_CALL):
        batch = cands[base:base + MAX_PER_CALL]
        lines = []
        for i, c in enumerate(batch):
            txt = _text_of(segs, float(c["start"]), float(c["end"]))
            lines.append(f'{i + 1}. id={i} | {float(c["start"]):.0f}-{float(c["end"]):.0f}s | {txt}')
        messages = [
            {"role": "system", "content": "你是严格的审片编辑，只输出 JSON。"},
            {"role": "user", "content": PROMPT + "\n".join(lines)},
        ]
        try:
            raw = llm.chat_json(cfg, messages)
            data = llm.extract_json(raw)
        except Exception as exc:                               # noqa: BLE001
            return {"ok": False, "error": f"AI 复核失败：{type(exc).__name__}: {exc}",
                    "partial": out}
        got = {}
        for row in (data.get("items") or []):
            try:
                idx = int(str(row.get("id")).strip())
            except (TypeError, ValueError):
                continue
            verdict = str(row.get("verdict") or "").strip()
            if verdict not in _VERDICTS:
                verdict = "看不懂"
            got[idx] = {"verdict": verdict,
                        "reason": str(row.get("reason") or "")[:120],
                        "fix": str(row.get("fix") or "")[:80]}
        for i, c in enumerate(batch):
            row = got.get(i) or {}
            out.append({
                "start": round(float(c["start"]), 3), "end": round(float(c["end"]), 3),
                "role": c.get("role") or "",
                "verdict": row.get("verdict") or "未复核",
                "reason": row.get("reason") or "",
                "fix": row.get("fix") or "",
            })

    bad = [x for x in out if x["verdict"] not in ("完整", "未复核")]
    return {"ok": True, "items": out, "checked": len(out), "needFix": len(bad),
            "summary": {v: sum(1 for x in out if x["verdict"] == v) for v in _VERDICTS}}
