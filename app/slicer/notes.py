#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本场备注（v3.132）：让用户告诉 AI「这场直播是什么内容、按什么单元切」。

**为什么要它**：切片效果好不好，八成取决于「模型知不知道这一场是什么」。原来只有
一个**全局**内容类型偏好（`/api/slice/content-type`），既不能按场次区分，也说不出
「哪个游戏」。用户的原话（2026-10-23）：

  「在录制位置加上这场直播内容是游戏，什么游戏，再根据对应的游戏对局切分，
    如 QQ飞车，就识别一场对局作为拆分……」

所以这里定三件事，**都是唯一出处（K44）**：

1. :data:`UNIT_PRESETS` —— 「切片单元」的**全部差异**（按什么切、默认时长带、
   边界强信号词）。想调规则只改这里，别在 prompts / segment_rules 里再写一遍。
2. :data:`NOTE_FIELDS` —— 备注的字段与校验口径（UI 拿它渲染，后端拿它校验）。
3. :func:`load_note` / :func:`save_note` / :func:`note_of` —— 备注落 **sidecar**：
   ``<源视频>.note.json``（跟着素材走，删素材一起删，重跑切片结果稳定）。

⚠️ 备注只描述**事实与结构**（叫什么、按什么切），不许让模型凭备注编战况 ——
   这条与 ``prompts.HONESTY_RULE`` 同源。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.common.paths import ws

NOTE_SUFFIX = ".note.json"

#: 备注字段（UI 照这个渲染，后端照这个校验）
#:
#: v3.142（用户 2026-10-25「备注太繁琐」＋「箭头这两个功能重复」）：
#:   · ``hideFor``：在哪些入口**不显示**。切片页已经有「内容类型」下拉（那是真正决定
#:     prompt 与权重的口径），备注里再放一个只是重复、还可能自相矛盾 ⇒ 切片页隐藏，
#:     保存时把工具栏选中的值写进备注（历史记录仍完整）。
#:   · ``advanced``：塞进「高级选项（一般不用改）」折叠里，默认收起 —— 日常只填
#:     名称/按什么切/补充说明三项就够。
NOTE_FIELDS: tuple[dict, ...] = (
    {"key": "contentType", "label": "内容类型", "type": "select",
     # v4.68：加 podcast（播客访谈）—— 与 prompts.CONTENT_TYPES 的 key 必须一致（K44）
     "options": ("", "game", "chat", "commerce", "general", "podcast"),
     "help": "决定用哪套切片 prompt；空 = 沿用原来的自动识别",
     "hideFor": ("source",)},
    {"key": "title", "label": "具体名称", "type": "text", "maxLen": 24,
     "help": "写具体是什么：王者荣耀 / QQ飞车 / 某档讲故事连线 / 卖什么品类"},
    {"key": "unit", "label": "按什么切", "type": "select",
     "options": ("chapter", "match", "topic", "item"),
     "help": "一场对局 / 一次连麦 / 一个商品 / 一个完整段落"},
    {"key": "note", "label": "补充说明", "type": "textarea", "maxLen": 200,
     "help": "例如「每局从选英雄开始到水晶爆炸」「第 3 位连线的女生是重点」"},
    {"key": "strict", "label": "严格模式（宁短不跨单元）", "type": "bool",
     "help": "开：切点吸到单元边界，绝不跨局/跨话题；关：允许模型自由取舍",
     "advanced": True},
    {"key": "splitHighlight", "label": "整局太长时另出高光小片段", "type": "bool",
     "help": "开：一整局 + 若干高光（各带标记）；关：只出一整局",
     "advanced": True},
)

