#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切片提示词库 + 内容类型识别（M3-B-2 / v3.19）。

**为什么要按内容类型分 prompt**：直播间内容差别极大 —— 游戏区看的是击杀翻车与操作，
聊天连线区看的是冲突与讨论，带货区看的是卖点与转化。同一套「按故事完整性切」的规则
套到游戏上会把整段跑图切进去、套到带货上会把两个商品合成一段。

本模块提供三件事（都是纯函数，无 IO、无网络）：

1. :data:`CONTENT_TYPES` —— 四种内容类型的**全部差异**（角色定位 / 切什么 / 丢什么 /
   分类词表 / 标签种子 / 钩子倾向 / 三维口径 / 加权 / 热度信号）。想调口径**只改这里**，
   不要在别处再写一遍（K44 同源）。
2. :func:`detect_content_type` —— 本地关键词识别（**不花 API 钱、确定性、可断言**），
   返回类型 + 置信度 + 证据词，供「自动」模式与 UI 展示用。
3. :func:`build_system_prompt` —— 按类型拼 system prompt。**JSON 契约字段名与旧版完全一致**
   （clips/rejected/scores/hook/trafficLevel…），所以 clip 表、素材库、发布队列都不用改。

**评分逻辑的优化点**：

- 三维（value/comedy/completeness）**按内容类型给不同的口径与权重**；
- ``overall`` 由**服务端按固定权重重算**（`slicing.weighted_overall`）—— 模型的 overall
  只当参考被覆盖，避免"模型随手给 90 分"污染排序（原版同样是"你返回的 score 只作兼容，
  不要自行改变权重"）；
- 热度只走 ``trafficLevel`` / ``trafficReason`` 两个字段，且 prompt 里**明确禁止**声称
  "抖音真实推流预测/爆款预测"，只能基于字幕事实推断（照原版口径，也符合文档 §11 合规）。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 类型定义

