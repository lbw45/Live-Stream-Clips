#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端（M3-B）：DeepSeek / 任何 OpenAI 兼容接口。

对应决策 **D3**（AI 可配置，默认 DeepSeek，**业务代码禁止硬编码厂商**）与
文档 **§6.4** 的参数（`temperature=0.3` / `retries=3` / 超时 90s / 转写上限 5 万字 /
**要最终 JSON 而非推理型补全**）。

⚠️ **`max_tokens` 与 `thinking` 是 v3.19 真机实测后修正的**（原记 500 来自原版另一处小批次调用）：
DeepSeek v4 系默认开思考模式，500 的预算会被 `reasoning_content` 全部吃掉、`content` 返回空串；
即便关掉思考，500 也装不下我们这种「切边界＋评分＋文案＋标签」合一的大 prompt。
现在的默认是 **thinking=disabled + max_tokens=2000**，并且 `chat_json` 在 content 为空时
会**说出真因**（思考吃光 / 被截断 / 只回 reasoning），不再让人对着"没产出候选"猜。

三个刻意的设计：

1. **transport 可注入**。真实网络调用是最难测的一层，所以把「发 HTTP」抽成一个函数，
   冒烟测试注入假 transport 就能把**提示词构造 → 请求体 → 响应解析 → 契约校验**整条
   链路测到（只有真正的 socket 没测）。
2. **无 Key 不算错误**。:meth:`LLMConfig.configured` 为假时由 `slicing` 侧回退本地规则
   规划器 —— 这样「没拿到 Key 也能出片」。任何 LLM 失败（超时 / 限流 / 坏 JSON）
   同样回退，绝不空手而归。
3. **Key 支持 DPAPI 加密存储**（§5.1「Key DPAPI 加密、脱敏显示」）。
   `dpapi:` 前缀的值用 Windows `CryptUnprotectData` 解，明文值也接受（开发期方便）。
   ⚠️ 注意：这里的 DPAPI 是**当前用户 + 本机**绑定，换用户/换机解不开，这是预期的。

与 K16 的对照：**回环请求必须绕代理**（否则被 `HTTP_PROXY` 劫持），
但**调外部 API 恰恰要走代理** —— 所以这里用默认 opener，不套 `ProxyHandler({})`。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import dataclasses
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from ..common.paths import ws

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_CHARS = 50000          # §6.4：转写上限 5 万字
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(Exception):
    """LLM 调用或契约校验失败（调用方据此回退本地规划器）。"""