#: 「切片单元」的全部差异 —— **唯一出处**（prompts / segment_rules / slicing 都读这里）
UNIT_PRESETS: dict[str, dict] = {
    "chapter": {
        "label": "一个完整段落",
        "hint": "按内容脉络切（原来的做法）",
        "minSec": 20, "maxSec": 90,
        "openWords": [],
        "closeWords": [],
        "highlightIfLonger": 0,          # 0 = 不做"整局+高光"两段式
    },
    "match": {
        "label": "一场对局",
        "hint": "从开局（选人/加载/倒计时）到结算（胜利失败/冲线/名次/再来一局）",
        "minSec": 45, "maxSec": 240,     # 片段时长带（高光/切分用）
        "unitMaxSec": 1200,              # **单元自身**允许的跨度：一整局 MOBA 20 分钟也不拆
        "openWords": [
            "开始匹配", "匹配成功", "进入游戏", "选英雄", "禁英雄", "ban", "pick",
            "加载中", "准备", "倒计时", "三二一", "出发", "开局", "新的一局",
            "第一局", "第二局", "第三局", "再来一局", "下一把", "继续下一局", "重开",
        ],
        "closeWords": [
            "胜利", "失败", "游戏结束", "比赛结束", "结算", "水晶", "基地爆炸",
            "冲线", "终点", "第一名", "第二名", "第三名", "第1名", "第2名", "第3名",
            "完胜", "翻盘", "投降", "GG", "victory", "defeat", "mvp", "评分",
            "本次对局", "本局", "这局结束", "退出了",
        ],
        "highlightIfLonger": 300,        # 超过 5 分钟的对局：额外出高光小片段
    },
    "topic": {
        "label": "一次连麦 / 一个话题",
        "hint": "从连上/开场到挂断/换下一个人，或话题明显切换",
        "minSec": 30, "maxSec": 300,
        "unitMaxSec": 900,
        "openWords": [
            "连麦", "上麦", "接通", "喂", "你好", "在吗", "能听到吗", "下一位",
            "换一个", "接到", "连线", "进来", "欢迎",
        ],
        "closeWords": [
            "挂断", "下麦", "再见", "拜拜", "结束", "下一个", "感谢连线",
            "就这样", "先这样", "不聊了", "挂了啊",
        ],
        "highlightIfLonger": 420,
    },
    "item": {
        "label": "一个商品",
        "hint": "从上架/介绍到讲完卖点、切下一个商品",
        "minSec": 30, "maxSec": 300,
        "unitMaxSec": 600,
        "openWords": ["上架", "上链接", "这个商品", "看一下这款", "接下来这款", "下一款",
                      "小黄车", "一号链接", "二号链接"],
        "closeWords": ["下一个", "卖完了", "没货了", "下架", "换一个", "再来一个",
                       "这款就这些", "拍完这波"],
        "highlightIfLonger": 0,
    },
}

DEFAULT_NOTE: dict = {
    "contentType": "",
    "title": "",
    "unit": "chapter",
    "note": "",
    "strict": True,
    "splitHighlight": True,
}