# 每个类型的字段含义：
#   label        UI 展示名
#   role         给模型的角色定位（prompt 第一句）
#   focus        这类内容「什么值得切」
#   avoid        这类内容「什么必须丢掉」
#   categories   category 字段的取值词表（模型只能从这里挑）
#   tagSeeds     抖音标签的取词倾向（给模型参考，不是硬约束）
#   hookStyle    钩子（3–12 s）该长什么样
#   dims         value / comedy / completeness 三个维度在**本类型下**的口径
#   weights      三维权重（和 = 1.0），overall 由服务端按它重算
#   traffic      热度信号清单（trafficReason 必须落在这里面的信号上）
#   markers      本地识别用关键词（越具体越好，见 detect_content_type）
CONTENT_TYPES: dict[str, dict] = {
    # ---------------------------------------------------------- 通用（口播/杂谈）
    "general": {
        "label": "通用口播",
        "role": "抖音竖屏口播短视频的切片策划",
        "focus": [
            "完整讲清一个观点、一件事或一个故事的段落",
            "有明确结论、经验、方法或反常识认知的段落",
        ],
        "avoid": ["寒暄、打招呼、等待、设备调试", "重复说过的话与无信息量的过渡", "半句话起头或半句话结尾"],
        "categories": ["干货", "故事", "互动", "搞笑", "观点"],
        "tagSeeds": ["具体主题词", "人群词", "场景词"],
        "hookStyle": "能独立成立的一句反常识、一个数字或一个强结论；不要用「大家好」这类开场",
        "dims": {
            "value": "干货价值：信息密度、是否可执行、有无具体事实（数字/身份/经历）而不是空泛口号",
            "comedy": "情绪强度：笑点、吐槽、情绪峰值、反转",
            "completeness": "故事完整度：背景→问题→发展→结论；首句不像半句、结尾有收束感",
        },
        "weights": {"value": 0.40, "comedy": 0.25, "completeness": 0.35},
        "traffic": ["前 3 秒能否建立悬念", "信息密度与可执行性", "反常识或争议点", "金句可截图/可引用"],
        "markers": [],
    },
    # ---------------------------------------------------------- 游戏
    "game": {
        "label": "游戏直播",
        "role": "抖音游戏区直播切片的内容主编",
        "focus": [
            "击杀、反杀、一打多、团灭、超神五杀等**操作高光**",
            "翻车、破防、整活、被队友气到、名场面（**这些往往比高光更易传播**）",
            "战术与机制讲解：为什么这么打、这个版本什么强、对新手的实用教学",
            "与观众/队友的强互动冲突（对喷、指挥、甩锅）",
        ],
        "avoid": [
            "跑图、等匹配、读条、沉默操作、无解说的空转",
            "纯报点、重复同一波战术、无信息量的口头禅",
            "一局被切成看不出前因后果的碎片",
        ],
        "categories": ["高光", "翻车", "教学", "名场面", "互动", "搞笑"],
        "tagSeeds": ["具体游戏名", "英雄/角色/枪械名", "模式名", "操作或梗"],
        "hookStyle": "直接把爆点放开头（击杀瞬间/翻车瞬间/一句炸裂台词），**不要铺垫局势**；3 秒内让人知道「发生了什么」",
        "dims": {
            "value": "局面信息量：局势、战术、机制与版本理解、对观众有没有可学的东西",
            "comedy": "爆点强度：击杀/反杀/翻车/破防/整活的冲击力，这是游戏切片的第一驱动力",
            "completeness": "高光闭环：起因→操作→结果讲清楚了；不是只有一下击杀、观众看不懂发生了什么",
        },
        "weights": {"value": 0.25, "comedy": 0.40, "completeness": 0.35},
        "traffic": [
            "前 3 秒是否有视觉/情绪爆点",
            "不看直播的人能不能看懂（脱离上下文仍成立）",
            "是否是可模仿的教学或可反复看的名场面",
            "是否引发争论（谁菜、版本强势、队友问题）",
        ],
        "markers": [
            "五杀", "超神", "击杀", "反杀", "团灭", "打野", "上分", "排位", "连招", "技能",
            "开镜", "爆头", "残血", "丝血", "回城", "推塔", "兵线", "经济", "装备", "版本",
            "英雄", "辅助", "中单", "adc", "gank", "视野", "蹲", "架枪", "拉枪线", "决赛圈",
            "落地", "刚枪", "solo", "狼人杀", "打本", "开黑", "上号", "开游戏", "掉分",
            "对面", "队友", "这把", "这局", "操作", "秀", "菜", "演员", "挂", "外挂",
            # v3.19 真机实测补：这盘真实游戏直播的语音里**没有**任何术语（只有"打架/枪/哈德森"），
            # 结果被误判成"带货"（依据竟是通用词"最后"）。补的是**口语化的游戏词**。
            "打架", "战斗", "火力", "枪", "子弹", "护甲", "包扎", "医疗包", "撤离", "舔包",
            "掩体", "点射", "围殴", "包夹", "蹲草", "偷袭", "突脸", "压枪", "狙击", "冲锋",
            "打团", "上弹", "换弹", "半血", "满血", "血条", "一队", "几个人", "偷人",
            "要不要上", "打不打", "别去", "死了", "杀掉", "爆了", "打中人", "架点",
        ],
    },
    # ---------------------------------------------------------- 聊天 / 连线 / PK
    "chat": {
        "label": "聊天·连线·PK",
        "role": "抖音聊天区（连线/连麦/PK/答疑）直播切片的故事总编",
        "focus": [
            "有**具体事实**的真实处境：年龄、身份、收入、家庭关系、明确事件",
            "有**核心冲突**：现实处境与选择、付出与回报、想要与不能之间的反差",
            "有**讨论空间**：能让人站队、「换成你会怎么选」的话题",
            "完整的一段连线/答疑：背景→问题→主播回应→结论",
        ],
        "avoid": [
            "寒暄、谢礼物、等下一麦、口头禅重复",
            "只有抽象观点或口号、没有具体经历支撑的空谈",
            "挂断之后的无关独白、上下半场都不完整的残片",
        ],
        "categories": ["故事", "冲突", "互动", "观点", "答疑", "搞笑"],
        "tagSeeds": ["话题领域（情感/职场/生活/健康）", "人群（年龄/身份）", "矛盾点"],
        "hookStyle": "把**冲突或反差最大的那句原话**放开头（谁对谁错、付出与回报、一句扎心结论），不要从「大家好」开始",
        "dims": {
            "value": "冲突与讨论潜力：事实是否具体（年龄/收入/身份/事件）+ 矛盾反差是否清楚 + 是否值得站队讨论；泛泛观点要低分",
            "comedy": "情绪与反转：情绪峰值、对话反差、意外反转、笑点；不是所有连线都要好笑，但情绪要真",
            "completeness": "故事闭环：包含完整背景、问题、发展与回应/结论；**完整连线不要因为时长长而扣分**，只有重复、空白、挂断后无关内容才降",
        },
        "weights": {"value": 0.35, "comedy": 0.25, "completeness": 0.40},
        "traffic": [
            "前 3 秒能否建立人物与矛盾",
            "是否存在合理分歧、能不能引发站队",
            "是否有人人可代入的现实压力、责任、遗憾或希望",
            "结尾是否有回应或余味（而不是聊到一半断了）",
        ],
        "markers": [
            "连麦", "上麦", "连线", "下麦", "pk", "PK", "对喷", "开麦", "麦上", "打个pk",
            "兄弟们", "家人们", "老铁", "姐妹们", "榜一", "刷礼物", "礼物", "打赏",
            "我今年", "我老公", "我老婆", "我妈", "我爸", "男朋友", "女朋友", "离婚", "结婚",
            "上班", "工资", "存款", "房贷", "彩礼", "孩子", "上学", "彩礼钱", "相亲",
            "你说", "你说说", "怎么办", "该不该", "有没有道理", "你评评理", "有没有错",
            "弹幕", "评论区", "直播间", "观众", "粉丝", "有人问", "有个朋友",
        ],
    },
    # ---------------------------------------------------------- 带货
    "commerce": {
        "label": "带货直播",
        "role": "抖音带货直播切片（商品场次）的选品编辑",
        "focus": [
            "完整的一次商品销售过程：铺垫（人群/痛点/场景）→ 卖点 → 演示/成分 → 价格优惠 → 下单指令",
            "同一次销售中的卖点、规格、赠品、库存、下单说明必须留在同一段",
            "有**具体数字**的说服链：价格对比、成分含量、销量、保质期、尺码适用范围",
        ],
        "avoid": [
            "跨商品合并：不同商品/型号/规格/SKU 必须切开",
            "闲聊、连麦、抽奖等无关主题（要标 other/丢掉，不要混进商品段）",
            "只有情绪喊麦没有卖点信息、或只有商品名没有讲解过程的碎片",
        ],
        "categories": ["卖点讲解", "价格福利", "演示实测", "使用场景", "下单引导"],
        "tagSeeds": ["商品品类词", "人群/场景", "核心卖点（成分/材质/功效）"],
        "hookStyle": "把**痛点或价格冲击**放开头（一句话说清「谁的问题、便宜多少」），不要先报商品名",
        "dims": {
            "value": "卖点与信任：痛点是否命中、卖点是否具体（成分/材质/数字对比）、是否给了下单理由",
            "comedy": "观看黏性：演示反差、上链接瞬间、库存与福利的紧张感（本类型权重最低，不强求好笑）",
            "completeness": "商品闭环：铺垫→讲解→报价→下单指令是否完整；**缺价格或缺下单引导会明显掉分**",
        },
        "weights": {"value": 0.45, "comedy": 0.15, "completeness": 0.40},
        "traffic": [
            "前 3 秒是否命中痛点或价格冲击",
            "卖点是否具体到可验证（数字、成分、对比）",
            "是否有直播专属的紧迫感（限时/限量/赠品）",
            "是否需要看完整段才懂（完播驱动）",
        ],
        "markers": [
            "下单", "小黄车", "链接", "上链接", "优惠券", "福利", "库存", "秒杀", "包邮",
            "拍一发", "买一送一", "价格", "多少钱", "9块9", "九块九", "活动价", "到手价",
            "成分", "材质", "尺码", "保质期", "运费险", "七天无理由", "正品", "专柜",
            "油皮", "干皮", "敏感肌", "学生党", "宝妈",
            "销量", "回购", "断货", "库存不多", "加库存", "改价",
            # v3.19 真机实测**删掉**了「最后 / 适合 / 人群 / 抢」这类通用词：
            # 一场游戏直播只因为说了句"最后一队"就被判成带货（依据词就是"最后"）。
            # 判定词必须**只有这个类型才会说**，宁少勿滥。
        ],
    },
    # ---------------------------------------------------------- 播客 / 访谈（v4.68）
    #
    # 为什么单开一类（用户 2026-11-12）：「类型下拉新增一个播客访谈的选择」。
    # 它与「聊天·连线」的区别：连线是**陌生人来求助/对喷**，切片看「冲突与站队」；
    # 播客是**嘉宾讲经历/给判断**，切片看「洞察与可摘金句」——两者 prompt 口径不能混。
    "podcast": {
        "label": "播客访谈",
        "role": "播客 / 长访谈节目的切片策划（主持人与嘉宾的对话体）",
        "focus": [
            "嘉宾讲出**一段完整经历或判断**：背景→过程→代价→结论讲清楚（辞职、创业、转行、行业变化）",
            "有**观点冲突或反常识**的片段：与主流说法相反、有具体理由、能引发讨论",
            "有**可摘出的金句**：一句能独立成立、能直接当标题或封面的判断",
            "主持人追问出来的关键信息：数字、时间、代价、失败经验、具体案例",
        ],
        "avoid": [
            "寒暄、自我介绍、片头片尾、口播广告与赞助",
            "只有提问没有回答、或回答被打断的残段",
            "长时间沉默、跑题闲聊、同一观点反复说但没有新增信息",
        ],
        "categories": ["经历", "观点", "行业洞察", "反常识", "金句", "方法"],
        "tagSeeds": ["领域词（职场/创业/科技/文化）", "嘉宾身份", "核心话题词", "争议点"],
        "hookStyle": "把**嘉宾最反常识或最扎心的一句原话**放开头（能直接当封面标题），不要从主持人提问开始",
        "dims": {
            "value": "信息与洞察：是否有具体事实（数字/时间/代价）、是否给出可迁移的判断或方法；空泛感慨要低分",
            "comedy": "情绪与张力：认知冲击、观点交锋、真诚的自嘲或遗憾、意外反转（访谈不必好笑，但要有让人听下去的劲）",
            "completeness": "对话闭环：问题→回答→展开→收束；**长对话不因时长扣分**，但半句起头/半句结尾要降分",
        },
        "weights": {"value": 0.45, "comedy": 0.20, "completeness": 0.35},
        "traffic": [
            "前 3 秒是否有一句能当标题的判断",
            "是否与主流说法相反、能不能引发讨论",
            "是否有人人可代入的处境（职场、钱、选择、遗憾）",
            "结尾是否收在一个完整判断上（而不是话没说完）",
        ],
        "markers": [
            "播客", "访谈", "嘉宾", "主持人", "这期", "上一期", "节目", "对谈", "连线嘉宾",
            "我那时候", "我当时", "后来我", "我的经历", "创业", "辞职", "转行", "行业", "公司",
            "你怎么看", "你觉得", "我想问", "我的观点", "我认为", "我的判断", "说回来", "换个话题",
            "举个例子", "具体来说", "代价", "学费", "亏了", "赚了", "经验", "教训",
            # 判定词纪律同上面几类：只收**这个类型才会说**的，宁少勿滥
        ],
    },
}

