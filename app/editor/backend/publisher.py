#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-B 发布执行器：自动上传 + 自动填表 + **停住等人工**。

对应文档 §5.2「发布执行（人工触发，逐条）」、§6.2 状态机、D15、**R1 红线**。

**默认绝不点「发布」**：走到 ``waiting_user`` 就停住，等人在浏览器里自己点发布；仅当平台卡片「自动发布」开关打开时，填完由执行器点一次「发布」（v4.38，R1 修订），
然后回软件把该条标记为「已发布」。

**driver 可注入**：默认 :class:`PlaywrightDriver`（``channel="msedge"`` 驱动系统 Edge，
零下载）；测试注入 fake driver，**没有账号也能验证 phase 流转到 waiting_user 为止**。
真实创作中心的 DOM 选择器留待有账号后实测填入（见 PlaywrightDriver 内的 TODO）。
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from app.common import publish_cover
from app.common.paths import ws
from app.editor.backend import database, platforms

# §6.2 状态机：queued → running → waiting_user → completed/failed
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting_user"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# phase 顺序（§6.2）
PHASES = ("preflight", "checking_login", "launching_browser",
          "loading_publish_page", "uploading", "filling", "waiting_user")

# ⚠️ 自动重试仅限这些 phase；文件已开始上传后绝不自动重试（防重复投稿）
RETRYABLE_PHASES = {"preflight", "checking_login", "launching_browser",
                    "loading_publish_page"}


class Driver:
    """发布执行器的驱动协议。实现它即可替换真实浏览器（测试用 fake）。"""

    def check_login(self) -> bool:
        """是否已登录创作中心。未登录返回 False（调用方停在 checking_login）。"""
        raise NotImplementedError

    def launch(self) -> None:
        """启动浏览器（persistent context，保留登录态）。"""
        raise NotImplementedError

    def load_publish_page(self) -> None:
        """打开创作中心上传页。"""
        raise NotImplementedError

    def upload(self, file_path: str) -> None:
        """选择并上传本地成片文件。"""
        raise NotImplementedError

    def set_cover(self, assets: dict) -> dict:
        """上传 3:4 / 4:3 封面（平台支持哪份传哪份）。**绝不点发布。**"""
        raise NotImplementedError

    def fill(self, title: str, tags: list[str]) -> None:
        """填标题 / 标签（封面）。填完**不点发布**。"""
        raise NotImplementedError

    def click_publish(self) -> dict:
        """【v4.38 R1 修订】点一次「发布」并等成功信号；失败绝不重试、绝不二次点击。

        仅当平台卡片「自动发布」开关打开时由 run_job 调用（用户 2026-11-05 授权）。
        """
        raise NotImplementedError

    def close(self) -> None:
        """关闭浏览器（若由执行器持有）。"""
        raise NotImplementedError


