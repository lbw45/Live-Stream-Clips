#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片引擎核心算法（M3-B-1）—— **纯计算，无 IO、无网络**。

对应文档 §6.4「切片引擎算法（产品核心）」的 12 步流程，以及决策 D6（时长区间）。

设计原则：
- **纯函数**：输入是 M3-A 的转写产物 dict，输出是候选列表 dict；不碰磁盘、不发请求，
  所以可以脱离 GUI / 服务单测，也便于以后换成 editor 侧调用。
- **不依赖分词库**：中文用**字符 n-gram + 停用词表**做关键词与词面新颖度，
  避免引入 jieba 这类额外依赖（要跟着打包体积走）。
- **确定性**：同输入同输出（无随机），冒烟测试才能断言具体数值。

两个规划器（planner）共用同一套后处理：
1. ``local_fallback`` —— 本文件里的规则规划器。**没有 API Key 也能出片**，
   这是「拿到 Key 之前也能用」的保障，也是 LLM 失败时的兜底（D3 要求 LLM 层可替换）。
2. ``deepseek`` / 任何 OpenAI 兼容接口 —— 见 :mod:`app.slicer.llm`；它给出的
   候选同样要过本文件的后处理（吸附 / 时长 / 去重 / 保底），保证不变量一致。

**不变量（后处理保证，冒烟测试逐条断言）**：
- I1 候选起止**落在转写句边界**上（±18 s 吸附，§6.4 第 2 步）；
- I2 时长在 ``[min_sec, max_sec]`` 内（v3.31 起由**内容类型**推导：通用 15–120 s /
  带货 20–120 s；旧口径是 story 35 / commerce 45，因录制已变成「几分钟到几十秒」而下放）；
