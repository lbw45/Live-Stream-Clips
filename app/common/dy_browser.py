#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音的**浏览器通道**（v3.69，）—— 补上 yt-dlp 缺的那一层。

为什么需要这个文件（所有需要登录态的模块都从这里走）。
------------------------------------------------------------------------
yt-dlp 的抖音提取器（`extractor/tiktok.py` 里的 ``DouyinIE``）走
``www.douyin.com/aweme/v1/web/aweme/detail/``，而这个接口现在被 **Argus 风控**挡着：

    零 cookie                 → HTTP 403  Blocked by ArgusSecurityPlugin Uifid Not Found
    自己造 __ac_nonce + ttwid → HTTP 403  同上（s_v_web_id 造不出来）
    yt-dlp 读浏览器 cookie     → Failed to decrypt with DPAPI（Chrome/Edge 新版 App-Bound 加密）

** ``s_v_web_id``（前端叫 uifid）只能由真实浏览器的 JS 生成**，所以纯 Python 这条路是堵死的。
但反过来：**浏览器进程自己能拿到明文 cookie** —— 实测在同一个 Edge persistent context 里
能读到 41 个 douyin 域 cookie，含 ``s_v_web_id``、``ttwid``、``sessionid``。

于是分工变成（这就是"拆解对齐"的结论）：
    · **浏览器**负责「拿到能过风控的身份」+「把分享短链展开成长链」；
    · **yt-dlp** 继续负责「解析 → 选格式 → 下载 → 合流 → 后处理」（它这部分是成熟的）。
本项目不内置视频下载功能，这里只提供登录态与风控前置。

纪律
----
· **唯一实现**：项目里所有"要跟抖音打交道且需要 cookie"的地方都走这里，
  不要各自开一次浏览器（开两次会抢同一个 profile，也会把风控敲得更狠）。
· **profile 只有一份**：`ws.publisher_profile`（paths.py），与发布执行器共用 ——
  同一个目录 = 同一份登录态，用户扫一次码两边都能用。
· **不闪窗口**：一律 `headless=True`（发布功能才需要人看，这里是后台取数据）。
· **不静默失败**：浏览器被占用 / 没装 / 拿不到 cookie，都要给**能照做**的中文提示（K69）。
· **要带闸门**：cookie 有 TTL，TTL 内复用同一个文件 —— 每下一次视频就开一次浏览器
  既慢又像爬虫（A3 的老教训：别高频敲）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .paths import ws

# ---------------------------------------------------------------- 纯函数部分（可离线断言）

#: yt-dlp 的 ``DouyinIE`` 只认这一种形态（**照抄它的 _VALID_URL，别自己发明**）
DOUYIN_VIDEO_RE = re.compile(r"douyin\.com/video/(?P<id>\d{6,})", re.IGNORECASE)

#: 老的分享页形态：``iesdouyin.com/share/video/<id>``（App 老版本复制出来的）
IES_VIDEO_RE = re.compile(r"iesdouyin\.com/(?:share/video|video)/(?P<id>\d{6,})",
                          re.IGNORECASE)

#: 需要"跟一次重定向"才知道真实地址的域名（抖音 App「复制链接」就是这种）
SHORT_HOST_RE = re.compile(r"^v\.douyin\.com$", re.IGNORECASE)

#: Argus 点名要的那个 cookie（没有它 detail 接口必 403）
UIFID_COOKIE = "s_v_web_id"

#: cookie 文件的有效期（秒）。过期就重新开一次浏览器取。
#: 定 30 分钟：抖音的 s_v_web_id 不会几分钟就失效，但也不会活一整天。
COOKIE_TTL_SEC = 1800.0

#: 一次浏览器动作的总超时（秒）—— 卡住不能把下载任务挂死
BROWSER_TIMEOUT_SEC = 90.0


def video_id_from(url: str) -> str:
    """从任意抖音链接里抽 ``aweme_id``；抽不到返回空串。**纯函数**。"""
    s = str(url or "")
    m = DOUYIN_VIDEO_RE.search(s) or IES_VIDEO_RE.search(s)
    return m.group("id") if m else ""


def is_final_video_url(url: str) -> bool:
    """是不是 yt-dlp 直接吃得下的形态（``douyin.com/video/<id>``）。**纯函数**。

    是 → 不用开浏览器（省一次外呼）；不是 → 必须先展开。
    """
    return bool(DOUYIN_VIDEO_RE.search(str(url or "")))