# ---------------------------------------------------------------- DPAPI

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_in(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(plain: str, entropy: str = "SlicerWorkbench") -> str:
    """把明文加密成 ``dpapi:<base64>``（设置页保存 Key 时用）。"""
    if os.name != "nt":
        raise LLMError("DPAPI 仅在 Windows 可用")
    blob_in = _blob_in(plain.encode("utf-8"))
    blob_ent = _blob_in(entropy.encode("utf-8"))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(      # type: ignore[attr-defined]
        ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise LLMError("CryptProtectData 失败")
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)   # type: ignore[attr-defined]
    return "dpapi:" + base64.b64encode(raw).decode("ascii")


def dpapi_unprotect(value: str, entropy: str = "SlicerWorkbench") -> str:
    """解开 ``dpapi:<base64>``。失败抛 :class:`LLMError`（不静默返回原串）。"""
    if os.name != "nt":
        raise LLMError("DPAPI 仅在 Windows 可用")
    raw = base64.b64decode(value[len("dpapi:"):])
    blob_in = _blob_in(raw)
    blob_ent = _blob_in(entropy.encode("utf-8"))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(    # type: ignore[attr-defined]
        ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0,
        ctypes.byref(blob_out))
    if not ok:
        raise LLMError("CryptUnprotectData 失败（Key 可能来自其他 Windows 用户或其他机器）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)   # type: ignore[attr-defined]


def decode_api_key(raw: str) -> str:
    """接受明文或 ``dpapi:`` 前缀；空值返回空串。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("dpapi:"):
        return dpapi_unprotect(raw)
    return raw


# ---------------------------------------------------------------- 配置

@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""
    temperature: float = 0.3
    # v3.19 实测修正：原按 §6.4 记的 500 来自**原版另一处调用**（发布优先级分析，批次小）。
    # 我们的 prompt 是「切边界 + 评分 + 文案 + 标签 + 钩子」合一的**大 prompt**，
    # 500 tokens 连一条候选都装不下（实测 B 组被 length 截断成非法 JSON）。
    max_tokens: int = 2000
    timeout: float = 90.0
    retries: int = 3
    max_chars: int = DEFAULT_MAX_CHARS
    # v3.19 实测：DeepSeek v4 系**默认开启思考模式**，500 tokens 预算会被
    # `reasoning_content` 全部吃掉、`content` 返回空串（实测 A 组 content_len=0）。
    # 原版代码里写死了「明确关闭思考模式，避免 reasoning_content 挤占输出预算」——照做。
    thinking_disabled: bool = True
    extra_body: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def redacted(self) -> dict:
        """用于日志/接口回显的脱敏视图（**绝不吐出完整 Key**）。"""
        key = self.api_key
        shown = "" if not key else (key[:4] + "*" * max(len(key) - 8, 3) + key[-4:]
                                    if len(key) > 12 else "*" * len(key))
        return {
            "provider": self.provider, "baseUrl": self.base_url, "model": self.model,
            "apiKey": shown, "configured": self.configured,
            "temperature": self.temperature, "maxTokens": self.max_tokens,
            "timeoutSec": self.timeout, "retries": self.retries,
            "maxChars": self.max_chars, "thinking": "disabled" if self.thinking_disabled else "auto",
        }


# settings.json 里 AI 服务的键名（16 键里的 4 个；设置页 M1-D 顺延，先按此约定读写）
SETTING_KEYS = {
    "provider": ("aiProvider",),
    "base_url": ("aiBaseUrl", "ai_base_url"),
    "api_key": ("aiApiKey", "ai_api_key"),
    "model": ("aiModel", "ai_model"),
}


# ---------------------------------------------------------------- 厂商预设（v3.81）
# 用户：「AI 配置要能配其他厂商的 key」。
#
# 选型决定（我按「一条路通吃」而非「每个厂商写一套」拍的板）：
# 我们整个调用链只有 **OpenAI 兼容的 chat/completions** 一种形态（见 build_request），
# 而国内主流厂商**全都提供这一形态**（这已经是事实标准）。所以与其为每个厂商写一本书，
# 不如把「服务商」降维成一张 **Base URL + 默认模型名的对照表**：换厂商 = 换这两个字段，
# 调用代码一行不改（D3：业务代码禁止硬编码厂商，这里连厂商名都只是给用户看的标签）。
#
# ⚠️ 三条纪律：
# 1. **这张表是唯一出处**（K44）。前端的下拉、服务端的回填、桌面桥放行的「去拿 Key」
#    链接，三者都从这里读 —— 别在 index.html 里再抄一遍厂商清单。
# 2. **默认模型名只是初始值，不是承诺**。厂商改名/下线是常事（2026-09-24 那次报障就是
#    填了个不存在的名字）。真正的答案只有服务端知道 → 让用户点「获取可用模型」。
# 3. **不在列表里的厂商 ≠ 不支持** —— 只要是 OpenAI 兼容接口，用「自定义」照样能用。
PROVIDER_PRESETS: tuple[dict, ...] = (
    {
        "id": "deepseek", "label": "DeepSeek（深度求索）",
        "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "keyUrl": "https://platform.deepseek.com/api_keys",
        "keyHint": "DeepSeek 开放平台 → 左侧「API Keys」→ 创建 API Key",
        "note": "默认服务商。它的模型名换过代，务必点「获取可用模型」确认",
    },
    {
        "id": "qwen", "label": "通义千问（阿里云百炼）",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "keyUrl": "https://bailian.console.aliyun.com/?tab=model#/api-key",
        "keyHint": "阿里云百炼控制台 → 右上角 API Key → 创建（要「兼容 OpenAI」的那种）",
        "note": "走的是百炼的 OpenAI 兼容模式，注意 Base URL 末尾是 compatible-mode/v1",
    },
    {
        "id": "kimi", "label": "Kimi（月之暗面）",
        "baseUrl": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
        "keyUrl": "https://platform.moonshot.cn/console/api-keys",
        "keyHint": "Moonshot 控制台 → API Key 管理 → 新建",
    },
    {
        "id": "zhipu", "label": "智谱 GLM",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
        "keyUrl": "https://open.bigmodel.cn/usercenter/apikeys",
        "keyHint": "智谱开放平台 → 右上角「API Keys」→ 添加",
        "note": " glm-4-flash 是它的免费档，用来试连通性很划算",
    },
    {
        "id": "doubao", "label": "豆包（火山方舟）",
        "baseUrl": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k",
        "keyUrl": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "keyHint": "火山方舟控制台 → API Key 管理 → 创建",
        "note": "方舟的「模型」要填**接入点 ID**（doubao-xxx-250428 那种），"
                "在「在线推理」里建好接入点后才有；不确定就点「获取可用模型」",
    },
    {
        "id": "siliconflow", "label": "硅基流动 SiliconFlow",
        "baseUrl": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct",
        "keyUrl": "https://cloud.siliconflow.cn/account/ak",
        "keyHint": "硅基流动 → 左侧「API 密钥」→ 新建密钥",
        "note": "聚合了很多开源模型，模型名带厂商前缀（如 Qwen/…）",
    },
    {
        "id": "openai", "label": "OpenAI",
        "baseUrl": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "keyUrl": "https://platform.openai.com/api-keys",
        "keyHint": "OpenAI Platform → API keys → Create new secret key",
        "note": "境外节点，需要本机网络能连出去",
    },
    {
        "id": "groq", "label": "Groq",
        "baseUrl": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
        "keyUrl": "https://console.groq.com/keys",
        "keyHint": "Groq Console → API Keys → Create API Key",
        "note": "境外节点。注意它的 Base URL 是 api.groq.com/openai/v1",
    },
    {
        "id": "openrouter", "label": "OpenRouter（聚合网关）",
        "baseUrl": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-sonnet",
        "keyUrl": "https://openrouter.ai/keys",
        "keyHint": "OpenRouter → Keys → Create Key",
        "note": "**想用 Claude / Gemini 走这里** —— 它们官方接口不是 OpenAI 格式，"
                "而 OpenRouter 把格式转好了。滑词能力的代价按实时费率算",
    },
    {
        "id": "ollama", "label": "本地 Ollama",
        "baseUrl": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
        "keyUrl": "", "keyHint": "本地运行不需要 Key，API Key 一栏随便填一个（比如 ollama）",
        "note": "完全离线、不花钱，但 7B 模型很难按我们的格式吐出合格 JSON —— "
                "切片建议仍用云端模型",
        "local": True,
    },
    {
        "id": "lmstudio", "label": "本地 vLLM / LM Studio",
        "baseUrl": "http://127.0.0.1:1234/v1", "model": "local-model",
        "keyUrl": "", "keyHint": "本地服务不需要 Key，随便填；Base URL 以你软件里显示的为准",
        "note": "和 Ollama 同理：能用，但小模型切不出好边界",
        "local": True,
    },
)

#: 下拉最后一项：自己填（任何 OpenAI 兼容接口都能用）
CUSTOM_PROVIDER = {
    "id": "custom", "label": "自定义（OpenAI 兼容接口）",
    "baseUrl": "", "model": "",
    "keyUrl": "", "keyHint": "自己填接口地址与模型名 —— 只要它是 OpenAI 兼容格式就能用",
    "note": "厂商不在上面列表里、或你有自建网关 / 内网代理时选它",
}


def providers_view() -> list[dict]:
    """厂商预设（含「自定义」），供设置页下拉与桌面桥白名单使用。"""
    return [dict(p) for p in PROVIDER_PRESETS] + [dict(CUSTOM_PROVIDER)]


def preset_by_id(pid: str) -> dict | None:
    """按 id 取预设；不在表里返回 ``None``（"自定义"也算命中，它有 id）。"""
    for p in providers_view():
        if p["id"] == str(pid or "").strip():
            return p
    return None


def allowed_key_hosts() -> set[str]:
    """桌面桥允许打开的主机集合（**唯一出处**就是上面的 presets）。

    为什么需要白名单：桥上那个方法会被页面用任意字符串调用，照单全开就是
    「网页能让你打开任意网址」的口子；只在表里的厂商官网才放行（K77 同源）。
    """
    from urllib.parse import urlparse

    hosts: set[str] = set()
    for p in providers_view():
        url = str(p.get("keyUrl") or "").strip()
        if not url.startswith("https://"):
            continue
        host = (urlparse(url).hostname or "").lower()
        if host:
            hosts.add(host)
    return hosts


def _from_settings() -> dict:
    path = ws.settings_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for field_name, keys in SETTING_KEYS.items():
        for k in keys:
            if data.get(k):
                out[field_name] = str(data[k])
                break
    return out


def load_config(overrides: dict | None = None) -> LLMConfig:
    """解析配置：**显式 overrides > 环境变量 > settings.json > 默认值**。

    环境变量：``SLICE_LLM_API_KEY`` / ``DEEPSEEK_API_KEY`` / ``SLICE_LLM_BASE_URL`` /
    ``SLICE_LLM_MODEL`` / ``SLICE_LLM_PROVIDER``。
    """
    st = _from_settings()
    env = os.environ
    cfg = LLMConfig(
        provider=(env.get("SLICE_LLM_PROVIDER") or st.get("provider") or "deepseek"),
        base_url=(env.get("SLICE_LLM_BASE_URL") or st.get("base_url") or DEFAULT_BASE_URL),
        model=(env.get("SLICE_LLM_MODEL") or st.get("model") or DEFAULT_MODEL),
    )
    raw_key = (env.get("SLICE_LLM_API_KEY") or env.get("DEEPSEEK_API_KEY")
               or env.get("OPENAI_API_KEY") or st.get("api_key") or "")
    raw_key = raw_key.strip()
    if raw_key:
        try:
            cfg.api_key = decode_api_key(raw_key)
        except LLMError:
            cfg.api_key = ""      # 解不开就当没配（宁可回退本地，也不要拿坏 Key 去撞网络）
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    return cfg


# ---------------------------------------------------------------- 提示词（§6.4 第 1–12 步）

# v3.19：提示词按**内容类型**分套，全部搬进 :mod:`app.slicer.prompts`
# （每种类型 = 角色定位 + 切什么 + 丢什么 + 三维口径 + 权重 + 热度信号）。
# 这里保留同名薄封装，是为了不打断既有 import；**不要再往本文件里写 prompt 正文**（K44 同源）。
def build_messages(window: dict, params: dict, index: int, total: int) -> list[dict]:
    """构造一次窗口送审的 messages（§6.4 第 1 步）—— 转发给 prompts 模块。"""
    from . import prompts
    return prompts.build_messages(window, params, index, total)


# ---------------------------------------------------------------- JSON 解析与契约校验

def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍 ```json 围栏与前后废话）。"""
    raw = (text or "").strip()
    m = JSON_FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    start = raw.find("{")
    if start < 0:
        raise LLMError(f"响应里没有 JSON：{raw[:200]!r}")
    try:
        return json.JSONDecoder().raw_decode(raw[start:])[0]
    except ValueError as exc:
        raise LLMError(f"JSON 解析失败：{exc}（原文前 200 字：{raw[:200]!r}）") from exc


def validate_clips_payload(data: dict, params: dict) -> tuple[list[dict], list[dict], list[str]]:
    """按 §6.4 的 JSON 契约校验。返回 ``(clips, rejected, warnings)``。

    只做**结构**校验（类型/必填/范围）；时长与吸附交给 `slicing.finalize_candidates`，
    因为那是两个 planner 共用的不变量，不该在这里重复实现。
    """
    if not isinstance(data, dict):
        raise LLMError("顶层不是 JSON 对象")
    clips_in = data.get("clips")
    if not isinstance(clips_in, list):
        raise LLMError("缺少 clips 数组")
    clips: list[dict] = []
    warnings: list[str] = []
    for i, c in enumerate(clips_in):
        if not isinstance(c, dict):
            warnings.append(f"clips[{i}] 不是对象，已跳过")
            continue
        try:
            start = float(c["start"])
            end = float(c["end"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"clips[{i}] 缺 start/end 或非数字，已跳过")
            continue
        if end <= start:
            warnings.append(f"clips[{i}] end<=start，已跳过")
            continue
        scores = c.get("scores")
        # v4.69 P3（方案主线 C）：分数来源标记 —— 四维都有**有效数字**才算 llm 给的，
        # 缺/非法 ⇒ fallback（本地口径会补齐）。没有这个标记，兜底分会伪装成模型分（K69）。
        def _ok_num(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return False
            return f == f and -1e9 < f < 1e9          # 排除 nan/inf；缩放交给 normalize_scores
        keys_ok = (isinstance(scores, dict)
                   and all(_ok_num((scores or {}).get(k)) for k in
                           ("value", "comedy", "completeness", "overall")))
        score_source = "llm" if keys_ok else "fallback"
        if not keys_ok:
            warnings.append(f"clips[{i}] 缺 scores 或分值非法，将按本地口径补齐（scoreSource=fallback）")
            scores = {}
        clip = {
            "start": start, "end": end,
            "title": str(c.get("title") or "")[:60],
            "summary": str(c.get("summary") or "")[:200],
            "category": str(c.get("category") or ""),
            "scores": {k: scores.get(k) for k in ("value", "comedy", "completeness", "overall")},
            "scoreSource": score_source,
            "trafficLevel": str(c.get("trafficLevel") or ""),
            "trafficReason": str(c.get("trafficReason") or "")[:200],
            "douyinTags": [str(t) for t in (c.get("douyinTags") or [])][:8],
            # v4.3：keywords（字幕强调词）/ endingOpen（结尾是否留扣）—— 可选字段，
            # 模型没给就空着，下游按"无强调词/未标注"处理，不影响其余字段
            "keywords": [str(t)[:16] for t in (c.get("keywords") or []) if str(t).strip()][:4],
            "endingOpen": (bool(c.get("endingOpen")) if c.get("endingOpen") is not None else None),
            "editingTips": [str(t) for t in (c.get("editingTips") or [])][:5],
            "bestDuration": str(c.get("bestDuration") or ""),
            "coreRisk": str(c.get("coreRisk") or "")[:200],
            "gameVoiceoverSuitable": bool(c.get("gameVoiceoverSuitable", False)),
            "suggestedBackgroundTag": str(c.get("suggestedBackgroundTag") or ""),
        }
        hook = c.get("hook")
        if isinstance(hook, dict):
            try:
                hs, he = float(hook.get("startMs") or 0), float(hook.get("endMs") or 0)
                if he > hs:
                    clip["hook"] = {"startMs": int(hs), "endMs": int(he),
                                    "text": str(hook.get("text") or "")[:80],
                                    "copiedToStart": True}
            except (TypeError, ValueError):
                warnings.append(f"clips[{i}] hook 非法，交给本地兜底")
        clips.append(clip)

    rejected: list[dict] = []
    for r in (data.get("rejected") or []):
        if not isinstance(r, dict):
            continue
        try:
            rejected.append({"start": float(r.get("start") or 0),
                             "end": float(r.get("end") or 0),
                             "reason": str(r.get("reason") or "")[:200]})
        except (TypeError, ValueError):
            continue
    return clips, rejected, warnings


# ---------------------------------------------------------------- HTTP

def default_transport(spec: dict) -> tuple[int, str]:
    """真实 HTTP 请求。返回 ``(status, body_text)``；连接层失败返回 status 0。

    ``spec["method"]`` 缺省为 ``POST``（``chat/completions`` 用）；
    ``GET /models``（列可用模型）走同一个 transport，传 ``"GET"`` ——
    这样「列模型」也能用假 transport 打桩测试，不必真连网。

    ⚠️ 与回环请求相反：**这里不绕代理**（K16 只针对 127.0.0.1）。
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(spec["url"], data=spec.get("body"),
                                 method=spec.get("method", "POST"))
    for k, v in spec["headers"].items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=spec["timeout"]) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            return exc.code, ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


Transport = Callable[[dict], tuple[int, str]]


def build_request(config: LLMConfig, messages: list[dict]) -> dict:
    """构造 OpenAI 兼容的 chat/completions 请求。"""
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        # §6.4：「要最终 JSON 而非推理型补全」
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if getattr(config, "thinking_disabled", True):
        # v3.19：v4 系默认开思考，思考 token 会挤掉 content（实测 content 为空串）
        payload["thinking"] = {"type": "disabled"}
    payload.update(config.extra_body or {})
    return {
        "url": config.base_url.rstrip("/") + "/chat/completions",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        },
        "body": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "timeout": config.timeout,
    }


def chat_json(config: LLMConfig, messages: list[dict],
              transport: Transport | None = None) -> str:
    """调一次 chat/completions，返回助手文本。带重试（§6.4 retries=3）。"""
    if not config.configured:
        raise LLMError("LLM 未配置（缺 api_key / base_url / model）")
    send = transport or default_transport
    spec = build_request(config, messages)
    last = ""
    for attempt in range(1, max(1, config.retries) + 1):
        status, body = send(spec)
        if status == 200:
            try:
                data = json.loads(body)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                if content.strip():
                    return content
                # v3.19：**200 但 content 为空**必须说出真因，否则上层只能报
                # 「所有窗口都没产出候选」这种查不下去的话（本轮实测就卡在这里）。
                finish = str(choice.get("finish_reason") or "")
                reason_len = len(message.get("reasoning_content") or "")
                usage = data.get("usage") or {}
                if finish == "length" and reason_len:
                    last = (f"输出预算被思考模式吃光：completion_tokens={usage.get('completion_tokens')}"
                            f" 全花在 reasoning（{reason_len} 字），content 为空。"
                            f"→ 已默认 thinking=disabled；若仍如此请调大 maxTokens（当前 {config.max_tokens}）")
                elif finish == "length":
                    last = (f"输出被截断（finish_reason=length，completion_tokens="
                            f"{usage.get('completion_tokens')}）：prompt 要求的内容超出 maxTokens="
                            f"{config.max_tokens}，请调大")
                elif reason_len:
                    last = f"接口只回了 reasoning_content（{reason_len} 字），content 为空"
                else:
                    last = f"接口 200 但 content 为空（finish_reason={finish or '未知'}）"
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                last = f"响应结构异常：{exc}（前 200 字 {body[:200]!r}）"
        elif status == 400 and "response_format" in body:
            # 部分兼容实现不认 response_format → 去掉重试一次（配置级降级）
            spec = dict(spec)
            payload = json.loads(spec["body"].decode("utf-8"))
            payload.pop("response_format", None)
            spec["body"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            last = f"该接口不支持 response_format，已降级重试（{body[:120]}）"
        elif status == 0:
            last = f"连接失败：{body[:200]}"
        else:
            last = f"HTTP {status}：{body[:200]}"
        if attempt < max(1, config.retries):
            time.sleep(min(2 ** (attempt - 1), 8))       # 1s / 2s / 4s …
    raise LLMError(f"重试 {config.retries} 次仍失败 —— {last}")


# ---------------------------------------------------------------- 编排

class PlanCancelled(Exception):
    """v3.159：用户在切片过程中点了「停止」（协作式取消）。"""


# ---------------------------------------------------------------- 两遍规划（v4.3）
#
# 方案 §3.3：Pass 1 全局大纲（解决"逐窗盲人摸象、好内容跨窗腰斩"）→ Pass 2 逐事件精切。
# 设计约束（与本项目既有纪律一致）：
# - 大纲失败/为空 → 静默回退旧的逐窗直切，**绝不空手而归**；
# - 长素材超字符预算时先逐段摘要、**本地合并**事件（不多花第三次调用的钱）；
# - 事件数封顶 24（防止模型把全场拆成 200 个碎片，调用次数爆炸）；
# - 候选结构与旧路径**完全同构**（start/end/scores/hook…），只多 eventTopic/keywords 等可选字段。

OUTLINE_MAX_EVENTS = 24


def _outline_events(data: dict, total_sec: float) -> list[dict]:
    """校验并归一化事件清单：时间合法、排序、去重叠、夹回全场范围。"""
    events: list[dict] = []
    for e in (data.get("events") or []):
        if not isinstance(e, dict):
            continue
        try:
            start, end = float(e.get("start")), float(e.get("end"))
        except (TypeError, ValueError):
            continue
        if end - start < 8.0:                       # 短于 8 秒的"事件"不是事件
            continue
        start = max(0.0, min(start, total_sec))
        end = max(start, min(end, total_sec))
        events.append({"start": start, "end": end,
                       "topic": str(e.get("topic") or "")[:60],
                       "peakType": str(e.get("peakType") or "")[:12],
                       "evidence": str(e.get("evidence") or "")[:160]})
    events.sort(key=lambda e: e["start"])
    out: list[dict] = []
    for e in events:
        if out and e["start"] < out[-1]["end"]:     # 重叠：截断后者（模型常见毛病）
            e["start"] = out[-1]["end"]
        if e["end"] - e["start"] < 8.0:
            continue
        out.append(e)
    return out[:OUTLINE_MAX_EVENTS]


def plan_outline(segments: list[dict], params: dict, *, config: LLMConfig,
                 transport: Transport | None = None) -> dict | None:
    """Pass 1：通读全场 → ``{"storyLine", "events"}``；失败返回 None（调用方回退旧路径）。"""
    from . import prompts
    from .slicing import build_windows

    total_sec = max((s["end"] for s in segments), default=0.0)
    cfg = config

    def render(rows: list[dict]) -> str:
        out = []
        for s in rows:
            mark = prompts.row_mark(s)
            row = f"[{s['start']:.2f}-{s['end']:.2f}]"
            out.append(f"{row} {mark} {s['text']}" if mark else f"{row} {s['text']}")
        return "\n".join(out)

    def ask(rows: list[dict], part: str = "") -> dict:
        body = render(rows)
        msgs = [{"role": "system", "content": prompts.OUTLINE_SYSTEM},
                {"role": "user", "content": prompts.outline_user(body, part=part)}]
        return extract_json(chat_json(cfg, msgs, transport=transport))

    whole = render(segments)
    if len(whole) <= int(cfg.max_chars):
        data = ask(segments)
        events = _outline_events(data, total_sec)
        if not events:
            return None
        return {"storyLine": str(data.get("storyLine") or "")[:200], "events": events}

    # 长素材：逐段摘要 → 本地合并（不再多花一次"汇总"调用）
    windows = build_windows(segments, params["windowSec"], params["overlapSec"])
    if not windows:
        return None
    per = max(2000, int(cfg.max_chars) // max(len(windows), 1))
    events: list[dict] = []
    story = ""
    for i, w in enumerate(windows):
        rows = w["segments"]
        used = sum(len(s["text"]) for s in rows)
        if used > per:
            rows = rows[: max(1, int(len(rows) * per / max(used, 1)))]
        try:
            data = ask(rows, part=f"第 {i + 1}/{len(windows)} 段")
        except Exception:                                   # noqa: BLE001 — 单段失败不拖垮大纲
            continue
        if not story:
            story = str(data.get("storyLine") or "")[:200]
        events.extend(_outline_events(data, total_sec))
    if not events:
        return None
    events.sort(key=lambda e: e["start"])
    merged: list[dict] = []
    for e in events:                       # 相邻/重叠且同主题的合并（跨窗被拆的同一事件）
        if (merged and e["start"] - merged[-1]["end"] <= params["overlapSec"]
                and e["topic"] and e["topic"] == merged[-1]["topic"]):
            merged[-1]["end"] = max(merged[-1]["end"], e["end"])
        else:
            merged.append(e)
    return {"storyLine": story, "events": merged[:OUTLINE_MAX_EVENTS]}


def _plan_by_events(segments: list[dict], params: dict, outline: dict,
                    cfg: LLMConfig, transport, should_cancel) -> tuple[list[dict], list[dict], str]:
    """Pass 2：逐事件精切。每个事件一次（或按 window 粒度多次）送审。"""
    from .slicing import build_windows

    clips: list[dict] = []
    rejected: list[dict] = []
    errors: list[str] = []
    total_sec = max((s["end"] for s in segments), default=0.0)
    budget = max(2000, int(cfg.max_chars) // 4)
    for ev in outline["events"]:
        if should_cancel is not None and should_cancel():
            raise PlanCancelled("已停止")
        rows = [s for s in segments if s["end"] > ev["start"] and s["start"] < ev["end"]]
        if not rows:
            continue
        p2 = dict(params)
        p2["_event"] = ev
        p2["_storyLine"] = outline.get("storyLine") or ""
        p2["_totalSec"] = total_sec
        # 事件长于 windowSec 时按 window 粒度再切（控制单次送审长度）
        if ev["end"] - ev["start"] > params["windowSec"]:
            wins = build_windows(rows, params["windowSec"], params["overlapSec"])
        else:
            wins = [{"segments": rows, "start": ev["start"], "end": ev["end"]}]
        for i, w in enumerate(wins):
            w = dict(w)
            w["_totalSec"] = total_sec
            texts = w["segments"]
            used = sum(len(s["text"]) for s in texts)
            if used > budget:
                w["segments"] = texts[: max(1, int(len(texts) * budget / max(used, 1)))]
            messages = build_messages(w, p2, i, len(wins))
            try:
                data = extract_json(chat_json(cfg, messages, transport=transport))
                cs, rs, warns = validate_clips_payload(data, p2)
                for c in cs:
                    c["eventTopic"] = ev["topic"]         # 事件溯源（UI/复盘可用）
                clips += cs
                rejected += rs
                if warns:
                    errors.append(f"事件「{ev['topic']}」警告 {len(warns)} 条：{warns[0]}")
            except Exception as exc:                      # noqa: BLE001
                errors.append(f"事件「{ev['topic']}」失败：{type(exc).__name__}: {exc}")
    if not clips:
        raise LLMError("两遍规划：所有事件都没产出候选 —— " + " ｜ ".join(errors[:3]))
    if errors:
        rejected.insert(0, {"start": 0, "end": 0,
                            "reason": "两遍规划部分事件异常：" + " ｜ ".join(errors[:3])})
    return clips, rejected, cfg.model


def plan_with_llm(segments: list[dict], params: dict, *, config: LLMConfig | None = None,
                  transport: Transport | None = None,
                  should_cancel=None) -> tuple[list[dict], list[dict], str]:
    """§6.4 第 1 步：**滑窗送审**。返回 ``(原始候选, rejected, 模型名)``。

    - 每窗一次调用；窗口数由 ``windowSec``/``overlapSec`` 决定；
    - 全局字符预算 ``maxChars``（5 万字）平均分给各窗口，超出即截断并记 notes；
    - 任一窗口失败**不整体放弃**：记为 warning，其余窗口继续（本地兜底只补空缺）；
    - 所有窗口都拿不到候选才抛错，交给 `slicing` 整体回退本地规划器。
    """
    from .slicing import build_windows

    cfg = config or load_config()
    if not cfg.configured:
        raise LLMError("LLM 未配置")

    # v4.3：两遍规划（默认开，params.twoPass=False 可关）—— 先全局大纲再逐事件精切；
    # 大纲失败/为空 → 回退旧的逐窗直切，**绝不空手而归**。
    if params.get("twoPass", True):
        try:
            outline = plan_outline(segments, params, config=cfg, transport=transport)
            if outline and outline.get("events"):
                clips, rejected, model = _plan_by_events(
                    segments, params, outline, cfg, transport, should_cancel)
                # v4.69 P2-D：LLM 调用统计（质量报告用）。两遍规划 = 大纲 1 次 +
                # 每事件 1 次精切（近似；_plan_by_events 内部重试未计入）。
                params["_llmCalls"] = 1 + len(outline.get("events") or [])
                params["_llmFailures"] = 0
                rejected.insert(0, {"start": 0, "end": 0,
                                    "reason": f"两遍规划：全局大纲 {len(outline['events'])} 个事件"
                                              f"（{outline.get('storyLine') or '主线未概括'}）"})
                return clips, rejected, model
        except PlanCancelled:
            raise
        except Exception as exc:                          # noqa: BLE001 — 大纲异常只降级，不失败
            rejected = [{"start": 0, "end": 0,
                         "reason": f"全局大纲失败，已回退逐窗直切：{type(exc).__name__}: {exc}"}]
        else:
            rejected = [{"start": 0, "end": 0,
                         "reason": "全局大纲未产出事件，按逐窗直切"}]
    else:
        rejected = []

    windows = build_windows(segments, params["windowSec"], params["overlapSec"])
    if not windows:
        raise LLMError("没有可送审的转写窗口")

    total_sec = max((s["end"] for s in segments), default=0.0)
    budget = max(2000, int(cfg.max_chars) // max(len(windows), 1))
    clips: list[dict] = []
    errors: list[str] = []
    # ⚠️ rejected **已在上面两遍规划分支里初始化**（可能带着"大纲失败，已回退"的原因），
    # 这里不能再 `rejected = []` —— 否则那条记录就被静默吞了。

    for i, w in enumerate(windows):
        # v3.159：「停止」在**每个窗口开始前**生效 —— 已经发出的那一窗会答完（没法掐断
        # 正在跑的 HTTP），但后面的窗口不会再发，也不会落任何产物。
        if should_cancel is not None and should_cancel():
            raise PlanCancelled("已停止")
        w = dict(w)
        w["_totalSec"] = total_sec
        # 字符预算：从后往前丢句子，直到不超预算
        texts = w["segments"]
        used = sum(len(s["text"]) for s in texts)
        if used > budget:
            w["segments"] = texts[: max(1, int(len(texts) * budget / max(used, 1)))]
        messages = build_messages(w, params, i, len(windows))
        try:
            content = chat_json(cfg, messages, transport=transport)
            data = extract_json(content)
            cs, rs, warns = validate_clips_payload(data, params)
            clips += cs
            rejected += rs
            if warns:
                errors.append(f"窗口{i + 1} 警告 {len(warns)} 条：{warns[0]}")
        except LLMError as exc:
            errors.append(f"窗口{i + 1} 失败：{exc}")
        except Exception as exc:                      # 兜底：单窗异常不拖垮整轮
            errors.append(f"窗口{i + 1} 异常：{type(exc).__name__}: {exc}")

    # v4.69 P2-D：LLM 调用统计（逐窗直切路径 —— 每窗 1 次，精确计数）
    params["_llmCalls"] = len(windows)
    params["_llmFailures"] = len(errors)

    if not clips:
        raise LLMError("所有窗口都没产出候选 —— " + " ｜ ".join(errors[:3]))
    if errors:
        rejected.insert(0, {"start": 0, "end": 0,
                            "reason": "LLM 部分窗口异常：" + " ｜ ".join(errors[:3])})
    return clips, rejected, cfg.model


# ---------------------------------------------------------------- 连通性测试（§5.1「测试连接」）

PING_MESSAGES = [
    {"role": "system", "content": "你是连通性探测端点。只输出 JSON，不要任何解释或代码块。"},
    {"role": "user", "content": '请回复这个 JSON：{"ok": true, "echo": "pong"}'},
]


# ---------------------------------------------------------------- 视觉能力标注（v3.172）

#: 已知**支持图片输入（视觉）**的模型名特征（小写子串）。模型名是服务端自由文本，
#: 没有统一 API 能查"这个模型能不能看图"，只能按名字启发式 —— 规则**只有这一份**（K44）。
VISION_HINTS: tuple[str, ...] = (
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-4-vision", "chatgpt-4o",
    "claude-3", "claude-4", "claude-opus", "claude-sonnet", "claude-haiku",
    "gemini-1.5", "gemini-2", "gemini-exp",
    "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qvq", "vl-plus",
    "glm-4v", "glm-4.5v", "cogvlm", "internvl",
    "doubao-vision", "doubao-1.5-vision", "doubao-seed-1.6",
    "kimi-latest", "moonshot-v1-vision", "yi-vision",
    "grok-vision", "grok-2-vision", "grok-4",
    "pixtral", "mistral-small-3", "hunyuan-vision", "step-1v", "step-1o",
)
#: 明确**不是**视觉的（防止 "gpt-4o-audio" 之类被误标）
VISION_NOISE: tuple[str, ...] = (
    "embedding", "tts", "whisper", "dall-e", "moderation", "rerank", "audio",
)


def supports_vision(model: str) -> str:
    """启发式判断模型是否支持**图片输入**。返回 ``"yes" / "no" / "unknown"``。

    判不出就如实回 ``"unknown"``（不给假结论）—— 用途：需要读图的环节（如封面主体定位）
    要发图给模型，纯文本模型会失败降级；设置页选模型时把这一点标注出来，
    用户就不会选错。
    """
    m = (model or "").lower()
    if not m:
        return "unknown"
    if any(x in m for x in VISION_NOISE):
        return "no"
    if any(x in m for x in VISION_HINTS):
        return "yes"
    return "unknown"


def vision_map(models: list[str]) -> dict:
    """一批模型名 → ``{名字: "yes"/"no"/"unknown"}``（随 /api/slice/llm-models 下发）。"""
    return {m: supports_vision(m) for m in (models or [])}


def build_models_request(config: LLMConfig) -> dict:
    """构造 ``GET /models`` 请求（OpenAI 兼容端点，用于列出该 Key 的可用模型）。"""
    return {
        "url": config.base_url.rstrip("/") + "/models",
        "headers": {
            "Authorization": f"Bearer {config.api_key}",
            "Accept": "application/json",
        },
        "body": None,
        "timeout": config.timeout,
        "method": "GET",
    }


# 「模型名不被接受」类错误的特征词。服务端措辞会变，所以按语义宽匹配
# （DeepSeek 实测原文："The supported API model names are …, but you passed …"）。
_MODEL_HINT_TOKENS = ("model name", "model_names", "supported api model",
                      "model not", "does not exist", "invalid model")


def list_models(config: LLMConfig | None = None, *,
                transport: Transport | None = None) -> dict:
    """列出**这个 Key 真正可用**的模型名。

    为什么需要它（2026-09-24 报障）：模型名是**服务端的自由文本**，用户不可能背下来。
    用户在设置页把模型填成 ``deepseek-v41-flash``（该账号根本没这个名字），
    「测试连通性」只回一个 400 —— 界面上完全看不出**该填什么**。
    实测该账号可用的只有 ``deepseek-flash`` / ``deepseek-v4-pro`` 两个。

    返回 ``{"ok", "models", "error", "baseUrl"}``；**不回显 Key**。
    """
    cfg = config or load_config()
    result: dict = {"ok": False, "models": [], "error": "", "baseUrl": cfg.base_url}
    if not (cfg.api_key and cfg.base_url):
        result["error"] = "未配置 API Key 或接口地址，无法列出模型"
        return result
    send = transport or default_transport
    status, body = send(build_models_request(cfg))
    if status == 0:
        result["error"] = f"连接失败：{body[:200]}"
        return result
    if status != 200:
        result["error"] = f"HTTP {status}：{body[:200]}"
        return result
    try:
        data = json.loads(body)
        ids = [str(m.get("id")) for m in (data.get("data") or [])
               if isinstance(m, dict) and m.get("id")]
    except (ValueError, TypeError, AttributeError) as exc:
        result["error"] = f"响应结构异常：{exc}（前 200 字 {body[:200]!r}）"
        return result
    result["models"] = ids
    result["modelsVision"] = vision_map(ids)      # v3.172：视觉能力标注（yes/no/unknown）
    result["ok"] = bool(ids)
    if not ids:
        result["error"] = "接口返回了空的模型列表（这个 Key 可能没有可用模型）"
    return result


def _attach_available_models(result: dict, cfg: LLMConfig,
                             transport: Transport | None) -> None:
    """错误看着像「模型名不被接受」时，顺手把**可用模型名**一并放进结果。

    用户填错模型名时，他最需要知道的不是「HTTP 400」，而是「那我该填什么」。
    """
    text = (result.get("error") or "").lower()
    if not any(tok in text for tok in _MODEL_HINT_TOKENS):
        return
    probe = list_models(cfg, transport=transport)
    if probe.get("models"):
        result["availableModels"] = probe["models"]
        result["availableModelsVision"] = probe.get("modelsVision") or {}
        result["note"] = ("模型名不被这个接口接受。该 Key 可用的模型名："
                          + "、".join(probe["models"])
                          + " —— 请在「模型」里改成其中之一（或点「获取可用模型」直接选）")


def test_connection(config: LLMConfig | None = None, *, transport: Transport | None = None,
                    timeout: float = 20.0, max_tokens: int = 64) -> dict:
    """**最小连通性探测** —— 供「测试连通性」按钮用，回答"这个 Key 到底能不能用"。

    三个刻意的取舍：

    1. **用收紧的超时与 max_tokens**（默认 20 s / 64 tokens，而不是规划用的 90 s / 500）：
       点一下按钮不该等一分半、也不该花一次完整调用的钱。
    2. **retries=1，只打一次**：这是"测通不通"，重试会掩盖不稳定的网络 —— 如实报失败更有用。
    3. **绝不回显 Key**，返回体只含 Base URL / 模型名 / 延迟 / 错误原文。
       DeepSeek 这类接口的错误信息里通常带 `****9999` 形式的尾部掩码，可以直接给用户看。
    """
    cfg = config or load_config()
    result = {
        "ok": False,
        "configured": cfg.configured,
        "provider": cfg.provider,
        "baseUrl": cfg.base_url,
        "model": cfg.model,
        "latencyMs": None,
        "reply": "",
        "note": "",
        "error": "",
    }
    if not cfg.configured:
        result["error"] = "未配置 API Key（或 Base URL / 模型名为空），无法测试"
        return result

    probe = dataclasses.replace(cfg, timeout=float(timeout),
                               max_tokens=int(max_tokens), retries=1)
    started = time.time()
    try:
        content = chat_json(probe, PING_MESSAGES, transport=transport)
    except Exception as exc:                     # LLMError 与网络异常同等对待：都是"不可用"
        result["latencyMs"] = round((time.time() - started) * 1000)
        result["error"] = f"{type(exc).__name__}: {exc}" if not isinstance(exc, LLMError) \
            else str(exc)
        # v3.44：模型名填错时，把「该 Key 可用的模型名」一并带回去 ——
        # 否则用户只看到一个 400，不知道该改成什么（2026-09-24 报障）。
        _attach_available_models(result, probe, transport)
        return result

    result["latencyMs"] = round((time.time() - started) * 1000)
    result["reply"] = (content or "").strip()[:160]
    if not result["reply"]:
        result["error"] = "接口有响应但内容为空（可能被内容策略拦截，或模型名不对）"
        return result
    result["ok"] = True
    # 连通性只要求"能拿到回复"；不强制模型输出 JSON（那属于提示词遵循度，不是连通性）
    if "pong" not in result["reply"].lower():
        result["note"] = "已连通，但模型未按示例回复 JSON（不影响切片，仅提示提示词遵循度一般）"
    return result