#: **内置备注预设**（v4.68，用户 2026-11-12）。
#:
#: 需求原话：「这个下拉选择框里面有的都在备注预设里面帮我新增预设，每个类型有帮我备好两份预设，
#: 要注意是不同类型的预设，预设内容也是根据类型变更的」。
#:
#: 设计口径：
#:   · **每个内容类型两条**，一条「整段/主用法」、一条「另一种典型场景」（比如游戏：整局 vs 高光集锦）；
#:   · 每条都带 ``contentType`` ⇒ 选中预设会**同时把类型下拉切过去**（前端 applyNotePick 负责）；
#:   · ``unit`` 用 :data:`UNIT_PRESETS` 的键（chapter/match/topic/item），不另造；
#:   · 内置预设**不可删**（可用**同名保存覆盖**）—— 它们不在 note-presets.json 里，
#:     由 ``web_server._load_note_presets()`` 合并下发，用户删文件也删不掉（见该函数注释）。
BUILTIN_NOTE_PRESETS: tuple[dict, ...] = (
    # ---- 游戏直播
    {"name": "游戏·整局不拆", "note": {
        "contentType": "game", "title": "王者荣耀", "unit": "match",
        "note": "每局从选英雄到水晶爆炸算一整局，不拆开；超过 5 分钟才额外出一段高光。"
                "开局前的排队与加载丢掉。",
        "strict": True, "splitHighlight": True}},
    {"name": "游戏·高光集锦", "note": {
        "contentType": "game", "title": "王者荣耀·高光", "unit": "chapter",
        "note": "只切击杀、反杀、翻盘、名场面，一条 20–45 秒；开头直接放爆点，不要局势铺垫。"
                "跑图、等匹配、沉默操作全部丢掉。",
        "strict": False, "splitHighlight": False}},
    # ---- 聊天 / 连线 / PK
    {"name": "聊天·一次连线", "note": {
        "contentType": "chat", "title": "情感连麦", "unit": "topic",
        "note": "以一次完整连线为单位（接通→讲问题→主播回应→挂断）；保留具体年龄、收入、"
                "家庭关系这些事实；挂断后的无关独白不要。",
        "strict": True, "splitHighlight": False}},
    {"name": "聊天·PK 冲突", "note": {
        "contentType": "chat", "title": "连线 PK", "unit": "topic",
        "note": "只切有冲突、能站队的段落（对喷、甩锅、争论谁对谁错）；PK 计分与喊票过程压掉，"
                "但结论要留。",
        "strict": False, "splitHighlight": True}},
    # ---- 带货直播
    {"name": "带货·单商品", "note": {
        "contentType": "commerce", "title": "美妆单品", "unit": "item",
        "note": "一个商品一段（上架→卖点→成分/实测→价格福利→下单引导），不同规格与 SKU 必须切开；"
                "跨商品的闲聊、抽奖、连麦都丢掉。",
        "strict": True, "splitHighlight": False}},
    {"name": "带货·价格福利", "note": {
        "contentType": "commerce", "title": "福利专场", "unit": "item",
        "note": "抓价格冲击与限时福利（到手价、拍一发、加库存、赠品）；一段里必须同时有价格数字"
                "和下单指令，只有喊麦没有卖点的不要。",
        "strict": False, "splitHighlight": True}},
    # ---- 通用口播
    {"name": "通用·观点干货", "note": {
        "contentType": "general", "title": "口播干货", "unit": "chapter",
        "note": "一个观点一段（结论先行→论据→例子→收束）；寒暄、打招呼、设备调试全部砍掉。"
                "缺结论的碎片不要。",
        "strict": False, "splitHighlight": False}},
    {"name": "通用·故事讲述", "note": {
        "contentType": "general", "title": "口播故事", "unit": "chapter",
        "note": "按故事切：背景→冲突→转折→结局；半句话起头或半句话结尾的片段不要，"
                "重复说过的段落合并。",
        "strict": True, "splitHighlight": False}},
    # ---- 播客访谈（v4.68 新增类型）
    {"name": "播客·嘉宾经历", "note": {
        "contentType": "podcast", "title": "创业访谈", "unit": "chapter",
        "note": "以嘉宾「一段完整经历」为单位（背景→过程→代价→结论）；保留具体数字与时间线，"
                "片头片尾、口播广告、寒暄全丢掉。",
        "strict": True, "splitHighlight": True}},
    {"name": "播客·观点交锋", "note": {
        "contentType": "podcast", "title": "行业对谈", "unit": "chapter",
        "note": "抓观点冲突与反常识判断，一条 40–90 秒；开头用嘉宾原话金句，不要从主持人提问起；"
                "只有提问没有回答的残段不要。",
        "strict": False, "splitHighlight": False}},
)


class NoteError(Exception):
    """备注不合法（message 面向用户）。"""


# ---------------------------------------------------------------- 存储（sidecar）

def note_path_for(video: str | Path) -> Path:
    p = Path(video)
    return p.with_name(p.name + NOTE_SUFFIX)


def _clean(data: dict | None) -> dict:
    out = dict(DEFAULT_NOTE)
    for f in NOTE_FIELDS:
        k = f["key"]
        if not isinstance(data, dict) or k not in data:
            continue
        v = data.get(k)
        if f["type"] == "bool":
            out[k] = bool(v)
        elif f["type"] == "select":
            v = str(v or "")
            out[k] = v if v in f["options"] else DEFAULT_NOTE[k]
        else:
            v = str(v or "").strip()
            out[k] = v[: int(f.get("maxLen") or 200)]
    if out["unit"] not in UNIT_PRESETS:
        out["unit"] = DEFAULT_NOTE["unit"]
    return out


