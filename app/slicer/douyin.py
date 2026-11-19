#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音直播间解析客户端（算法经本机实测验证）。

关键结论（改动前务必知道）：
- 解析目标是 **React Flight 载荷**（含 ``roomStore`` + ``__pace_f.push``），**不是** RENDER_DATA
- ``room.status == 2`` 才是在播（D19）；未开播时 ``web_stream_url`` 本就为空
- 请求必须带 ``Referer``，否则拉流会被拒
- 页面结构变化会导致解析失效（A10）→ 解析逻辑只放这一个文件，改一处即可

**抖音号为什么能直接监控（v3.32 更正）**：参考实现 ``live_recorder.py`` 的
``normalize_room_id`` 把**非 URL 输入当成 ``live.douyin.com/{输入}`` 的路径**去抓
（它的输入提示原文就是「请输入抖音号或抖音直播链接」）。本机实测（2026-09-23）：

- ``live.douyin.com/103282154559`` 离线时**仍返回 roomStore**（``status=4``、anchor 齐全）
  → 轮询就能判开播（这是「监控」能成立的前提）；
- ``live.douyin.com/bbFps``（真实抖音号）离线返回 anchor（``hasAnchor=True``）→ **抖音号通路成立**；
- ``live.douyin.com/<不存在的号>`` 页面 HTTP 200、roomStore 也在，但 **room/anchor 全空**
  → 必须靠这条把「无效抖音号」**明确挡掉**，否则会静默收下一个永远 ``status=0`` 的假房间。

⚠️ 之前「抓主播**主页**」才是 JS 空壳（``www.douyin.com/user/<sec_uid>`` 里 0 处房间号），
抓**直播间页**不是 —— 所以「抖音号没有反查通路」的旧结论不对，抖音号这条路走得通。
证据落盘：``tools/reports/probe_douyin_handle.json`` 与 ``probe_douyin_invalid.json``。
"""
from __future__ import annotations

import json
import re
import time

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
REFERER = "https://live.douyin.com/"
QUALITY_ORDER = ("FULL_HD1", "HD1", "SD1", "SD2")


class LiveRecorderError(Exception):
    """解析/拉流失败。所有可恢复错误都用它，调用方按需重试。"""


class DouyinBlockedError(LiveRecorderError):
    """抖音要求人机验证 / 我们正处于风控冷却期。

    与"网络抖一下"**必须分开**：抖一下重试就行，被风控时**要停手** ——
    继续请求只会把"临时要求验证"升级成更长时间的封禁（A3/A10 的实战教训）。
    调用方见到它应该拉长间隔、并如实告诉用户去浏览器里过一次验证。
    """


# ---------------------------------------------------------------- 请求节流与风控（v3.50）

# 全局冷却：命中验证后**所有**抖音请求一起停。
# 为什么是全局而不是"这个房间退避一下"：风控按**出口 IP / 设备**判，换个直播间照样被拦；
# 而且继续敲门会把临时限制拖长。
COOLDOWN_SEC = 900.0            # 命中验证后暂停 15 分钟
_COOLDOWN_UNTIL = 0.0           # time.time() 时间戳；0 = 不在冷却

# 会话级 cookie（至少把首次访问拿到的 ttwid 带上）。
# 原来是**每个请求新建 AsyncClient 且不带任何 cookie** → 抖音眼里每次都是全新陌生客户端，
# 匿名高频访问最容易被判爬虫。
# 这里只存一个纯字典：调用方会在不同线程里各自 asyncio.run()，
# 共享 AsyncClient 会踩"事件循环已关闭"，而字典没有这个问题。
_COOKIE_JAR: dict[str, str] = {}
_COOKIE_MAX = 40                # 只留最近若干个，别让字典无限长


def cooldown_left() -> float:
    """风控冷却剩余秒数（0 = 不在冷却）。给接口/界面如实展示用。"""
    return max(0.0, _COOLDOWN_UNTIL - time.time())


def enter_cooldown(seconds: float = COOLDOWN_SEC) -> None:
    """进入（或延长）风控冷却。幂等：只延长、不缩短。"""
    global _COOLDOWN_UNTIL
    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.time() + float(seconds))


def reset_cooldown() -> None:
    """手动解除冷却（用户说"我已在浏览器里过完验证"时用）。"""
    global _COOLDOWN_UNTIL
    _COOLDOWN_UNTIL = 0.0


def _before_request() -> None:
    """冷却期内**快速失败**，连请求都不发（这才是冷却的意义）。"""
    left = cooldown_left()
    if left > 0:
        raise DouyinBlockedError(
            f"抖音要求人机验证，已暂停对抖音的请求（约还剩 {left / 60:.0f} 分钟）。"
            "请用浏览器打开 https://live.douyin.com 手动过一次验证，"
            "再回来点「刷新状态」；期间继续自动请求会让限制更久。")


def _remember(cookies) -> None:
    """把响应里带回来的 cookie 存进会话 jar（无 ttwid 时这就是"身份"）。"""
    try:
        for k, v in cookies.items():
            _COOKIE_JAR[str(k)] = str(v)
    except Exception:                                          # noqa: BLE001
        return
    if len(_COOKIE_JAR) > _COOKIE_MAX:
        for k in list(_COOKIE_JAR)[: len(_COOKIE_JAR) - _COOKIE_MAX]:
            _COOKIE_JAR.pop(k, None)


def session_cookie_names() -> list[str]:
    """当前会话 jar 里的 cookie 名（冒烟/诊断用，别在业务里依赖具体名字）。"""
    return sorted(_COOKIE_JAR)


# 「返回 200 但没有 roomStore」是抖音要验证时最典型的表现，但页面文案会变，
# 所以判据不写死在 HTML 关键词上（K44：那种检查一改就失效）。
# 这里只是为了在日志里多给一点线索，**判定本身**走"解析不出 roomStore"这条路。
_VERIFY_HINTS = ("captcha", "verify", "secsdk", "滑块", "验证")


def verification_hint(text: str) -> str:
    """页面里有没有验证相关的痕迹（仅用于把日志写得更具体，不参与判定）。"""
    low = (text or "")[:6000].lower()
    for h in _VERIFY_HINTS:
        if h in low:
            return h
    return ""



def parse_web_rid(raw: str) -> str:
    """从用户输入里提取直播间编号（web_rid）。

    支持：
    - ``12345678``（纯编号）
    - ``https://live.douyin.com/12345678``（可带查询串/锚点）
    - ``live.douyin.com/12345678``（不带 scheme）

    v3.26：短链 / 主页链接 / 抖音号走 :func:`parse_room_input` + :func:`resolve_input`
    （要跟一次重定向或抓一次页面，这里做不了同步解析）。
    解析不出来返回空串，由调用方给 400。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"live\.douyin\.com/(\d{5,})", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{5,}", s):
        return s
    return ""