def needs_resolve(url: str) -> bool:
    """这个链接要不要先展开。**纯函数**（冒烟直接断言，不必开浏览器）。

    注意：``iesdouyin.com/share/video/<id>`` 虽然能直接抽到 id，
    但 yt-dlp 不认这个域名（实测只有 generic 接）⇒ 也要展开（其实是**改写成**长链）。
    """
    s = str(url or "")
    if not s:
        return False
    if is_final_video_url(s):
        return False
    return bool(SHORT_HOST_RE.search(_host_of(s)) or IES_VIDEO_RE.search(s)
                or "douyin.com" in s.lower())


def _host_of(url: str) -> str:
    s = re.sub(r"^[a-zA-Z]+://", "", str(url or ""))
    return s.split("/")[0].split("?")[0].split("#")[0].strip().lower()


# ---------------------------------------------------------------- cookie 文件

def cookie_file() -> Path:
    """抖音 cookie 的落点（Netscape 格式，yt-dlp 直接吃）。"""
    return ws.slicer_runtime / "douyin_cookies.txt"


def cookie_state() -> dict:
    """cookie 文件的体检信息（给界面如实显示，K69）。**不读 cookie 内容**。

    返回 ``{exists, updatedAt, ageSec, stale, domains, hasUifid, count, hint}``
    —— 界面要能回答用户"为什么下不动"，所以"缺哪个 cookie"必须看得见。
    """
    p = cookie_file()
    out = {"path": str(p), "exists": False, "updatedAt": 0.0, "ageSec": 0.0,
           "stale": True, "count": 0, "hasUifid": False, "hint": "还没取过抖音 Cookie"}
    if not p.is_file():
        return out
    out["exists"] = True
    out["updatedAt"] = round(p.stat().st_mtime, 1)
    out["ageSec"] = round(max(0.0, time.time() - p.stat().st_mtime), 1)
    out["stale"] = out["ageSec"] > COOKIE_TTL_SEC
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t") if "\t" in line else line.split()
            if len(parts) >= 7:
                out["count"] += 1
                if parts[5] == UIFID_COOKIE:
                    out["hasUifid"] = True
    except OSError:
        pass
    if out["count"] == 0:
        out["hint"] = "Cookie 文件是空的，需要重新取一次"
    elif not out["hasUifid"]:
        out["hint"] = (f"Cookie 里没有 {UIFID_COOKIE}（抖音风控认的就是它），"
                       "多半取的时候正赶上验证页 —— 请在浏览器里打开抖音看一眼，再重新取")
    elif out["stale"]:
        out["hint"] = f"Cookie 已取于 {out['ageSec'] / 60:.0f} 分钟前，下次下载会自动刷新"
    else:
        out["hint"] = f"Cookie 有效（{out['count']} 条，含 {UIFID_COOKIE}）"
    return out


# ---------------------------------------------------------------- Netscape 格式（纯函数）

def netscape_line(cookie: dict) -> str:
    """一条 playwright cookie → Netscape 文件的一行。**纯函数**。

    格式：``domain  includeSubdomains  path  secure  expiry  name  value``
    · domain 必须**带前导点**（``.douyin.com``），否则很多库不认子域；
    · expiry 用整数秒；session cookie（expires <= 0）写 0。
    """
    domain = str(cookie.get("domain") or "")
    if domain and not domain.startswith("."):
        domain = "." + domain
    flag = "TRUE" if str(domain).startswith(".") else "FALSE"
    path = str(cookie.get("path") or "/")
    secure = "TRUE" if cookie.get("secure") else "FALSE"
    try:
        exp = int(float(cookie.get("expires") or 0))
    except (TypeError, ValueError):
        exp = 0
    if exp <= 0:
        exp = 0
    return "\t".join([domain, flag, path, secure, str(exp),
                      str(cookie.get("name") or ""), str(cookie.get("value") or "")])