- I3 候选之间**不重复覆盖**同一段（重叠去重，D6「不等长切割」）；
- I4 四维评分齐全且都在 0–100；
- I5 钩子时长为 3–12 s 且落在候选内（§6.5 的 ``hook`` 前置手法）；
- I6 标题已过敏感词处理（命中即脱敏，白名单放行，§6.4 第 11 步）。
"""
from __future__ import annotations

import math
import re
import time

# ---------------------------------------------------------------- 参数（D6 / §6.4）

DEFAULT_PARAMS: dict = {
    # §6.4 第 1 步：滑窗送审
    "windowSec": 720.0,
    "overlapSec": 90.0,
    # §6.4 第 2 步：边界吸附
    "snapToleranceSec": 18.0,
    # §6.4 第 8 步 / D6：时长区间（v3.31 起由**内容类型**推导，story=通用、commerce=带货）
    # v3.31：录制时长已变成「几分钟到几十秒」，35/45s 的下限会让短素材一条都切不出来。
    # 下放到 15/20s 后短素材也能切；实际时长由 LLM 按内容自然长度决定（不硬凑、不拉长）。
    "storyMinSec": 15.0,
    "storyMaxSec": 120.0,
    "commerceMinSec": 20.0,
    "commerceMaxSec": 120.0,
    # §6.4 第 12 步：保底出片
    # v3.31：既然走 AI 提炼，条数由内容决定，不再硬凑 8 条；默认只保 1 条
    #（素材本身不够长时仍**不硬凑**，如实写进 notes）。
    "minCandidates": 1,
    # v3.21：LLM 语义切点的「抢救线」。当**全部** LLM 候选都被 minSec 砍掉时，
    # 与其让兜底补一条超长整段，不如保留 LLM 的切点（短的好片段比超长整段好发）。
    # 0 = 关闭（退化为 v3.20 及以前的「宁可兜底整段」行为）。
    # v3.31：配合下限下放，抢救线降到 12s，让 12–15s 的短片段也能被保留。
    "llmSalvageMinSec": 12.0,
    # 规则规划器的切分灵敏度
    "silenceBoundarySec": 1.2,      # 句间停顿 ≥ 此值 → 视为候选边界
    "noveltyDropRatio": 0.42,       # 词面新颖度跌破此比例 → 视为话题切换
    "noveltyBlockSec": 60.0,        # 计算新颖度时的滑窗块长
    # 钩子（§6.5）
    "hookMinSec": 3.0,
    "hookMaxSec": 12.0,
}

def params_for(total_sec: float) -> dict:
    """v4.69 P1-B1：**参数随素材自适应**（方案《直播切片优化方案》主线 B）。

    固定参数对 3 分钟素材和 6 小时素材一视同仁是短板（autoclip 的 quality.py 按
    DurationProfile 分档，注释里记录过真实后果："5 分钟视频切出 3×2 分钟"）。
    数值按短视频场景重定，**不是照抄 autoclip 的播客档**。

    返回该档位的 ``windowSec / overlapSec / suggestLo / suggestHi / maxCandidates / minSec / maxSec / tier``。
    ⚠️ 调用方（slice_transcript）**只采用 window/overlap/建议条数/上限**四类；
    ``minSec/maxSec`` 仅供参考（实际时长带由**内容类型**决定 —— commerce 下限 20s
    不能被素材档位的 15s 压掉，那是内容语义不是素材长度问题）。
    """
    if total_sec < 10 * 60:
        return {"windowSec": 300.0, "overlapSec": 40.0,
                "minSec": 15.0, "maxSec": 90.0,
                "suggestLo": 1, "suggestHi": 3, "maxCandidates": 6,
                "tier": "short"}
    if total_sec < 60 * 60:
        return {"windowSec": 600.0, "overlapSec": 75.0,
                "minSec": 15.0, "maxSec": 120.0,
                "suggestLo": 3, "suggestHi": 8, "maxCandidates": 10,
                "tier": "mid"}
    return {"windowSec": 720.0, "overlapSec": 90.0,
            "minSec": 15.0, "maxSec": 120.0,
            "suggestLo": 5, "suggestHi": 15, "maxCandidates": 15,
            "tier": "long"}


MODE_STORY = "story"
MODE_COMMERCE = "commerce"


def resolve_params(mode: str = MODE_STORY, overrides: dict | None = None,
                   content_type: str | None = None) -> dict:
    """取参数：默认参数 + 模式 → 落到统一的 ``minSec`` / ``maxSec``。

    ``content_type``（v3.19）决定用哪套 prompt 与三维权重，取值见
    :data:`app.slicer.prompts.ALL_TYPES` 再加 ``auto``（本地识别）：

    - ``None``（**旧调用方兼容路径**）：按 ``mode`` 推导 —— story→general、commerce→commerce。
      这条路径下权重与旧版完全一致，所以历史用例的数值口径不变；
    - ``"auto"``：由 :func:`app.slicer.prompts.detect_content_type` 按转写内容判定；
    - 具体类型：直接采用。
    """
    params = dict(DEFAULT_PARAMS)
    if overrides:
        params.update({k: v for k, v in overrides.items() if v is not None})
    params["mode"] = MODE_STORY if mode != MODE_COMMERCE else MODE_COMMERCE

    from . import prompts
    if content_type is None:
        # 旧调用方兼容路径：按 mode 推导 —— story→general（与旧版 prompt/权重等价，
        # 历史用例数值不变）、commerce→commerce。
        content_type = "commerce" if params["mode"] == MODE_COMMERCE else "general"
    params["contentType"] = content_type
    params["typeConfidence"] = 1.0 if content_type != "auto" else 0.0
    params["typeEvidence"] = []
    # v3.31：时长区间由**内容类型**推导（切片页已删故事/带货模式，content_type 才是选择入口）。
    # auto 还没识别出具体类型，先按 mode 口径给，识别后在 slice_transcript 里用同一函数校准。
    apply_duration_band(params, content_type, fallback_mode=params["mode"])
    return params


def apply_duration_band(params: dict, content_type: str,
                        fallback_mode: str = MODE_STORY) -> None:
    """按内容类型落 ``minSec`` / ``maxSec``：带货用带货区间，其余用通用区间（就地修改）。

    v3.31：UI 删掉了故事/带货模式选择，时长口径改跟内容类型走。``auto`` 尚未识别出具体
    类型，先按 ``fallback_mode``（旧 mode）给；识别出具体类型后在 slice_transcript 里
    再调一次本函数校准。注意：显式覆盖 ``minSec`` 仍会被这里覆盖（与旧版一致——要调时长
    请覆盖 ``storyMinSec`` / ``commerceMinSec``，不要直接给 ``minSec``）。
    """
    if content_type == "commerce":
        params["minSec"] = params["commerceMinSec"]
        params["maxSec"] = params["commerceMaxSec"]
    elif content_type == "auto" and fallback_mode == MODE_COMMERCE:
        params["minSec"] = params["commerceMinSec"]
        params["maxSec"] = params["commerceMaxSec"]
    else:
        params["minSec"] = params["storyMinSec"]
        params["maxSec"] = params["storyMaxSec"]


def weighted_overall(content_type: str, dims: dict) -> float:
    """按内容类型的固定权重重算 overall（**服务端唯一实现**，模型给的 overall 会被覆盖）。

    这样做的理由：模型倾向于"随手给高分"，而排序、发布队列优先级全看 overall，
    一旦被虚高污染，「最该发的那条」就会被挤下去。三维口径仍由模型判断，权重不由它定。
    """
    from . import prompts
    weights = prompts.weights_of(content_type)
    total = 0.0
    for key, weight in weights.items():
        try:
            total += float(dims.get(key) or 0.0) * float(weight)
        except (TypeError, ValueError):
            continue
    return round(max(0.0, min(100.0, total)), 1)


def weights_of_type(content_type: str) -> dict:
    """取某内容类型的三维权重（薄封装，避免调用方各自 import prompts）。"""
    from . import prompts
    return prompts.weights_of(content_type)


# ---------------------------------------------------------------- 词表（本地启发式）

# 停用词：既是关键词抽取的过滤表，也用于判断 2-gram 是否只是虚词组合
STOPWORDS = set(
    "的 了 是 我 你 他 她 它 们 这 那 就 还 也 都 很 好 吧 啊 呢 呀 吗 嘛 哦 呃 嗯 哈 咦 哎"
    " 哇 诶 诶 然后 但是 因为 所以 如果 可是 而且 就是 这个 那个 这些 那些 什么 怎么 怎样"
    " 一个 我们 你们 他们 自己 现在 时候 可以 应该 这样 那样 只是 已经 不是 没有 还有 感觉"
    " 觉得 真的 特别 有点 一下 一样 知道 看到 来了 上去 出来 回来 过去 起来 下来 的话"
    " 一下 一点 什么 多少 干嘛 咋 啥 嘞 咧 咯 嘿 靠 卧槽".split()
)

# 高能/笑点标记（第 9 步「comedy」维度）
COMEDY_MARKERS = [
    "哈哈", "笑死", "卧槽", "我靠", "麻了", "离谱", "绝了", "好家伙", "我的天", "天呐",
    "什么鬼", "无语", "逆天", "抽象", "服了", "绷不住", "笑不活", "绷", "骚", "秀",
    "牛", "牛逼", "顶", "啊这", "哎呦", "emmm", "??", "？？",
]
# 干货标记（第 9 步「value」维度）
VALUE_MARKERS = [
    "第一", "第二", "第三", "首先", "其次", "最后", "方法", "技巧", "关键是", "重点",
    "注意", "建议", "记得", "千万", "一定要", "原理", "其实", "为什么", "怎么", "原因是",
    "因为", "所以", "数据", "经验", "教训", "总结", "记住",
]
# 互动/弹幕/连麦标记（第 5、6 步）
INTERACT_MARKERS = ["弹幕", "评论区", "有人问", "有人说", "有人", "连麦", "上麦", "接电话",
                    "电话", "连一下", "连线", "连线"]
# 话题收束标记（用于「completeness」判断结尾是否有终止感）
CLOSING_MARKERS = ["就这样", "所以", "总之", "好了", "结束了", "下一把", "再来", "总结一下"]

_NUM_RE = re.compile(r"\d")
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
_SENT_TAIL_RE = re.compile(r"[呢吧啊呀嘛哦哈啦咯嘞诶]+$")

# 敏感词（§6.4 第 11 步）：**双关词 + 白名单**。命中但不命中白名单才算风险。
# 白名单存在的意义就是「撸铁」「操作」这类词不该被误伤。
SENSITIVE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (r"撸", ("撸铁", "撸猫", "撸串", "撸狗", "撸游戏", "撸代码")),
    (r"操", ("操作", "操场", "体操", "操练", "操心", "情操", "操盘", "操刀")),
    (r"靠", ("靠谱", "依靠", "靠山", "可靠", "靠近", "靠前", "靠着", "投靠")),
    (r"干", ("干活", "干嘛", "干净", "干货", "干扰", "能干", "实干", "干杯")),
    (r"睡", ("睡衣", "睡眠", "睡觉")),
    (r"骚", ("骚扰", "骚动")),
]
# 明确要脱敏的词：**只放真正需要规避的**。游戏直播里「卧槽」「我靠」「麻了」出现频率极高，
# 若一并脱敏会让标题几乎全是星号、反而失去可读性，因此它们只作为笑点标记（COMEDY_MARKERS），
# 不做脱敏。要调整口径改这里一处即可。
BLOCK_WORDS = ("他妈", "妈的", "尼玛", "傻逼", "傻比", "辣鸡")


# ---------------------------------------------------------------- 工具

def _clip_text(segments: list[dict], start: float, end: float) -> str:
    """取出 [start, end) 区间内的转写文本（拼接）。"""
    parts = [s["text"] for s in segments if s["end"] > start and s["start"] < end]
    return "".join(parts)


def _seg_indices(segments: list[dict], start: float, end: float) -> list[int]:
    return [i for i, s in enumerate(segments) if s["end"] > start and s["start"] < end]


def _bigrams(text: str) -> set[str]:
    """字符 2-gram 集合（去掉纯标点与纯停用词）。"""
    cleaned = _PUNCT_RE.sub("", text)
    out: set[str] = set()
    for i in range(len(cleaned) - 1):
        g = cleaned[i:i + 2]
        if g in STOPWORDS:
            continue
        if all(ch in STOPWORDS for ch in g):
            continue
        out.add(g)
    return out


def _density(text: str, duration: float) -> float:
    """语速（字/秒）。停顿多 → 密度低。"""
    return len(_PUNCT_RE.sub("", text)) / max(duration, 1e-6)


def _marker_hits(text: str, markers: list[str]) -> int:
    return sum(text.count(m) for m in markers)


def _band_label(duration: float) -> str:
    for lo, hi in ((15, 30), (30, 60), (60, 90), (90, 120)):
        if duration <= hi:
            return f"{lo}-{hi}s"
    return "120s+"


# ---------------------------------------------------------------- I1：滑窗与吸附

def build_windows(segments: list[dict], window_sec: float,
                  overlap_sec: float) -> list[dict]:
    """§6.4 第 1 步：把转写按 ``window_sec`` 切窗，相邻窗重叠 ``overlap_sec``。

    返回 ``[{index, start, end, segments:[...]}, ...]``。窗口数 = ``ceil((total-overlap)/(window-overlap))``。
    最后一窗的 ``end`` **截到素材实际结束**（不虚增）。
    """
    if not segments:
        return []
    total = max(s["end"] for s in segments)
    step = max(window_sec - overlap_sec, 1.0)
    windows: list[dict] = []
    start = 0.0
    index = 0
    while start < total - 1e-6:
        end = min(start + window_sec, total)
        picked = [s for s in segments if s["end"] > start and s["start"] < end]
        windows.append({
            "index": index,
            "start": round(start, 3),
            "end": round(end, 3),
            "segments": picked,
        })
        index += 1
        if end >= total - 1e-6:
            break
        start += step
    return windows


def sentence_edges(segments: list[dict]) -> list[float]:
    """所有**句边界**（每句的 start 与 end），升序去重。"""
    return sorted({round(s["start"], 3) for s in segments}
                  | {round(s["end"], 3) for s in segments})


def edge_at_or_before(t: float, edges: list[float]) -> float | None:
    """不超过 ``t`` 的最大句边界（**向内**吸附：保证不越界）。"""
    lo = [e for e in edges if e <= t + 1e-6]
    return lo[-1] if lo else None


def edge_at_or_after(t: float, edges: list[float]) -> float | None:
    """不小于 ``t`` 的最小句边界。"""
    hi = [e for e in edges if e >= t - 1e-6]
    return hi[0] if hi else None


def snap_to_sentence(t: float, segments: list[dict],
                     tolerance: float = 18.0,
                     stats: list | None = None) -> tuple[float, bool]:
    """§6.4 第 2 步：把时间点吸附到**最近的转写句边界**。

    句边界 = 每句的 ``start`` 与 ``end``。容差内取最近的；**都不在容差内就原样返回**
    （不强行吸附，避免把边界拽偏十几秒反而切坏故事）。

    v4.69 P2-A（方案主线 A 第一步）：``stats`` 传列表时把**实际吸附偏移**（|snapped - t|）
    记进去 —— 只加统计不改行为，攒够 20 场真实素材按 p90 决定 18 秒容差去留（P5）。
    只在**真的吸附了**时记录；没吸附（原样返回）不记，否则会把"本来就在边界上"混进来。
    """
    if not segments:
        return t, False
    edges = sentence_edges(segments)
    best_edge, best_dist = t, float("inf")
    for e in edges:
        d = abs(e - t)
        if d < best_dist:
            best_edge, best_dist = e, d
    if best_dist <= tolerance:
        if stats is not None:
            stats.append(round(best_dist, 3))
        return round(best_edge, 3), True
    return t, False


# ---------------------------------------------------------------- I2-I3：时长与去重

def split_long_candidates(cands: list[dict], segments: list[dict],
                          max_sec: float) -> list[dict]:
    """超长候选在**句边界**上切成多段（每段 ≥ 最短时长的前提下尽量均分）。"""
    out: list[dict] = []
    for c in cands:
        # v3.132：``role == "whole"`` 是**一整局**（用户备注要的），本就该长，
        # 不许被 maxSec 切开 —— 否则"一整局 + 高光"里的"整局"永远出不来。
        if c.get("role") == "whole":
            out.append(c)
            continue
        duration = c["end"] - c["start"]
        if duration <= max_sec + 1e-6:
            out.append(c)
            continue
        parts = max(2, math.ceil(duration / max_sec))
        chunk = duration / parts
        cursor = c["start"]
        for i in range(parts):
            seg_start = c["start"] if i == 0 else snap_to_sentence(cursor, segments)[0]
            target_end = c["start"] + chunk * (i + 1)
            seg_end = (c["end"] if i == parts - 1
                       else snap_to_sentence(target_end, segments)[0])
            # 夹回原候选范围内：吸附可能轻微外扩，越界会破坏「不重叠」不变量
            seg_start = max(c["start"], min(seg_start, c["end"]))
            seg_end = max(c["start"], min(seg_end, c["end"]))
            if seg_end - seg_start < 1.0:      # 退化段直接丢弃
                cursor = target_end
                continue
            piece = dict(c)
            piece["start"] = round(seg_start, 3)
            piece["end"] = round(seg_end, 3)
            piece["splitFrom"] = c.get("splitFrom") or round(c["start"], 3)
            piece.setdefault("notes", []).append("长候选按句边界切分")
            out.append(piece)
            cursor = seg_end
    return out


def enforce_min_duration(cands: list[dict], min_sec: float,
                         max_sec: float) -> tuple[list[dict], list[dict]]:
    """低于下限的候选：**能合并就合并，不能就丢弃**（D6「低于下限直接丢弃」）。"""
    if not cands:
        return [], []
    ordered = sorted(cands, key=lambda c: c["start"])
    kept: list[dict] = []
    rejected: list[dict] = []
    for c in ordered:
        if c["end"] - c["start"] >= min_sec - 1e-6:
            kept.append(c)
            continue
        # 尝试并进前一个（并后仍不超上限）
        if kept:
            prev = kept[-1]
            merged_len = c["end"] - prev["start"]
            gap = c["start"] - prev["end"]
            if c["start"] <= prev["end"] + 1e-6 or gap <= 3.0:
                if merged_len <= max_sec + 1e-6:
                    prev["end"] = max(prev["end"], c["end"])
                    prev.setdefault("notes", []).append("过短候选已并入前一段")
                    continue
        rejected.append({"start": c["start"], "end": c["end"],
                         "reason": f"时长 {c['end'] - c['start']:.1f}s 低于下限 {min_sec:.0f}s"})
    return kept, rejected


def dedupe_overlap(cands: list[dict], iou_threshold: float = 0.35
                   ) -> tuple[list[dict], list[dict]]:
    """重叠去重（I3）：IoU 超阈值时保留 ``overall`` 更高的那个。"""
    kept: list[dict] = []
    rejected: list[dict] = []
    for c in sorted(cands, key=lambda x: -_overall(x)):
        dup = None
        for k in kept:
            inter = min(c["end"], k["end"]) - max(c["start"], k["start"])
            if inter <= 0:
                continue
            union = max(c["end"], k["end"]) - min(c["start"], k["start"])
            if union > 0 and inter / union >= iou_threshold:
                dup = k
                break
        if dup is None:
            kept.append(c)
        else:
            rejected.append({"start": c["start"], "end": c["end"],
                             "reason": f"与 {dup['start']:.1f}–{dup['end']:.1f}s 高度重叠且评分更低"})
    kept.sort(key=lambda x: x["start"])
    return kept, rejected


def _overall(c: dict) -> float:
    scores = c.get("scores") or {}
    try:
        return float(scores.get("overall") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- I4：四维评分（本地启发式）

def score_candidate(segments: list[dict], start: float, end: float,
                    baseline_density: float,
                    min_sec: float = 35.0, max_sec: float = 120.0) -> dict:
    """本地四维评分（0–100）。**只用于 local_fallback**；LLM 路径用自己的分数。

    四维口径（对应 clips 表）：
    - ``value`` 干货：语速相对基线 + 干货标记密度 + 数字出现；
    - ``comedy`` 笑点：笑点标记密度 + 感叹/问号密度；
    - ``completeness`` 完整度：首句不像半句 + 结尾有收束感 + 内部大停顿少 + 时长落在甜区；
    - ``overall`` 加权（value .40 / comedy .25 / completeness .35）。

    v3.31：时长「甜区」``[min_sec, max_sec]`` 由调用方按**当前配置区间**传入
    （默认 35–120 是旧口径，仅为兼容直接调用本函数的测试）；这样短素材按内容切出的
    15–35s 片段不会再被误判成「不完整」而压分。
    """
    duration = max(end - start, 1e-6)
    text = _clip_text(segments, start, end)
    idxs = _seg_indices(segments, start, end)
    density = _density(text, duration)
    density_ratio = density / max(baseline_density, 1e-6)

    value = 45.0
    value += min(25.0, (density_ratio - 1.0) * 22.0)
    value += min(20.0, _marker_hits(text, VALUE_MARKERS) * 3.2)
    value += min(10.0, len(_NUM_RE.findall(text)) * 1.2)

    comedy = 30.0
    comedy += min(45.0, _marker_hits(text, COMEDY_MARKERS) * 7.5)
    comedy += min(15.0, (text.count("!") + text.count("！")) * 3.0)
    comedy += min(10.0, (text.count("?") + text.count("？")) * 1.5)
    # v4.2 情绪雷达：候选内有 [高能] 句（音量全片前 10%）→ 爆点大概率在这里，
    # 本地兜底路径没有 LLM 帮忙看，这一条尤其重要。
    loud_top = max((float(segments[i].get("_loud") or 0) for i in idxs), default=0.0)
    if loud_top >= 0.90:
        comedy += 12.0
    elif loud_top >= 0.75:
        comedy += 6.0

    completeness = 55.0
    first = segments[idxs[0]]["text"] if idxs else ""
    last = segments[idxs[-1]]["text"] if idxs else ""
    if first[:1] in "这那他就还也我你":
        completeness -= 8.0                      # 以虚词/代词开头 → 像半句话
    if any(m in last for m in CLOSING_MARKERS) or last.endswith(("。", "！", "？", ".", "!", "?")):
        completeness += 12.0
    # 内部大停顿占比高 → 节奏碎
    gaps = 0.0
    for a, b in zip(idxs, idxs[1:]):
        gaps += max(0.0, segments[b]["start"] - segments[a]["end"])
    if duration > 0:
        completeness -= min(25.0, (gaps / duration) * 45.0)
    if min_sec <= duration <= max_sec:
        completeness += 12.0
    elif duration < min_sec:
        completeness -= 10.0

    def clamp(v: float) -> float:
        return round(max(0.0, min(100.0, v)), 1)

    dims = {"value": clamp(value), "comedy": clamp(comedy),
            "completeness": clamp(completeness)}
    dims["overall"] = clamp(dims["value"] * 0.40 + dims["comedy"] * 0.25
                            + dims["completeness"] * 0.35)
    return dims


# ---------------------------------------------------------------- I5：钩子挖掘

def find_hook(segments: list[dict], start: float, end: float,
              min_sec: float = 3.0, max_sec: float = 12.0) -> dict | None:
    """§6.4 第 7 步：在候选内找**能量最高**的 3–12 s 片段，作为前置钩子。"""
    idxs = _seg_indices(segments, start, end)
    if not idxs:
        return None
    best: tuple[float, float, float] | None = None   # (score, s, e)
    for i in idxs:
        s0 = max(segments[i]["start"], start)
        for j in idxs:
            if j < i:
                continue
            e0 = min(segments[j]["end"], end)
            dur = e0 - s0
            if dur < min_sec - 1e-6:
                continue
            if dur > max_sec + 1e-6:
                continue
            text = _clip_text(segments, s0, e0)
            score = (_marker_hits(text, COMEDY_MARKERS) * 3.0
                     + _marker_hits(text, VALUE_MARKERS) * 1.2
                     + _density(text, dur) * 0.8)
            if best is None or score > best[0]:
                best = (score, s0, e0)
    if best is None or best[0] <= 0:
        # 没有标记也要给钩子：取候选前 min_sec 秒
        # v3.140：起止都要**夹回候选内** —— 候选起点可能落在某句中间（吸附保底），
        # 不夹的话 hook.startMs 会越出候选（I5）。
        idx0 = idxs[0]
        s0 = max(segments[idx0]["start"], start)
        e0 = min(max(s0 + min_sec, start + min_sec), end)
        text = _clip_text(segments, s0, e0)
        return {"startMs": int(s0 * 1000), "endMs": int(e0 * 1000),
                "text": text[:60], "copiedToStart": True, "energyScore": 0.0}
    _, s0, e0 = best
    return {"startMs": int(s0 * 1000), "endMs": int(e0 * 1000),
            "text": _clip_text(segments, s0, e0)[:60],
            "copiedToStart": True, "energyScore": round(best[0], 2)}


# ---------------------------------------------------------------- I6：敏感词

def detect_sensitive(text: str) -> list[dict]:
    """本地正则双关词检测（**含白名单**）。返回命中列表；白名单命中的记为 ``whitelisted``。"""
    hits: list[dict] = []
    for pattern, whitelist in SENSITIVE_PATTERNS:
        for m in re.finditer(pattern, text):
            token = m.group(0)
            context = text[max(0, m.start() - 2):m.start() + 4]
            wl = next((w for w in whitelist if w in context), "")
            hits.append({
                "word": token,
                "position": m.start(),
                "whitelisted": bool(wl),
                "matchedWhitelist": wl,
                "rule": pattern,
            })
    return hits


def sanitize_title(text: str) -> tuple[str, list[str]]:
    """标题脱敏：命中 ``BLOCK_WORDS`` 的替换为等长星号。返回 (新文本, 命中词)。"""
    masked: list[str] = []
    out = text
    for w in BLOCK_WORDS:
        if w in out:
            out = out.replace(w, "*" * len(w))
            masked.append(w)
    return out, masked


# ---------------------------------------------------------------- 文案生成

# 单字虚词 + 高频通用动词（由 STOPWORDS 单字条目再补一批）。
# 2-gram 只要沾上其中一个，几乎肯定是**跨词边界的碎片**：
# 「你们看」→「们看」、「给我」→「我给」、「一天用」→「天用」、「不建议」→「不建」。
# 拿这种碎片当抖音标签是真机实测出来的问题（K53），所以宁可少给，也不要给垃圾。
_FUNC_CHARS = ({w for w in STOPWORDS if len(w) == 1}
               | set("不没别被把让从向到过再又才只太更最用做去来上说看给拿放"))
# 数词/量词单字：「一次」「一百」同样是碎片，且与内容无关
_NUM_CHARS = set("一二三四五六七八九十百千万零0123456789")


def _keywords(text: str, top: int = 6) -> list[str]:
    """字符 2-gram 词频取 top（本地兜底用；LLM 路径用模型给的真标签）。

    过滤口径见 :data:`_FUNC_CHARS`：宁可少给几个词，也不要给跨词碎片。
    剩下的词太少时，由 :func:`build_copy` 用**内容类型的默认标签**补齐。
    """
    cleaned = _PUNCT_RE.sub("", text)
    freq: dict[str, int] = {}
    for i in range(len(cleaned) - 1):
        g = cleaned[i:i + 2]
        if g in STOPWORDS or _FUNC_CHARS & set(g) or _NUM_CHARS & set(g):
            continue
        if len(set(g)) == 1:          # 「哈哈」「噢噢」这类重复字不算关键词
            continue
        freq[g] = freq.get(g, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1] * len(kv[0]), kv[0]))
    return [w for w, n in ranked if n >= 2][:top]


def build_copy(segments: list[dict], start: float, end: float,
               scores: dict, category: str, content_type: str | None = None) -> dict:
    """生成标题 / 摘要 / 标签 / 剪辑建议 / 流量等级 / 风险（§6.4 第 9–10 步）。

    ``content_type``（v3.21）：本地抽不出足够的关键词时，用它取
    :func:`app.slicer.prompts.default_tags` 兜底 —— 宁可给这个类型的通用标签，
    也不要给 2-gram 切出来的碎片（K53）。
    """
    idxs = _seg_indices(segments, start, end)
    text = _clip_text(segments, start, end)
    duration = end - start

    # 标题：取信息量最高的一句（按干货+笑点标记与长度加权），截到 20 字
    best_line, best_score = "", -1.0
    for i in idxs:
        line = segments[i]["text"].strip()
        if len(line) < 6:
            continue
        sc = (_marker_hits(line, VALUE_MARKERS) * 2.0
              + _marker_hits(line, COMEDY_MARKERS) * 2.5
              + min(len(line), 24) * 0.25)
        if sc > best_score:
            best_line, best_score = line, sc
    title = _SENT_TAIL_RE.sub("", best_line)[:20] if best_line else ""
    if not title.strip():
        # v4.29：纯笑声行（「哈哈哈哈哈哈哈哈」）的喜剧标记得分最高，但整行都是
        # 尾语气词 ⇒ 去尾后标题被删空（真实转写 20261027 场 idx22 踩到，I6 不变量）。
        # 退回候选窗口原文的前 20 字（剥掉标点），保证标题非空。
        title = _PUNCT_RE.sub("", text)[:20]
    title, masked = sanitize_title(title)
    if not title.strip():
        title = "精彩片段"        # 最终兜底：标题不许为空（宁可通用，不可空着，K69）

    summary = "".join(segments[i]["text"] for i in idxs[:3])[:80]

    overall = float(scores.get("overall") or 0)
    if overall >= 85:
        traffic, reason = "high", f"综合 {overall} 分，多重信号叠加（标记密度与语速均高于均值）"
    elif overall >= 70:
        traffic, reason = "medium", f"综合 {overall} 分，单项突出但整体平稳"
    else:
        traffic, reason = "low", f"综合 {overall} 分，信号偏弱，建议人工复核"

    tips: list[str] = []
    hook = find_hook(segments, start, end)
    if hook and hook.get("energyScore"):
        tips.append(f"开头 {int((hook['endMs'] - hook['startMs']) / 1000)}s 有高能片段，建议前置")
    if duration < 45:
        tips.append("时长偏短，可考虑与相邻候选合并以留住完播率")
    if duration > 90:
        tips.append("时长偏长，建议压缩中段停顿")
    if scores.get("completeness", 0) < 60:
        tips.append("完整性偏低，开头可能缺少前文语境，建议加一句引导字幕")
    if category == "互动":
        tips.append("互动段落，建议保留口播原声、少加背景音")
    elif category == "干货":
        tips.append("干货段落，建议加关键词字幕条强化记忆点")
    if masked:
        tips.append("标题含敏感词已脱敏，请人工复核")
    if not tips:
        tips.append("结构完整，可直接进入剪辑")

    risk = ""
    if masked:
        risk = "标题命中敏感词（已脱敏）"
    elif scores.get("completeness", 0) < 55:
        risk = "开头依赖前文语境，独立观看可能看不懂"
    elif duration < 40:
        risk = "时长接近下限，完播率可能受损"

    hits = detect_sensitive(text)
    from . import prompts as _prompts

    tags = _keywords(text)
    if len(tags) < 2:                    # 抽不出像样的词 → 用类型默认标签兜底（K53）
        tags = _prompts.default_tags(content_type or "general") + tags
    return {
        "title": title,
        "summary": summary,
        "douyinTags": tags[:6],
        "editingTips": tips,
        "bestDuration": _band_label(duration),
        "trafficLevel": traffic,
        "trafficReason": reason,
        "coreRisk": risk,
        "sensitive": {"hits": [h for h in hits if not h["whitelisted"]],
                      "whitelisted": [h for h in hits if h["whitelisted"]],
                      "maskedTitle": masked},
        "hook": hook,
    }


def classify(segments: list[dict], start: float, end: float) -> str:
    """归类（§6.4 第 5、6 步的本地近似）：互动 / 搞笑 / 干货 / 故事。"""
    text = _clip_text(segments, start, end)
    inter = _marker_hits(text, INTERACT_MARKERS)
    comedy = _marker_hits(text, COMEDY_MARKERS)
    value = _marker_hits(text, VALUE_MARKERS)
    top = max(inter, comedy, value)
    if top == 0:
        return "故事"
    if top == inter:
        return "互动"
    if top == comedy:
        return "搞笑"
    return "干货"


# ---------------------------------------------------------------- local_fallback 规划器

def local_plan(segments: list[dict], params: dict) -> tuple[list[dict], list[dict]]:
    """规则规划器：**不调 LLM** 也能产出候选。

    思路（把 §6.4 的 LLM 判断换成可解释的启发式）：
    1. 找出所有「强边界」= 句间停顿 ≥ ``silenceBoundarySec``；
    2. 再按**词面新颖度**（相邻 60 s 块的 2-gram Jaccard）跌破阈值处补边界 —— 这近似
       「话题切换」，是 LLM 找故事边界的主要依据；
    3. 相邻边界之间的片段即「故事单元」，按需与邻居合并到最短时长；
    4. 逐单元评分、生成文案、挖钩子。
    """
    if not segments:
        return [], []
    min_sec = params["minSec"]
    max_sec = params["maxSec"]
    total = max(s["end"] for s in segments)

    # --- 1) 停顿边界
    # v3.140：起点用**第一句的 start**，不再写死 0.0 —— 直播前几分钟没说话时
    # （如用户这份转写首句在 185.65s），0.0 不在句边界上，会切出一条无语音的假候选
    # （0–120s 全程没人说话，标题为空、也没有钩子）。
    boundaries: set[float] = {round(segments[0]["start"], 3)}
    for a, b in zip(segments, segments[1:]):
        if b["start"] - a["end"] >= params["silenceBoundarySec"]:
            boundaries.add(round(a["end"], 3))
    boundaries.add(round(total, 3))

    # --- 2) 词面新颖度边界（话题切换）
    block = params["noveltyBlockSec"]
    cursor = block
    while cursor < total:
        left = _bigrams(_clip_text(segments, cursor - block, cursor))
        right = _bigrams(_clip_text(segments, cursor, min(cursor + block, total)))
        if left and right:
            jaccard = len(left & right) / max(len(left | right), 1)
            if jaccard < params["noveltyDropRatio"]:
                boundaries.add(snap_to_sentence(cursor, segments,
                                                params["snapToleranceSec"])[0])
        cursor += block
    ordered = sorted(boundaries)

    # --- 3) 形成故事单元，并短则并、长则切
    # v3.140：先把 ≥ silenceBoundarySec 的大空洞**挖掉**（直播前几分钟没说话、
    # 中途长时间无声）。否则单元会横跨空洞（462.78→636.13 中间 73s 无人说话），
    # 长候选切分的落点又吸不到句边界 ⇒ 切出「大部分是静音」的假候选。
    holes = [(round(a["end"], 3), round(b["start"], 3))
             for a, b in zip(segments, segments[1:])
             if b["start"] - a["end"] >= params["silenceBoundarySec"]]

    def _cut_holes(u_start: float, u_end: float) -> list[tuple[float, float]]:
        parts = [(u_start, u_end)]
        for h0, h1 in holes:
            nxt: list[tuple[float, float]] = []
            for p0, p1 in parts:
                if h1 <= p0 or h0 >= p1:          # 洞不在本段里
                    nxt.append((p0, p1))
                    continue
                if p0 < h0:                        # 洞前还剩语音
                    nxt.append((p0, h0))
                if h1 < p1:                        # 洞后还剩语音
                    nxt.append((h1, p1))
            parts = nxt
        return parts

    units: list[dict] = []
    for s0, e0 in zip(ordered, ordered[1:]):
        if e0 - s0 < 1.0:
            continue
        for p0, p1 in _cut_holes(s0, e0):
            if p1 - p0 < 1.0:
                continue
            units.append({"start": p0, "end": p1, "notes": []})
    merged: list[dict] = []
    for u in units:
        if merged and (u["end"] - u["start"] < min_sec):
            prev = merged[-1]
            if (u["end"] - prev["start"]) <= max_sec:
                prev["end"] = u["end"]
                prev["notes"].append("过短单元已并入前一段")
                continue
        merged.append(u)
    # 首段若过短，往并到后面
    if merged and (merged[0]["end"] - merged[0]["start"] < min_sec) and len(merged) > 1:
        merged[1]["start"] = merged[0]["start"]
        merged[1]["notes"].append("首段过短已并入后一段")
        merged.pop(0)
    units = split_long_candidates(merged, segments, max_sec)

    # --- 4) 打分 + 文案
    baseline = _density(_clip_text(segments, 0, total), total)
    cands: list[dict] = []
    rejected: list[dict] = []
    for u in units:
        duration = u["end"] - u["start"]
        if duration < min_sec - 1e-6:
            rejected.append({"start": u["start"], "end": u["end"],
                             "reason": f"时长 {duration:.1f}s 低于下限 {min_sec:.0f}s"})
            continue
        scores = score_candidate(segments, u["start"], u["end"], baseline,
                                 params["minSec"], params["maxSec"])
        category = classify(segments, u["start"], u["end"])
        copy = build_copy(segments, u["start"], u["end"], scores, category,
                          params.get("contentType"))
        cands.append({
            "start": round(u["start"], 3), "end": round(u["end"], 3),
            "durationSec": round(duration, 2),
            "category": category, "scores": scores,
            "notes": u.get("notes", []),
            "plannerSource": "local_fallback",
            **copy,
        })
    return cands, rejected


# ---------------------------------------------------------------- 保底出片（第 12 步）

def fill_to_min(cands: list[dict], segments: list[dict], params: dict
                ) -> list[dict]:
    """候选不足 ``minCandidates`` 时，从未覆盖区域**补切**（§6.4 第 12 步）。

    补切单元一律标注 ``category="兜底"`` 且在剪辑建议里说明来源，便于人工优先复核。
    素材本身不够长时**不硬凑**（有多少给多少），如实反映在 ``notes``。
    """
    target = int(params.get("minCandidates") or 0)
    if target <= 0 or not segments:
        return cands
    min_sec = params["minSec"]
    max_sec = params["maxSec"]
    total = max(s["end"] for s in segments)
    baseline = _density(_clip_text(segments, 0, total), total)

    covered = sorted((c["start"], c["end"]) for c in cands)
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in covered:
        if s - cursor >= min_sec:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if total - cursor >= min_sec:
        gaps.append((cursor, total))

    out = list(cands)
    edges = sentence_edges(segments)
    for g0, g1 in gaps:
        if len(out) >= target:
            break
        # 两端都**向内**吸附（不越出缺口），否则会盖住相邻候选、破坏「不重叠」不变量
        s0 = edge_at_or_after(g0, edges)
        if s0 is None:
            continue
        end = edge_at_or_before(min(g1, s0 + max_sec), edges)
        if end is None or end - s0 < min_sec - 1e-6:
            end = edge_at_or_before(g1, edges)
        if end is None or end - s0 < min_sec - 1e-6:
            continue
        scores = score_candidate(segments, s0, end, baseline,
                                 params["minSec"], params["maxSec"])
        category = "兜底"
        copy = build_copy(segments, s0, end, scores, category, params.get("contentType"))
        copy["editingTips"] = ["兜底补切（非 LLM 选中），建议人工优先复核"] + copy["editingTips"]
        out.append({
            "start": round(s0, 3), "end": round(end, 3),
            "durationSec": round(end - s0, 2),
            "category": category, "scores": scores,
            "notes": ["保底补切"], "plannerSource": "local_fallback",
            **copy,
        })
    return out


# ---------------------------------------------------------------- 后处理（两个 planner 共用）

def finalize_candidates(raw: list[dict], segments: list[dict], params: dict,
                        planner: str,
                        snap_stats: list | None = None,
                        stats_out: dict | None = None,
                        gaps: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """把任意 planner 的原始候选过一遍**不变量**：吸附 → 时长 → 去重 → 补字段。

    v4.69 P2（方案主线 A/D）：
    · ``snap_stats`` 传列表时收集所有**实际吸附偏移**（|吸附后-原始|，秒）——
      先测量，再决定 18 秒容差去留（P5 之前不许动它）；
    · ``stats_out`` 传 dict 时记录**候选在各阶段被吃掉多少**（raw → 吸附 → 时长带 →
      去重 → 兜底），这就是质量报告的主体（改参数后拿两份报告对比，不用肉眼判断）。

    v4.75 P-C（方案《录制模块优化方案》优化项 C）：``gaps`` 是录制报告里的**内容死区**
    （L1 重连真实丢失的时长，相对秒区间）—— 候选跨越任一 gap ⇒ **剔除**并写明原因
    （跨断层的候选必然"话说一半跳了"，模型不知道有断层，降分不够干净，直接剔除）。
    """
    tol = params["snapToleranceSec"]
    gap_list = [(float(g.get("from") or 0), float(g.get("to") or 0))
                for g in (gaps or [])
                if float(g.get("to") or 0) > float(g.get("from") or 0)]
    if stats_out is None:
        stats_out = {}
    stats_out["candidates"] = len(raw)          # 送进不变量的原始候选数
    min_sec = params["minSec"]
    max_sec = params["maxSec"]
    rejected: list[dict] = []

    # I1 吸附
    # v3.140：吸附失败（最近句边界超出容差）**不再原样保留** —— 那会留下根本不在
    # 句边界上的候选（用户这份转写里出现 0-120 / 240-300 这类窗口值）。
    # 改为**向内吸附**：起点取「其后第一条句边界」、终点取「其前最后一条句边界」；
    # 两端夹不出有效区间（整段都落在无语音区）就如实丢弃。
    edges = sentence_edges(segments)
    snapped: list[dict] = []
    for c in raw:
        s2, ok_s = snap_to_sentence(float(c["start"]), segments, tol, stats=snap_stats)
        e2, ok_e = snap_to_sentence(float(c["end"]), segments, tol, stats=snap_stats)
        if not ok_s:
            s2 = edge_at_or_after(float(c["start"]), edges)
        if not ok_e:
            e2 = edge_at_or_before(float(c["end"]), edges)
        if s2 is None or e2 is None:
            rejected.append({"start": c["start"], "end": c["end"],
                             "reason": "整段落在无语音区（首尾都吸附不到句边界），已丢弃"})
            continue
        item = dict(c)
        item["start"], item["end"] = round(min(s2, e2), 3), round(max(s2, e2), 3)
        item["snapped"] = bool(ok_s or ok_e)
        if item["end"] - item["start"] < 1.0:
            rejected.append({"start": item["start"], "end": item["end"],
                             "reason": "吸附后时长退化"})
            continue
        snapped.append(item)

    # I1.5（v4.75 P-C）：剔除**跨越内容死区**的候选 —— gap 是 L1 重连真实丢失的时长，
    # 跨过去的候选会出现"话说一半跳了"的断层。重叠 > 1 秒才算跨（边缘擦过不算）。
    stats_out["afterSnap"] = len(snapped)
    if gap_list:
        kept2: list[dict] = []
        for c in snapped:
            cs = float(c.get("start") or 0)
            ce = float(c.get("end") or 0)
            cross_sec = max((min(ce, gt) - max(cs, gf)) for gf, gt in gap_list)
            if cross_sec > 1.0:
                rejected.append({"start": cs, "end": ce,
                                 "reason": f"跨越录制断层（内容缺失 {cross_sec:.1f}s）——"
                                           "该区间内说过的话可能跳了，已剔除"})
                continue
            kept2.append(c)
        snapped = kept2
    snapped = split_long_candidates(snapped, segments, max_sec)
    pre_min = list(snapped)
    snapped, rej2 = enforce_min_duration(snapped, min_sec, max_sec)
    stats_out["afterDurationBand"] = len(snapped)
    stats_out["droppedBelowMin"] = len(rej2)

    # I2 的**唯一例外**（v3.21）：LLM 已按语义切好，但每一条都够不到 minSec。
    # 这时若照旧全丢，结果会是兜底补一条超长整段 —— 反而更差。所以只要够抢救线就
    # 保留 LLM 的切点，并**显式标注**（belowMin），让人工与下游都看得见（K53）。
    floor = float(params.get("llmSalvageMinSec") or 0)
    if not snapped and pre_min and floor > 0 and planner != "local_fallback":
        keep = [dict(c) for c in pre_min if c["end"] - c["start"] >= floor]
        if keep:
            for c in keep:
                c["belowMin"] = True
                c["notes"] = list(c.get("notes") or []) + [
                    f"时长 {c['end'] - c['start']:.1f}s 低于常规下限 {min_sec:.0f}s，"
                    f"按 LLM 语义切点保留（抢救线 {floor:.0f}s）"]
            snapped = keep
            rejected += [{"start": c["start"], "end": c["end"],
                          "reason": f"时长 {c['end'] - c['start']:.1f}s 低于极限下限 "
                                    f"{floor:.0f}s"}
                         for c in pre_min if c["end"] - c["start"] < floor]
        else:
            rejected += rej2
    else:
        rejected += rej2

    # 补全缺字段（LLM 可能少给）
    baseline = _density(_clip_text(segments, 0, total_of(segments)), total_of(segments) or 1)
    filled: list[dict] = []
    for c in snapped:
        dur = c["end"] - c["start"]
        item = dict(c)
        item["durationSec"] = round(dur, 2)
        item["scores"] = normalize_scores(item.get("scores")
                                         or score_candidate(segments, item["start"],
                                                            item["end"], baseline,
                                                            params["minSec"],
                                                            params["maxSec"]))
        # v3.19：overall **一律按内容类型的固定权重重算**（模型给的只当参考被覆盖），
        # 否则"随手给 90 分"的候选会把真正该发的那条挤下去。
        item["scores"]["overall"] = weighted_overall(params.get("contentType") or "general",
                                                     item["scores"])
        # v4.69 P3：分数来源必须可见（方案主线 C 的硬要求）——没有它，界面上看不出
        # "这条分是兜底的"，违反 K69「不许把兜底值伪装成真值」。
        # LLM 给了四维 → llm；LLM 缺分由本地 score_candidate 补 → local；
        #   （validate_clips_payload 在 LLM 给不出分时已打 fallback，这里原样保留）
        # 本地规划器 / 单元模式 → local。
        item.setdefault("scoreSource",
                        "local" if planner in ("local_fallback", "unit") else "llm")
        item["contentType"] = params.get("contentType") or "general"
        item["scoreWeights"] = weights_of_type(params.get("contentType") or "general")
        item.setdefault("category", classify(segments, item["start"], item["end"]))
        if not item.get("title") or not item.get("summary"):
            copy = build_copy(segments, item["start"], item["end"], item["scores"],
                              item["category"], params.get("contentType"))
            for k, v in copy.items():
                item.setdefault(k, v)
        # 敏感词与钩子必须由本地兜底补齐（不信 LLM 的脱敏）
        item["sensitive"] = item.get("sensitive") or build_copy(
            segments, item["start"], item["end"], item["scores"],
            item["category"], params.get("contentType"))["sensitive"]
        item["title"], masked = sanitize_title(str(item.get("title") or "")[:40])
        if masked:
            item["sensitive"]["maskedTitle"] = masked
        # v3.140：钩子必须**落在候选内**且时长达标 —— LLM 给的/随长候选切分带过来的
        # 钩子可能越界（此前 #8/#9 的钩子在候选外）。无效就按最终边界重找一遍；
        # find_hook 本身把起止夹在候选内，保证 I5。
        _h = item.get("hook")
        _hok = (isinstance(_h, dict) and _h.get("startMs") is not None
                and _h.get("endMs") is not None
                and params["hookMinSec"] - 1e-6
                <= (_h["endMs"] - _h["startMs"]) / 1000.0 <= params["hookMaxSec"] + 1e-6
                and _h["startMs"] / 1000.0 >= item["start"] - 1e-6
                and _h["endMs"] / 1000.0 <= item["end"] + 1e-6)
        if not _hok:
            item["hook"] = find_hook(segments, item["start"], item["end"],
                                     params["hookMinSec"], params["hookMaxSec"]) or {}
        item.setdefault("trafficLevel", "medium")
        item.setdefault("trafficReason", "")
        item.setdefault("bestDuration", _band_label(dur))
        item.setdefault("douyinTags", [])
        item.setdefault("editingTips", [])
        item.setdefault("coreRisk", "")
        item.setdefault("gameVoiceoverSuitable", item["scores"]["overall"] >= 75)
        item.setdefault("suggestedBackgroundTag", "轻快暖场")
        item["plannerSource"] = planner
        filled.append(item)

    # I3 去重
    filled, rej3 = dedupe_overlap(filled)
    rejected += rej3
    stats_out["afterDedupe"] = len(filled)
    stats_out["merged"] = len(rej3)

    # 保底出片
    filled = fill_to_min(filled, segments, params)
    stats_out["afterFillToMin"] = len(filled)

    # 排序 + 编号（评分降序，便于「Live Stream Clips」按优先级展示；同时给 start 供时间轴用）
    filled.sort(key=lambda c: (-_overall(c), c["start"]))
    for i, c in enumerate(filled):
        c["idx"] = i + 1
        c["publishPriority"] = i + 1
    return filled, rejected


def total_of(segments: list[dict]) -> float:
    return max((s["end"] for s in segments), default=0.0)


def normalize_scores(scores: dict) -> dict:
    """四维评分归一：缺项按 overall 估、越界裁剪到 0–100、无量纲（I4）。

    v4.69 P3（方案主线 C，照 autoclip 的 ``_to_score`` 补细）——模型给的分数常见三种脏形态：
      · 非数字 / inf / nan ⇒ 丢（按缺项处理）；
      · 越界（<0 或 >100）⇒ 丢而不是裁剪 —— 987 这种数大概率不是 0–100 分制的分，
        裁到 100 反而把它"洗白"成高分；
      · 0–1 区间 ⇒ ×100（模型有时给 0–1）；0–10 区间 ⇒ ×10（有时给 0–10）。
    """
    def num(v, default: float) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(f):          # nan / inf 一律当缺项
            return default
        if f < 0 or f > 100:
            return default
        if f <= 1.0:
            f *= 100
        elif f <= 10:
            f *= 10
        return max(0.0, min(100.0, f))

    overall = num((scores or {}).get("overall"), 50.0)
    return {
        "value": num((scores or {}).get("value"), overall),
        "comedy": num((scores or {}).get("comedy"), overall),
        "completeness": num((scores or {}).get("completeness"), overall),
        "overall": overall,
    }


# ---------------------------------------------------------------- 对外入口

def _unit_candidates(segments: list[dict], spans: list[dict], note: dict,
                     params: dict, notes_mod) -> list[dict]:
    """把一个「单元」变成候选：**每个单元一条整段** +（太长时）内部 top-2 高光。

    为什么这样切：用户要的是「一整局 + 高光小片段」都要（2026-10-23）。整段直接由
    边界给出（确定性、不可能跨局）；高光只在单元超过 ``highlightIfLonger`` 时才补，
    且用本地评分挑（不花 API 钱），并打 ``role`` 标记供 UI 分开展示。
    """
    preset = notes_mod.UNIT_PRESETS.get(note.get("unit") or "", {})
    hi_after = float(preset.get("highlightIfLonger") or 0)
    min_sec = float(params["minSec"])
    max_sec = float(params["maxSec"])
    total_sec = max([float(x["end"]) for x in segments] or [1.0])
    baseline = _density(_clip_text(segments, 0, total_sec), total_sec) or 1.0

    out: list[dict] = []
    for i, sp in enumerate(spans):
        start, end = float(sp["start"]), float(sp["end"])
        dur = end - start
        if dur < min_sec * 0.6:
            continue
        whole = score_candidate(segments, start, end, baseline, min_sec, max(max_sec, dur))
        out.append({
            "start": round(start, 3), "end": round(end, 3),
            "role": "whole", "unitIndex": i + 1,
            "title": f"第 {i + 1} 场（完整）",
            "summary": (f"按「{preset.get('label') or '单元'}」整段切出，"
                        f"时长 {dur:.0f} 秒"),
            "scores": whole.get("scores") or {},
            "hook": whole.get("hook") or {},
        })
        if not note.get("splitHighlight", True) or hi_after <= 0 or dur < hi_after:
            continue
        # 单元太长 → 再挑 2 个高光小窗口（互不重叠、间隔 ≥ 3 秒）
        win = max(25.0, min(45.0, min_sec))
        step = 10.0
        scored: list[dict] = []
        t = start + (end - start) * 0.12      # 跳过开场
        while t + win < end - (end - start) * 0.08:
            sc = score_candidate(segments, t, t + win, baseline, win * 0.6, win * 1.6)
            scored.append({"start": round(t, 3), "end": round(t + win, 3),
                           "scores": sc.get("scores") or {}, "hook": sc.get("hook") or {},
                           "rank": float(sc.get("overall") or 0)})
            t += step
        scored.sort(key=lambda x: -x["rank"])
        picked: list[dict] = []
        for c in scored:
            if len(picked) >= 2:
                break
            if all(abs(c["start"] - p["start"]) > win * 0.8 for p in picked):
                picked.append(c)
        for j, c in enumerate(picked):
            out.append({
                "start": c["start"], "end": c["end"],
                "role": "highlight", "unitIndex": i + 1,
                "title": f"第 {i + 1} 场 · 高光 {j + 1}",
                "summary": f"第 {i + 1} 场内的局部高光（本地评分 {c['rank']:.0f}）",
                "scores": c["scores"], "hook": c["hook"],
            })
    return out


class SliceCancelled(Exception):
    """v3.159：用户点了「停止」—— 本次切片**不产出**任何候选（不留半成品）。"""


def slice_transcript(transcript: dict, *, planner: str = "auto",
                     mode: str = MODE_STORY,
                     content_type: str | None = None,
                     params: dict | None = None,
                     llm_config=None, transport=None,
                     note: dict | None = None,
                     review_hint: str | None = None,
                     should_cancel=None) -> dict:
    """**切片主入口**：转写产物 → 候选列表。

    ``planner``：``auto``（有 Key 用 LLM，否则本地兜底）/ ``local_fallback`` / ``deepseek``。
    ``content_type``（v3.19）：``None`` 按 ``mode`` 推导（旧行为）、``auto`` 本地识别、
    或直接指定 ``general/game/chat/commerce``。无论走哪条路，最终都过
    :func:`finalize_candidates`，所以不变量一致。
    ``review_hint``（v3.140）：AI 完整性复核的意见文本 —— 「按复核意见重新拆分」时随
    窗口送审（只有 LLM 规划器会读；本地规则规划器忽略它）。**必须在这里进 params**，
    否则 LLM 那条路根本看不到（与 note 同一个坑）。
    """
    segments = list(transcript.get("segments") or [])

    def _ck(when: str = "") -> None:
        """v3.159：每个关键点查一次「停止」；命中就抛，**不落任何文件**。"""
        if should_cancel is not None and should_cancel():
            raise SliceCancelled(f"已停止（{when or '切片过程中'}）")

    _ck("开始前")
    resolved = resolve_params(mode, params, content_type=content_type)
    notes: list[str] = []

    # v4.2（用户「切片内容没有亮点」）：**情绪雷达**——切片送审只有干文本，
    # 而直播的爆点（喊叫/爆笑/欢呼）在转写里经常平淡甚至缺席。这里用本地音频能量
    # （ffmpeg 解 RMS，有缓存）给每句标上 [高能]/[语速↑]，并把全场 Top 峰值段
    # 作为「高光时刻」随窗口送审 —— 模型第一次能看到"哪里在喊"。任何失败静默降级。
    energy_info: dict | None = None
    video_path = str(transcript.get("video") or "")
    if video_path:
        try:
            from . import audio_energy
            segments, energy_info = audio_energy.enrich(video_path, segments)
            if energy_info and energy_info.get("peaks"):
                resolved["_energyPeaks"] = energy_info["peaks"]
                notes.append(f"情绪雷达：识别出 {len(energy_info['peaks'])} 个高光时刻"
                             f"（音频能量峰值，已随送审标注）"
                             + ("，命中能量缓存" if energy_info.get("fromCache") else ""))
        except Exception as exc:                              # noqa: BLE001
            notes.append(f"情绪雷达不可用（不影响切片）：{type(exc).__name__}")

    # v3.132：备注（本场是什么 / 按什么单元切）必须在**送入模型之前**进 params，
    # 否则 LLM 那条路根本看不到用户备注（本轮自己踩的坑：一开始写在 LLM 调用之后）。
    early_unit = ""
    early_boundaries: list[dict] = []
    if note:
        from . import notes as notes_mod
        from . import segment_rules
        note = notes_mod._clean(note)
        resolved["note"] = note
        if note.get("unit") and note["unit"] != "chapter":
            early_unit = note["unit"]
            early_boundaries = segment_rules.detect_boundaries(segments, early_unit)
            resolved["unitBoundaries"] = early_boundaries
            lo, hi = notes_mod.duration_band(note)
            resolved["minSec"], resolved["maxSec"] = lo, hi
    started = time.time()
    rejected_llm: list[dict] = []

    # v3.140：复核意见与 note 同一个坑 —— 必须在**送审之前**进 params
    if review_hint:
        resolved["reviewHint"] = str(review_hint).strip()[:1500]
        notes.append("已按上一版完整性复核意见重新拆分（意见已随窗口送审，"
                     "本地规则规划器不会读它）")

    # v3.19：auto → 本地识别（确定性、不花 API 钱）；识别证据随结果返回给 UI 展示
    detected: dict | None = None
    if resolved.get("contentType") == "auto":
        from . import prompts
        detected = prompts.detect_content_type(segments)
        resolved["contentType"] = detected["type"]
        resolved["typeConfidence"] = detected["confidence"]
        resolved["typeEvidence"] = detected["evidence"]
        # v3.31：识别出具体类型后，按它校准时长区间（带货下限更长，与 resolve_params 同源）
        apply_duration_band(resolved, detected["type"], fallback_mode=resolved["mode"])
        notes.append(
            f"内容类型自动识别为「{prompts.label_of(detected['type'])}」"
            f"（置信度 {detected['confidence']:.2f}"
            + (f"，依据：{'、'.join(detected['evidence'][:5])}" if detected["evidence"] else "")
            + "）")
        if detected["confidence"] < 0.35:
            notes.append("内容类型证据不足，已按「通用口播」口径处理；可在设置里手动指定类型")

    # v4.69 P1-B1（方案主线 B）：**参数随素材自适应** —— 3 分钟素材与 6 小时素材
    # 不再用同一套窗口/条数（autoclip 记录过真实后果："5 分钟视频切出 3×2 分钟"）。
    # 只覆盖 window/overlap/建议条数/数量上限；**min/max 保留类型带**（那是内容语义，
    # commerce 下限 20s 不能被素材档位的 15s 压掉）。用户显式 override 的键也不覆盖。
    total_sec = total_of(segments)
    tier = params_for(total_sec)
    _user_keys = set((params or {}).keys())
    for _k in ("windowSec", "overlapSec", "maxCandidates"):
        if _k not in _user_keys:
            resolved[_k] = tier[_k]
    resolved["_taskParams"] = {"totalSec": round(total_sec, 1), "tier": tier["tier"],
                               "suggestLo": tier["suggestLo"], "suggestHi": tier["suggestHi"],
                               "minSec": resolved["minSec"], "maxSec": resolved["maxSec"]}
    notes.append(f"素材 {total_sec:.0f} 秒（{tier['tier']} 档）：窗口 {tier['windowSec']:.0f}s、"
                 f"建议 {tier['suggestLo']}–{tier['suggestHi']} 条候选、上限 {tier['maxCandidates']} 条"
                 "（参数已随素材长度自适应）")

    # v3.142（用户 2026-10-25「备注太繁琐 / 内容类型重复」）：备注里的 contentType
    # 只用于历史记录展示，**实际口径以本次选的为准**（切片页有工具栏下拉）。
    # 这里对齐 —— 否则 prompt 里会出现「内容类型：game」，system prompt 却按 commerce 写，
    # 模型收到两条互相矛盾的指令（自动识别时也一样）。
    if resolved.get("note"):
        resolved["note"] = dict(resolved["note"])
        resolved["note"]["contentType"] = resolved.get("contentType") or ""

    use_llm = planner in ("auto", "deepseek", "llm")
    if planner == "auto":
        from .llm import load_config
        cfg = llm_config or load_config()
        use_llm = bool(getattr(cfg, "configured", False))
        if not use_llm:
            notes.append("未配置 API Key，已使用本地规则规划器（planner=local_fallback）")
    if planner == "local_fallback":
        use_llm = False

    raw: list[dict] = []
    used = "local_fallback"
    model_name = "local-rules-v1"

    if use_llm:
        from .llm import LLMError, plan_with_llm, PlanCancelled
        cfg = llm_config or None
        _ck("送模型前")
        try:
            raw, rejected_llm, model_name = plan_with_llm(
                segments, resolved, config=cfg, transport=transport,
                should_cancel=should_cancel)
            used = "llm"
            notes.append(f"LLM 规划完成（{model_name}），原始候选 {len(raw)} 条")
        except PlanCancelled:
            # ⚠️ 必须排在通用 except 之前：否则「停止」会被当成 LLM 失败去回退本地规则，
            #    用户点了停止反而继续算 —— 这就是最典型的「假停止」。
            raise SliceCancelled("已停止（送模型途中）")
        except (LLMError, Exception) as exc:      # LLM 任何失败都退回本地，绝不空手而归
            notes.append(f"LLM 规划失败，已回退本地规则规划器：{type(exc).__name__}: {exc}")
            raw, rejected_llm = [], []

    if not raw:
        raw, rejected_llm = local_plan(segments, resolved)
        used = "local_fallback"
        model_name = "local-rules-v1" if not use_llm else model_name + "→fallback"
        if not use_llm:
            notes.append("本地规则规划器产出候选（无 LLM 参与）")

    # ---- v3.132 单元模式：备注说「一场对局 / 一次连麦 / 一个商品」时，按边界切 ----
    unit = early_unit
    boundaries: list[dict] = list(early_boundaries)
    unit_spans: list[dict] = []
    if note and unit:
        from . import notes as notes_mod
        from . import segment_rules
        if True:
            if len(boundaries) >= 2:
                total = max((float(x.get("end") or 0) for x in segments), default=0.0)
                # 单元跨度用 unitMaxSec（一整局 20 分钟也不拆），片段时长带另外管高光
                unit_hi = float(notes_mod.UNIT_PRESETS[unit].get("unitMaxSec") or hi)
                unit_spans = segment_rules.units_from(boundaries, total, unit=unit,
                                                     min_sec=lo, max_sec=unit_hi)
            if unit_spans:
                notes.append(
                    f"按「{notes_mod.UNIT_PRESETS[unit]['label']}」切分：检测到 "
                    f"{len(boundaries)} 个边界（证据：{'、'.join(sorted({b['evidence'] for b in boundaries})[:6])}），"
                    f"划出 {len(unit_spans)} 个单元")
                raw = _unit_candidates(segments, unit_spans, note, resolved, notes_mod)
                used = "unit"
                model_name = ("unit+" + model_name) if model_name else "unit"
            else:
                notes.append(
                    "备注要求按单元切，但转写里没找到足够的单元边界"
                    f"（得到 {len(boundaries)} 个）—— 已退回按内容脉络切，建议补备注或开 OCR 双重验证")

    _ck("收尾前")
    snap_stats: list = []
    finalize_stats: dict = {}
    candidates, rejected = finalize_candidates(raw, segments, resolved, used,
                                               snap_stats=snap_stats,
                                               stats_out=finalize_stats,
                                               gaps=transcript.get("gaps") or [])
    if rejected_llm:
        rejected = list(rejected_llm) + rejected
    # v3.132：严格模式把每条候选的起止吸到最近的单元边界（切点落在结算/开局那一刻）
    if unit != "chapter" and boundaries and (note or {}).get("strict", True):
        from . import segment_rules
        moved = segment_rules.snap_candidates(candidates, boundaries,
                                              tol=float(resolved.get("snapToleranceSec") or 4) * 2)
        if moved:
            notes.append(f"严格模式：{moved} 个切点已吸附到单元边界")

    # v4.69 P1-B3（方案主线 B）：候选数量上限 —— "分高的全留"会让一场 6 小时直播
    # 产出几十条候选，全部渲染 + 进发布队列，人工挑不动。按 overall 降序截断到上限
    #（上限来自素材档位，最低 6 条），截掉几条写进 notes 与质量报告。
    max_cand = int(resolved.get("maxCandidates") or 0)
    truncated = 0
    if max_cand >= 6 and len(candidates) > max_cand:
        candidates.sort(key=_overall, reverse=True)
        truncated = len(candidates) - max_cand
        candidates = candidates[:max_cand]
        candidates.sort(key=lambda c: float(c.get("start") or 0))
        notes.append(f"候选超过本档上限 {max_cand} 条，已按总分截断（丢弃 {truncated} 条低分候选）")

    if len(candidates) < int(resolved.get("minCandidates") or 0):
        notes.append(
            f"候选 {len(candidates)} 条，未达保底目标 {resolved['minCandidates']} 条"
            f"（素材可切区间不足，未硬凑）")

    # v4.69 P2-D（方案主线 D）：**切片质量报告**——"这次切得好不好"第一次有客观记录。
    # autoclip 把质检当一等公民（quality.py 比评分文件大 3 倍）；这份报告能看出
    # 候选在哪一步被吃掉（吸附/时长带/去重/截断），改参数后拿两份对比即可。
    # ⚠️ 本函数保持**纯计算**（模块注释的承诺）——落盘由调用方（/api/slice/run）
    #    写 <stem>.quality.json，与 candidates.json 同生命周期。
    def _p90(vals: list) -> float:
        if not vals:
            return 0.0
        xs = sorted(vals)
        return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]

    fallback_scored = sum(1 for c in candidates
                          if str(c.get("scoreSource") or "") == "fallback")
    local_scored = sum(1 for c in candidates
                       if str(c.get("scoreSource") or "") == "local")
    quality = {
        "totalSec": round(total_sec, 1),
        "tier": tier["tier"],
        "windows": len(build_windows(segments, resolved["windowSec"],
                                     resolved["overlapSec"])),
        "candidates": finalize_stats.get("candidates", 0),
        "afterSnap": finalize_stats.get("afterSnap", 0),
        "afterDurationBand": finalize_stats.get("afterDurationBand", 0),
        "afterDedupe": finalize_stats.get("afterDedupe", 0),
        "afterFillToMin": finalize_stats.get("afterFillToMin", 0),
        "merged": finalize_stats.get("merged", 0),
        "droppedBelowMin": finalize_stats.get("droppedBelowMin", 0),
        "truncated": truncated,
        "finalCandidates": len(candidates),
        "scoreSourceLlm": sum(1 for c in candidates
                              if str(c.get("scoreSource") or "") == "llm"),
        "scoreSourceFallback": fallback_scored,
        "scoreSourceLocal": local_scored,
        "snapOffsetP50": (sorted(snap_stats)[min(len(snap_stats) - 1,
                                                 int(round(0.5 * (len(snap_stats) - 1))))] if snap_stats else 0.0),
        "snapOffsetP90": _p90(snap_stats),
        "snapMax": max(snap_stats) if snap_stats else 0.0,
        "snapSamples": len(snap_stats),
        "llmCalls": int(resolved.get("_llmCalls") or 0),
        "llmFailures": int(resolved.get("_llmFailures") or 0),
        "generatedAt": round(time.time(), 2),
    }

    return {
        "video": transcript.get("video", ""),
        "videoName": transcript.get("videoName", ""),
        "audioSec": transcript.get("audioSec", 0),
        "planner": used,
        "model": model_name,
        "mode": resolved["mode"],
        # v3.19 内容类型：prompt 与三维权重都由它决定；auto 时附识别置信度与证据词
        "contentType": resolved.get("contentType") or "general",
        "typeConfidence": resolved.get("typeConfidence", 1.0),
        "typeEvidence": resolved.get("typeEvidence") or [],
        "scoreWeights": weights_of_type(resolved.get("contentType") or "general"),
        "params": {k: resolved[k] for k in (
            "windowSec", "overlapSec", "snapToleranceSec", "minSec", "maxSec",
            "minCandidates", "hookMinSec", "hookMaxSec")},
        "windowCount": len(build_windows(segments, resolved["windowSec"],
                                        resolved["overlapSec"])),
        "segmentCount": len(segments),
        "candidateCount": len(candidates),
        "generatedAt": round(time.time(), 2),
        "elapsedSec": round(time.time() - started, 3),
        "notes": notes,
        "quality": quality,
        "snapOffsets": snap_stats[-200:],       # 只带尾部 200 个，防止 6 小时素材把 JSON 撑爆
        "unit": unit or "chapter",
        "unitBoundaries": boundaries,
        "unitSpans": unit_spans,
        "note": resolved.get("note") or {},
        "candidates": candidates,
        "rejected": rejected,
    }