# ---------------------------------------------------------------- v3.26 输入解析（v3.32 扩到抖音号）
# 用户要的是「**填抖音号**就能监控开播」（因为主播什么时候开播事先不知道，
# 不可能提前拿到直播间链接）。所以入口必须吃得下五种形态。
# 实测（2026-09-23，probe_douyin_id3.json / probe_douyin_handle.json / probe_douyin_invalid.json）：
#   - live.douyin.com/<rid> 页面里能直接读到 web_rid      → 稳
#   - v.douyin.com 短链可以正常跟随重定向                 → 稳
#   - www.douyin.com/user/<sec_uid> 返回 200 但**是 JS 空壳**  → 主页这条路不通
#   - **抖音号**：把抖音号当 live.douyin.com/{抖音号} 抓**直播间页**是通的（参考实现做法）
#     （主页才是空壳；直播间页离线也返回 roomStore+anchor）
# 所以「抖音号」现在能直接监控；真的解析不出来时必须给出**能照做的提示**，不能默默失败。

INPUT_KINDS = ("empty", "web_rid", "short", "sec_uid", "handle")

_SHORT_RE = re.compile(r"v\.douyin\.com/([A-Za-z0-9_\-]{4,})")
_USER_RE = re.compile(r"(?:www\.douyin\.com|douyin\.com)/user/([A-Za-z0-9_\-]{8,})")
_SEC_UID_RE = re.compile(r"^MS4wLjAB[A-Za-z0-9_\-]{10,}$")
# v3.32：抖音号形态（字母/数字/点/下划线/连字符，2–32 位）。抖音号本身是 ASCII，
# 中文昵称不算抖音号（当昵称处理 → 明确报错，而不是拿去乱抓页面）。
#
# ⚠️ **v3.34：直播间标识的字符集是「一个事实」，必须只有一个出处**（K44）。
# 服务端路由用 :data:`RID_SEGMENT` 匹配 URL 里的标识段 —— 原来路由写死 ``(\d+)``
# 只认纯数字，v3.32 把抖音号也变成合法标识之后，``POST /api/recorders/keyis153/start``
# 直接匹配不上、返回「未实现的接口」（K66）。
RID_SEGMENT = r"[A-Za-z0-9._\-]{1,64}"
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._\-]{2,32}$")
# live.douyin.com 的保留路径段，不能当房间标识（参考实现也显式排除这三个）
_RESERVED_PATHS = {"hot_live", "category", "creator-center"}
# 页面里直播间号的所有可能形态（HTML 里常带转义反斜杠，故 ``\\?`` 容一个反斜杠）
_RID_PATTERNS = (
    r"live\.douyin\.com/(\d{5,})",
    r'web_?rid\\?"?\s*[:=]\s*\\?"?(\d{6,})',
    r'room_?id\\?"?\s*[:=]\s*\\?"?(\d{6,})',
)


