#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取各平台**当前登录账号**的昵称（v4.18，）。

为什么不能靠手填：用户 2026-11-02 报「我登录了还是显示未设置账号昵称，应该显示对应
登录账号的昵称」。账号备注是给人看的别名，**真实昵称只有平台知道**。

怎么读：Chromium 127+ 的 cookie 是 App-Bound 加密，第三方进程解不开（本项目
`browser_cookies.py` 已踩过）。所以这里复用**浏览器进程自己**：
persistent context 打开该平台 profile（与发布执行器同一份登录态）→ 打开创作页 →
**旁听页面自己发出的 JSON 响应**（不自己拼签名参数，平台怎么签就怎么用）→
按注册表里的字段路径取昵称 / 账号 ID。

红线：**只读**。不点发布/投稿、不改资料、不发评论；浏览器 headless，不闪窗口。
"""
from __future__ import annotations

import json
import threading
from typing import Any

from app.common.paths import ws
from app.editor.backend import platforms

#: 同一时刻只允许一个读取会话（四个 profile 各自独立，但 Edge 进程重、也避免抢资源）
_LOCK = threading.Lock()

#: 单次读取最多等多久（页面 SPA 拉用户信息通常在 2~8 秒内）
WAIT_SEC = 12.0


def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in str(path).split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _first(data: dict, paths: tuple[str, ...]) -> Any:
    for p in paths:
        v = _dig(data, p)
        if v not in (None, "", 0, "0"):
            return v
    return None


def _local_storage(page, keys: tuple[str, ...]) -> dict:
    """平台把用户信息塞进 localStorage 时的兜底（小红书就是）。"""
    if not keys:
        return {}
    try:
        return page.evaluate(
            """(keys) => {
                const out = {};
                for (const k of keys) {
                    const v = localStorage.getItem(k);
                    if (v) out[k] = v;
                }
                return out;
            }""", list(keys)) or {}
    except Exception:                                         # noqa: BLE001
        return {}


def _flat_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """接口路径是 ``data.userName``，但 localStorage 往往直接是 ``userName``。
    两种都试，别因为前缀不同就取不到（实测小红书就是这种）。"""
    out = list(paths)
    for p in paths:
        if p.startswith("data."):
            out.append(p[len("data."):])
    return tuple(dict.fromkeys(out))


def _from_storage(spec, raw: dict) -> dict:
    for key in spec.account_storage_keys:
        text = raw.get(key)
        if not text:
            continue
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = _first(data, _flat_paths(spec.nickname_paths))
        if not name and isinstance(data.get("user"), dict):
            name = _first(data["user"], _flat_paths(spec.nickname_paths))
        if name:
            return {"nickname": str(name),
                    "accountId": _first(data, _flat_paths(spec.account_id_paths)),
                    "avatarUrl": _first(data, _flat_paths(spec.avatar_paths))}
    return {}


def read_account(platform: str, *, wait_sec: float = WAIT_SEC) -> dict:
    """读某平台当前登录账号。返回 ``{ok, platform, nickname, accountId, avatarUrl, ...}``。

    失败**必须说清原因**（K69）：没登录 / 浏览器占着 profile / 页面没返回用户信息 / 缺 playwright。
    """
    spec = platforms.get(platform)
    out: dict = {"ok": False, "platform": spec.key, "platformName": spec.name,
                 "nickname": "", "accountId": None, "avatarUrl": None,
                 "locked": False, "error": "", "source": ""}
    # 先判登录态：没登录就别起浏览器（快、也不闪）
    try:
        from app.editor.backend import publisher
        st = publisher.login_state(spec.key)
    except Exception as exc:                                  # noqa: BLE001
        out["error"] = f"读登录态失败：{type(exc).__name__}: {exc}"
        return out
    if not st.get("loggedIn"):
        out["error"] = f"还没登录{spec.name}（先点卡片上的「登录/换号」）"
        out["locked"] = bool(st.get("locked"))
        return out

    from app.editor.backend import publisher                 # 延迟：避免模块级循环 import
    if not _LOCK.acquire(timeout=wait_sec):
        out["error"] = "另一个平台的昵称正在读取，请稍后重试"
        return out
    pw = ctx = page = None
    hits: list[dict] = []
    try:
        from playwright.sync_api import sync_playwright
        prof = publisher.profile_dir(spec.key)
        pw = sync_playwright().start()
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(prof), channel="msedge", headless=True,
                viewport={"width": 1400, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:                              # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}".lower()
            locked = any(k in text for k in (
                "processsingletonlock", "user data directory", "already in use",
                "target page, context or browser has been closed", "拒绝访问", "permission"))
            out["locked"] = locked
            out["error"] = ("该平台的浏览器正开着（登录目录被占用）。关掉它再点昵称重试。"
                            if locked else f"打不开 Edge：{type(exc).__name__}")
            return out

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(resp):
            try:
                if spec.account_api_match not in resp.url:
                    return
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct:
                    return
                body = resp.json()
                if isinstance(body, dict):
                    hits.append(body)
            except Exception:                                 # noqa: BLE001
                pass

        page.on("response", on_resp)
        page.goto(spec.account_page or spec.creator_url,
                  wait_until="domcontentloaded", timeout=60000)
        waited = 0.0
        while waited < wait_sec:
            page.wait_for_timeout(500)
            waited += 0.5
            if hits:
                # 拿到就再等 300ms，避免只截到半截的骨架响应
                page.wait_for_timeout(300)
                break

        for body in hits:
            name = _first(body, spec.nickname_paths)
            if name:
                out.update({"ok": True, "nickname": str(name),
                            "accountId": _first(body, spec.account_id_paths),
                            "avatarUrl": _first(body, spec.avatar_paths),
                            "source": "api"})
                return out

        # 兜底：localStorage 里的用户信息（小红书页面会写 USER_INFO_FOR_BIZ）
        got = _from_storage(spec, _local_storage(page, spec.account_storage_keys))
        if got.get("nickname"):
            out.update({"ok": True, "nickname": got["nickname"],
                        "accountId": got.get("accountId"),
                        "avatarUrl": got.get("avatarUrl"), "source": "storage"})
            return out

        out["error"] = (f"页面没有返回{spec.name}的用户信息（可能登录态已失效，"
                        f"或平台改版）；请点「登录/换号」重新登录后再试")
        return out
    except Exception as exc:                                  # noqa: BLE001
        out["error"] = f"读取昵称失败：{type(exc).__name__}: {exc}"
        return out
    finally:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        try:
            if pw is not None:
                pw.stop()
        except Exception:
            pass
        _LOCK.release()