# 顺序即 UI 下拉的顺序；general 同时兼作「识别不出来」的兜底
TYPE_ORDER = ("auto", "general", "game", "chat", "commerce", "podcast")
ALL_TYPES = ("general", "game", "chat", "commerce", "podcast")

# 旧版 `slicing.MODE_STORY / MODE_COMMERCE` 的映射（向后兼容，别删）
LEGACY_MODE_TO_TYPE = {"story": "chat", "commerce": "commerce"}

# 「诚实边界」——照原版口径写死，任何类型都不许声称能预测推流
HONESTY_RULE = (
    "热度判断只能基于字幕里的事实做**区间判断**（high/medium/low），"
    "禁止声称这是抖音真实推流预测、爆款预测或确定性流量结论；"
    "也不得只凭标题脑补，必须指出依据的字幕事实。"
)


# ---------------------------------------------------------------- 本地类型识别

_CN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]+")


def _norm(text: str) -> str:
    """小写 + 去掉标点，便于子串匹配（英文标记如 pk/gank/adc 要能命中）。"""
    return re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", str(text).casefold())


def detect_content_type(segments: list[dict], *, sample_chars: int = 12000) -> dict:
    """本地识别直播间内容类型（纯规则、确定性、不花 API 钱）。

    做法：把转写文本归一化后，用每类的 ``markers`` 做**加权计数**（长词权重更高，
    避免「操作」这种单字常用词压过「小黄车」这种强特征词），再按总字符数归一化。

    返回 ``{"type", "confidence", "scores", "evidence"}``：

    - ``type``：识别结果；证据不足时回落 ``general``；
    - ``confidence``：0–1，最高分与次高分的差距 + 绝对命中量共同决定（宁可低置信，
      也不要假装很确定 —— 低置信时 UI 会提示用户手动选择）；
    - ``evidence``：命中最多的几个词（给用户看的依据，**不暴露内部权重**）。
    """
    text = _norm("".join(str(s.get("text") or "") for s in segments)[:sample_chars])
    if not text:
        return {"type": "general", "confidence": 0.0, "scores": {}, "evidence": []}

    scores: dict[str, float] = {}
    hits: dict[str, dict[str, int]] = {}
    for ctype in ALL_TYPES:
        markers = CONTENT_TYPES[ctype]["markers"]
        if not markers:
            continue
        total = 0.0
        detail: dict[str, int] = {}
        for m in markers:
            n = text.count(_norm(m))
            if n:
                # 长特征词更可信：2 字 1.5 分、3 字 2 分、4 字及以上 2.5 分
                weight = 1.0 + min(len(m), 4) * 0.5
                total += n * weight
                detail[m] = n
        scores[ctype] = total
        hits[ctype] = detail

    if not scores or max(scores.values()) <= 0:
        return {"type": "general", "confidence": 0.0, "scores": scores, "evidence": []}

    # 按文本长度归一化：每 1000 字命中多少分（避免长视频天然占优）
    per_kchar = {k: v / max(len(text), 1) * 1000 for k, v in scores.items()}
    ranked = sorted(per_kchar.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score <= 0:
        return {"type": "general", "confidence": 0.0, "scores": per_kchar, "evidence": []}

    # 绝对量够（说明不是偶然命中）＋ 与次高有可见差距，才算有把握
    volume = min(1.0, best_score / 6.0)                       # 命中密度得分
    margin = 1.0 if second <= 0 else max(0.0, (best_score - second) / best_score)
    # v3.19 实测补：光看"密度 + 领先幅度"会**过度自信** —— 一场游戏直播只因为说了句
    # "最后一队"（命中 1 个通用词）就被判成「带货」且置信度 1.00。所以要按
    # **命中了几个不同的特征词**打折：<3 个不同词几乎不算证据。
    distinct = len(hits.get(best, {}))
    evidence_factor = min(1.0, distinct / 3.0)
    confidence = round(max(0.0, min(1.0, (0.55 * volume + 0.45 * margin) * evidence_factor)), 2)

    if confidence < 0.35:
        # 证据弱 → 老实回落通用，别硬猜（硬猜比不猜更糟：prompt 会跑偏）
        return {"type": "general", "confidence": confidence,
                "scores": per_kchar, "evidence": []}

    evidence = [w for w, _ in sorted(hits.get(best, {}).items(),
                                     key=lambda kv: -kv[1])[:8]]
    return {"type": best, "confidence": confidence,
            "scores": per_kchar, "evidence": evidence}


# ---------------------------------------------------------------- prompt 组装

_CONTRACT = """输出 JSON 结构（字段名严格照抄，不要增删）：
{{"clips":[{{"start":秒,"end":秒,"title":"","summary":"","category":"{categories}",\
"scores":{{"value":0,"comedy":0,"completeness":0,"overall":0}},\
"trafficLevel":"high","trafficReason":"","hook":{{"startMs":0,"endMs":0,"text":""}},\
"douyinTags":[""],"keywords":[""],"endingOpen":true,"editingTips":[""],"bestDuration":"60-90s",\
"coreRisk":"","gameVoiceoverSuitable":true,"suggestedBackgroundTag":"轻快暖场"}}],\
"rejected":[{{"start":0,"end":0,"reason":""}}]}}"""


#: v4.2 情绪信号：句级标注 → 送审行首标记（唯一出处，别在别处再写阈值）
MARK_LOUD, MARK_WARM, MARK_RATE = "[高能]", "[情绪↑]", "[语速↑]"


def row_mark(s: dict) -> str:
    """一句转写的情绪标记（来自 audio_energy.annotate 的 _loud/_rate 分位）。"""
    marks = ""
    loud = float(s.get("_loud") or 0)
    if loud >= 0.90:
        marks += MARK_LOUD
    elif loud >= 0.75:
        marks += MARK_WARM
    if float(s.get("_rate") or 0) >= 0.90:
        marks += MARK_RATE
    return marks


#: v4.3 两遍规划 Pass 1 的 system 角色（方案 §3.3：一句话即可）
OUTLINE_SYSTEM = "你是一名直播切片策划，只输出 JSON，不要解释、不要 markdown 代码块。"


def outline_user(body: str, *, part: str = "") -> str:
    """Pass 1 全局大纲的 user prompt（方案 §3.3 模板）。

    ``part`` 非空表示这是**长素材的分段摘要**（全文超字符预算时先逐段读，
    再由调用方本地合并事件，不多花第三次调用的钱）。
    """
    scope = "下面是一场直播的完整转写" if not part else f"下面是一场直播的{part}转写"
    return f"""{scope}（带时间戳与信号标注，[高能]/[语速↑]＝音频能量/语速峰值）。
请通读之后输出：
1. storyLine：一句话概括本场主线；
2. events：事件清单——每个事件是一件完整发生的事（一个话题、一段冲突、一局游戏、
   一次带货讲解），给出起止时间（素材绝对秒数）、主题、峰值类型（笑点/冲突/干货/情绪/反转）
   与判断依据（引用信号标注或原话）。
事件之间不得重叠；宁可少列，不要拆碎同一事件，也不要合并两个事件；
拿不准的段落不要硬凑事件。
只输出 JSON：{{"storyLine":"","events":[{{"start":0,"end":0,"topic":"","peakType":"","evidence":""}}]}}
{body}"""


def build_system_prompt(content_type: str, params: dict) -> str:
    """按内容类型拼 system prompt（JSON 契约与旧版一致，只有口径不同）。"""
    ctype = content_type if content_type in CONTENT_TYPES else "general"
    spec = CONTENT_TYPES[ctype]
    min_sec = int(params["minSec"])
    max_sec = int(params["maxSec"])
    dims = spec["dims"]
    weights = spec["weights"]

    focus = "\n".join(f"   - {x}" for x in spec["focus"])
    avoid = "\n".join(f"   - {x}" for x in spec["avoid"])
    traffic = "\n".join(f"   - {x}" for x in spec["traffic"])
    weight_text = " / ".join(f"{k} {v:.0%}" for k, v in weights.items())

    # v3.132：本场备注（用户写的「这是什么内容 / 按什么单元切」）—— **追加在最后**，
    # 因为它是最具体的指令，模型对靠后的约束更敏感。没有备注时这块是空串（行为不变）。
    try:
        from . import notes as notes_mod
        note_block = notes_mod.prompt_block(params.get("note") or {},
                                            params.get("unitBoundaries") or [])
    except Exception:                                            # noqa: BLE001
        note_block = ""
    if note_block:
        note_block = "\n\n" + note_block + "\n"

    # v4.2（用户「切片内容没有亮点」）：情绪信号说明 —— 只有真的带了能量数据才出现，
    # 没有数据时这段是空串（prompt 保持原样，不空谈）。
    try:
        has_energy = bool(params.get("_energyPeaks"))
    except Exception:                                            # noqa: BLE001
        has_energy = False
    energy_block = ("""

【情绪信号（来自本地音频能量分析）】
送审文本的行首可能带这些客观标记，它们告诉你"哪里在喊 / 在笑 / 语速飙升"——
直播最炸的瞬间在文字里往往看着平淡，**必须以信号为准**：
- [高能]＝这一句的音量能量在全片前 10%（喊叫、爆笑、欢呼、激烈对喷的瞬间）；
- [情绪↑]＝能量全片前 25%；[语速↑]＝语速全片前 10%（激动、连珠炮）。
user 消息里还附【本窗高光时刻】：全片能量 Top 峰值段（按强度排序）。
⚠️ 某个高光时刻附近**没有转写句**时，说明那里是 Whisper 没转出来的笑声/欢呼/音效爆发
—— 那往往正是名场面，优先把它的前后句组成一条，或建议以该时刻为钩子。
选片段、定钩子、写标题都**优先围绕这些信号**；但事实仍只以转写文本为准
（信号只回答"哪里精彩"，不许据此虚构没说过的话）。"""
                     if has_energy else "")

    # v4.3（方案 §3.3 组接导向）：开头钩子优先级 + 结尾留扣 + keywords 字段说明。
    # 所有路径都带这一段（它是"没有互联网镜头感"的语法层修正，成本为零）。
    direct_block = """

【组接与留扣】
- 开头 3 秒钩子的优先级：话没说完的悬念 > 冲突/对峙现场 > 反常识结论 >
  情绪爆发（笑场、破防、惊讶）> 金句。**严禁**用总结句、寒暄、自我介绍开头；
- 结尾**不收束**：优先在答案揭晓前、笑声最高点、或反问处结束；
  避免「所以说……」「好了就这样」式收尾。素材确实只有收束结尾时才允许完整收束，
  并把该条的 endingOpen 标为 false；
- keywords：每条给 2–4 个本条里**最有冲击力的词**（会用作字幕变色放大强调），
  必须逐字来自该条文本，不许造词。"""

    # v4.69 P1-B2：本次任务参数覆盖段（方案《直播切片优化方案》主线 B）。
    # `params["_taskParams"]` 由 slicing.slice_transcript 按素材总时长分档填入；
    # 没有就空串（prompt 保持原样，DEFAULT_PARAMS 兜底）。**放在 note_block 之前**：
    # 数量/时长这类"硬数字"要比用户备注先出现，备注才是最后最敏感的指令。
    params_block = _task_params_block(params)

    return f"""你是{spec['role']}。用户给你一段**直播转写**（带时间戳），\
你要把它切成若干条可独立发布的候选片段。**本场直播的内容类型已判定为「{spec['label']}」**，\
请严格按该类型的内容规律来切，不要套用其他类型（例如游戏直播不要按"故事完整性"切，\
带货不要按"情绪高能"切）。

【本类型什么值得切】
{focus}

【本类型什么必须丢掉】
{avoid}

【热点化提炼 · 提升传播力】
把每段素材当成要发到抖音、和全网内容抢注意力的成品来对待：
- 先提炼「观看点」——观众凭什么在这一秒停下来（反转、冲突、干货、共鸣、梗、信息差、
  情绪高点）；切条、写标题、写摘要、给 douyinTags 都围绕这个观看点来做。
- 参考当前网络热点、热门梗、时事话题与平台热词的「讲法与角度」，让片段更容易被点进来、
  被搜到、被推荐；但只能借用热点的**表达方式与切入角度**——素材里没有的事实、数据、
  人物、事件一律不许编造，也不许硬蹭到与内容无关的热点。
- 评分时把「是否贴近当下观众关心的点、是否具备传播潜质」纳入 value 维度：能自然贴上
  热点/观看点的片段适当给高分，贴不上的不要硬抬。

硬性要求：
1. 每条候选时长 {min_sec}–{max_sec} 秒，**不等长切割**，按内容自然长度决定（宁可短而完整，不要长而拖沓）；
2. start / end **必须直接引用转写句自带的时间戳**（就是输入行里「[起始秒-结束秒]」的数字，"
   可以取某句的 start 或 end，也可以取相邻两句的交界），**不要自行推算或四舍五入秒数**；
   找不到合适断点时，**宁可缩短片段**，也不要写出一个估算出来的时间；
3. 丢弃的内容写进 rejected[i].reason（一句话说清为什么不要），不要静默丢弃；
4. 为每条候选挖一个 **3–12 秒**的高能钩子（hook 字段，startMs/endMs 用**毫秒**，text 逐字来自该范围）；
   {spec['hookStyle']}
5. 三维评分（0–100 整数），**本类型口径如下**：
   - value（{weights['value']:.0%}）＝ {dims['value']}；能自然贴上热点/观看点的片段在此维度适当上浮（见【热点化提炼】）
   - comedy（{weights['comedy']:.0%}）＝ {dims['comedy']}
   - completeness（{weights['completeness']:.0%}）＝ {dims['completeness']}
   - overall：**随便填一个同量级整数即可，服务端会按固定权重（{weight_text}）重算覆盖它**
     —— 不要为了拉高排序而给虚高分；
6. 热度判断（trafficLevel: high/medium/low + trafficReason 一句话）只看这些信号：
{traffic}
   {HONESTY_RULE}
7. category 只能从这些里挑：{spec['categories']}。
8. douyinTags：3–6 个，第一个必须是本段最具体的主题标签；倾向：{'、'.join(spec['tagSeeds'])}。
9. editingTips：1–3 条具体可执行的剪辑建议；coreRisk：只写一个最关键的留人风险，没有就写空串；
   gameVoiceoverSuitable：是否适合**只保留人声、配游戏背景画面**（不是剪游戏画面）；\
suggestedBackgroundTag：轻快暖场/跑酷/赛车/休闲/紧张/无。
10. **只输出最终 JSON**：不要解释、不要 markdown 代码块、不要推理过程。

{_CONTRACT.format(categories='/'.join(spec['categories']))}{params_block}{note_block}{energy_block}{direct_block}{_CHECKLIST}"""


#: v4.69 P0-②：输出前自检清单（方案《直播切片优化方案》P0-②）。
#: 拼在 system prompt **最末尾**（direct_block 之后）——最后出现的要求最敏感，
#: 而它恰好是"输出前的最后一道关"。每条都在拦一种已知坏输出，不是凑数。
_CHECKLIST = """

【输出前自检（逐条过一遍再输出，不通过就修正后再输出）】
1. 每条候选的 start / end 是否都直接引用了转写行的时间戳？有没有哪个数是估算或四舍五入出来的？
2. 每条是否以完整句开头、完整句或留扣结尾？半句话开头/结尾的那条要缩短或删掉；
3. 每条时长是否落在本次任务参数区间内？超长的拆分，过短并条或丢弃并写明原因；
4. hook 的 text 是否逐字来自 startMs–endMs 范围内的转写？毫秒是否落在这个范围内；
5. 被丢弃的内容是否全部写进了 rejected 并各带一句话理由？
确认无误后只输出最终 JSON。"""


def _task_params_block(params: dict) -> str:
    """v4.69 P1-B2：「本次任务参数」覆盖段（方案主线 B2，autoclip DurationProfile 思路）。

    固定参数对 3 分钟素材和 6 小时素材一视同仁是短板（autoclip 的注释记录过真实后果：
    "5 分钟视频切出 3×2 分钟"）。这里按素材总时长把**建议条数 / 时长带 / 窗口口径**
    追加到提示词末尾，**优先级高于上文所有数量与时长规则**。
    `params["_taskParams"]` 由 :func:`slicing.slice_transcript` 按档位填入；没有就空串。
    """
    tp = params.get("_taskParams") or {}
    total = int(tp.get("totalSec") or 0)
    if total <= 0 or not tp.get("minSec"):
        return ""
    mm, ss = divmod(total, 60)
    lo = int(tp.get("suggestLo") or 0)
    hi = int(tp.get("suggestHi") or 0)
    mn = int(tp.get("minSec") or 0)
    mx = int(tp.get("maxSec") or 0)
    tier = str(tp.get("tier") or "")
    return f"""

【本次任务参数（优先级高于上文所有数量与时长规则）】
- 本段素材总时长：{mm} 分 {ss} 秒（分档：{tier}）
- 建议提取 {lo}–{hi} 条候选，候选之间不要重叠
- 每条目标时长 {mn}–{mx} 秒：最短不少于 {mn} 秒，最长不超过 {mx} 秒
- 上文中出现的所有具体秒数（含「硬性要求」第 1 条），一律以本节为准
- start / end 必须直接引用转写句的时间戳，不要自行推算
"""


def build_messages(window: dict, params: dict, index: int, total: int) -> list[dict]:
    """构造一次窗口送审的 messages（§6.4 第 1 步）。

    与旧版唯一区别：system 段由 :func:`build_system_prompt` 按 ``params['contentType']`` 生成。
    v3.140：``params['reviewHint']``（AI 完整性复核的意见）会作为「上一版复核意见」块
    附在 user 消息里 —— 用户要求「复核完能提示是否重新拆分」，重切时把意见喂给规划器。
    """
    lines = []
    for s in window["segments"]:
        mark = row_mark(s)                       # v4.2：[高能]/[情绪↑]/[语速↑]（无数据=空串）
        row = f"[{s['start']:.2f}-{s['end']:.2f}]"
        lines.append(f"{row} {mark} {s['text']}" if mark else f"{row} {s['text']}")
    body = "\n".join(lines)
    ctype = params.get("contentType") or "general"
    spec = CONTENT_TYPES.get(ctype, CONTENT_TYPES["general"])
    user = (
        f"【素材】总时长 {window['_totalSec']:.1f} 秒；本窗为第 {index + 1}/{total} 窗，"
        f"覆盖 {window['start']:.1f}–{window['end']:.1f} 秒。\n"
        f"【参数】minSec={params['minSec']:.0f}，maxSec={params['maxSec']:.0f}，"
        f"mode={params['mode']}，contentType={ctype}（{spec['label']}）\n"
        f"【本窗转写】每行格式为「起始秒-结束秒 [情绪标记] 文本」：\n{body}\n\n"
        f"请只针对**本窗覆盖的时间范围**产出候选，时间戳用素材的**绝对秒数**，"
        f"并只输出 JSON。"
    )
    # v4.2：本窗内的能量峰值段（全场 Top 高光）—— 优先在这些时刻附近成条
    peaks = [p for p in (params.get("_energyPeaks") or [])
             if float(p.get("endSec") or 0) > float(window.get("start") or 0)
             and float(p.get("startSec") or 0) < float(window.get("end") or 0)]
    if peaks:
        # 峰段内有没有转写句：没有 = Whisper 没转出来的喊叫/笑声/音效（最容易被漏掉的名场面）
        seg_rows = window.get("segments") or []
        hot_lines = []
        for p in peaks[:6]:
            covered = any(float(s.get("start") or 0) < float(p["endSec"])
                          and float(s.get("end") or 0) > float(p["startSec"])
                          for s in seg_rows)
            tag = "" if covered else "（⚠ 此处转写为空＝未被转出的笑声/欢呼/音效名场面）"
            hot_lines.append(f"- {p['startSec']:.0f}-{p['endSec']:.0f}s"
                             f"（能量强度 {float(p.get('level') or 0):.0%}）{tag}")
        user += ("\n\n【本窗高光时刻（音频能量峰值，按强度排序）】\n" + "\n".join(hot_lines)
                 + "\n优先在这些时刻及其前后成条；转写为空的峰不许虚构台词，"
                   "但可以把它当钩子位或与相邻句子组条。")
    hint = str(params.get("reviewHint") or "").strip()
    if hint:
        user += (
            "\n\n【上一版复核意见】AI 完整性复核对上一批候选的判定与建议如下。"
            "重新拆分时请**吸收这些修正**（起点/终点按建议微移、把跨单元的拆开、"
            "开头结尾不收束的调整边界），但不要因此刻意缩水候选数量：\n" + hint
        )
    # v4.3 两遍规划 Pass 2：把「全场主线 + 当前事件」喂给精切器 —— 事件级上下文
    # 让模型知道"这一段在整场里是什么角色"，不再逐窗盲人摸象。
    event = params.get("_event") or {}
    if event:
        story = str(params.get("_storyLine") or "").strip()
        head = ""
        if story:
            head += f"【本场主线】{story}\n"
        head += (f"【本事件】主题：{event.get('topic') or '未命名'}"
                 f"｜峰值类型：{event.get('peakType') or '未标注'}"
                 f"｜范围 {float(event.get('start') or 0):.0f}–{float(event.get('end') or 0):.0f} 秒")
        if event.get("evidence"):
            head += f"\n【事件依据】{str(event['evidence'])[:160]}"
        user = head + "\n\n" + user + (
            "\n\n请只围绕**本事件**产出候选（必要时可引用紧邻上下文做背景交代，"
            "但候选起止必须落在本事件范围内）；事件外的内容一律进 rejected。")
    return [{"role": "system", "content": build_system_prompt(ctype, params)},
            {"role": "user", "content": user}]


def weights_of(content_type: str) -> dict:
    """取某类型的三维权重（未知类型按 general）。"""
    spec = CONTENT_TYPES.get(content_type) or CONTENT_TYPES["general"]
    return dict(spec["weights"])


def label_of(content_type: str) -> str:
    return (CONTENT_TYPES.get(content_type) or CONTENT_TYPES["general"])["label"]


# ---------------------------------------------------------------- 偏好持久化

CONTENT_TYPE_PREF_KEY = "sliceContentType"


def load_content_type_pref() -> str:
    """读用户在设置里选的切片内容类型（默认 ``auto`` = 按转写内容判定）。

    **读写唯一出处**（K44）：内容类型的取值、标签、权重、prompt 全在本文件定义，
    偏好只是它的持久化，所以放这里。**别在别处再手拼键名** —— v3.20 就是因为在
    ``web_server`` 里独占，自动化流水线拿不到，永远按 general 切（K53）。
    """
    import json

    from app.common.paths import ws
    try:
        data = json.loads(ws.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "auto"
    value = str((data or {}).get(CONTENT_TYPE_PREF_KEY) or "auto").strip()
    return value if value in TYPE_ORDER else "auto"


def save_content_type_pref(value: str) -> str:
    """写偏好；非法值一律落到 ``auto``（宁可自动识别，也不要写进一个坏值）。"""
    import json

    from app.common.paths import ws
    value = str(value or "auto").strip()
    if value not in TYPE_ORDER:
        value = "auto"
    path = ws.settings_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[CONTENT_TYPE_PREF_KEY] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return value


# 本地兜底（无 Key / LLM 没给标签）时的标签底稿：宁可给"这个类型的通用标签"，
# 也不要给 2-gram 切出来的碎片 —— 实测产出过「们看」「天用」「我给」这种跨词垃圾（K53）。
DEFAULT_TAGS: dict[str, list[str]] = {
    "general": ["口播精选", "直播切片", "高能片段"],
    "game": ["游戏高光", "名场面", "实战操作"],
    "chat": ["直播间聊天", "情感话题", "连麦名场面"],
    "commerce": ["直播间好价", "好物推荐", "购物攻略"],
}


def default_tags(content_type: str) -> list[str]:
    """兜底标签（未知类型按 general）。"""
    return list(DEFAULT_TAGS.get(content_type) or DEFAULT_TAGS["general"])