def parse_room_input(raw: str) -> dict:
    """**输入分类**（不发网络请求）。返回 ``{"kind": ..., "value": ...}``。

    - ``web_rid``：直播间号 / 直播间链接（可直接录）
    - ``short``  ：``v.douyin.com/xxxx`` 分享短链（要跟重定向）
    - ``sec_uid``：主播主页链接 / ``MS4wLjABAAAA…``（要抓页面碰运气）
    - ``handle`` ：抖音号或昵称。**抖音号能直接监控**（v3.32：当
      ``live.douyin.com/{抖音号}`` 抓直播间页）；非抖音号形态的昵称才明确报错
    """
    s = (raw or "").strip()
    if not s:
        return {"kind": "empty", "value": ""}
    rid = parse_web_rid(s)
    if rid:
        return {"kind": "web_rid", "value": rid}
    m = _SHORT_RE.search(s)
    if m:
        return {"kind": "short", "value": m.group(1)}
    m = _USER_RE.search(s)
    if m:
        return {"kind": "sec_uid", "value": m.group(1)}
    if _SEC_UID_RE.match(s):
        return {"kind": "sec_uid", "value": s}
    return {"kind": "handle", "value": s}


def parse_identifier(raw: str) -> str:
    """**给「添加直播间」用的标识解析**：直播间号 / 直播间链接 / **抖音号**。

    与 :func:`parse_web_rid` 的唯一区别：**抖音号也算**（v3.32）。抖音号会原样当
    ``live.douyin.com/{抖音号}`` 的路径用（参考实现做法，本机实测可行）。

    为什么「添加」需要它：前端「解析并添加」会把解析出的标识交给 ``POST /api/recorders``，
    而那个标识对抖音号来说不是纯数字 —— 用旧的 :func:`parse_web_rid` 会被判成「不是编号」拒掉
    （K58 同类坑）。解析不出来返回空串，由调用方给 400。
    """
    rid = parse_web_rid(raw)
    if rid:
        return rid
    s = (raw or "").strip().lstrip("@").split("?", 1)[0].strip("/")
    if _HANDLE_RE.match(s) and s not in _RESERVED_PATHS:
        return s
    return ""


def _rid_from_text(text: str) -> str:
    """从一段 HTML / URL 里找直播间号。找不到返回空串（**不猜、不编造**）。"""
    for pat in _RID_PATTERNS:
        m = re.search(pat, text or "")
        if m:
            return m.group(1)
    return ""


# v3.25：画幅识别要用的「正在连线/PK」证据键。
# 刻意只认这几个**明确表示 PK 会话**的键 —— 抖音页面里带 "linkmic" 的字段
# 有一堆（布局、版本号…），照关键字宽泛匹配会把**所有**直播间都判成 PK（K57）。
PK_KEYS = ("pk_id", "pkid", "pk_room_id", "pkanchorid", "battle_id",
           "battlesetting", "pk_status", "in_pk", "is_pk")


