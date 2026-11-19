#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把刚启动的浏览器窗口**拉到最上层**（v4.20，）。

用户 2026-11-02：「点击登录/换号时，这个新开的浏览器窗确实有正确打开，
但是没有弹出来到最上层……还得额外点击一次下方任务栏才能进入窗口」。

为什么不能只靠 `subprocess.Popen`：Windows 的前台窗口锁（foreground lock）允许
**当前前台进程**把窗口带到前台，但浏览器是另一个进程，`CreateProcess` 本身不会激活它。
这里用 EnumWindows 找到**启动前后新增的 Edge 窗口**（或标题变成该平台的已有窗口），
再 `ShowWindow` + `SetForegroundWindow`（必要时 AttachThreadInput 绕过前台锁）拉上来。

**只操作窗口**：不点页面、不读页面内容、不影响登录流程。找不到窗口就如实返回 False，
不阻塞登录（窗口本身已经开了，只是没置顶）。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

SW_RESTORE = 9
SW_SHOW = 5
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


def _user32():
    return ctypes.windll.user32


def _edge_windows() -> list[dict]:
    """枚举当前可见的 Edge 顶层窗口（按 exe 名过滤，避免误抓其它 Chromium 应用）。"""
    u = _user32()
    k = ctypes.windll.kernel32
    out: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # 进程 exe 名：QueryFullProcessImageNameW（x64 下必须声明 argtypes）
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            ph = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            exe = ""
            if ph:
                try:
                    size = wintypes.DWORD(1024)
                    ebuf = ctypes.create_unicode_buffer(1024)
                    k.QueryFullProcessImageNameW.argtypes = [
                        wintypes.HANDLE, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_wchar), ctypes.POINTER(wintypes.DWORD)]
                    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
                    if k.QueryFullProcessImageNameW(ph, 0, ebuf, ctypes.byref(size)):
                        exe = ebuf.value
                finally:
                    k.CloseHandle(ph)
            if Path(exe).name.lower() != "msedge.exe":
                return True
            out.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value),
                        "exe": exe})
        except Exception:                                     # noqa: BLE001
            pass
        return True

    u.EnumWindows(cb, 0)
    return out


def bring_to_front(hwnd: int) -> bool:
    """把指定窗口拉到前台（恢复最小化 + 置顶 + 抢焦点）。返回是否已在前台。"""
    u = _user32()
    try:
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        else:
            u.ShowWindow(hwnd, SW_SHOW)
        u.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        try:
            u.AllowSetForegroundWindow(-1)                    # ASFW_ANY
        except Exception:                                     # noqa: BLE001
            pass
        # 前台锁的老办法：轻点一下 Alt 再 SetForegroundWindow，成功率明显更高
        try:
            u.keybd_event(VK_MENU, 0, 0, 0)
            u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:                                     # noqa: BLE001
            pass
        fg = u.GetForegroundWindow()
        fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
        tg_tid = u.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if fg_tid and tg_tid and fg_tid != tg_tid:
            attached = bool(u.AttachThreadInput(fg_tid, tg_tid, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            u.SetFocus(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(fg_tid, tg_tid, False)
        if u.GetForegroundWindow() != hwnd:
            try:
                u.SwitchToThisWindow(hwnd, True)              # 兜底（老 API）
            except Exception:                                 # noqa: BLE001
                pass
        return u.GetForegroundWindow() == hwnd
    except Exception:                                         # noqa: BLE001
        return False


def _pick(cands: list[dict], before: dict[int, str], hints: tuple[str, ...],
          *, require_hint: bool = False) -> dict | None:
    """从窗口列表里挑目标：优先「新增/标题变了的」且标题含平台提示词。纯函数，可离线测。

    ``require_hint=True`` 时**不接受**「只有唯一新窗口但没有提示词」的兜底：
    Edge 新 profile 启动的头几百毫秒会先出一个「无标题 - 用户配置 1」的空壳窗口，
    这时页面标题还没加载，拿它去置顶就会「激活了错误的窗口」（实测踩过）。
    """
    def hit(w):
        title = w.get("title") or ""
        return any(h and h.lower() in title.lower() for h in hints)

    fresh = [w for w in cands if w["hwnd"] not in before or before[w["hwnd"]] != w["title"]]
    for w in fresh:
        if hit(w):
            return w
    if require_hint:
        return None
    if len(fresh) == 1:
        return fresh[0]
    # ⚠️ 不要回退到「所有窗口里找标题含平台词的」：用户本来就开着一个该平台窗口时，
    #    启动失败也会把那个旧窗口拉上来，看着像成功（会骗人）。宁可返回找不到。
    return None


def activate_new_edge_window(before: dict[int, str], hints: tuple[str, ...],
                             *, wait_sec: float = 6.0,
                             lister=None, activator=None) -> dict:
    """等新 Edge 窗口出现并拉到前台。

    :param before: 启动前 ``{hwnd: title}`` 快照（由 :func:`snapshot_edge_windows` 取）
    :param hints: 标题里可能出现的平台词（如 ``("小红书", "创作服务")``）
    :return: ``{ok, activated, hwnd, title, waitedSec, reason}``
    """
    lister = lister or _edge_windows
    activator = activator or bring_to_front
    t0 = time.time()
    fallback: dict | None = None
    while time.time() - t0 < max(0.5, wait_sec):
        try:
            cands = lister()
            # ① 先等标题里出现平台词（页面真正加载完的那个窗口）
            target = _pick(cands, before, hints, require_hint=True)
            if target is None:
                # ② 记一个兜底（唯一新窗口），但**至少等 4 秒**再用，避免抓到空壳
                fallback = fallback or _pick(cands, before, hints)
                if fallback and (not hints or (time.time() - t0) >= 4.0):
                    target = fallback
        except Exception:                                     # noqa: BLE001
            target = None
        if target:
            ok = bool(activator(target["hwnd"]))
            return {"ok": True, "activated": ok, "hwnd": target["hwnd"],
                    "title": target.get("title") or "",
                    "waitedSec": round(time.time() - t0, 2), "reason": ""}
        time.sleep(0.15)
    if fallback:
        ok = bool(activator(fallback["hwnd"]))
        return {"ok": True, "activated": ok, "hwnd": fallback["hwnd"],
                "title": fallback.get("title") or "",
                "waitedSec": round(time.time() - t0, 2),
                "reason": "只找到新窗口、标题未出现平台名，仍尝试置顶"}
    return {"ok": False, "activated": False, "hwnd": None, "title": "",
            "waitedSec": round(time.time() - t0, 2),
            "reason": "没找到新打开的 Edge 窗口（登录页仍已打开，可从任务栏进入）"}


def snapshot_edge_windows() -> dict[int, str]:
    """启动前快照：``{hwnd: title}``。取不到就回空 dict（不影响登录）。"""
    try:
        return {w["hwnd"]: w["title"] for w in _edge_windows()}
    except Exception:                                         # noqa: BLE001
        return {}