class PlaywrightDriver(Driver):
    """真实浏览器驱动（系统 Edge，零下载；persistent context 保住登录态）。

    抖音那套选择器是 2026-09-23 在真实登录态下探测出来的（不是猜的）；
    快手 / B站 / 小红书 的页面结构写进 :mod:`platforms` 的候选选择器**列表**，
    逐个探测：命中就用，全都不中就把「哪个字段没填上」记进 ``self.warnings``，
    由执行器带进错误信息 —— **绝不假装填好了**。

    DOM 会变，所以选择器全部集中在 ``platforms.PLATFORMS``，改一处即可（§5.2）。

    **R1 红线**：这里没有任何「点发布/投稿」的定位方式，填完就返回。
    """

    def __init__(self, *, platform: str = platforms.DEFAULT_PLATFORM,
                 publish_url: str | None = None) -> None:
        self.platform = platforms.normalize(platform)
        self.spec = platforms.get(self.platform)
        self._publish_url = publish_url or self.spec.creator_url
        self._page = None
        self._ctx = None
        self._pw = None
        #: 填表阶段没找到的字段（如「标题框」），执行器会把它写进任务错误信息
        self.warnings: list[str] = []
        #: 封面上传阶段的非致命提示（页面未提供某个比例/确认按钮等）
        self.cover_warnings: list[str] = []

    def _ensure_browser(self) -> None:
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright  # 延迟导入：无浏览器环境也能加载本模块
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            # K44：登录目录**唯一出处**是 profile_dir()，与设置页判定是同一个
            user_data_dir=str(profile_dir(self.platform)),
            channel="msedge",
            headless=False,
            viewport={"width": 1440, "height": 950},
            # 关掉最显眼的自动化特征位（拟人化，§5.2）
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    @staticmethod
    def _pick(page, selectors) -> object | None:
        """按顺序探测选择器，返回第一个命中元素；全不中返回 None（不抛）。"""
        for sel in selectors:
            try:
                el = page.query_selector(sel)
            except Exception:                                 # noqa: BLE001  选择器语法随平台改版
                continue
            if el is not None:
                return el
        return None

    @staticmethod
    def _pick_visible(page, selectors) -> object | None:
        """同 :meth:`_pick`，但跳过隐藏节点：同一文案常同时存在于隐藏弹层与可见按钮。"""
        for sel in selectors:
            try:
                els = page.query_selector_all(sel)
            except Exception:                                 # noqa: BLE001
                continue
            for el in els:
                try:
                    if el.is_visible():
                        return el
                except Exception:                             # noqa: BLE001
                    continue
        return None

    def click_publish(self) -> dict:
        """v4.38：点一次「发布」，等成功信号（URL 离开发布页 / 出现成功文案）。

        R1 修订（用户 2026-11-05 授权）：仅当平台卡片「自动发布」开关打开时被调用；
        **只点一次**，失败绝不重试、绝不二次点击（防重复投稿）。
        """
        page = self._page
        url_before = page.url
        # v4.39d：改走 JS 全页扫描（CSS text 选择器在重 DOM 上实测 120s）
        btn = self._pick_text_any(page, tuple(self.spec.publish_labels))
        if btn is None:
            raise RuntimeError(
                f"没找到{self.spec.name}「发布」按钮（期望文字："
                + " / ".join(self.spec.publish_labels) + "）；已停止，绝不二次点击")
        if not self._wait_enabled(page, btn):
            raise RuntimeError(
                f"{self.spec.name}「发布」按钮一直不可用；已停止，绝不二次点击")
        self._click_el(btn)
        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                if page.url != url_before:
                    return {"clicked": True, "verified": "url"}
                body = page.inner_text("body") or ""
                if any(x in body for x in self.spec.publish_success_texts):
                    return {"clicked": True, "verified": "text"}
            except Exception:                                 # noqa: BLE001  跳转中 DOM 抖动
                pass
            page.wait_for_timeout(1000)
        raise RuntimeError(
            f"{self.spec.name}点了「发布」，60 秒内没有出现成功信号（URL 未跳转、页面也没有"
            f"{'/'.join(self.spec.publish_success_texts)}）；按失败处理，绝不二次点击")

    def _wait_form(self, page, timeout_ms: int) -> None:
        """等表单出现：标题框、描述框任一命中即可（有的平台没有独立标题框）。"""
        deadline = time.time() + timeout_ms / 1000.0
        selectors = tuple(self.spec.title_inputs) + tuple(self.spec.desc_inputs)
        while time.time() < deadline:
            if self._pick(page, selectors) is not None:
                return
            page.wait_for_timeout(800)
        raise RuntimeError(f"等待 {self.spec.name} 发布表单超时（页面结构可能已改版）")

    def check_login(self) -> bool:
        """先用 cookie 判（**不启动浏览器**：快，而且不会突然闪出一个窗口）。"""
        try:
            return bool(login_state(self.platform).get("loggedIn"))
        except Exception:                                     # noqa: BLE001
            return False

    def launch(self) -> None:
        self._ensure_browser()

    def load_publish_page(self) -> None:
        self._page.goto(self._publish_url, wait_until="domcontentloaded", timeout=60000)
        # v4.37：不再死等 3.5s——轮询等上传框出现（通常 1s 内就绪，省 2s+）
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self._pick(self._page, self.spec.file_input) is not None:
                break
            self._page.wait_for_timeout(250)
        if self._pick(self._page, self.spec.file_input) is None:
            raise RuntimeError(
                f"没能进入{self.spec.creator_label}上传页（登录态可能已失效，"
                f"请到发布页该平台的卡片上点「登录/换号」重新登录）")

    def upload(self, file_path: str) -> None:
        """选文件即"开始上传"。**这一步之后绝不自动重试**（防重复投稿，见 RETRYABLE_PHASES）。"""
        page = self._page
        el = self._pick(page, self.spec.file_input)
        if el is None:
            page.wait_for_selector(self.spec.file_input[0], timeout=30000)
            el = self._pick(page, self.spec.file_input)
        if el is None:
            raise RuntimeError(f"没找到{self.spec.name}的文件选择框")
        el.set_input_files(file_path)
        # 选完文件页面通常会跳到发布表单页；此时**文件仍在后台上传**（大文件要几分钟）
        if self.spec.post_url_glob:
            try:
                page.wait_for_url(self.spec.post_url_glob, timeout=120000)
            except Exception:                                 # noqa: BLE001
                # URL 没变不等于失败：有的版本停在原页原地展开表单，交给 fill 去等输入框
                pass

    def _cover_inputs(self, page) -> list:
        """页面里的**图片**文件框（按平台候选去重，绝不把视频框当封面）。

        多个 accept 条件必须拼成**一个逗号选择器**再查：逐个 selector 查再按
        ElementHandle 去重是不可靠的（同一个元素从两条规则各返回一个句柄，
        `a == b` 为 False，结果同一个框被上传两次）。
        """
        sel = ", ".join(self.spec.cover_inputs)
        try:
            return page.query_selector_all(sel)
        except Exception:                                     # noqa: BLE001
            return []

    @staticmethod
    def _safe_visible(el) -> bool:
        try:
            return bool(el is not None and el.is_visible())
        except Exception:                                     # noqa: BLE001
            return False

    def _open_cover_editor(self, page, timeout_ms: int = 12000,
                           ignore_existing: bool = False) -> None:
        """没有现成图片框时，点「设置/上传封面」打开编辑器（只点这个语义的按钮）。

        ``ignore_existing=True``：页面上已有**隐藏**的图片框，但封面弹层没打开 ——
        仍然要点一次触发器，否则后面的「确定」永远在 display:none 的弹层里点不到。
        """
        # v4.39（用户实测「卡住上下跳动、进不去上传页」）：入口**同一个元素最多点一次**。
        # 旧循环每 ~900ms 把同一个元素再点一遍，click 自带滚动到元素，
        # 页面就反复上下跳还进不了弹层。v4.39b 再平衡：
        # 同元素点过一次就只等不点（防跳动）；
        # 缩略图允许 8s 后**补点一次**（真机实测点击可能被吞，弹层迟到）；
        # 晚出现的入口（B站要等视频传完才出现「添加封面」）用元素指纹识别，照样点。
        deadline = time.time() + timeout_ms / 1000.0

        def _dialog_open() -> bool:
            if self._cover_dialog(page) is not None:
                return True
            imgs = self._cover_inputs(page)
            if imgs and any(self._safe_visible(x) for x in imgs):
                return True          # 弹层开着但类名不认识：有**可见**图片框也算到位
            return False

        if _dialog_open():
            return

        seen = set()                 # 已点击过的元素指纹
        thumb_clicks = 0
        # v4.39d：**彻底不用 CSS text 选择器**（`button:text-is` 重 DOM 上一轮 120s，
        # 真机实测元凶）。`_pick_text_any`（一次 JS 全页扫描，毫秒级）+ entry_labels
        # 已覆盖全部 cover_trigger 文案（设置视频封面也已补进 entry_labels）。
        while time.time() < deadline:
            if _dialog_open():
                return
            el = self._pick_text_any(page, self.spec.cover_entry_labels)
            key = "text"
            if el is None and thumb_clicks < 2:
                # 快手 / 小红书实测：入口是**封面缩略图**，有的还要先悬停才出现「编辑封面」
                thumb, _ = self._cover_thumb(page)
                if thumb is not None:
                    try:
                        thumb.hover(timeout=1500)             # v4.39e：默认超时会挂 30~60s
                        page.wait_for_timeout(500)
                    except Exception:                         # noqa: BLE001
                        pass
                    el = self._pick_text_any(page, self.spec.cover_entry_labels)
                    if el is None:
                        el = page.query_selector('[data-pbthumb="1"]')
                        key = "thumb"
            if el is None:
                page.wait_for_timeout(800)                    # 入口还没出现（B站上传中）
                continue
            try:
                el.evaluate("n => n.setAttribute('data-pbentry','1')")
                marker = n2 if False else el.evaluate(
                    "n => (n.tagName||'') + '|' + Math.round(n.getBoundingClientRect().top)"
                    " + '|' + (n.getAttribute('data-pbentry') || '')")
            except Exception:                                 # noqa: BLE001
                marker = key + "#" + str(len(seen))
            if marker in seen:
                # 同一个元素点过没用：只等，不再点（防上下跳动）
                if key == "thumb" and thumb_clicks == 1 and time.time() < deadline:
                    thumb_clicks += 1                         # 8s 后补点一次（吞点击兜底）
                    page.wait_for_timeout(3000)
                    fresh = page.query_selector('[data-pbthumb="1"]')
                    if fresh is None:
                        page.wait_for_timeout(5000)
                        continue
                    try:
                        self._click_el(fresh)
                    except Exception:                         # noqa: BLE001
                        pass
                    if self._wait_cover_dialog(page, 8000):
                        return
                page.wait_for_timeout(800)
                continue
            seen.add(marker)
            if key == "thumb":
                thumb_clicks += 1
            try:
                self._click_el(el)
            except Exception:                                 # noqa: BLE001
                pass
            # 点一次就够：轮询等弹层（4s），期间绝不重复点击（防上下跳动）
            if self._wait_cover_dialog(page, 4000):
                return
            if _dialog_open():
                return
        # 超时返回：调用方再等 8s 并如实报错

    def _wait_enabled(self, page, el, timeout_ms: int = 5000) -> bool:
        """等按钮可用（上传后平台要解码/初始化裁切框，立刻点会点空）。"""
        for _ in range(max(1, int(timeout_ms / 250))):
            try:
                if bool(el.is_enabled()):
                    return True
            except Exception:                                 # noqa: BLE001
                return True
            page.wait_for_timeout(250)
        return False

    def _wait_cover_dialog(self, page, timeout_ms: int = 6000):
        """v4.37：轮询等封面弹层出现（替代点入口后的固定 sleep）。"""
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            dlg = self._cover_dialog(page)
            if dlg is not None:
                return dlg
            page.wait_for_timeout(250)
        return None

    _COVER_READY_JS = r"""
    el => {
      if (!el) return false;
      for (const i of el.querySelectorAll('img')) {
        const s = i.currentSrc || i.src || '';
        if (s.startsWith('blob:') || s.startsWith('data:')) return true;
      }
      if (el.querySelector('canvas')) return true;
      return /裁剪|比例|裁切/.test(el.innerText || '');
    }
    """

    def _wait_cover_ready(self, page, dlg=None, timeout_ms: int = 6000) -> bool:
        """v4.37：轮询等「封面已进裁剪区」的信号（预览图/canvas/裁剪文案）。

        替代原来每次上传后的 1.5~2.6s 固定 sleep——平台解码快就立刻走，
        慢也能等到，单步平均省 1s+，整条封面流程省 6~10s。
        """
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                target = dlg if dlg is not None else page.query_selector("body")
                if target is not None and bool(target.evaluate(self._COVER_READY_JS)):
                    return True
            except Exception:                                 # noqa: BLE001  句柄重挂
                dlg = self._cover_dialog(page)
            page.wait_for_timeout(250)
        return False

    def _cover_dialog(self, page):
        """当前可见、文字含「封面」的弹层；多个命中时取**面积最大**的那个。

        真机教训（2026-11-05 小红书）：表单上的 `cover-plugin-preview`（小预览）
        类名同样含 `cover-plugin`，按选择器顺序会先命中它 —— 拿到的句柄里当然没有
        「上传/完成」按钮。所以候选要按面积排序，取真正的大面板。
        """
        best, best_area = None, 0
        seen = set()
        vp = page.viewport_size or {"width": 1440, "height": 900}
        vp_area = float(vp["width"]) * float(vp["height"])
        for sel in self.spec.cover_dialogs:
            try:
                dialogs = page.query_selector_all(sel)
            except Exception:                                 # noqa: BLE001
                continue
            for dlg in dialogs:
                try:
                    if not dlg.is_visible():
                        continue
                    if "封面" not in (dlg.inner_text() or ""):
                        continue
                    box = dlg.bounding_box() or {}
                    area = float(box.get("width", 0)) * float(box.get("height", 0))
                    key = id(dlg)
                    if key in seen:
                        continue
                    seen.add(key)
                    # 弹层必须**足够大**：表单上的 cover-plugin-preview 小预览
                    # 类名也含 cover-plugin、文字也含封面，但它不是弹层。
                    if area < vp_area * 0.08:
                        continue
                    if area > best_area:
                        best, best_area = dlg, area
                except Exception:                             # noqa: BLE001
                    continue
        return best

    def _confirm_covers(self, page, max_saves: int = 2) -> int:
        """点掉封面弹层的「保存/确定/完成/应用」，返回实际点了几次。

        ⚠️ 必须**作用域限定在含「封面」的弹层**里，且按钮文字精确匹配：
        · 抖音实测按钮是「保存」，没有再点它就会一直停在裁切弹层；
        · 不能用整页 `text=保存` 盲点，既可能点到别的表单，也有误触投稿的风险。
        """
        saved = 0
        for _ in range(max(0, int(max_saves))):
            dlg = self._cover_dialog(page)
            if dlg is None:
                break
            # v4.32：按钮按**文字**兜底找（真机 :text-is 匹配不到「完成」这类按钮，
            # 元素可能不是 button 或含多个文本节点）
            btn = self._pick_text_any(dlg, self.spec.cover_confirm_labels)
            if btn is None:
                raise RuntimeError(
                    f"{self.spec.name}封面弹层里没找到保存按钮（期望文字："
                    + " / ".join(self.spec.cover_confirm_labels)
                    + "）；已停止后续填表，避免卡在裁切层")
            # 刚选完图时「保存」可能还禁用（平台在等图片解码/裁剪框初始化），等它可用
            if not self._wait_enabled(page, btn):
                raise RuntimeError(f"{self.spec.name}封面「保存」按钮一直不可用")
            try:
                btn.click()
            except Exception as exc:                          # noqa: BLE001
                self.cover_warnings.append(f"{self.spec.name}点封面保存失败：{exc}")
                break
            saved += 1
            # 等这个弹层真正关掉；没关就停，避免连点两次保存
            closed = False
            for _ in range(32):                               # 最多等 8s（平台保存常带请求）
                page.wait_for_timeout(250)
                try:
                    if not dlg.is_visible():
                        closed = True
                        break
                except Exception:                             # noqa: BLE001
                    closed = True
                    break
            if not closed:
                raise RuntimeError(
                    f"{self.spec.name}点了封面保存，但弹层 8 秒内没关闭；"
                    f"已停止后续填表，避免在弹层后面盲填（请在浏览器里确认保存结果）")
        return saved

    def _wait_cover_dialog_closed(self, page, dlg, timeout_ms: int = 8000) -> bool:
        for _ in range(max(1, int(timeout_ms / 250))):
            page.wait_for_timeout(250)
            try:
                if not dlg.is_visible():
                    return True
            except Exception:                                 # noqa: BLE001
                return True
        return False

    def _pick_text_any(self, scope, labels):
        """在给定作用域里按**文字**找可见元素（先 Playwright 选择器，再 JS 兜底）。

        真机教训（2026-11-04 抖音）：弹层底部「设置横封面」按钮用
        ``button:text-is("设置横封面")`` 匹配不到（元素不是 button，或按钮内含
        多个文本节点/图标），但它的 innerText 就是这四个字。所以再加一层
        「遍历元素、innerText 精确等于目标文字、取最内层可见元素」的兜底。
        """
        labels = tuple(labels)
        sels: list[str] = []
        for x in labels:
            sels += [f'button:text-is("{x}")', f'button:has-text("{x}")',
                     f'[role="button"]:text-is("{x}")', f'[role="button"]:has-text("{x}")',
                     f'text="{x}"']
        el = self._pick_visible(scope, tuple(sels))
        if el is not None:
            return el
        try:
            handle = scope.evaluate_handle(
                """(root, labels) => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                    const all = Array.from(root.querySelectorAll('*'))
                      .filter(x => x.children.length === 0 || norm(x.innerText).length <= 12);
                    const visible = x => { const r = x.getBoundingClientRect();
                                           return r.width > 2 && r.height > 2; };
                    // 1) innerText 精确等于目标文字（取最内层）
                    for (const lab of labels) {
                      const hit = all.filter(x => visible(x) && norm(x.innerText) === lab)
                        .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length)[0];
                      if (hit) return hit;
                    }
                    // 2) 包含（应对「＋ 添加封面」这类带图标的），取文字最短的可见元素
                    for (const lab of labels) {
                      const hit = all.filter(x => visible(x) && norm(x.innerText).includes(lab)
                                                 && norm(x.innerText).length <= lab.length + 4)
                        .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length)[0];
                      if (hit) return hit;
                    }
                    return null;
                }""", list(labels))
            return handle.as_element() if handle else None
        except Exception:                                     # noqa: BLE001
            return None

    def _click_el(self, el) -> bool:
        """点一个元素：普通点击 → force → JS click；**全程不抛**（元素可能被重挂）。

        真机（快手/抖音）常见：点击瞬间弹层或卡片重渲染，旧句柄 detached，
        Playwright 抛「Element is not attached to the DOM」；这里吞掉，
        由调用方决定重查句柄或放弃，避免一条异常把整条发布打断。
        """
        for kwargs in ({"timeout": 3000}, {"force": True, "timeout": 3000}):
            try:
                el.click(**kwargs)
                return True
            except Exception:                                 # noqa: BLE001
                continue
        try:
            el.evaluate("node => node.click()")
            return True
        except Exception:                                     # noqa: BLE001
            return False

    #: v4.32：在封面弹层里找**真正的封面文件框**（真机结构决定，不是猜 DOM 顺序）。
    #: 抖音实测：`生成参考图` 与 `上传封面` 各是一个 Semi Upload；
    #: 前者所在块文案含「生成参考图」，后者是「点击上传文件或拖拽文件到这里」。
    #: 规则：每个 input 向上找**第一个有文字且不太长**的祖先当“所属块”，
    #: 含「生成参考图」的直接排除；含「上传封面 / 点击上传文件」的才是封面；
    #: 多个命中时优先**可见**的、再优先**文字更短**的（更内层）。
    _COVER_ZONES_JS = r"""
    () => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      const dlg = Array.from(document.querySelectorAll(
          'div[role="dialog"], .semi-modal, .semi-modal-content, .ant-modal, .d-modal, ' +
          '[class*="modal"], [class*="dialog"], [class*="popup"], [class*="cover-plugin"]'))
        .find(x => x.offsetWidth && (x.innerText || '').indexOf('封面') >= 0);
      if (!dlg) return {ok: false, why: 'no-dialog'};
      document.querySelectorAll('input[data-pbzone]').forEach(x => x.removeAttribute('data-pbzone'));
      const inputs = Array.from(dlg.querySelectorAll('input[type="file"]'));
      const seen = [], hits = [];
      inputs.forEach((inp) => {
        const accept = inp.accept || '';
        let p = inp.parentElement, zone = null, zoneText = '';
        for (let n = 0; p && p !== dlg && n < 8; n++, p = p.parentElement) {
          const t = norm(p.innerText);
          if (!t) continue;
          zone = p; zoneText = t.slice(0, 120); break;
        }
        const r = zone ? zone.getBoundingClientRect() : inp.getBoundingClientRect();
        const visible = r.width > 2 && r.height > 2;
        seen.push({zoneText, visible, accept});
        if (!zoneText) return;
        if (!/image/i.test(accept)) return;                 // 绝不碰视频框
        if (zoneText.indexOf('生成参考图') >= 0) return;     // 明确排除 AI 参考图
        if (/(参考图|AI\s*生成|智能参考)/.test(zoneText)) return;
        const looksCover = zoneText.indexOf('上传封面') >= 0
          || zoneText.indexOf('上传新封面') >= 0
          || zoneText.indexOf('更换封面') >= 0
          || zoneText.indexOf('点击上传') >= 0
          || zoneText.indexOf('拖拽文件') >= 0;
        if (!looksCover) return;
        hits.push({inp, zoneText, visible, score: (visible ? 1000 : 0) - zoneText.length});
      });
      hits.sort((a, b) => b.score - a.score);
      hits.forEach((h, seq) => h.inp.setAttribute('data-pbzone', String(seq)));
      return {ok: hits.length > 0,
              zones: hits.map(h => ({zoneText: h.zoneText, visible: h.visible})),
              seen};
    }
    """

    def _cover_zones(self, page):
        """封面弹层里的**上传区**文件框，按「可见优先、更内层优先」排序。

        真机结构决定（2026-11-04 抖音实测）：封面可能是拖拽上传区，块文案含
        「上传封面 / 点击上传文件 / 拖拽文件」；左侧「生成参考图」也是图片框，
        必须按文案排除，不能按 DOM 顺序取。返回（句柄列表, 诊断信息）。
        """
        try:
            info = page.evaluate(self._COVER_ZONES_JS) or {}
        except Exception as exc:                              # noqa: BLE001
            return [], {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
        if not info.get("ok"):
            return [], info
        handles = []
        for seq in range(len(info.get("zones") or [])):
            h = page.query_selector(f'input[data-pbzone="{seq}"]')
            if h is not None:
                handles.append(h)
        return handles, info

    def _cover_upload_input(self, page):
        """返回（最合适的封面文件框句柄, 诊断信息）。找不到返回 (None, 诊断)。"""
        handles, info = self._cover_zones(page)
        return (handles[0] if handles else None), info

    _COVER_THUMB_JS = r"""
    () => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      const anchors = Array.from(document.querySelectorAll('div, span, p'))
        .filter(el => {
          const t = norm(el.innerText);
          return t === '封面设置' || t === '设置封面' || t.indexOf('设置封面') === 0;
        })
        .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length);
      const anchor = anchors[0];
      const collect = (scope) => Array.from(scope.querySelectorAll('img, div, figure, span'))
        .map(el => {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          const bg = (st.backgroundImage || '').indexOf('url(') >= 0;
          return {el, w: r.width, h: r.height,
                  score: (el.tagName === 'IMG' ? 300 : 0) + (bg ? 500 : 0)
                         + Math.min(r.width, r.height)};
        })
        .filter(x => x.w >= 60 && x.h >= 60)
        .sort((a, b) => b.score - a.score);
      // 从锚点逐级向上找：不同平台缩略图挂在 2~4 层祖先里
      let pick = null, scope = anchor;
      for (let lv = 0; lv < 5 && !pick; lv++) {
        if (!scope || !scope.parentElement) break;
        scope = scope.parentElement;
        const cands = collect(scope);
        if (cands.length) pick = cands[0];
      }
      if (!pick) {
        const cands = collect(document.body);     // 最后兜底：全页最大图
        if (cands.length) pick = cands[0];
      }
      if (!pick) return {ok: false, why: 'no-thumb'};
      pick.el.setAttribute('data-pbthumb', '1');
      return {ok: true, tag: pick.el.tagName.toLowerCase(),
              cls: String(pick.el.className || '').slice(0, 60),
              size: [Math.round(pick.w), Math.round(pick.h)]};
    }
    """

    def _cover_thumb(self, page):
        """封面缩略图（快手「封面设置」/ 小红书「设置封面」左上那张），可能只有悬停才出按钮。"""
        try:
            info = page.evaluate(self._COVER_THUMB_JS) or {}
        except Exception:                                     # noqa: BLE001
            return None, {}
        if not info.get("ok"):
            return None, info
        return page.query_selector('[data-pbthumb="1"]'), info

    def _upload_covers_dialog(self, page, covers: dict, wanted: list[str]) -> dict:
        """快手 / B站 / 小红书实测流程：入口 → 弹层 → 上传按钮 → 文件选择器 → 确定。

        三家的共同点：封面弹层里都有一个**明确的上传按钮/拖拽区**（快手「上传图片」、
        B站「上传封面」、小红书「上传」），点它才弹文件选择器；弹层里同时还有
        「AI 生成封面」「智能推荐」等干扰项，不能按图片框顺序猜（v4.32 抖音教训）。
        """
        self._clear_blocking_overlays(page)               # v4.35：先清引导层/遮罩
        if self.spec.key == "xiaohongshu":
            # v4.35：小红书走**真实入口**（div.upload-cover → 「上传封面」tab → upload-wrapper input），
            # 必须在通用「按文字找入口」之前——「设置封面」在那里只是纯文案，点了没用。
            return self._upload_cover_xhs(page, covers, wanted)
        dlg = self._cover_dialog(page)
        if dlg is None:
            # 注意 ignore_existing=True：真机上弹层里的 input 常常**已在 DOM 但隐藏**，
            # 不能因为「有图片框」就不点入口（夹具已复现：不点就永远等不到弹层）。
            self._open_cover_editor(page, timeout_ms=90000, ignore_existing=True)
            dlg = self._wait_cover_dialog(page, 8000) or self._cover_dialog(page)
        if dlg is None:
            raise RuntimeError(f"{self.spec.name}点了封面入口后没有出现封面弹层")

        # v4.36：有 PK 封面开关就打开（开了才有多封面位）
        pk = self._enable_pk_cover(page)
        if pk.get("clicked") or pk.get("on"):
            # v4.37：PK 是**表单上**的多缩略图流程。刚点开时平台会重置普通弹层；
            # 账号本来就开着 PK 时不会重置，但只要弹层还开着就先关掉，避免挡住缩略图。
            dlg = self._cover_dialog(page)
            if dlg is not None:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            return self._upload_covers_pk(page, covers, wanted)

        uploaded: list[str] = []
        labels = tuple(self.spec.cover_upload_labels) or ("上传封面",)
        for idx, key in enumerate(wanted):
            dlg = self._cover_dialog(page) or dlg
            btn = self._pick_text_any(dlg, labels)
            if btn is None:
                # 真机兜底：弹层里有「上传封面/拖拽图片」上传区时，直接喂它的 input
                # （快手有的版本按钮文字与用户截图不同）
                inp, _ = self._cover_upload_input(page)
                if inp is not None:
                    try:
                        inp.set_input_files(covers[key]["path"])
                    except Exception as exc:                  # noqa: BLE001
                        raise RuntimeError(
                            f"{self.spec.name}上传封面 {key} 失败：{exc}") from exc
                    self._wait_cover_ready(page, dlg, 6000)
                    uploaded.append(key)
                    continue
                if idx == 0:
                    raise RuntimeError(
                        f"{self.spec.name}封面弹层里没找到上传按钮（期望："
                        + " / ".join(labels) + "）")
                break
            picked = False
            try:
                with page.expect_file_chooser(timeout=8000) as fc:
                    self._click_el(btn)
                fc.value.set_files(covers[key]["path"])
                picked = True
            except Exception:                                 # noqa: BLE001
                picked = False
            if not picked:
                # 有的平台上传按钮只是展开一个隐藏 input（不弹系统对话框）
                inp, _ = self._cover_upload_input(page)
                if inp is None:
                    raise RuntimeError(
                        f"{self.spec.name}点「{labels[0]}」后既没弹文件选择器，也没有可用的图片框")
                inp.set_input_files(covers[key]["path"])
            self._wait_cover_ready(page, dlg, 6000)           # 平台解码 + 裁剪预览
            uploaded.append(key)

        dlg = self._cover_dialog(page) or dlg
        confirm_labels = tuple(self.spec.cover_confirm_labels) or ("确定", "完成", "保存")
        btn = self._pick_text_any(dlg, confirm_labels)
        if btn is None:
            raise RuntimeError(
                f"{self.spec.name}封面弹层里没找到确定按钮（期望："
                + " / ".join(confirm_labels) + "）；已停止后续填表，避免在弹层后面盲填")
        if not self._wait_enabled(page, btn):
            raise RuntimeError(f"{self.spec.name}封面「{confirm_labels[0]}」按钮一直不可用")
        self._click_el(btn)
        if not self._wait_cover_dialog_closed(page, dlg, timeout_ms=8000):
            raise RuntimeError(
                f"{self.spec.name}点了封面确定，但弹层 8 秒内没关闭；"
                f"已停止后续填表，避免在弹层后面盲填")
        for w in self.cover_warnings:
            print(f"[publisher] {self.spec.name} 封面提示：{w}", flush=True)
        return {"uploaded": uploaded, "confirmed": 1,
                "warnings": list(self.cover_warnings)}

    #: v4.35：遮挡浮层清理（借鉴 social-auto-upload 的 _clear_blocking_overlays）。
    #: 平台常在新手引导/话题下拉期间把点击全拦掉；**注意不含 .semi-portal**——
    #: 抖音封面弹层本身就挂在 semi-portal 里，删了会把自己要用的弹层一起删掉。
    _CLEAR_OVERLAY_JS = r"""
    () => {
      const sels = ['.shepherd-element', '.shepherd-modal-overlay-container',
                    '[class*="mention-wrapper"]', '[class*="tour-"]',
                    '[class*="guide-mask"]'];
      let removed = 0;
      for (const sel of sels) {
        document.querySelectorAll(sel).forEach(el => { el.remove(); removed += 1; });
      }
      return removed;
    }
    """

    def _clear_blocking_overlays(self, page) -> int:
        """删掉引导层/话题下拉等遮挡浮层；找不到就不动页面。返回删除数量（日志用）。"""
        try:
            return int(page.evaluate(self._CLEAR_OVERLAY_JS) or 0)
        except Exception:                                     # noqa: BLE001
            return 0

    def _upload_cover_xhs(self, page, covers: dict, wanted: list[str]) -> dict:
        """小红书封面（v4.35，借鉴 social-auto-upload 的实战选择器）。

        真实流程：`div.upload-cover`（备用 `cover-plugin-preview div.default.pointer`）打开弹层 →
        需要时点「上传封面」tab → `div.upload-wrapper` 里的图片 input 直接 set →
        `d-modal-footer` 里的「确定」。封面是**增强项**：任何一步失败 ⇒ Esc 关弹层 →
        记 warning → 继续填表（首帧兜底）；只有「Esc 也关不掉弹层」才报错（避免盲填）。
        """
        self._clear_blocking_overlays(page)
        dlg = self._cover_dialog(page)
        if dlg is None:
            entry = None
            for sel in ("div.upload-cover",
                        "div.cover-plugin-preview div.default.pointer"):
                try:
                    el = page.query_selector(sel)
                except Exception:                         # noqa: BLE001  选择器随改版
                    el = None
                if el is not None:
                    entry = el
                    break
            if entry is not None:
                self._click_el(entry)
                dlg = self._wait_cover_dialog(page, 8000)
            if dlg is None:
                # 兜底：通用入口（文字/缩略图悬停）
                self._open_cover_editor(page, timeout_ms=60000, ignore_existing=True)
                dlg = self._wait_cover_dialog(page, 8000)
        if dlg is None:
            # v4.35：入口没命中（页面类名是 CSS module，随版本变）⇒ 不让任务失败，
            # 记警告跳过封面继续填表；已 6 次真机未命中，遗留待专项 dump 悬停层级。
            msg = (f"{self.spec.name}没找到封面入口（div.upload-cover / 封面缩略图），"
                   f"本次不上传封面，继续填标题/标签")
            self.cover_warnings.append(msg)
            print(f"[publisher] {self.spec.name} 封面提示：{msg}", flush=True)
            return {"uploaded": [], "confirmed": 0, "skipped": True,
                    "warnings": list(self.cover_warnings)}
        # v4.36：小红书同样有「PK封面」开关，开了才有多封面位
        # 小红书 PK 是「PK封面 1/3 + 编辑」的**逐张**流程（2026-11-05 真机），
        # 与快手的三输入框不同，暂不在此启用（cover_pk_labels 为空），先保证单封面可用。
        key = wanted[0]
        labels = tuple(self.spec.cover_upload_labels) or ("上传",)
        try:
            inp = dlg.query_selector('div[class*="upload-wrapper"] input[type="file"]')
            if inp is None:
                # 真机（2026-11-04 截图）：弹层里没有「上传封面」tab，而是底部右侧
                # 一个「上传」按钮 + 右下角红色「完成」
                btn = self._pick_text_any(dlg, labels)
                if btn is None:
                    raise RuntimeError(
                        f"封面弹层里没找到「{' / '.join(labels)}」按钮")
                picked = False
                try:
                    with page.expect_file_chooser(timeout=8000) as fc:
                        self._click_el(btn)
                    fc.value.set_files(covers[key]["path"])
                    picked = True
                except Exception:                         # noqa: BLE001
                    picked = False
                if not picked:
                    imgs = self._cover_inputs(dlg)
                    inp = imgs[-1] if imgs else None
                    if inp is None:
                        raise RuntimeError("点「上传」后既没弹文件选择器，也没有图片框")
            if inp is not None:
                inp.set_input_files(covers[key]["path"])
            self._wait_cover_ready(page, dlg, 6000)
            btn = self._pick_text_any(dlg, self.spec.cover_confirm_labels)
            if btn is None:
                raise RuntimeError(
                    "封面弹层里没找到保存按钮（期望："
                    + " / ".join(self.spec.cover_confirm_labels) + "）")
            if not self._wait_enabled(page, btn):
                raise RuntimeError("封面保存按钮一直不可用")
            self._click_el(btn)
            if not self._wait_cover_dialog_closed(page, dlg, timeout_ms=8000):
                raise RuntimeError("点了封面保存，但弹层 8 秒内没关闭")
        except Exception as exc:                              # noqa: BLE001
            # 增强项失败：先 Esc，再点标题栏的关闭 ×（小红书是全屏面板，Esc 不一定生效）
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            if self._cover_dialog(page) is not None:
                try:
                    closer = self._pick_text_any(dlg, ("关闭", "取消", "×", "✕"))
                    if closer is None:
                        closer = dlg.query_selector('[class*="close"]')
                    if closer is not None:
                        self._click_el(closer)
                        page.wait_for_timeout(800)
                except Exception:                             # noqa: BLE001
                    pass
            if self._cover_dialog(page) is not None:
                raise RuntimeError(
                    f"{self.spec.name}封面设置失败且关不掉弹层（{exc}）；"
                    f"已停止后续填表，避免在弹层后面盲填") from exc
            msg = f"{self.spec.name}封面设置失败，已跳过（用视频首帧兜底）：{exc}"
            self.cover_warnings.append(msg)
            print(f"[publisher] {self.spec.name} 封面提示：{msg}", flush=True)
            return {"uploaded": [], "confirmed": 0, "skipped": True,
                    "warnings": list(self.cover_warnings)}
        for w in self.cover_warnings:
            print(f"[publisher] {self.spec.name} 封面提示：{w}", flush=True)
        return {"uploaded": [key], "confirmed": 1,
                "warnings": list(self.cover_warnings)}

    _PK_TOGGLE_JS = r"""
    () => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      const anchors = Array.from(document.querySelectorAll('span, div, p, label'))
        .filter(el => norm(el.innerText).indexOf('PK封面') >= 0 && norm(el.innerText).length <= 16)
        .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length);
      for (const a of anchors) {
        let scope = a;
        for (let lv = 0; lv < 4 && scope; lv++) {
          scope = scope.parentElement;
          if (!scope) break;
          const sw = scope.querySelector('[role="switch"], [class*="switch"], input[type="checkbox"]');
          if (!sw) continue;
          const cls = String(sw.className || '') + ' ' + String((sw.parentElement || {}).className || '');
          const aria = sw.getAttribute && sw.getAttribute('aria-checked');
          const boxed = sw.querySelector && sw.querySelector('input[type="checkbox"]');
          const on = aria === 'true' || /checked|d-switch-on|semi-switch-checked/i.test(cls)
                     && !/unchecked/i.test(cls) || (boxed && boxed.checked);
          sw.setAttribute('data-pbpk', '1');
          if (!on) return {found: true, clicked: true, cls: String(sw.className || '').slice(0, 60)};
          return {found: true, clicked: false, on: true, cls: String(sw.className || '').slice(0, 60)};
        }
      }
      return {found: false};
    }
    """

    def _enable_pk_cover(self, page) -> dict:
        """v4.36：打开「PK封面」开关（开了才有多封面）。找不到/已开都返回诊断，不抛。"""
        labels = tuple(self.spec.cover_pk_labels)
        if not labels:
            return {"found": False, "why": "platform-no-pk"}
        try:
            info = page.evaluate(self._PK_TOGGLE_JS) or {}
        except Exception as exc:                              # noqa: BLE001
            return {"found": False, "why": f"{type(exc).__name__}: {exc}"}
        if info.get("clicked"):
            sw = page.query_selector('[data-pbpk="1"]')
            if sw is not None:
                self._click_el(sw)
            page.wait_for_timeout(1800)                       # 平台会重排封面位
        return info

    def _cover_slot_inputs(self, page, dlg, limit: int) -> list:
        """弹层里的**封面**文件框（按出现顺序，最多 limit 个）；排除参考图/AI 区。"""
        try:
            info = page.evaluate(self._COVER_ZONES_JS) or {}
        except Exception:                                     # noqa: BLE001
            info = {}
        handles = []
        for seq in range(len(info.get("zones") or [])):
            h = page.query_selector(f'input[data-pbzone="{seq}"]')
            if h is not None:
                handles.append(h)
        if not handles and dlg is not None:
            handles = [x for x in self._cover_inputs(dlg)]
        return handles[:max(1, limit)]

    _COVER_THUMBS_JS = r"""
    (limit) => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
      const anchors = Array.from(document.querySelectorAll('span, div, p'))
        .filter(el => /设置封面|封面设置/.test(norm(el.innerText)) && norm(el.innerText).length < 14)
        .sort((a, b) => norm(a.innerText).length - norm(b.innerText).length);
      const anchor = anchors[0];
      let scope = anchor;
      for (let i = 0; i < 4 && scope; i++) scope = scope.parentElement;
      const root = scope || document.body;
      const cands = Array.from(root.querySelectorAll('img, div'))
        .map(el => {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          const bg = (st.backgroundImage || '').indexOf('url(') >= 0;
          return {el, w: r.width, h: r.height,
                  score: (el.tagName === 'IMG' ? 300 : 0) + (bg ? 500 : 0)
                         + Math.min(r.width, r.height)};
        })
        .filter(x => x.w >= 50 && x.h >= 50)
        .sort((a, b) => b.score - a.score);
      const picked = [];
      for (const c of cands) {
        const r = c.el.getBoundingClientRect();
        const clash = picked.some(p => {
          const q = p.el.getBoundingClientRect();
          return !(r.right < q.left + 8 || r.left > q.right - 8
                   || r.bottom < q.top + 8 || r.top > q.bottom - 8);
        });
        if (!clash) {
          c.el.setAttribute('data-pbthumb' + picked.length, '1');
          picked.push(c);
        }
        if (picked.length >= (limit || 3)) break;
      }
      return picked.length;
    }
    """

    def _cover_thumbs(self, page, limit: int = 3) -> list:
        """v4.37：表单封面区里的**多张缩略图**（PK 封面打开后会有 2~3 张）。"""
        try:
            n = int(page.evaluate(self._COVER_THUMBS_JS, limit) or 0)
        except Exception:                                     # noqa: BLE001
            return []
        out = []
        for i in range(n):
            h = page.query_selector(f'[data-pbthumb{i}="1"]')
            if h is not None:
                out.append(h)
        return out

    def _upload_covers_pk(self, page, covers: dict, wanted: list[str]) -> dict:
        """v4.37：PK 封面 = 表单上出现**多个封面缩略图**，逐个点开弹层上传。

        真机（2026-11-05 快手，用户红框截图）：PK 开关在封面设置下方（表单上）；
        打开后封面区出现多张缩略图，**每张各自点开「封面截取」弹层**上传。
        """
        limit = max(2, int(self.spec.cover_pk_slots or 3))
        thumbs = self._cover_thumbs(page, limit)
        if len(thumbs) < 2:
            raise RuntimeError(
                f"{self.spec.name}开了 PK 封面但页面上只有 {len(thumbs)} 个封面缩略图")
        # 多封面尽量用**不同的图**：先按平台比例，再把其余可用比例补上
        order = [k for k in self.spec.cover_ratios if k in covers]
        order += [k for k in covers if k not in order]
        if not order:
            raise RuntimeError("没有可用的封面文件")
        confirm_labels = tuple(self.spec.cover_confirm_labels) or ("确认", "确定", "完成")
        uploaded: list[str] = []
        for i, th in enumerate(thumbs[:limit]):
            path = covers[order[i % len(order)]]["path"]
            self._click_el(th)
            dlg = self._wait_cover_dialog(page, 8000)
            if dlg is None:
                raise RuntimeError(
                    f"{self.spec.name}PK 第 {i + 1} 个封面位点开后没有出现弹层")
            self._upload_cover_file(page, path, f"PK{i + 1}")
            btn = self._pick_text_any(self._cover_dialog(page) or dlg, confirm_labels)
            if btn is None:
                raise RuntimeError(
                    f"{self.spec.name}PK 第 {i + 1} 张封面弹层里没找到确定按钮（期望："
                    + " / ".join(confirm_labels) + "）")
            if not self._wait_enabled(page, btn):
                raise RuntimeError(f"{self.spec.name}PK 第 {i + 1} 张确认按钮一直不可用")
            self._click_el(btn)
            if not self._wait_cover_dialog_closed(page, dlg, timeout_ms=8000):
                raise RuntimeError(
                    f"{self.spec.name}PK 第 {i + 1} 张确认后弹层 8 秒内没关闭")
            uploaded.append(f"{order[i % len(order)]}#{i + 1}")
        return {"uploaded": uploaded, "confirmed": len(uploaded), "pk": True,
                "warnings": list(self.cover_warnings)}

    def _cover_upload_button(self, page):
        """兜底入口：封面弹层里「上传封面」按钮（真机是拖拽区，不一定有 button）。"""
        dlg = self._cover_dialog(page)
        if dlg is None:
            return None
        labels = tuple(self.spec.cover_upload_labels) or ("上传封面",)
        sels: list[str] = []
        for x in labels:
            sels += [f'button:text-is("{x}")', f'[role="button"]:text-is("{x}")',
                     f'button:has-text("{x}")', f'[role="button"]:has-text("{x}")',
                     f'text="{x}"']
        return self._pick_visible(dlg, tuple(sels))

    def _upload_cover_file(self, page, path: str, step_name: str) -> None:
        """把图放进**封面上传区**里的那个 input；找不到就如实报错，不按顺序猜。

        真机实测（2026-11-04 抖音）：封面是 Semi Upload 的拖拽/点击区，里面藏着
        ``input[type=file]``；**点它容易被图标挡住**（Playwright 报 element intercepts
        pointer events），直接 ``set_input_files`` 才稳。所以优先走 input，
        没有 input 时才退化为「点按钮 + 文件选择器」。
        """
        # v4.35：**结构化槽位选择器优先**（`.semi-upload:has(拖拽主文案)`）——
        # 不依赖「生成参考图/上传封面」文案，改版容忍度更高
        dlg = self._cover_dialog(page)
        for sel in self.spec.cover_slot_selectors:
            roots = [dlg, page] if dlg is not None else [page]
            for root in roots:
                try:
                    el = root.query_selector(sel)
                except Exception:                             # noqa: BLE001  选择器语法随改版
                    el = None
                if el is None:
                    continue
                try:
                    el.set_input_files(path)
                except Exception as exc:                      # noqa: BLE001
                    raise RuntimeError(
                        f"{self.spec.name}{step_name}选文件失败：{exc}") from exc
                self._wait_cover_ready(page, self._cover_dialog(page), 6000)
                return
        inp, info = self._cover_upload_input(page)
        if inp is None and dlg is not None:
            # v4.37 兜底：PK 态弹层的上传区文案可能与普通态不同 ⇒ 直接用弹层里的图片框
            imgs = self._cover_inputs(dlg)
            inp = imgs[0] if imgs else None
            if inp is None:
                # v4.37b：PK2 疑似输入框**不带 accept=image**（参考项目就是裸
                # input[type=file] 直接用）⇒ 弹层里任何**非视频**文件框都可以试
                try:
                    cands = [x for x in dlg.query_selector_all('input[type="file"]')
                             if "video" not in (x.get_attribute("accept") or "").lower()]
                except Exception:                             # noqa: BLE001
                    cands = []
                inp = cands[0] if cands else None
        if inp is not None:
            try:
                inp.set_input_files(path)
            except Exception as exc:                          # noqa: BLE001
                raise RuntimeError(
                    f"{self.spec.name}{step_name}选文件失败：{exc}") from exc
            self._wait_cover_ready(page, self._cover_dialog(page), 6000)
            return

        btn = self._cover_upload_button(page)
        if btn is None:
            seen = "; ".join(
                f"{x.get('zoneText', '')[:40]}({'可见' if x.get('visible') else '隐藏'})"
                for x in (info.get("seen") or [])[:6])
            raise RuntimeError(
                f"{self.spec.name}{step_name}页没找到「上传封面」上传区（页面可能改版）。"
                f"为避免把封面传进「生成参考图」，已停止；请在浏览器里手动选封面。"
                f"（识别到的文件框：{seen or info.get('why') or '无'}）")
        try:
            with page.expect_file_chooser(timeout=8000) as fc:
                btn.click(force=True)
            fc.value.set_files(path)
        except Exception:
            # 点的可能只是个 tab（「上传封面」在 PK 态是页签）；再试弹层内的图片框
            dlg2 = self._cover_dialog(page)
            cands = []
            if dlg2 is not None:
                try:
                    cands = [x for x in dlg2.query_selector_all('input[type="file"]')
                             if "video" not in (x.get_attribute("accept") or "").lower()]
                except Exception:                             # noqa: BLE001
                    cands = []
            if not cands:
                raise RuntimeError(
                    f"{self.spec.name}{step_name}页点了「上传封面」但没弹出文件选择器，"
                    f"弹层里也没有可用的图片框；已停止，避免传错位置")
            try:
                cands[0].set_input_files(path)
            except Exception as exc:                          # noqa: BLE001
                raise RuntimeError(
                    f"{self.spec.name}{step_name}选文件失败：{exc}") from exc
        self._wait_cover_ready(page, self._cover_dialog(page), 6000)

    def _upload_covers_dual_wizard(self, page, covers: dict) -> dict:
        """抖音实测流程：同一弹层里两步向导。

        1. 点竖封面卡片的「选择封面」；
        2. 在「设置竖封面」页把 3:4 图放进**上传封面上传区**；
        3. 点「设置横封面」切到第二步；
        4. 在「设置横封面」页把 4:3 图放进上传区；
        5. 点「完成」，等弹层关闭，后续 fill 才能继续。

        ⚠️ 两个真机教训（2026-11-04）：
        · **绝不按「弹层里第一个图片文件框」选文件**——左侧「生成参考图」也是
          accept=image 的文件框，会把封面传错位置（用户截图实测）；
        · **每次上传后弹层 DOM 会重挂**，之前拿到的 `dlg` 句柄会失效，
          必须重新 `_cover_dialog(page)`，否则「设置横封面」永远找不到。
        """
        self._clear_blocking_overlays(page)               # v4.35：清引导层/话题下拉遮挡
        dlg = self._cover_dialog(page)
        if dlg is None:
            trg = self._pick_visible(page, self.spec.cover_open_trigger
                                     or self.spec.cover_trigger)
            if trg is None:
                raise RuntimeError(
                    f"没找到{self.spec.name}封面入口（期望「选择封面」）")
            trg.click()
            dlg = self._wait_cover_dialog(page, 8000)
            if dlg is None:
                raise RuntimeError(f"{self.spec.name}点「选择封面」后没有出现封面弹层")

        # 第一步：竖封面 3:4
        self._upload_cover_file(page, covers["3x4"]["path"], "竖封面")

        # 上传后弹层 DOM 会重挂（真机实测）⇒ 重新取弹层，别用旧句柄
        dlg = self._cover_dialog(page) or dlg

        # 切到横封面步骤（顶部 tab 或右下角按钮，文案相同）
        next_btn = self._pick_text_any(dlg, self.spec.cover_next_labels)
        if next_btn is None:
            raise RuntimeError(
                f"{self.spec.name}竖封面页没找到切换按钮（期望："
                + " / ".join(self.spec.cover_next_labels) + "）")
        if not self._wait_enabled(page, next_btn):
            raise RuntimeError(f"{self.spec.name}「设置横封面」按钮一直不可用")
        self._click_el(next_btn)
        self._wait_cover_ready(page, dlg, 5000)

        # 第二步：横封面 4:3
        dlg = self._cover_dialog(page) or dlg
        self._upload_cover_file(page, covers["4x3"]["path"], "横封面")
        dlg = self._cover_dialog(page) or dlg

        # 最后一步：完成（**这个才是把两张封面落盘的按钮**）
        done = self._pick_text_any(dlg, self.spec.cover_confirm_labels)
        if done is None:
            raise RuntimeError(
                f"{self.spec.name}横封面页没找到完成按钮（期望："
                + " / ".join(self.spec.cover_confirm_labels) + "）")
        if not self._wait_enabled(page, done):
            raise RuntimeError(f"{self.spec.name}封面「完成」按钮一直不可用")
        self._click_el(done)
        if not self._wait_cover_dialog_closed(page, dlg):
            raise RuntimeError(
                f"{self.spec.name}点了封面「完成」，但弹层 8 秒内没关闭；"
                f"已停止后续填表，避免在弹层后面盲填")
        uploaded = ["3x4", "4x3"]
        for w in self.cover_warnings:
            print(f"[publisher] {self.spec.name} 封面提示：{w}", flush=True)
        return {"uploaded": uploaded, "confirmed": 1,
                "warnings": list(self.cover_warnings)}

    def set_cover(self, assets: dict) -> dict:
        """上传 3:4 / 4:3 封面。

        平台页面**只提供一份图片框时只传首选比例**；提供两份时按 cover_ratios
        顺序各传一份。图片路径由 ``publish_cover`` 统一派生，本方法只负责选文件。
        找不到入口会如实报错；找到入口但没找到弹层确认按钮，只记 warning
        （有的平台选完文件即生效，不能因为没弹窗就判失败）。
        """
        page = self._page
        self.cover_warnings = []
        if not assets or not assets.get("ok"):
            raise RuntimeError("没有可用的封面文件（封面生成失败）")
        covers = {str(c.get("key")): c for c in (assets.get("covers") or [])
                  if c.get("exists")}
        if not covers:
            raise RuntimeError("没有找到 3:4 / 4:3 封面文件")
        self._wait_form(page, 300000)

        wanted = [k for k in self.spec.cover_ratios if k in covers]
        if not wanted:
            raise RuntimeError("封面比例与该平台配置不匹配")
        if self.spec.cover_flow == "dual_wizard":
            # 抖音：同一弹层内「竖封面 → 设置横封面 → 完成」
            return self._upload_covers_dual_wizard(page, covers)
        if self.spec.cover_flow == "dialog":
            # 快手 / B站 / 小红书（用户截图的真实流程）：入口 → 弹层 → 上传按钮 → 确定
            return self._upload_covers_dialog(page, covers, wanted)

        # 通用平台（快手 / B站 / 小红书）：先打开封面弹层，再按**上传区**定位文件框
        # ⚠️ B站实测：表单先出现、视频还在上传（0%），封面区要等上传推进后才渲染 ⇒
        # 这里必须**等封面入口出现**再点，不能只等表单（否则必然报「没找到封面入口」）。
        dlg = self._cover_dialog(page)
        if dlg is None:
            self._open_cover_editor(page, timeout_ms=90000)
            page.wait_for_timeout(2600)     # B站弹层要 2s 左右才渲染出上传区
            dlg = self._cover_dialog(page)

        zones, zinfo = self._cover_zones(page) if dlg is not None else ([], {})
        if zones:
            uploaded: list[str] = []
            for idx, key in enumerate(wanted):
                if idx >= len(zones):
                    self.cover_warnings.append(
                        f"{self.spec.name}页面上只有 {len(zones)} 个封面上传区，"
                        f"已上传 {'/'.join(uploaded) or '无'}；{key} 没有独立入口")
                    break
                try:
                    zones[idx].set_input_files(covers[key]["path"])
                except Exception as exc:                      # noqa: BLE001
                    raise RuntimeError(
                        f"{self.spec.name}上传封面 {key} 失败：{exc}") from exc
                self._wait_cover_ready(page, dlg, 6000)       # 平台解码/初始化裁切框
                uploaded.append(key)
                # 上传后弹层 DOM 可能重挂（抖音实测），句柄要重新取
                dlg = self._cover_dialog(page) or dlg
                zones, _ = self._cover_zones(page) if dlg is not None else ([], {})
            if not uploaded:
                raise RuntimeError(f"{self.spec.name}没能上传任何封面")
            confirmed = 0
            btn = (self._pick_text_any(dlg, self.spec.cover_confirm_labels)
                   if dlg is not None else None)
            if btn is not None and self._wait_enabled(page, btn):
                self._click_el(btn)
                confirmed = 1
                if not self._wait_cover_dialog_closed(page, dlg, timeout_ms=8000):
                    raise RuntimeError(
                        f"{self.spec.name}点了封面保存，但弹层 8 秒内没关闭；"
                        f"已停止后续填表，避免在弹层后面盲填")
            elif dlg is not None:
                # 有弹层却没有保存按钮 ⇒ 绝不能带着弹层去填标题（会填到弹层后面）
                raise RuntimeError(
                    f"{self.spec.name}封面弹层里没找到保存按钮（期望："
                    + " / ".join(self.spec.cover_confirm_labels)
                    + "）；已停止后续填表，避免在弹层后面盲填")
            else:
                self.cover_warnings.append(
                    f"{self.spec.name}封面是页内直接上传（无弹层），已选文件即生效")
            for w in self.cover_warnings:
                print(f"[publisher] {self.spec.name} 封面提示：{w}", flush=True)
            return {"uploaded": uploaded, "confirmed": confirmed,
                    "warnings": list(self.cover_warnings)}

        # 兜底：弹层里没有「上传区」语义的框，退回页面上的图片框（仍不碰视频框）
        inputs0 = self._cover_inputs(page)
        if not inputs0:
            self._open_cover_editor(page)
        elif not any(self._safe_visible(el) for el in inputs0):
            # 图片框已在 DOM 但整块弹层是隐藏的：点一次触发器把它打开
            self._open_cover_editor(page, timeout_ms=2500, ignore_existing=True)
        if not self._cover_inputs(page):
            seen = "; ".join((x.get("zoneText") or "")[:36]
                             for x in (zinfo.get("seen") or [])[:6])
            # v4.32：**找不到封面入口不再让整条任务失败**——发布才是主目的，
            # 封面缺失只记警告继续填表（否则平台一改版就完全发不出去）。
            msg = (f"没找到{self.spec.name}封面上传入口（页面可能改版；"
                   f"识别到的文件框：{seen or zinfo.get('why') or '无'}）；"
                   f"本次不上传封面，继续填标题/标签")
            self.cover_warnings.append(msg)
            print(f"[publisher] {self.spec.name} 封面提示：{msg}", flush=True)
            return {"uploaded": [], "confirmed": 0, "skipped": True,
                    "warnings": list(self.cover_warnings)}

        uploaded: list[str] = []
        for idx, key in enumerate(wanted):
            inputs = self._cover_inputs(page)
            if idx >= len(inputs):
                if idx == 0:
                    raise RuntimeError(f"没找到{self.spec.name}封面图片文件框")
                self.cover_warnings.append(
                    f"{self.spec.name}页面只有一个封面图片框，已上传 {wanted[0]}；"
                    f"{key} 没有独立的上传入口")
                break
            try:
                inputs[idx].set_input_files(covers[key]["path"])
                page.wait_for_timeout(1200)
                uploaded.append(key)
            except Exception as exc:                          # noqa: BLE001
                raise RuntimeError(f"上传封面 {key} 失败：{exc}") from exc

        confirmed = self._confirm_covers(page, max_saves=len(wanted))
        if not confirmed:
            self.cover_warnings.append(
                "已选封面图片，但没找到含「封面」的弹层保存按钮；"
                "若页面仍停在裁切/预览层，请人工点一次保存")
        for w in self.cover_warnings:
            print(f"[publisher] {self.spec.name} 封面提示：{w}", flush=True)
        return {"uploaded": uploaded, "confirmed": int(confirmed),
                "warnings": list(self.cover_warnings)}

    def fill(self, title: str, tags: list[str]) -> None:
        """填标题 / 简介 / 标签。**只填，不点发布**（R1）。

        三家平台的差异只在 ``platforms`` 的规格里：
        · 有独立标题框（抖音/B站/小红书）→ 填标题框；
        · 没有（快手）→ 标题写进描述第一行（``title_in_desc``）；
        · 有独立标签框（B站）→ 逐个填 + 回车；没有 → 在描述里打 ``#话题``。
        """
        page = self._page
        self.warnings = []
        # 大文件要等上传开始后表单才齐，这里给足时间（5 分钟）
        self._wait_form(page, 300000)
        limit = self.spec.tag_count_max
        clean = [str(t).strip().lstrip("#") for t in (tags or []) if str(t).strip()][:limit]

        desc = self._pick(page, self.spec.desc_inputs)
        title_el = None if self.spec.title_in_desc else self._pick(page, self.spec.title_inputs)

        if title and title_el is not None:
            try:
                title_el.click()
                title_el.fill(title[: self.spec.title_max])
                page.wait_for_timeout(400)
            except Exception:                                 # noqa: BLE001
                self.warnings.append("标题框写入失败")
        elif title and not self.spec.title_in_desc:
            self.warnings.append("没找到标题输入框")

        if clean and self.spec.tag_inputs:
            tag_el = self._pick(page, self.spec.tag_inputs)
            if tag_el is None:
                self.warnings.append("没找到标签输入框")
            else:
                for t in clean:
                    try:
                        tag_el.click()
                        tag_el.fill(t[: self.spec.tag_max])
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(500)
                    except Exception:                         # noqa: BLE001
                        self.warnings.append(f"标签「{t}」写入失败")
                        break
        elif clean or (title and self.spec.title_in_desc):
            if desc is None:
                self.warnings.append("没找到描述输入框（标题/话题都没写进去）")
            else:
                try:
                    desc.click()
                    page.wait_for_timeout(300)
                    if self.spec.title_in_desc and title:
                        page.keyboard.type(title[: self.spec.title_max])
                        page.wait_for_timeout(300)
                        if clean:
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(200)
                    for t in clean:
                        page.keyboard.type("#" + t[: self.spec.tag_max])
                        page.wait_for_timeout(600)   # 等话题下拉出现
                        page.keyboard.press("Space") # 空格确认话题（变成 chip）
                        page.wait_for_timeout(400)
                except Exception:                             # noqa: BLE001
                    self.warnings.append("描述框写入失败")
        if self.warnings:
            raise RuntimeError(f"{self.spec.name}填表不完整：" + "；".join(self.warnings))

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:                                     # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:                                     # noqa: BLE001
            pass
        self._ctx = None
        self._page = None
        self._pw = None


# ---------------------------------------------------------------- 执行器

def profile_dir(platform: str = platforms.DEFAULT_PLATFORM) -> Path:
    """某平台的浏览器用户数据目录（**登录态就存在这里**）。

    与 :meth:`PlaywrightDriver._ensure_browser` 里的 ``user_data_dir`` 必须是同一个，
    否则「界面显示已登录」和「发布时能免扫码」会指向两份数据（规则写两遍必漏）。

    · **抖音沿用** ``ws.publisher_profile`` ——
      历史上所有用户的登录态都在那里，不能改路径；
    · 其余平台各自一个 ``profile_<平台>``：四个平台登不同账号、同时只允许一个浏览器实例，
      共用一个目录会互相抢锁，也会把「换号」弄成把别的平台顶掉。
    """
    key = platforms.normalize(platform)
    if key == platforms.DEFAULT_PLATFORM:
        return ws.publisher_profile
    return ws.publisher_data / f"profile_{key}"


def login_state(platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    """轻量判定「某平台创作者是否已登录」——**不启动浏览器**。

    判据：该平台 profile 目录里的 Cookies（Chromium/Edge 的 SQLite）中是否有该平台域的 cookie。
    cookie 还在 ≠ 服务端会话一定有效，所以同时给出 ``updatedAt`` 与 ``hint`` 让界面如实说明。

    浏览器开着时 Cookies 会被锁住（直接 sqlite3 连会抛异常）→ **复制一份再读**，
    读不到就如实回 ``locked=True``，不假装"未登录"。
    """
    spec = platforms.get(platform)
    d = profile_dir(spec.key)
    probe_file = ws.tmp_dir / f"publisher_cookies_probe_{spec.key}.sqlite"
    out = {
        "platform": spec.key,
        "platformName": spec.name,
        "profileDir": str(d),
        "exists": d.is_dir(),
        "loggedIn": False,
        "cookieCount": 0,
        "updatedAt": None,
        "locked": False,
        "hint": "",
    }
    if not out["exists"]:
        out["hint"] = (f"还没登录过{spec.name}。点「登录/换号」扫码后，"
                       f"登录数据会保存在上面的目录里。")
        return out

    cookie_file = None
    for rel in ("Default/Network/Cookies", "Default/Cookies", "Cookies"):
        p = d / rel
        if p.is_file():
            cookie_file = p
            break
    if cookie_file is None:
        out["hint"] = "登录数据目录已建，但还没写入 cookie —— 请先扫码登录。"
        return out

    out["updatedAt"] = round(cookie_file.stat().st_mtime, 1)
    probe = probe_file
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cookie_file, probe)
        conn = sqlite3.connect(f"file:{probe.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM cookies WHERE " +
                " OR ".join("host_key LIKE ?" for _ in spec.cookie_hosts),
                tuple(f"%{h}%" for h in spec.cookie_hosts)).fetchone()
            out["cookieCount"] = int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:                                  # noqa: BLE001
        out["locked"] = True
        out["hint"] = f"读 cookie 失败（浏览器可能正在占用）：{exc}"
        return out
    except OSError as exc:                                        # noqa: BLE001
        out["locked"] = True
        out["hint"] = f"复制 cookie 失败：{exc}"
        return out
    finally:
        try:
            if probe.is_file():
                probe.unlink()
        except OSError:
            pass

    out["loggedIn"] = out["cookieCount"] > 0
    out["hint"] = (f"已检测到 {spec.name} 登录 cookie —— 发布时通常无需重新扫码。"
                   if out["loggedIn"] else
                   f"目录里没有 {spec.name} 的 cookie，发布时会要求扫码登录。")
    return out


def open_creator(url: str | None = None, platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    """打开某平台创作者中心让人扫码登录（**只开页面，不做任何发布动作**，R1）。

    用系统 Edge 直接拉起并指定 ``--user-data-dir`` 指向该平台的 :func:`profile_dir`，
    与发布执行器共用同一份登录数据；不直接起 Playwright 是因为那会把浏览器
    生命周期绑进本进程（登录是要**留给人在浏览器里操作**的）。

    ``url`` 仍是**第一位**参数（v3.x 的调用方按位置传地址，别破坏它）；
    新代码用 ``platform=`` 选平台，地址由注册表给。
    """
    spec = platforms.get(platform)
    target = (url or "").strip() or spec.creator_url
    if not target.startswith("http"):
        return {"ok": False, "error": "创作者中心地址不合法", "platform": spec.key}
    edge = None
    for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
              Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")):
        if p.is_file():
            edge = p
            break
    if edge is None:
        return {"ok": False, "error": "没找到系统 Edge，请手动打开创作者中心登录",
                "url": target, "platform": spec.key}
    try:
        from app.common import window_focus
        from app.common.proc import popen
        # v4.20：启动前先记下现有 Edge 窗口，启动后把**新出现的那个**拉到前台。
        # 只靠 CreateProcess 不会激活浏览器（Windows 前台窗口锁），用户得再点一次任务栏。
        before = window_focus.snapshot_edge_windows()
        popen([str(edge), f"--user-data-dir={profile_dir(spec.key)}", "--new-window",
               "--no-first-run", "--no-default-browser-check", target])
        act = window_focus.activate_new_edge_window(
            before, spec.window_hints or (spec.name, spec.creator_label), wait_sec=6.0)
    except Exception as exc:                                      # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": target,
                "platform": spec.key}
    return {"ok": True, "url": target, "profileDir": str(profile_dir(spec.key)),
            "platform": spec.key, "activated": bool(act.get("activated")),
            "activateHint": act.get("reason") or "",
            "windowTitle": act.get("title") or ""}


def _set_job(conn, job_id: int, *, status: str | None = None,
             phase: str | None = None, error: str | None = None) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if phase is not None:
        sets.append("phase = ?")
        params.append(phase)
    if error is not None:
        sets.append("error_message = ?")
        params.append(error)
    sets.append("updated_at = ?")
    params.append(database.utcnow())
    params.append(job_id)
    conn.execute(f"UPDATE publish_jobs SET {', '.join(sets)} WHERE id = ?", params)


def run_job(job_id: int, *, driver: Driver | None = None,
            db: Path | None = None, log=print) -> dict:
    """执行一条发布任务：自动上传 + 填表，**停在 waiting_user**。

    返回最终状态快照。driver 缺省时用 PlaywrightDriver。
    """
    with database.session(db) as conn:
        row = conn.execute("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
        platform = platforms.normalize(row["platform"] if "platform" in row.keys() else None)
        # 驱动跟着**任务自己的平台**走：队列里每条都记了平台，切换界面的当前平台不影响它
        drv = driver or PlaywrightDriver(platform=platform)
        title = row["title"]
        tags = []
        import json as _json
        try:
            tags = _json.loads(row["tags_json"] or "[]")
        except ValueError:
            tags = []
        file_path = row["file_path"]
        source_kind = (row["source_kind"] if "source_kind" in row.keys() else "edit")
        # 封面是发布资产的一部分，**在启动浏览器之前**就派生好：
        # 这里失败就停在 preflight，不会出现「视频已上传、封面却生成失败」的半截任务。
        cover_assets = None
        # 只对**真的实现了封面**的 driver 生成封面资产。老的 fake driver 继承协议里的
        # 占位 set_cover（NotImplementedError），不能因此把无关的队列冒烟也拖去抽帧。
        impl = getattr(type(drv), "set_cover", None)
        cover_setter = None if impl is Driver.set_cover else getattr(drv, "set_cover", None)
        auto_cover = True
        if cover_setter is not None:
            try:
                from app.editor.backend.api import settings as settings_api
                # v4.31：开关按平台独立；读**任务自己的平台**（队列每条都记了平台）
                auto_cover = settings_api.auto_cover_enabled(platform)
            except Exception:                            # noqa: BLE001
                auto_cover = True                        # 设置读不到时不改变旧行为
            if not auto_cover:
                cover_setter = None
        if cover_setter is not None:
            try:
                cover_assets = publish_cover.assets_for_file(file_path, str(source_kind or "edit"))
            except Exception as exc:                      # noqa: BLE001
                msg = f"生成发布封面失败：{exc}"
                _set_job(conn, job_id, status=STATUS_FAILED, phase="preflight", error=msg)
                return {"ok": False, "status": STATUS_FAILED, "phase": "preflight",
                        "error": msg}
        cover_result = ({"enabled": False, "skipped": True}
                        if not auto_cover else None)

        # 每次触发都计一次尝试（重试也计数，用于界面展示与排障）
        conn.execute(
            "UPDATE publish_jobs SET attempt_count = attempt_count + 1, "
            "error_message = '', updated_at = ? WHERE id = ?",
            (database.utcnow(), job_id),
        )
        _set_job(conn, job_id, status=STATUS_RUNNING, phase="preflight")

        for phase in PHASES:
            if phase == "waiting_user":
                break
            _set_job(conn, job_id, phase=phase)
            log(f"[publisher] job#{job_id} → {phase}")
            try:
                if phase == "checking_login":
                    if not drv.check_login():
                        msg = (f"未登录{platforms.get(platform).name}创作中心，"
                               f"请先在发布页该平台的卡片上点「登录/换号」")
                        # v4.17：顺手把 DB 状态也落成 failed。原先只写 phase + error、
                        # status 仍停在 running ⇒ 队列里永远显示「处理中」，还挡着重来。
                        _set_job(conn, job_id, status=STATUS_FAILED, phase=phase, error=msg)
                        return {"ok": False, "status": STATUS_FAILED, "phase": phase,
                                "error": msg}
                elif phase == "launching_browser":
                    drv.launch()
                elif phase == "loading_publish_page":
                    drv.load_publish_page()
                elif phase == "uploading":
                    drv.upload(file_path)
                elif phase == "filling":
                    if cover_setter is not None:
                        cover_result = cover_setter(cover_assets)
                        for w in (cover_result.get("warnings") or []):
                            log(f"[publisher] job#{job_id} 封面提示：{w}")
                    drv.fill(title, tags)
            except Exception as exc:                       # noqa: BLE001
                # 防重复投稿：uploading 及之后失败不自动重试，直接 failed
                _set_job(conn, job_id, status=STATUS_FAILED, phase=phase,
                         error=f"{phase} 失败：{exc}")
                log(f"[publisher] job#{job_id} {phase} 失败：{exc}")
                return {"ok": False, "status": STATUS_FAILED, "phase": phase,
                        "error": f"{phase} 失败：{exc}"}

        # v4.38：填表完成后的去向——「自动发布」开 ⇒ 点一次「发布」；关 ⇒ 停 waiting_user
        auto_publish = False
        try:
            from app.editor.backend.api import settings as settings_api
            auto_publish = settings_api.auto_publish_enabled(platform)
        except Exception:                                  # noqa: BLE001
            auto_publish = False                            # 设置读不到时保持旧行为（不点发布）
        if auto_publish:
            if getattr(type(drv), "click_publish", None) is Driver.click_publish:
                msg = "该任务驱动未实现 click_publish，无法自动发布"
                _set_job(conn, job_id, status=STATUS_FAILED, phase="publishing", error=msg)
                return {"ok": False, "status": STATUS_FAILED, "phase": "publishing",
                        "error": msg}
            _set_job(conn, job_id, phase="publishing")
            log(f"[publisher] job#{job_id} → publishing（自动发布开关已开，只点一次）")
            try:
                pub_result = drv.click_publish()
            except Exception as exc:                       # noqa: BLE001
                # 防重复投稿：发布失败绝不重试、绝不二次点击
                _set_job(conn, job_id, status=STATUS_FAILED, phase="publishing",
                         error=f"publishing 失败：{exc}")
                log(f"[publisher] job#{job_id} publishing 失败：{exc}")
                return {"ok": False, "status": STATUS_FAILED, "phase": "publishing",
                        "error": f"publishing 失败：{exc}"}
            _set_job(conn, job_id, status=STATUS_COMPLETED, phase="published")
            log(f"[publisher] job#{job_id} 自动发布成功")
            return {"ok": True, "status": STATUS_COMPLETED, "phase": "published",
                    "publish": pub_result, "cover": cover_result}

        # 停住等人点发布（R1 默认口径）
        _set_job(conn, job_id, status=STATUS_WAITING, phase="waiting_user")
        log(f"[publisher] job#{job_id} 已填表，停住等人工点发布")
        return {"ok": True, "status": STATUS_WAITING, "phase": "waiting_user",
                "cover": cover_result}


# ---------------------------------------------------------------- 重置 / 重试

def get_job(job_id: int, *, db: Path | None = None) -> dict | None:
    """取一条队列记录（含 status / phase / attempt_count），没有返回 None。"""
    with database.session(db) as conn:
        row = conn.execute("SELECT id, platform, status, phase, title, attempt_count, "
                           "error_message FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "platform": row["platform"], "status": row["status"],
                "phase": row["phase"], "title": row["title"],
                "attemptCount": row["attempt_count"],
                "errorMessage": row["error_message"]}


def reset_job(job_id: int, *, db: Path | None = None) -> bool:
    """把任务重置回「待发布」（重试用：失败后 / 人工打断后再次点击发布）。

    **为什么要这个**：人是执行过程中的一环 —— 关掉浏览器、切走网络、直接杀进程都会让任务
    停在 ``running``，而「半自动」的前提就是**人随时可以重来**（§5.2「失败后应能再次触发」）。
    ``attempt_count`` 不清零，保留审计痕迹。
    """
    with database.session(db) as conn:
        row = conn.execute("SELECT id FROM publish_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return False
        _set_job(conn, job_id, status=STATUS_QUEUED, phase="preflight", error="")
    return True


def is_retry_notice(phase: str | None) -> bool:
    """上一轮是否**已经进到「上传」及之后**——重跑会重新传同一文件，需要提醒防重复投稿。"""
    try:
        return PHASES.index(phase or "") >= PHASES.index("uploading")
    except ValueError:
        return False


def mark_done(job_id: int, *, db: Path | None = None) -> bool:
    """人工在浏览器点完发布后，回软件把该条标记为「已发布」。"""
    with database.session(db) as conn:
        if conn.execute("SELECT id FROM publish_jobs WHERE id = ?", (job_id,)).fetchone() is None:
            return False
        _set_job(conn, job_id, status=STATUS_COMPLETED, phase="completed")
    return True