def _write_cookie_file(cookies: list[dict], path: Path) -> int:
    """写 Netscape cookie 文件（**newline=""**，K94：否则 LF 被悄悄改成 CRLF）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File",
             "# 由「Live Stream Clips」从本机浏览器导出，仅用于本机下载抖音素材，勿外传。"]
    for c in cookies:
        lines.append(netscape_line(c))
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    tmp.replace(path)
    return len(cookies)


# ---------------------------------------------------------------- 浏览器代取（v3.70）

#: yt-dlp 的 ``DouyinIE`` 调的就是这个接口 —— 但**只有浏览器自己发才回 200**
DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

#: 浏览器页面里那次请求自带的标准参数（照抄它，别自创一套）
DETAIL_PARAMS = ("device_platform=webapp&aid=6383&channel=channel_pc_web"
                 "&pc_client_type=1&version_code=170400&version_name=17.4.0")

#: 档位名里夹着的**高度**（比特率序号不是高度，别误认）
KNOWN_HEIGHTS = (2160, 1440, 1080, 720, 540, 480, 360)

#: 只挑 mp4 档：dash 是音视频分离，要合流；mp4 是单文件自带音轨
PREFER_FORMAT = "mp4"

#: 打开视频页后等前端 JS 跑完的毫秒数（太短则 detail 还没被放行）
PAGE_SETTLE_MS = 5000

#: CDN 直连地址要不要 Referer —— 实测**裸请求 403、带它就 206**
CDN_REFERER = "https://www.douyin.com/"


def detail_url(video_id: str) -> str:
    """detail 接口的完整地址（**纯函数**，冒烟可离线断言）。"""
    return f"{DETAIL_API}?aweme_id={video_id}&{DETAIL_PARAMS}"


def gear_height(gear_name: str) -> int:
    """档位名 → 画质高度（**纯函数**）。认不出来返回 0（调用方按"未知"处理）。

    实测的档位名长这样：``adapt_lowest_4_1``（4K）｜``normal_1080_0``（1080）
    ｜``540_2_1``（540）｜``low_720_0``（720）。夹在中间的数字才是高度，
    ``bit_rate`` 字段本身不带高度（全是 None），只能从名字认。
    """
    s = str(gear_name or "")
    for tok in re.split(r"[^0-9]+", s):
        if tok.isdigit() and int(tok) in KNOWN_HEIGHTS:
            return int(tok)
    if re.search(r"(?:^|_)4(?:_|$)", s):          # "adapt_lowest_4_1"：4 是 4K 的简写
        return 2160
    return 0


def _first_url(node: dict) -> str:
    urls = ((node or {}).get("url_list") or [])
    return str(urls[0]) if urls else ""


def _urls(node: dict) -> list[str]:
    """play_addr 里的**全部**候选地址（去重、保序）。

    v3.193 对照官方 DouyinIE（tiktok.py 的 ``extract_addr``）：它把 ``url_list``
    里**每一条**都展开成独立 format —— 备用地址不是摆设，主地址挂了换下一条。
    """
    out: list[str] = []
    for u in ((node or {}).get("url_list") or []):
        u = str(u or "").strip()
        if u and u not in out:
            out.append(u)
    return out


def mp4_gears(detail: dict) -> list[dict]:
    """``aweme_detail`` → 可用的 mp4 档位（按码率降序）。**纯函数**。"""
    v = (detail or {}).get("video") or {}
    gears = [g for g in (v.get("bit_rate") or [])
             if str(g.get("format") or "") == PREFER_FORMAT and _first_url(g.get("play_addr"))]
    gears.sort(key=lambda g: int(g.get("bit_rate") or 0), reverse=True)
    return gears


def pick_source(detail: dict, quality: str = "best") -> dict:
    """挑一条**直连 CDN 地址**（**纯函数**，离线可断言）。

    只在这里做"选档"，下载/后处理仍归 yt-dlp（K118：坏哪层补哪层）。
    返回 ``{url, urls, height, bitrate, gear, note}``：``url`` 是选中的主地址，
    ``urls`` 是同一 play_addr 的**全部备用地址**（v3.193，官方每条都展开）；
    拿不到就 url 为空 + 能照做的 note。
    """
    out = {"url": "", "urls": [], "height": 0, "bitrate": 0, "gear": "", "note": ""}
    v = (detail or {}).get("video") or {}
    gears = mp4_gears(detail)

    def _take(g: dict, note: str = "") -> dict:
        out.update({"url": _first_url(g.get("play_addr")),
                    "urls": _urls(g.get("play_addr")),
                    "height": gear_height(str(g.get("gear_name") or "")),
                    "bitrate": int(g.get("bit_rate") or 0),
                    "gear": str(g.get("gear_name") or ""),
                    "note": note})
        return out

    if not gears:
        # 没有分档信息就退回页面主地址（它也是一条直连 CDN 地址）
        url = _first_url(v.get("play_addr")) or _first_url(v.get("download_addr"))
        out.update({"url": url, "height": int(v.get("height") or 0),
                    "gear": "play_addr",
                    "urls": _urls(v.get("play_addr")) or _urls(v.get("download_addr")),
                    "note": "这条视频没给分档，用的是页面主地址"})
        return out

    if quality == "audio":
        # 音频也从同一个 mp4 抽（worker 会用 ffmpeg 抽音轨）→ 挑码率最低的省流量
        return _take(min(gears, key=lambda g: int(g.get("bit_rate") or 0)),
                     "音频由此档位的 mp4 抽出")

    target = {"1080": 1080, "720": 720, "480": 480}.get(str(quality or ""), 0)
    if target:
        fit = [g for g in gears
               if 0 < gear_height(str(g.get("gear_name") or "")) <= target]
        if fit:
            return _take(fit[0], "按画质档 %s 选中" % quality)
        lowest = min(gears, key=lambda g: int(g.get("bit_rate") or 0))
        return _take(lowest, "没有 %s 及以下的档，已给到最省的一档" % quality)
    return _take(gears[0], "最高画质档")


def summarize(detail: dict) -> dict:
    """``aweme_detail`` → 界面要的元数据（**纯函数**）。"""
    d = detail or {}
    v = d.get("video") or {}
    gears = mp4_gears(d)
    heights = sorted({gear_height(str(g.get("gear_name") or "")) for g in gears} - {0},
                     reverse=True)
    if not heights and int(v.get("height") or 0):
        heights = [int(v.get("height"))]
    return {
        "id": str(d.get("aweme_id") or ""),
        "title": str(d.get("desc") or "").strip(),
        "duration": round(float(v.get("duration") or 0) / 1000.0, 1),
        "uploader": str((d.get("author") or {}).get("nickname") or ""),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "thumbnail": (_first_url(v.get("cover")) or _first_url(v.get("origin_cover"))),
        "heights": heights[:8],
        "extractor": "Douyin",
        "isLive": False,
    }


# ---------------------------------------------------------------- 浏览器会话（唯一实现）

class DyBrowserError(RuntimeError):
    """浏览器通道走不通。**message 必须是能照做的中文**（K69）。"""


def _launch():
    """打开 persistent context（系统 Edge + 发布功能的 profile）。延迟导入 playwright。"""
    try:
        from playwright.sync_api import sync_playwright      # noqa: PLC0415
    except ImportError as exc:
        raise DyBrowserError(
            "缺少 playwright，没法读取本机浏览器的抖音登录态。"
            f"（{exc}）") from exc
    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ws.publisher_profile),
            channel="msedge",                 # 系统 Edge，零下载（与发布执行器同款）
            headless=True,                    # 后台取数据，**不闪窗口**
            args=["--disable-blink-features=AutomationControlled"],
        )
    except Exception as exc:                                # noqa: BLE001
        pw.stop()
        text = f"{type(exc).__name__}: {exc}".lower()
        if "target page, context or browser has been closed" in text or \
           "processsingletonlock" in text or "user data directory" in text or \
           "already in use" in text:
            raise DyBrowserError(
                "浏览器登录目录正被占用（多半是「发布」那边的浏览器还开着）。"
                "请先关掉发布窗口，或等发布完成后再下载。") from exc
        raise DyBrowserError(
            f"打不开本机 Edge（{type(exc).__name__}）。请确认已安装 Microsoft Edge。") from exc
    return pw, ctx


def _douyin_cookies(ctx) -> list[dict]:
    """取 douyin 域的 cookie（**只取抖音的**，别的站点一概不碰）。"""
    try:
        all_cookies = ctx.cookies()
    except Exception as exc:                                # noqa: BLE001
        raise DyBrowserError(f"读不到浏览器 Cookie：{exc}") from exc
    return [c for c in all_cookies
            if "douyin" in str(c.get("domain") or "").lower()]


def ensure_cookies(*, force: bool = False) -> tuple[str, dict]:
    """保证 cookie 文件是**新鲜可用**的；返回 ``(cookie文件路径, 体检信息)``。

    跳过条件：文件存在 + 未过期 + 含 ``s_v_web_id`` 且 ``force=False``。
    需要真取时才开一次浏览器（TTL 内的重复下载**不会**反复开浏览器 —— 别高频敲门）。
    """
    st = cookie_state()
    if not force and st["exists"] and not st["stale"] and st["hasUifid"]:
        return str(cookie_file()), st

    pw, ctx = _launch()
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cookies = _douyin_cookies(ctx)
        # s_v_web_id 是页面 JS 算出来的：库里没有就说明这个 profile 还没在浏览器里看过抖音
        if not any(c.get("name") == UIFID_COOKIE for c in cookies):
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                          timeout=int(BROWSER_TIMEOUT_SEC * 1000))
                page.wait_for_timeout(3500)                  # 等前端 JS 把 uifid 写进 cookie
                cookies = _douyin_cookies(ctx)
            except Exception as exc:                         # noqa: BLE001
                raise DyBrowserError(
                    f"打开抖音页面失败（{type(exc).__name__}）。"
                    "请在浏览器里打开 https://www.douyin.com 看一眼（如需验证就过一次），"
                    "再回来重新取。") from exc
    finally:
        try:
            ctx.close()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:                                    # noqa: BLE001
            pass

    if not cookies:
        raise DyBrowserError(
            "浏览器里没有抖音的 Cookie。请先到「设置」页登录抖音（扫码即可），"
            "或在 Edge 里打开抖音看一眼，再回来下载。")
    names = {c.get("name") for c in cookies}
    if UIFID_COOKIE not in names:
        raise DyBrowserError(
            f"拿到了 {len(cookies)} 条抖音 Cookie，但里面没有 {UIFID_COOKIE} —— "
            "这通常是抖音正要求人机验证。请在 Edge 里打开抖音手动过一次验证，再回来重试。")
    _write_cookie_file(cookies, cookie_file())
    return str(cookie_file()), cookie_state()


def resolve_url(url: str) -> tuple[str, str]:
    """把抖音的**分享短链**展开成长链（只做这一件事）。

    ⚠️ v3.70 起 **App 的抖音前置统一走 ``prepare()``**（一次浏览器会话里做完
    展开 + 取 detail + 导出 cookie）—— 因为 detail 只能由浏览器自己取。
    本函数保留给「**只要**展开链接」的场景（诊断脚本在用），不要拿它做下载前置。

    返回 ``(最终URL, 说明)``；展开不了时 URL 为空串、说明里写**能照做**的下一步。
    · 已经是 ``douyin.com/video/<id>`` → **不开浏览器**，原样返回（省一次外呼）；
    · ``iesdouyin.com/share/video/<id>`` → 直接**改写**成长链（也不开浏览器，
      实测证据 2：这个域名 yt-dlp 只有 generic 接，但 id 是同一个）；
    · ``v.douyin.com/<code>`` → 只能跟重定向，开一次浏览器拿最终 URL。
    """
    raw = str(url or "").strip()
    if not raw:
        return "", "链接是空的"
    if is_final_video_url(raw):
        return raw, "已是可直接下载的长链"
    vid = video_id_from(raw)
    if vid and IES_VIDEO_RE.search(raw):
        return f"https://www.douyin.com/video/{vid}", "老分享链已改写成长链"

    pw, ctx = _launch()
    final = ""
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(raw, wait_until="domcontentloaded",
                      timeout=int(BROWSER_TIMEOUT_SEC * 1000))
            page.wait_for_timeout(1500)
            final = str(page.url or "")
        except Exception as exc:                             # noqa: BLE001
            raise DyBrowserError(
                f"打不开这条短链（{type(exc).__name__}）：可能已失效或被删除。"
                "请回抖音重新复制一次分享链接。") from exc
        # 重定向后 JS 还会补一段：再等一下拿最终的 /video/<id>
        if not is_final_video_url(final):
            page.wait_for_timeout(1500)
            final = str(page.url or "")
    finally:
        try:
            ctx.close()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:                                    # noqa: BLE001
            pass

    if is_final_video_url(final):
        return final, "短链已展开成长链"
    vid = video_id_from(final)
    if vid:
        return f"https://www.douyin.com/video/{vid}", "短链已展开成长链"
    return "", ("短链展开后没拿到视频地址（可能指向直播间或主页，而不是单条视频）。"
                "请复制**单条视频**的分享链接。")


def prepare(url: str, *, need_cookies: bool = True,
            settle_ms: int = PAGE_SETTLE_MS) -> dict:
    """一次浏览器会话做完**三件事**（v3.70，抖音前置的**唯一实现**）：

        ① 展开短链（长链也要真的进到视频页 —— 见下）；
        ② 在**页面里**取 ``aweme_detail``；
        ③ 顺带把 cookie 导出成文件（留着给别的站点与后续接口用）。

    为什么必须合在一次会话里
    ------------------------
    · **detail 只能由浏览器自己取**：实测带同一份 cookie 的外部请求一律 403
      ``Blocked by ArgusSecurityPlugin Uifid Not Found``，连 curl_cffi 伪装
      Chrome 的 TLS 指纹也不行；而同一次会话里页面内 ``fetch`` 稳定 200（118 KB）。
      所以"信息提取"这一层只能挪进浏览器 —— 这正是 K118 说的"坏哪层补哪层"。
    · **必须真的进到视频页**：detail 是同源接口，从 ``about:blank`` 发不算数。
      于是长链也一样要导航一次（v3.69 时它是不开浏览器的，现在必须开）。
    · 开两次会抢同一个 profile，也会把风控敲得更狠（A3 / K119）。

    返回 ``{url, videoId, detail, cookieFile, note, error}``。
    **不抛异常**：走不通时 ``error`` 里写能照做的中文（K69）。
    """
    raw = str(url or "").strip()
    res = {"url": raw, "videoId": video_id_from(raw), "detail": None,
           "cookieFile": "", "note": "", "error": ""}
    if not raw:
        res["error"] = "链接是空的"
        return res

    pw, ctx = _launch()
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(raw, wait_until="domcontentloaded",
                      timeout=int(BROWSER_TIMEOUT_SEC * 1000))
        except Exception as exc:                             # noqa: BLE001
            res["error"] = (f"打不开这条链接（{type(exc).__name__}）：可能已失效或被删除。"
                            "请回抖音重新复制一次分享链接。")
            return res
        # 等前端 JS 跑完（这一等是必须的：不等的话 detail 还没被放行）
        page.wait_for_timeout(int(settle_ms))

        final = str(page.url or "")
        vid = video_id_from(final) or res["videoId"]
        if not vid:
            res["error"] = ("这条链接里没有单条视频的地址（可能指向直播间或主页，"
                            "或者正要求过验证）。请复制**单条视频**的分享链接。")
            return res
        res["url"] = f"https://www.douyin.com/video/{vid}"
        res["videoId"] = vid
        res["note"] = "已是长链" if is_final_video_url(raw) else "短链已展开成长链"

        try:
            text = page.evaluate(
                "async (u) => { const r = await fetch(u, {credentials: 'include'});"
                " return await r.text(); }", detail_url(vid))
        except Exception as exc:                             # noqa: BLE001
            res["error"] = f"读取视频信息失败（{type(exc).__name__}），请稍后再试"
            return res
        try:
            payload = json.loads(str(text or "") or "{}")
        except ValueError:
            payload = {}
        detail = payload.get("aweme_detail") or None
        if not detail:
            res["error"] = ("抖音没返回视频信息（多半撞上风控或需要过一次验证）。"
                            "请在 Edge 里打开抖音看一眼，再回来重试 —— 别连续重试。")
            return res
        res["detail"] = detail

        if need_cookies:
            try:
                cookies = _douyin_cookies(ctx)
                if cookies:
                    _write_cookie_file(cookies, cookie_file())
                    res["cookieFile"] = str(cookie_file())
            except Exception:                                # noqa: BLE001
                pass                     # cookie 导出失败不影响下载（CDN 用不上它）
        return res
    finally:
        try:
            ctx.close()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:                                    # noqa: BLE001
            pass


#: 给别的模块复用的**公开别名**（v3.85）：浏览器怎么起、cookiefile 怎么写，
#: **唯一实现仍在本文件**（`_launch` / `_write_cookie_file`）；别的模块
#: （如 `app.common.browser_cookies`）直接引用这两个名字，别再各写一份（K44）。
launch = _launch
write_netscape = _write_cookie_file