def _looks_pk(node, depth: int = 0) -> bool:
    """嵌套结构里找 PK 会话证据（``pk_id`` 之类为真值才算）。"""
    if depth > 10:
        return False
    if isinstance(node, dict):
        for key, value in node.items():
            k = str(key).lower()
            if k in PK_KEYS and value not in (None, "", 0, "0", [], {}):
                return True
            if _looks_pk(value, depth + 1):
                return True
    elif isinstance(node, list):
        for value in node:
            if _looks_pk(value, depth + 1):
                return True
    return False


def _nested_dicts(node, depth: int = 0):
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _nested_dicts(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _nested_dicts(v, depth + 1)


def extract_page_state(page: str) -> dict:
    """从直播间 HTML 里抽出 React Flight 载荷中的 roomStore。"""
    candidates = []
    for script in re.findall(r"<script[^>]*>(.*?)</script>", page, re.S | re.I):
        if "roomStore" not in script or "__pace_f.push" not in script:
            continue
        start, end = script.find("("), script.rfind(")")
        if start < 0 or end <= start:
            continue
        try:
            flight = json.loads(script[start + 1:end])[1]
            if not isinstance(flight, str) or ":" not in flight:
                continue
            root, _ = json.JSONDecoder().raw_decode(flight.split(":", 1)[1])
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _nested_dicts(root):
            room_store = item.get("roomStore")
            if isinstance(room_store, dict) and isinstance(room_store.get("roomInfo"), dict):
                candidates.append(item)
    if not candidates:
        raise LiveRecorderError("抖音未返回直播间数据，可能触发了网页验证（A10）")
    return max(candidates,
               key=lambda i: len((i.get("roomStore") or {}).get("roomInfo") or {}))


class DouyinClient:
    """直播间信息 / 流地址解析。所有方法为 async，需在事件循环里调用。"""

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": UA, "Referer": REFERER}

    async def _get_page(self, web_rid: str) -> str:
        url = f"https://live.douyin.com/{web_rid}"
        return (await self._fetch(url, self.headers))[1]

    async def _fetch(self, url: str, headers: dict) -> tuple[str, str]:
        """带**会话 cookie** 的 GET，返回 ``(最终URL, 文本)``。

        v3.50：① 冷却期内直接抛 :class:`DouyinBlockedError`，**连请求都不发**；
        ② 复用会话 cookie（首次访问拿到的 ttwid 之后一路带上）——
        原来每次都是"全新陌生客户端且零 cookie"，正是匿名被风控判爬虫的典型特征。
        """
        _before_request()
        try:
            async with httpx.AsyncClient(headers=headers, cookies=dict(_COOKIE_JAR),
                                         follow_redirects=True,
                                         timeout=self.timeout) as c:
                r = await c.get(url)
            _remember(r.cookies)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 4xx/5xx 之一是抖音在拦：也进冷却，但**不宣称**是验证（如实区分）
            if e.response is not None and e.response.status_code in (403, 429, 503):
                enter_cooldown()
                raise DouyinBlockedError(
                    f"抖音返回 {e.response.status_code}，判定为被限流，已暂停请求 "
                    f"{COOLDOWN_SEC / 60:.0f} 分钟。稍后再试；别连续重试。") from e
            raise LiveRecorderError(f"打不开 {url}：{e}") from e
        except httpx.HTTPError as e:
            raise LiveRecorderError(f"打不开 {url}：{e}") from e
        return str(r.url), r.text

    async def _state_for(self, web_rid: str) -> dict:
        """抓页面 + 解析出 React Flight 状态；**解析不出房间数据时按风控处理**。

        为什么把这一步收成一个方法：三个入口（room_info / resolve_identifier /
        stream_urls）都要"抓+解析"，风控判定必须**每一条路都生效**，
        写在各调用点迟早漏一路（K44 同源）。判据是"HTTP 200 但没有 roomStore"，
        不写死页面关键词（那种检查抖音一改文案就失效）。
        """
        text = await self._get_page(web_rid)
        try:
            return extract_page_state(text)
        except LiveRecorderError:
            enter_cooldown()
            hint = verification_hint(text)
            raise DouyinBlockedError(
                "抖音没有返回直播间数据（多半是在要求人机验证"
                + (f"，页面命中「{hint}」" if hint else "")
                + f"）。已暂停对抖音的请求 {COOLDOWN_SEC / 60:.0f} 分钟 —— "
                "请用浏览器打开 https://live.douyin.com 手动过一次验证，再回来重试；"
                "期间继续自动请求会把限制拖得更久。") from None

    @staticmethod
    def _info_from(web_rid: str, ri: dict) -> dict:
        """把 roomStore 的 ``roomInfo`` 归一成前端用的房间信息（唯一实现）。"""
        room = ri.get("room") if isinstance(ri.get("room"), dict) else {}
        anchor = ri.get("anchor") if isinstance(ri.get("anchor"), dict) else {}
        status = int(room.get("status") or 0)
        return {
            "webRid": web_rid,
            "roomId": str(ri.get("roomId") or room.get("id_str") or ""),
            "owner": str(anchor.get("nickname") or anchor.get("nick_name") or f"抖音主播{web_rid}"),
            "title": str(room.get("title") or "未命名直播"),
            "status": status,
            "live": status == 2,
            # v3.25：连线/PK 是画幅判定的辅助信号（PK 常是 3:4，见 app.slicer.aspect）
            "pk": _looks_pk(ri),
            "channelUrl": f"https://live.douyin.com/{web_rid}",
            # v3.32：页面是否真的带有主播信息（用于「无效抖音号」校验 + UI 展示）
            "hasAnchor": bool(anchor),
        }

    async def room_info(self, web_rid: str) -> dict:
        """房间信息；``live=True`` 表示正在直播（D19 判定依据 ``status==2``）。

        监测轮询用这个（**不做 room/anchor 校验**）：偶发拿不到 anchor 时不该中断监控，
        交给调用方按 LiveRecorderError 重试即可。
        """
        state = await self._state_for(web_rid)      # v3.50：风控判定在这一处（唯一出处）
        return self._info_from(web_rid, state["roomStore"]["roomInfo"])

    async def resolve_identifier(self, identifier: str) -> dict:
        """解析并**校验**一个直播间标识（直播间号 / **抖音号**），返回房间信息。

        v3.32：无效标识（页面 HTTP 200、roomStore 也在，但 ``room``/``anchor`` 全空）
        必须**明确失败** —— 否则会把一个永远 ``status=0`` 的假房间静默加进监控列表。
        实测证据：``probe_douyin_invalid.json`` 里不存在的号就是 room/anchor 全空。
        **只在「解析并添加」这条路上校验**；监测轮询仍走 :meth:`room_info`（不校验）。
        """
        state = await self._state_for(identifier)   # v3.50：风控判定在这一处（唯一出处）
        ri = state["roomStore"]["roomInfo"]
        room = ri.get("room") if isinstance(ri.get("room"), dict) else {}
        anchor = ri.get("anchor") if isinstance(ri.get("anchor"), dict) else {}
        if not room and not anchor:
            raise LiveRecorderError(
                f"「{identifier}」没有对应的抖音直播间：可能是抖音号写错，或该账号已改名/注销。"
                "请核对「抖音号」（抖音 App → 我 → 资料页里那串字母数字），"
                "或直接粘贴直播间链接（live.douyin.com/…）／开播时复制的分享短链。")
        return self._info_from(identifier, ri)

    async def _get_text(self, url: str, referer: str | None = None) -> tuple[str, str]:
        """取页面正文，返回 ``(最终URL, 文本)``（跟随重定向）。"""
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        return await self._fetch(url, headers)      # v3.50：走同一个带 cookie/冷却的通道

    async def resolve_input(self, raw: str) -> dict:
        """把用户贴的**任意形态**解析成直播间信息（v3.26；v3.32 起支持抖音号）。

        吃得下：**抖音号** / 直播间号 / 直播间链接 / ``v.douyin.com`` 分享短链 /
        主播主页链接 / sec_uid。抖音号直接当 ``live.douyin.com/{抖音号}`` 抓**直播间页**
        （参考实现做法，实测可行）。解析不出来就抛 :class:`LiveRecorderError` ——
        **错误消息必须能照着做**（K58：宁可明确失败，也不要「解析成功但拿到空号」这种静默失败）。
        """
        parsed = parse_room_input(raw)
        kind, value = parsed["kind"], parsed["value"]

        if kind == "empty":
            raise LiveRecorderError("请先填写抖音号、直播间链接或分享短链")
        if kind == "web_rid":
            info = await self.resolve_identifier(value)
            info["inputKind"] = "web_rid"
            return info

        if kind == "short":
            final, text = await self._get_text(f"https://v.douyin.com/{value}/", REFERER)
            rid = _rid_from_text(final) or _rid_from_text(text)
            if not rid:
                raise LiveRecorderError(
                    f"这个短链打开后没有指向直播间（最终落在 {final}）。"
                    "请确认贴的是**直播间**的分享链接，并且主播正在开播。")
            try:
                info = await self.resolve_identifier(rid)
            except LiveRecorderError as exc:
                raise LiveRecorderError(
                    f"短链指向的直播间号 {rid} 读不到房间信息：{exc}。可直接在抖音里打开直播间，"
                    "复制地址栏的 live.douyin.com/… 链接。") from exc
            info["inputKind"] = "short"
            info["viaUrl"] = final
            return info

        if kind == "sec_uid":
            final, text = await self._get_text(
                f"https://www.douyin.com/user/{value}", "https://www.douyin.com/")
            rid = _rid_from_text(final) or _rid_from_text(text)
            if not rid:
                raise LiveRecorderError(
                    "抖音没有在主播主页里暴露直播间号（实测：主页是 JS 渲染 + 需登录，"
                    "整页 0 处直播间号）。请直接填**抖音号**（抖音 App → 我 → 资料页），"
                    "或在主播开播时复制直播间链接（live.douyin.com/…）/分享短链。")
            info = await self.resolve_identifier(rid)
            info["inputKind"] = "sec_uid"
            info["viaUrl"] = final
            return info

        # handle：抖音号 / 昵称（v3.32：抖音号直接当 live.douyin.com/{抖音号} 试）
        handle = value.lstrip("@").split("?", 1)[0].strip("/")
        if _HANDLE_RE.match(handle) and handle not in _RESERVED_PATHS:
            info = await self.resolve_identifier(handle)   # 无效号在这里明确报错（含照做提示）
            info["inputKind"] = "handle"
            return info
        raise LiveRecorderError(
            f"「{value}」不能当抖音号用：抖音号只能由字母/数字/点/下划线/连字符组成（2–32 位）。"
            "请填抖音 App →「我」→ 资料页里的「抖音号」，或粘贴直播间链接 / 分享短链。")

    async def stream_urls(self, web_rid: str) -> dict:
        """``{flv: {quality: url}, hls: {...}, default: str}``；未在播返回空字典。"""
        state = await self._state_for(web_rid)      # v3.50：风控判定在这一处（唯一出处）
        ri = state["roomStore"]["roomInfo"]
        room = ri.get("room") if isinstance(ri.get("room"), dict) else {}
        if int(room.get("status") or 0) != 2:
            return {}
        wsu = ri.get("web_stream_url") or {}
        flv = wsu.get("flv_pull_url") or {}
        hls = wsu.get("hls_pull_url_map") or {}
        return {
            "flv": {q: flv[q] for q in QUALITY_ORDER if q in flv},
            "hls": {q: hls[q] for q in QUALITY_ORDER if q in hls},
            "default": wsu.get("default_resolution") or "HD1",
        }

    def pick(self, urls: dict, quality: str = "FULL_HD1") -> tuple[str, str]:
        """按 D17 选档：优先 FULL_HD1，缺失依次降级。返回 ``(quality, url)``。"""
        flv = urls.get("flv") or {}
        for q in (quality,) + tuple(x for x in QUALITY_ORDER if x != quality):
            if q in flv:
                return q, flv[q]
        raise LiveRecorderError("直播间已开播，但没有解析到可录制的播放流")

    @staticmethod
    def headers_for(web_rid: str) -> str:
        """ffmpeg/ffprobe 的 ``-headers`` 值（CRLF 结尾；缺 Referer 会被拒）。"""
        return f"User-Agent: {UA}\r\nReferer: {REFERER}{web_rid}\r\n"