def load_note(video: str | Path) -> dict:
    """读备注；没有就返回默认值（**不报错** —— 没有备注是正常状态）。"""
    p = note_path_for(video)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_NOTE)
    note = _clean(raw)
    note["updatedAt"] = raw.get("updatedAt") or ""
    return note


def save_note(video: str | Path, data: dict) -> dict:
    note = _clean(data)
    note["updatedAt"] = time.strftime("%Y-%m-%d %H:%M")
    p = note_path_for(video)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise NoteError(f"备注保存失败：{exc}") from exc
    return note


def note_of(video_name: str, search_dirs: tuple[Path, ...] = ()) -> dict:
    """按**文件名**找备注（录制历史/切片页拿到的都是名字）。

    查找顺序：给定目录里同名文件 → 工作区源根（``paths.source_roots()``，K44）
    → 候选/成片目录。找不到返回默认值。

    v3.139：源根改走 ``source_roots()``。旧实现 `getattr(ws, "live_dir")` ——
    Workspace 没有这个属性（真名 ``live_source_dir``）⇒ live_raw 被静默跳过，
    「存了备注再读」在直播素材上永远读不到（和 web_server._find_source_by_name
    是同一处笔误的两份拷贝，K44 的教训：同一逻辑写两份，错也是错两份）。
    """
    name = str(video_name or "").strip()
    if not name:
        return dict(DEFAULT_NOTE)
    cands: list[Path] = []
    for d in search_dirs:
        cands.append(Path(d) / name)
    from app.common.paths import source_roots
    for _kind, root in source_roots():
        cands.append(Path(root) / name)
    for attr in ("candidates_dir", "editor_output_dir"):
        root = getattr(ws, attr, None)
        if root:
            cands.append(Path(root) / name)
    for c in cands:
        if note_path_for(c).is_file():
            return load_note(c)
    return dict(DEFAULT_NOTE)


# ---------------------------------------------------------------- 给 prompt 用

def unit_of(note: dict) -> dict:
    return UNIT_PRESETS.get((note or {}).get("unit") or "chapter", UNIT_PRESETS["chapter"])


def duration_band(note: dict) -> tuple[int, int]:
    u = unit_of(note)
    return int(u["minSec"]), int(u["maxSec"])


def prompt_block(note: dict, boundaries: list[dict] | None = None) -> str:
    """把备注拼成 prompt 里的一段（**只讲结构与事实**，不替模型编内容）。"""
    note = _clean(note)
    if not any((note["title"], note["note"], note["contentType"])) and note["unit"] == "chapter":
        return ""
    u = unit_of(note)
    lines = ["【本场事实（用户备注，视为真，但只作背景，不得据此编造战况/数据）】"]
    if note["contentType"]:
        lines.append(f"- 内容类型：{note['contentType']}")
    if note["title"]:
        lines.append(f"- 具体是什么：{note['title']}")
    if note["note"]:
        lines.append(f"- 补充说明：{note['note']}")
    lines.append("")
    lines.append(f"【切片单元：{u['label']}】{u['hint']}")
    lines.append(f"- 单元时长参考：{u['minSec']}–{u['maxSec']} 秒；"
                 f"**一个单元就是一条候选，不要把一个单元切成看不出前因后果的碎片**")
    if note["strict"]:
        lines.append("- **严格模式**：候选的起止必须落在单元边界上（开局/结算/换人），"
                     "**绝不跨两个单元拼接**；边界证据不足时宁可这条不切。")
    if boundaries:
        pts = "、".join(f"{b['t']:.0f}s" for b in boundaries[:40])
        lines.append(f"- 已检测到本场的单元边界（秒）：{pts}"
                     f"（共 {len(boundaries)} 个，这些点不许被候选跨越）")
    return "\n".join(lines)
