#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.78：cover_designer_window.py 重写为**模块级函数**版。

闭包挂属性（app.open_cover_designer = ...）在 pywebview 的 js_api 枚举下不可见
（pywebview 用 inspect 枚举，只认真方法）⇒ 桥调用报 has no attribute。
"""
from __future__ import annotations

import threading
from pathlib import Path

from app.common.paths import ws


def _designer_html_path() -> Path:
    return Path(__file__).parent / "assets" / "cover" / "designer.html"


_APP = None            # register 时存的 DesktopApi 引用（self 兜底用）
_PENDING: list[dict] = []   # 待创建的子窗口参数（open_cover_designer 入队，消费循环出队）
# v4.78：最近一次打开的成片名 —— 子窗 JS 用 `cover_designer_target()` 桥方法拿它
#（实测 file:// URI 的 query 在 WebView2 里会丢，不能只靠 location.search）
_LAST_FILM: str = ""
#: v4.88：是否见过主窗（防止启动早期误判"没有主窗"就把子窗收掉）
_SEEN_MAIN: bool = False


def _is_designer(w) -> bool:
    return str(getattr(w, "title", "")).startswith("封面设计")


def reap_orphans() -> int:
    """主窗已经不在了 ⇒ 把工具子窗一起收掉，返回收掉几个。

    v4.88（用户 2026-11-14：「关闭主程序时这个封面设计的窗口也没有跟随关闭」）：
    给主窗绑 ``events.closing/closed`` 是**第一道**，但实测 ``Window.destroy()``
    （脚本关窗/程序内关窗）在这台机器上并没有把事件派到我们的 handler —— 于是主窗没了、
    子窗还挂在桌面上，而且它现在无边框，用户抓不到把手关不掉。
    所以加这道**不依赖 pywebview 事件**的兜底：常驻循环（``drain_pending``）每 0.3 秒看一眼，
    只剩子窗时全部销毁。进程退出时窗口本就会消失，这里的价值是"主窗关了、进程还在"的那种情况。
    """
    import webview
    global _SEEN_MAIN
    wins = list(webview.windows)
    if any(not _is_designer(w) for w in wins):
        _SEEN_MAIN = True
        return 0
    if not _SEEN_MAIN or not wins:
        return 0
    n = 0
    for w in wins:
        if not _is_designer(w):
            continue
        try:
            w.destroy()
            n += 1
        except Exception:                              # noqa: BLE001
            pass
    return n


def open_cover_designer(self, film_name: str) -> dict:
    """打开**封面设计子窗口**（pywebview 原生多窗口，非抽屉）。

    必须在子线程里 create_window —— 主窗口的事件循环正在跑，不能阻塞。
    片名经 query 传给子窗 JS（designer.html 自己解析 location.search）。
    ``self`` 是 DesktopApi（js_api 用同一个桥：slicer/media/设置全可用）。
    """
    import webview

    html = _designer_html_path()
    if not html.is_file():
        return {"ok": False, "error": "designer.html 缺失（assets/cover/）"}
    try:
        # 成片统一由 ws.resolve_film 解析（只认 `edits/` 前缀与裸文件名）
        film = ws.resolve_film(str(film_name or ""))
        if film is None:
            return {"ok": False, "error": f"成片不存在：{film_name}"}
        v = int(film.stat().st_mtime)
        # ⚠️ 只能用 **fragment**（#v=）做防缓存，**不能用 query**：
        #    pywebview/WebView2 会把 `?` 编码成 `%3F`，整段被当成文件名 ⇒ 打开的是
        #    chrome-error 错误页（v4.78 实测：title 变成 `designer.html%3Ffilm=...`）。
        #    片名走桥方法 cover_designer_target()，不进 URL。
        url = html.as_uri() + f"#v={v}"
        global _LAST_FILM
        # 子窗 JS 靠 cover_designer_target() 拿它，再原样回传给 /api/edits/cover* —— 所以必须给
        # **相对路径**（成片文件名直接传，不要只传 video.mp4）。
        try:
            _LAST_FILM = film.relative_to(ws.root).as_posix()
        except ValueError:
            _LAST_FILM = film.name
    except Exception as exc:                   # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    # v4.78 P-C：**同步 create_window** —— pywebview 的 js_api 方法本来就是被
    # GUI 线程 Invoke 调用的（winforms 后端），在这里 create_window 是官方多窗口姿势。
    # ⚠️ 反例（实测卡死）：threading.Thread 里 create、或 webview.start(func) 的
    #    func 线程里 create —— 两者都不是 GUI 线程。
    #
    # v4.88（用户：窗口滑到屏幕外就拉不回来、关主程序子窗还不跟随）：
    # ① 已经开着同一条片子的子窗 ⇒ **不重复开**，先把它拉回屏幕内（顶栏是唯一把手，
    #    跑出去就抓不到；点一次「封面图」＝ 一键复位）；
    # ② 开的是另一条片子 ⇒ 先干掉旧的再开新的，避免一堆子窗叠着。
    for w in list(webview.windows):
        title = str(getattr(w, "title", ""))
        if not title.startswith("封面设计"):
            continue
        if title.endswith(film.stem):
            moved = _ensure_visible(w)
            try:
                w.restore()
            except Exception:                          # noqa: BLE001
                pass
            return {"ok": True, "reused": True, "movedBack": moved}
        try:
            w.destroy()
        except Exception:                              # noqa: BLE001
            pass
    _w, _h = _designer_window_size()
    try:
        _show = film.stem
        webview.create_window(
            title=f"封面设计 · {_show}",
            url=url,
            js_api=self,                       # 同一个桥：slicer/media/设置全可用
            # v4.81：尺寸**按屏幕工作区自适应**（见 _designer_window_size 里的 125% DPI 教训）；
            # background_color 与页面底色一致 —— 否则 WebView2 首帧会闪一下白底。
            width=_w, height=_h,
            min_size=(720, 620),
            # v4.90：与子窗页面 --bg 保持一致（差异化后的 #0e141d）——
            # 首帧防白闪的底色若还是主窗色，开窗瞬间会闪一下。
            background_color="#0e141d",
            text_select=True,
            # v4.87（用户 2026-11-14 截图圈出标题栏：「上面这个展示能不能去掉…好丑」）：
            # 页面内部**已经有自绘顶栏**（🎬 封面设计 + 片名 + 比例药丸 + ✕），
            # 系统标题栏是纯重复 ⇒ 去掉。代价是失去系统边框/最小化按钮，
            # 所以配套：① 顶栏当拖动把手（designer_move_window）；
            #          ② 关闭仍走内部 ✕（close_cover_designer）与 Esc。
            # ⚠️ easy_drag 必须 False：它会把**画布里的元素拖动**一起抢走（拖 emoji 变拖窗）。
            frameless=True,
            easy_drag=False,
            shadow=True,
        )
        return {"ok": True}
    except Exception as exc:                   # noqa: BLE001
        try:
            self.log(f"[cover-designer] 子窗口创建失败：{exc}")
        except Exception:                      # noqa: BLE001
            pass
        return {"ok": False, "error": str(exc)}


def _designer_window_size() -> tuple[int, int]:
    """按**屏幕工作区**算子窗尺寸（逻辑像素，与 pywebview 的口径一致）。

    为什么不能写死：v4.81 首版写 900×1000，在 125% 缩放的 1920×1080 屏上
    物理高度 = 1000×1.25 = 1250 > 1080 ⇒ 底栏「生成封面」直接被切到屏幕外（实测截图）。
    口径：系统 DPI 缩放后的工作区（已扣掉任务栏），再留出标题栏与阴影的余量。
    """
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    work_w, work_h, dpi = 1920, 1080, 96
    try:
        r = RECT()
        # SPI_GETWORKAREA = 0x0030（返回物理像素，已扣任务栏）
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            work_w, work_h = r.right - r.left, r.bottom - r.top
        dpi = int(ctypes.windll.user32.GetDpiForSystem() or 96)
    except Exception:                                  # noqa: BLE001 — 拿不到就按 1080p@100%
        pass
    scale = (dpi / 96.0) if dpi else 1.0
    lw, lh = work_w / scale, work_h / scale             # 逻辑像素
    # 宽度留 48、高度留 72：后者 = 标题栏(~30) + 边框/阴影 + 任务栏取整误差。
    # v4.81 第一版只留 46，实测底栏还是压到任务栏下面（截图里「生成封面」看不见）。
    width = int(max(800, min(960, lw - 48)))
    height = int(max(660, min(960, lh - 72)))
    return width, height


def cover_emoji_url(code: str) -> dict:
    """子窗口用：把 emoji PNG 变成 **data URI**（assets/emoji 不在 ws 下，走不了媒体流）。

    128px PNG 约 9KB ⇒ base64 约 12KB，按需取、前端会缓存。
    """
    import base64
    code = str(code or "").strip().lower()
    if not code or "/" in code or "\\" in code or ".." in code:
        return {"ok": False, "error": "非法 emoji 码点"}
    p = Path(__file__).parent / "assets" / "emoji" / f"{code}.png"
    if not p.is_file():
        return {"ok": False, "error": f"缺图：{code}.png"}
    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return {"ok": True, "url": "data:image/png;base64," + b64}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _work_area() -> tuple[int, int, int, int]:
    """屏幕工作区（**逻辑像素**）：``(left, top, right, bottom)``，已扣任务栏。

    pywebview 的 ``Window.x/y/move`` 用逻辑像素（与 DPI 缩放后的口径一致），
    而 SPI_GETWORKAREA 返回物理像素 ⇒ 这里统一换算，别混着用。
    """
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    r = RECT()
    ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    if not ok:
        return 0, 0, 1920, 1080
    dpi = 96
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem() or 96)
    except Exception:                                  # noqa: BLE001
        pass
    s = (dpi / 96.0) or 1.0
    return int(r.left / s), int(r.top / s), int(r.right / s), int(r.bottom / s)


#: 窗口允许出界多少：左右各留这么宽（够抓到顶栏）、顶边必须完整可见
KEEP_X, KEEP_TOP = 80, 44


def _clamp_pos(x: float, y: float, w: int, h: int) -> tuple[int, int]:
    """把窗口位置夹进「顶栏永远抓得到」的范围。

    v4.88（用户 2026-11-14）：「这个封面设计的窗口不小心滑到外面就无法拉下来关闭了」——
    无边框后顶栏是**唯一**的拖动把手，一旦整条滑出屏幕就再也抓不回来。
    所以：顶边不得高出工作区（顶栏必须可见），左右至少留 KEEP_X 宽在屏内，底边不管
    （窗口本来就比屏幕矮，往下拖自然被任务栏挡住）。
    """
    L, T, R, B = _work_area()
    nx = max(L - w + KEEP_X, min(float(x), R - KEEP_X))
    ny = max(T, min(float(y), B - KEEP_TOP))
    return int(round(nx)), int(round(ny))


def _ensure_visible(w) -> bool:
    """把已经跑出屏幕的窗口拉回来（返回是否真的动过）。"""
    try:
        x, y = int(getattr(w, "x", 0) or 0), int(getattr(w, "y", 0) or 0)
        ww, hh = int(getattr(w, "width", 900) or 900), int(getattr(w, "height", 900) or 900)
    except Exception:                                  # noqa: BLE001
        return False
    nx, ny = _clamp_pos(x, y, ww, hh)
    if (nx, ny) == (x, y):
        return False
    try:
        w.move(nx, ny)
        return True
    except Exception:                                  # noqa: BLE001
        return False


def sync_theme(theme: str) -> dict:
    """v5.1：把主题推给**已开着**的封面子窗。

    需求（用户）：「这个封面设计的子窗口没有和主题色联动，我换到白色主题时，这个子窗口的色调还是黑色的」。
    子窗自己也会读 `localStorage.theme`（file:// 同源共享），但**运行中切主题**它不会自动重载，
    所以主窗 `applyTheme()` 每次切换都调这里推一次。
    """
    import webview
    t = "light" if str(theme) == "light" else "dark"
    n = 0
    for w in list(webview.windows):
        if not _is_designer(w):
            continue
        try:
            w.evaluate_js('window.setDesignerTheme && window.setDesignerTheme("%s")' % t)
            n += 1
        except Exception:                              # noqa: BLE001
            pass                                       # 推不到不影响主流程
    return {"ok": True, "theme": t, "windows": n}


def designer_move_window(dx: float, dy: float) -> dict:
    """v4.87：无边框子窗的**拖动把手** —— 按增量挪窗口（前端 rAF 节流后调用）。

    为什么不用 pywebview 的 ``easy_drag``：它会把页面里**所有非交互元素的拖动**都变成
    "拖窗口"，画布上的文字/emoji 就拖不动了（试过，直接冲突）。所以只让顶栏当把手，
    由前端算增量、这里执行 ``Window.move``。
    v4.88：增量落点先过 :func:`_clamp_pos` —— 拖不出屏幕。
    """
    import webview
    try:
        ddx, ddy = int(round(float(dx))), int(round(float(dy)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "非法增量"}
    if not ddx and not ddy:
        return {"ok": True}
    for w in list(webview.windows):
        try:
            if not str(getattr(w, "title", "")).startswith("封面设计"):
                continue
            x, y = int(getattr(w, "x", 0) or 0), int(getattr(w, "y", 0) or 0)
            ww = int(getattr(w, "width", 900) or 900)
            hh = int(getattr(w, "height", 900) or 900)
            nx, ny = _clamp_pos(x + ddx, y + ddy, ww, hh)
            w.move(nx, ny)
            return {"ok": True, "x": nx, "y": ny}
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "error": "封面设计窗口不在"}


def close_cover_designer() -> dict:
    """关闭子窗口（「取消」按钮）：销毁标题以「封面设计」开头的窗口。"""
    import webview
    closed = 0
    for w in list(webview.windows):
        try:
            if str(getattr(w, "title", "")).startswith("封面设计"):
                w.destroy()
                closed += 1
        except Exception:                          # noqa: BLE001
            pass
    return {"ok": True, "closed": closed}


def cover_designer_target() -> dict:
    """子窗口 JS 用：返回**最近一次打开的成片名**。

    为什么需要它：file:// URI 上的 query（`?film=`）在 WebView2 里实测会丢
    （`location.search` 为空），子窗拿不到片名就没法调 /api/edits/cover。
    """
    return {"ok": bool(_LAST_FILM), "film": _LAST_FILM}


def register(app) -> None:
    """把 DesktopApi 引用存到模块级（self 兜底用）。"""
    global _APP
    _APP = app


def drain_pending() -> int:
    """消费 _PENDING：为每个待建窗口调 webview.create_window（**事件循环就绪后调用**）。

    由 desktop_entry.main() 的 ``webview.start(func=...)`` 里循环调用
    （func 跑在事件循环线程，create_window 在那里是安全的）。
    返回本轮创建的窗口数。
    """
    import webview
    n = 0
    # v4.88：先看主窗还在不在（不在了就把孤儿工具窗收掉）
    try:
        reap_orphans()
    except Exception:                                  # noqa: BLE001
        pass
    while _PENDING:
        item = _PENDING.pop(0)
        try:
            webview.create_window(
                title=item["title"], url=item["url"], js_api=item["api"],
                width=760, height=980, min_size=(680, 860), text_select=True,
            )
            n += 1
        except Exception as exc:               # noqa: BLE001
            try:
                _APP.log(f"[cover-designer] 子窗口创建失败：{exc}")
            except Exception:                  # noqa: BLE001
                pass
    return n
