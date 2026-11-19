#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制调度器 —— **必须跑在 slicer 服务进程里**（关桌面窗口不能停录）。

设计要点：
- 一条后台线程持有一个 asyncio 事件循环，所有 :class:`LiveRecorder` 都跑在这个循环里；
  HTTP 请求线程通过 ``asyncio.run_coroutine_threadsafe`` 提交任务，跨线程安全。
- 配置持久化到 ``live_recorders.json``（``ws.live_recorders_path``），服务重启后恢复；
  标了「自动监听」的录制器在服务启动时**自动重新进入监控**（挂机链路的前提，D8）。
- 状态快照全部来自 recorder 自身的 stats，**不用 shell 外的进程信息**（K17/K19 的教训）。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common.file_ops import delete_files_in
from ..common.paths import ws
from .aspect import MODE_AUTO, aspect_of_tag, normalize_mode
from .douyin import (DouyinClient, LiveRecorderError, parse_identifier,
                     parse_room_input)
from .merge import probe_duration
from .recorder import (DEFAULT_SEGMENT_KEY, DEFAULT_SEGMENT_SECONDS,
                       PROBE_INTERVAL_SEC, SEGMENT_CHOICES, LiveRecorder,
                       normalize_segment_key)

# 历史直播间最多留多少条（防止无限增长；超出按「最近使用」淘汰）
HISTORY_LIMIT = 50
# 录制素材文件名里的画幅后缀（录制器分段时加的，见 recorder.HourAssembler.close）
_ASPECT_SUFFIX_RE = re.compile(r"_(169|916|34|43)$")

# ---------------------------------------------------------------------------
# v3.53：「自动监听开播」= 一场录完（主播下播）后**自动回到监控等下一场**。
# 这三个常量是**防打爆**的闸门（K91/K92 同源：周期性/循环性的外部请求必须有硬上限）：
# AUTO_RESUME_DELAY_SEC     一场结束到重新进入监控之间的静默期
# AUTO_RESUME_MIN_GAP_SEC   两次「重新进入监控」之间的最小间隔（低于它就跳过这一轮）
# AUTO_RESUME_MIN_CYCLE_SEC 这一场太短（<30s）就认为是异常，不自动重启
# 三者叠起来最坏情况是「每 2 分钟 1 次探测」，比正常监控（30s 一次）**还慢**，
# 所以自动重启不可能变成新的打爆源。改这三个值前请先读 K92。
AUTO_RESUME_DELAY_SEC = 15.0
AUTO_RESUME_MIN_GAP_SEC = 120.0
AUTO_RESUME_MIN_CYCLE_SEC = 30.0


def _net_enabled() -> bool:
    """是否允许真的去外网（冒烟/离线环境要关掉，见 `SLICE_DISABLE_AUTO`）。

    v3.53：**新增的自动提交录制任务也必须过这道闸** —— 以前只有 `add()` 里的
    「解析主播昵称」看了这个开关，所以冒烟里 `PUT autoMonitor=true` 会真的去敲抖音。
    凡是「用户动作之外自发发起的外部请求」都要过这里（K91 同源）。

    v3.59（）：判定逻辑搬到 `app.common.net_gate`（唯一出处）——
    其他模块也要过同一道闸，再抄一份就是第三次复制（K44）。名字保留，
    所有既有调用点与冒烟断言都不受影响。
    """
    from ..common.net_gate import net_enabled
    return net_enabled()


def _aspect_suffix(stem: str) -> str:
    m = _ASPECT_SUFFIX_RE.search(stem or "")
    return m.group(1) if m else ""


@dataclass
class RecorderEntry:
    """一个直播间的配置 + 运行态。"""
    webRid: str
    quality: str = "FULL_HD1"
    note: str = ""
    autoMonitor: bool = False
    addedAt: float = 0.0
    # v3.25：录制画幅模式（auto / 16:9 / 9:16 / 3:4 / 4:3 / copy）
    aspectMode: str = MODE_AUTO
    # v3.35：分段时长档（"1h" / "3h"）→ 每段录满就落一个成片，录制**不停**继续下一段
    segmentKey: str = DEFAULT_SEGMENT_KEY
    # v4.47：录屏模式——整屏录制直播间（含头像/弹幕/连线），屏蔽送礼特效，**原样比例**
    screenMode: bool = False
    # 运行态（不持久化）
    state: str = "idle"           # idle | waiting | recording | finished | error
    recorder: LiveRecorder | None = None
    task: asyncio.Future | None = None
    owner: str = ""
    title: str = ""
    lastCheckAt: float = 0.0
    lastError: str = ""
    # v3.35：最近一次轮询到的**开播状态**（录制器卡片要显眼地显示「是否正在直播」）。
    #   ``liveKnown`` 区分「查过了、没开播」与「还没查过 / 查失败」—— 界面不能把两者显示成一样。
    live: bool = False
    liveStatus: int = 0           # 抖音 room.status（2 = 在播）
    liveKnown: bool = False
    startedAt: float = 0.0
    endedAt: float = 0.0
    # v3.53：**这一场是不是用户手动停的**（不持久化 —— 重启服务按 `autoMonitor`
    # 的落盘值恢复监控，挂机前提不能被一次手动停止永久取消）。
    # 手动停止后不再自动回到监控，必须用户再点一次「开始录制」（用户原话：
    # 「除非是手动停止录制才会是要再点开始录制才会再录制」）。
    manualStopped: bool = False
    # v3.53：上次「自动回到监控」的时间戳（给最小间隔闸门用，见 AUTO_RESUME_MIN_GAP_SEC）
    lastResumeAt: float = 0.0
    recentLog: list[str] = field(default_factory=list)

    def cur_state(self) -> str:
        """**此刻真正在干什么**（v4.15，）。

        `state` 是"提交任务时手写的字符串"：等开播与正在录**都落在 waiting**
        （`recorder.py` 的 `pulling` 注释里早就写明了这一点、`recording` 这个值
        从未被赋值过）⇒ 卡片徽标在录制全程都写「监控中」、按钮全程写「停止监控」，
        用户看着正在录的卡片却找不到「停止录制」，取消勾选「自动监听开播」还会
        把正在录的那场一起杀掉（`_auto_monitor_off` 只看 state）。K69：不许对状态撒谎。

        真值一律从**事实**推，不再依赖手写的字符串：
          · 要求停止 + 还在收尾 → ``stopping``
          · ffmpeg 在拉流       → ``recording``
          · 任务在跑（等开播）  → ``waiting``（任务跑完前的终态值照用）
          · 任务没了            → 落盘值（finished / idle / error）
        """
        rec = self.recorder
        if rec is not None:
            if getattr(rec, "stopping", False):
                # 已经进入收尾：落盘值是终态就照用，否则就是"正在停"
                if self.state in ("finished", "idle", "error"):
                    return self.state
                return "stopping"
            if rec.pulling:
                return "recording"
        if self.task is not None and not self.task.done():
            return self.state if self.state in ("finished", "idle", "error") else "waiting"
        return self.state

    def snapshot(self) -> dict:
        # v3.63（）：**监控到底还在不在跑**，由后端算好告诉界面。
        # 为什么必须新增：`state` 是"提交时写的字符串"，而界面那条显眼的开播状态条
        # 只要不是"正在录/已开播"就写「监控中」—— 于是任务早就结束（或压根没起来）时，
        # 用户看到的仍是「监控中：尚未查到开播状态」，以为在盯着，其实没有任何探测在跑
        # （用户报的「开播了却没录」就有这一层：他没有可判断的依据）。K69：不许对状态撒谎。
        alive = bool(self.task is not None and not self.task.done())
        base = {
            "webRid": self.webRid,
            "quality": self.quality,
            "note": self.note,
            "autoMonitor": self.autoMonitor,
            # v3.53：这一场是用户手动停的 → 界面要说「需再点开始录制」，
            # 而不是让用户以为是监控坏了（K69：不能对状态撒谎）。
            "manualStopped": self.manualStopped,
            "addedAt": self.addedAt,
            "aspectMode": self.aspectMode,
            # v4.15：不再是手写的字符串 —— 等开播 / 正在录 / 收尾中 分得开（见 cur_state）
            "state": self.cur_state(),
            "monitorAlive": alive,
            "owner": self.owner,
            "title": self.title,
            "lastCheckAt": self.lastCheckAt,
            "lastError": self.lastError,
            "channelUrl": f"https://live.douyin.com/{self.webRid}",
            # v3.35：分段时长 + 开播状态（录制器卡片用；`liveKnown=False` 表示还没查到，
            # 界面要显示「尚未检查」而不是「未开播」—— 两者含义完全不同）
            "segmentKey": self.segmentKey,
            "screenMode": self.screenMode,          # v4.47：录屏模式（勾选=整屏录）
            "segmentSeconds": int(SEGMENT_CHOICES.get(self.segmentKey, 3600.0)),
            "live": self.live,
            "liveStatus": self.liveStatus,
            "liveKnown": self.liveKnown,
            "pulling": bool(self.recorder is not None and self.recorder.pulling),
            # v3.42：开播状态轮询间隔（界面「每 30 秒自动检测」的数字从这里来，别写死）
            # v3.50：退避生效时下发**当前真实间隔** —— 否则界面还写着"每 30 秒"，
            # 而实际已经退到 5 分钟，用户会以为监控坏了。
            "pollIntervalSec": int(getattr(self.recorder, "current_interval", None)
                                   or (self.recorder.poll_interval
                                       if self.recorder is not None
                                       else PROBE_INTERVAL_SEC)),
        }
        rec = self.recorder
        if rec is not None:
            s = rec.stats
            base["recording"] = {
                # v3.33：**此刻是否真在拉流**（ffmpeg 活着）。编排器靠它判断
                # 「重启服务会不会打断正在录的这场」（state 里 waiting 同时代表
                # 「等开播」和「正在录」，分不开 —— 见 LiveRecorder.pulling 的说明）。
                "pulling": bool(rec.pulling),
                # v3.25：画幅（当前段 / 已封存各段 / 切换次数）
                "aspect": rec.aspect,
                "aspectMode": rec.aspect_mode,
                "aspects": list(s.aspects),
                "aspectSwitches": s.aspectSwitches,
                "startedAt": self.startedAt,
                # v4.50（用户 2026-11-06）：「已录」= **实际录制时长**（ffmpeg 在拉流的时间），
                # 监控/等开播一律不计。以前是「点开始录制到现在」，没开播也一直涨 —— 明显不对。
                "elapsedSec": round(getattr(rec, "recorded_seconds", lambda: 0.0)(), 1),
                "recordedSec": round(getattr(rec, "recorded_seconds", lambda: 0.0)(), 1),
                "attempts": s.attempts,
                "interruptions": s.interruptions,
                "segments": s.segments,
                "hourFiles": len(s.outputFiles),
                "merges": s.merges[-3:],
            }
        # v4.74 P-B（方案优化项 B）：录制卡片显示**剩余磁盘空间**（低水位自动停录的
        # 判据用户要看得见，K69）。实现调 recorder.disk_state_now（唯一出处）。
        try:
            from .recorder import disk_state_now
            ds = disk_state_now()
            base["diskFreeGb"] = ds["freeGb"]
            base["diskFreePct"] = ds["freePct"]
        except Exception:                                      # noqa: BLE001
            pass
        base["recentLog"] = self.recentLog[-12:]
        return base


def _apply_probe(entry, info: dict | None) -> None:
    """把一次「是否开播」的探测结果记到录制器上（v3.35 的 on_probe 逻辑，v3.45 抽出）。

    **只写一份**（K44）：录制器自己的 ``on_probe`` 回调与界面「刷新状态」
    （:meth:`RecorderManager.probe_now`）都调它 —— 否则两处各写一遍字段映射，
    迟早出现「刷新按钮探到了、但界面显示没变」这种半坏状态。
    """
    entry.lastCheckAt = time.time()
    if not info:
        return                            # 没探到：保留上次结果，但不更新 liveKnown
    entry.liveKnown = True
    entry.live = bool(info.get("live"))
    entry.liveStatus = int(info.get("status") or 0)
    entry.owner = str(info.get("owner") or entry.owner)
    entry.title = str(info.get("title") or entry.title)


class RecorderManager:
    """服务内的录制调度器（单例，随 slicer 服务启动而启动）。"""

    def __init__(self, log=print) -> None:
        self.log = log or (lambda *a, **k: None)
        self._entries: dict[str, RecorderEntry] = {}
        # 必须 RLock：add/remove 在持锁状态下调 _save()，_save 内还要再加锁，
        # 普通 Lock 不可重入会死锁（K26：曾导致 POST /api/recorders 挂死、锁被永久持有）
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        # v3.25 历史直播间：{webRid: {...}}，移除录制器时**不丢**，可一键复用
        self._history: dict[str, dict] = {}
        # v3.35：分段落盘后的消费者（web_server 注入 → 通知自动化流水线）。
        # 用注入而不是直接 import AUTO：录制调度器不认识流水线（D29 解耦）。
        self._hour_file_sink = None
        self._load_history()

    def set_hour_file_sink(self, sink) -> None:
        """设置「分段落盘」的消费者（可调 ``None`` 断开）。

        注入的是**非阻塞**的登记函数（见 ``AutoPipeline.nudge``）—— 它会在录制器的
        事件循环里被调用，任何耗时操作都会卡住录制。
        """
        self._hour_file_sink = sink

    # ------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台事件循环线程，并恢复上次的监控配置。"""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop_thread, name="recorder-manager", daemon=True)
        self._thread.start()
        self._load_and_resume()
        self.log(f"[recorder-manager] 已启动，恢复 {len(self._entries)} 个录制器配置")

    def stop(self) -> None:
        """停止全部录制任务与事件循环（服务退出时调用）。"""
        self._running = False
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
            try:
                self._stop_recording_sync(e)
            except Exception as exc:
                self.log(f"[recorder-manager] 停止 {e.webRid} 失败：{exc}")
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop_thread(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ------------------------------------------------ 配置持久化

    def _load_and_resume(self) -> None:
        path = ws.live_recorders_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        if isinstance(data, dict):
            data = data.get("recorders", [])
        for item in data if isinstance(data, list) else []:
            try:
                rid = str(item["webRid"])
                entry = RecorderEntry(
                    webRid=rid,
                    quality=str(item.get("quality") or "FULL_HD1"),
                    note=str(item.get("note") or ""),
                    autoMonitor=bool(item.get("autoMonitor")),
                    addedAt=float(item.get("addedAt") or time.time()),
                    aspectMode=normalize_mode(item.get("aspectMode")),
            screenMode=bool(item.get("screenMode")),
                    segmentKey=normalize_segment_key(item.get("segmentKey")),
                )
                # v3.42：昵称/标题随配置一起持久化了，重启后卡片仍显示「抖音名字」；
                # 旧文件没有这两项 → 从历史直播间兜底回填（历史里一直存着）。
                entry.owner = str(item.get("owner") or "")
                entry.title = str(item.get("title") or "")
                if not entry.owner:
                    hist = self._history.get(rid) or {}
                    entry.owner = str(hist.get("owner") or "")
                    entry.title = entry.title or str(hist.get("title") or "")
            except (KeyError, TypeError, ValueError):
                continue
            self._entries[entry.webRid] = entry
        # 自动监听的录制器：服务一启动就重新进入监控（D8 挂机前提）
        for e in self._entries.values():
            if e.autoMonitor:
                self._submit_recording(e)

    def _save(self) -> None:
        with self._lock:
            data = [
                {
                    "webRid": e.webRid,
                    "quality": e.quality,
                    "note": e.note,
                    # v3.42：昵称/标题也持久化 —— 重启后卡片显示「抖音名字」不回退成抖音号
                    "owner": e.owner,
                    "title": e.title,
                    "autoMonitor": e.autoMonitor,
                    "addedAt": e.addedAt,
                    "aspectMode": e.aspectMode,
                    "screenMode": e.screenMode,
                    "segmentKey": e.segmentKey,
                }
                for e in self._entries.values()
            ]
        ws.live_recorders_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = ws.live_recorders_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ws.live_recorders_path)    # 原子写，避免中途断电留下半截

    # ------------------------------------------------ v3.25 历史直播间

    def _load_history(self) -> None:
        try:
            data = json.loads(ws.live_rooms_history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = data if isinstance(data, list) else (data.get("history") or [])
        for item in items if isinstance(items, list) else []:
            try:
                rid = str(item["webRid"])
            except (KeyError, TypeError):
                continue
            self._history[rid] = {
                "webRid": rid,
                "owner": str(item.get("owner") or ""),
                "title": str(item.get("title") or ""),
                "quality": str(item.get("quality") or "FULL_HD1"),
                "note": str(item.get("note") or ""),
                "aspectMode": normalize_mode(item.get("aspectMode")),
                # v3.35：分段时长档（一键复用要能还原「上次每段存多久」）
                "segmentKey": normalize_segment_key(item.get("segmentKey")),
                "recordCount": int(item.get("recordCount") or 0),
                "lastUsedAt": float(item.get("lastUsedAt") or item.get("addedAt") or 0),
                "removedAt": float(item.get("removedAt") or 0),
                # v3.26：上一场直播的内容类型（切片自动回填 / 可手动改）
                "lastContentType": str(item.get("lastContentType") or ""),
            }

    def _save_history(self) -> None:
        with self._lock:
            items = sorted(self._history.values(),
                           key=lambda d: d.get("lastUsedAt") or 0, reverse=True)
            items = items[:HISTORY_LIMIT]
            self._history = {i["webRid"]: i for i in items}
        path = ws.live_rooms_history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _touch_history(self, rid: str, touch_used: bool = True, **patch) -> None:
        """记录/更新一条历史（add 时建、开录/结束时补 owner/title/次数）。

        ``touch_used=False`` 用于**后台自动回填**（切片识别出内容类型）：那种写入不代表
        "用户用了一次"，不该把这条历史顶到列表最前面（K59）。
        """
        item = self._history.get(rid) or {
            "webRid": rid, "owner": "", "title": "", "quality": "FULL_HD1",
            "note": "", "aspectMode": MODE_AUTO, "recordCount": 0,
            "lastUsedAt": 0.0, "removedAt": 0.0, "lastContentType": "",
        }
        for key, value in patch.items():
            if value not in (None, ""):
                item[key] = value
        if touch_used:
            item["lastUsedAt"] = time.time()
        self._history[rid] = item
        self._save_history()

    def history(self) -> tuple[int, dict]:
        """历史直播间列表（按最近使用倒序），并标出哪些**仍在当前列表**里。

        v3.26：顺带把「直播类型」词表带出去 —— 前端的「上一场直播类型」下拉
        不能自己写死词表（K44：词表唯一出处是 ``prompts.CONTENT_TYPES``）。
        """
        # ALL_TYPES 而不是 TYPE_ORDER：词表里不能出现 "auto"（过去那场没有「待识别」态）
        from .prompts import ALL_TYPES, CONTENT_TYPES
        with self._lock:
            live_rids = set(self._entries.keys())
            items = sorted(self._history.values(),
                           key=lambda d: d.get("lastUsedAt") or 0, reverse=True)
        out = []
        for i in items:
            out.append({**i, "inList": i["webRid"] in live_rids,
                        "channelUrl": f"https://live.douyin.com/{i['webRid']}"})
        types = [{"value": k, "label": (CONTENT_TYPES.get(k) or {}).get("label", k)}
                 for k in ALL_TYPES]
        return 200, {"history": out, "count": len(out), "contentTypes": types}

    def set_history_type(self, rid: str, content_type: str) -> tuple[int, dict]:
        """手动指定一条历史的「上一场直播类型」（自动识别错了就靠它纠正）。"""
        item = self._history.get(str(rid))
        if item is None:
            return 404, {"error": f"历史里没有 {rid} 这个直播间"}
        from .prompts import ALL_TYPES
        value = str(content_type or "").strip()
        # 注意用 ALL_TYPES 而不是 TYPE_ORDER：后者含 "auto"，
        # 「上一场」已经是过去式，不可能是「待自动识别」。
        if value and value not in ALL_TYPES:
            return 400, {"error": f"直播类型只能是 {' / '.join(ALL_TYPES)}"}
        item["lastContentType"] = value      # 空串 = 「还没定过」，不算错误
        self._save_history()
        return 200, {"ok": True, "item": item, "contentType": value}

    def note_content_type_for_source(self, video_name: str, content_type: str) -> str:
        """切片完成后把识别出的内容类型回填到**录这个文件的主播**身上。

        录制素材命名是 ``<主播名>_<时间戳>[_画幅].mp4``（见 ``HourAssembler``），
        所以按主播名前缀匹配即可；一场直播切片只回填一次，认不准就**不写**
        （宁可空着，也不要把 A 主播的类型安到 B 主播头上）。
        返回被回填的 webRid（没匹配上返回空串）。
        """
        value = str(content_type or "").strip()
        if not value:
            return ""
        stem = Path(str(video_name or "")).stem
        if not stem:
            return ""
        best_rid, best_len = "", 0
        with self._lock:
            items = list(self._history.values())
        for item in items:
            owner = str(item.get("owner") or "").strip()
            if not owner:
                continue
            if stem == owner or stem.startswith(owner + "_"):
                if len(owner) > best_len:      # 主播名互相包含时取更长的那个
                    best_rid, best_len = item["webRid"], len(owner)
        if not best_rid:
            return ""
        self._touch_history(best_rid, touch_used=False, lastContentType=value)
        return best_rid

    # ---------------------------------------- v3.132 本场备注（开播前就能写）

    def set_live_note(self, rid: str, note: dict) -> tuple[int, dict]:
        """把「本场备注」记在**历史直播间**上（这场直播是什么 / 按什么单元切）。

        为什么挂在主播身上而不是文件上：录制过程中文件还在写，用户是在**开播前**就想
        标注「今天打王者」。录完的文件自己没有 sidecar 时，切片那一步会按主播名兜底取这里。
        """
        with self._lock:
            item = self._history.get(str(rid))
            if item is None:
                return 404, {"ok": False, "error": f"没有这个直播间：{rid}"}
        from app.slicer import notes as notes_mod
        clean = notes_mod._clean(note or {})
        # v3.141：盖时间戳 —— 「历史备注」下拉要按时间排序/显示（之前只有素材 sidecar
        # 有 updatedAt，直播间那份没有，列表里全是空白）
        clean["updatedAt"] = time.strftime("%Y-%m-%d %H:%M")
        self._touch_history(str(rid), touch_used=False, contentNote=clean)
        return 200, {"ok": True, "rid": str(rid), "note": clean}

    def note_for_rid(self, rid: str) -> dict:
        """按 rid 读本场备注（录制页「备注」按钮打开面板时用）。"""
        from app.slicer import notes as notes_mod
        with self._lock:
            item = self._history.get(str(rid)) or {}
        return notes_mod._clean(item.get("contentNote"))

    def content_notes(self) -> list[dict]:
        """v3.141：所有写过的「本场备注」（历史备注下拉的一键填入源之一）。

        素材自己的 sidecar 由 `web_server./api/note/history` 扫源根得到；这里补的是
        **直播间维度**的那份（开播前写的、还没录出文件时也有）。全空白的跳过，
        否则下拉里会塞满一堆「没填过」的默认值。
        """
        from app.slicer import notes as notes_mod
        out: list[dict] = []
        with self._lock:
            items = list(self._history.values())
        for item in items:
            raw = item.get("contentNote")
            if not isinstance(raw, dict):
                continue
            note = notes_mod._clean(raw)
            # ⚠️ _clean 只保留 NOTE_FIELDS ⇒ updatedAt 要在 clean **之前**读
            ts = str(raw.get("updatedAt") or "")
            if not any(str(note.get(k) or "").strip() for k in ("contentType", "title", "note")):
                continue
            if not ts:
                t = float(item.get("lastUsedAt") or 0)
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""
            out.append({"owner": str(item.get("owner") or item.get("rid") or ""),
                        "note": note, "updatedAt": ts})
        return out

    def note_for_source(self, video_name: str) -> dict:
        """按素材名反查「录它的主播」的本场备注（没标过返回默认值）。

        匹配规则与 :meth:`note_content_type_for_source` 完全一致（主播名前缀），
        认不准就返回默认值 —— **宁可没有备注，也不要把 A 主播的备注安到 B 头上**。
        """
        from app.slicer import notes as notes_mod
        stem = Path(str(video_name or "")).stem
        if not stem:
            return dict(notes_mod.DEFAULT_NOTE)
        best, best_len = None, 0
        with self._lock:
            items = list(self._history.values())
        for item in items:
            owner = str(item.get("owner") or "").strip()
            if not owner:
                continue
            if stem == owner or stem.startswith(owner + "_"):
                if len(owner) > best_len and isinstance(item.get("contentNote"), dict):
                    best, best_len = item, len(owner)
        return notes_mod._clean(best.get("contentNote")) if best else dict(notes_mod.DEFAULT_NOTE)

    def forget_history(self, rid: str) -> tuple[int, dict]:
        """从历史里彻底删掉（不影响当前列表）。"""
        existed = self._history.pop(str(rid), None) is not None
        if existed:
            self._save_history()
        return 200, {"ok": True, "removed": existed}

    # ------------------------------------------------ 增删改查

    def add(self, raw: str, quality: str = "FULL_HD1", note: str = "",
            auto_monitor: bool = False,
            aspect_mode: str = MODE_AUTO,
            segment_key: str = DEFAULT_SEGMENT_KEY,
            screen_mode: bool = False) -> tuple[int, dict]:
        """添加直播间。``raw`` 可以是编号 / 链接 / **抖音号**（v3.32）。

        正常流程是前端先「解析并添加」把抖音号换成可监控的标识再调这里；
        ``parse_identifier`` 认得抖音号形态，所以直接调也不会被误判成「不是编号」（K58）。
        """
        rid = parse_identifier(raw)
        if not rid:
            return 400, {"error":
                         f"无法从输入里解析出直播间标识：{raw!r}。"
                         "请填抖音号 / 直播间链接 / 分享短链，或先点「解析并添加」。"}
        with self._lock:
            if rid in self._entries:
                return 409, {"error": f"{rid} 已在列表中",
                             "recorder": self._entries[rid].snapshot()}
            entry = RecorderEntry(webRid=rid, quality=quality, note=note,
                                  autoMonitor=auto_monitor, addedAt=time.time(),
                                  aspectMode=normalize_mode(aspect_mode),
                                  segmentKey=normalize_segment_key(segment_key),
                                  screenMode=bool(screen_mode))
            # v3.42：**添加时就尽力解析主播昵称**（用户明确要求卡片显示「抖音名字」
            # 而不是抖音号）。以前 owner 只有等开播轮询（on_probe）才填 → 没开播的
            # 卡片永远显示一串数字。这里解析失败**不影响添加**（挂机监控不依赖它），
            # 历史直播间里存过的昵称也会在下面兜底回填。
            history_item = self._history.get(rid) or {}
            entry.owner = str(history_item.get("owner") or "")
            entry.title = str(history_item.get("title") or "")
            self._entries[rid] = entry
            self._save()
        # v3.42：再向抖音问一次「这个号叫什么」（只填名字/标题，失败静默）。
        # ⚠️ 冒烟环境必须跳过：SLICE_DISABLE_AUTO 是「不外呼真实 API」的同款总开关。
        if _net_enabled():
            code, info = self.resolve_room_sync(rid)
            if code == 200 and isinstance(info, dict):
                with self._lock:
                    entry.owner = str(info.get("owner") or entry.owner)
                    entry.title = str(info.get("title") or entry.title)
                    self._save()
        # v3.25：加过就进历史（移除后还能一键复用）
        self._touch_history(rid, owner=entry.owner, title=entry.title,
                            quality=quality, note=note,
                            aspectMode=normalize_mode(aspect_mode),
                            segmentKey=normalize_segment_key(segment_key))
        self.log(f"[recorder-manager] 添加 {rid}"
                 f"（{entry.owner or note or '未备注'}）")
        # v3.53：勾了「开播自动录制」就**当场进入监控**，不用用户再点一次「开始录制」。
        # 以前这个开关只在服务启动时（`_load_and_resume`）生效 —— 添加后要手动点开始，
        # 或者等重启服务，用户报的就是这个（「勾上了还要我点一次」）。
        if auto_monitor:
            self._auto_monitor_on(entry, "添加时勾选了自动监听")
        return 200, {"ok": True, "recorder": entry.snapshot()}

    def remove(self, web_rid: str) -> tuple[int, dict]:
        with self._lock:
            entry = self._entries.pop(web_rid, None)
            self._save()
        if entry is None:
            return 404, {"error": f"没有 {web_rid} 这个录制器"}
        self._stop_recording_sync(entry)
        # 移除**不是**遗忘：历史要留着，否则「一键复用」无从谈起
        self._touch_history(web_rid, owner=entry.owner, title=entry.title,
                            quality=entry.quality, note=entry.note,
                            aspectMode=entry.aspectMode,
                            segmentKey=entry.segmentKey, removedAt=time.time())
        self.log(f"[recorder-manager] 移除 {web_rid}（已存入历史直播间）")
        return 200, {"ok": True}

    def list(self) -> tuple[int, dict]:
        with self._lock:
            entries = [e.snapshot() for e in self._entries.values()]
        return 200, {"recorders": entries, "count": len(entries),
                     "now": round(time.time(), 1)}

    def status(self, web_rid: str) -> tuple[int, dict]:
        entry = self._entries.get(web_rid)
        if entry is None:
            return 404, {"error": f"没有 {web_rid} 这个录制器"}
        return 200, entry.snapshot()

    def set_options(self, web_rid: str, quality: str | None = None,
                    note: str | None = None,
                    auto_monitor: bool | None = None,
                    aspect_mode: str | None = None,
                    segment_key: str | None = None,
                    screen_mode: bool | None = None) -> tuple[int, dict]:
        entry = self._entries.get(web_rid)
        if entry is None:
            return 404, {"error": f"没有 {web_rid} 这个录制器"}
        if quality:
            entry.quality = quality
        if note is not None:
            entry.note = note
        if auto_monitor is not None:
            entry.autoMonitor = bool(auto_monitor)
        if aspect_mode is not None:
            entry.aspectMode = normalize_mode(aspect_mode)
        if segment_key is not None:
            entry.segmentKey = normalize_segment_key(segment_key)
        if screen_mode is not None:
            entry.screenMode = bool(screen_mode)
            # v5.3（bugfix，用户：「测试一下勾选录屏方式的，好像有问题」）：
            # 录制器实例是在 `_submit_recording` 提交时构造的（那时传的旧值）——
            # **监控中的任务**开播后按的还是旧 screen_mode，用户以为勾上就录屏，
            # 实际录成了普通模式。`screen_mode` 在 `run()` 的开播确认**之后**才被读取
            # （recorder.run → _record_loop_screen），所以这里同步改实例属性即可当场生效。
            if entry.recorder is not None:
                entry.recorder.screen_mode = entry.screenMode
        self._save()
        self._touch_history(web_rid, quality=entry.quality, note=entry.note,
                            aspectMode=entry.aspectMode,
                            segmentKey=entry.segmentKey)
        # v3.53：「自动监听开播」从**只落盘的标志**变成真的会动的开关：
        #   勾上 → 立刻进入监控（检测到开播就自动录，不用再点「开始录制」）；
        #   取消 → 只在「监控中（还没开播）」时收手；**正在录制绝不打断**（K55：
        #          销毁型操作只在用户明确要求时做，取消勾选不等于「停止录制」）。
        if auto_monitor is not None:
            if entry.autoMonitor:
                self._auto_monitor_on(entry, "勾选了自动监听开播")
            else:
                self._auto_monitor_off(entry, "取消了自动监听开播")
        return 200, {"ok": True, "recorder": entry.snapshot()}

    # ------------------------------------------------ 自动监听启停（v3.53）

    def _auto_monitor_on(self, entry: RecorderEntry, why: str) -> None:
        """打开自动监听：**立刻**进入监控（幂等，已在跑就不重复提交 —— K10）。"""
        # ⚠️ 两道闸：事件循环要在跑（否则会去改 state 成 error）、且允许外网
        # （冒烟里 PUT autoMonitor=true 不应该真的去敲抖音，见 _net_enabled）。
        if self._loop is None or not _net_enabled():
            self.log(f"[recorder-manager] {entry.webRid} 暂不进入监控"
                     f"（调度器未就绪或离线环境）：{why}")
            return
        entry.manualStopped = False        # 勾选 = 明确要求继续监听
        if entry.task is not None and not entry.task.done():
            return                         # 已在监控/录制，不重复启动（K10）
        self._elog(entry, f"{why} → 进入监控，检测到开播会自动录制")
        self._submit_recording(entry)

    def _auto_monitor_off(self, entry: RecorderEntry, why: str) -> None:
        """关闭自动监听：只收掉「还在等开播」的监控，正在录的那场不碰。

        v4.15：判据改用 :meth:`RecorderEntry.cur_state` —— 原来读手写的 `state`，
        而它录制全程都是 ``waiting``（见那里的说明）⇒ 取消勾选会把**正在录的那场**
        当成"还在等开播"给停掉，录到一半的素材跟着没了（v3.53 那句「正在录绝不打断」
        当时只是一句注释，判据根本分不开）。
        """
        if entry.cur_state() != "waiting":
            return
        entry.manualStopped = True
        self._elog(entry, f"{why} → 停止监控（正在录制的那场不受影响）")
        self._stop_recording_sync(entry)

    def _elog(self, entry: RecorderEntry, msg: str) -> None:
        """往卡片日志里加一行（时间戳格式与 `_submit_recording.rec_log` 一致）。"""
        entry.recentLog.append(f"{time.strftime('%H:%M:%S')} {msg}")
        if len(entry.recentLog) > 60:
            del entry.recentLog[:20]

    def _schedule_auto_resume(self, entry: RecorderEntry) -> None:
        """一场结束后，若仍勾着「自动监听」就**延时**重新进入监控等下一场。

        为什么必须延时：这个回调跑在 `_run()` 的 finally 里，此刻 `entry.task`
        还没标记完成，`_submit_recording` 会因为「任务还在跑」直接返回（K10），
        于是永远不会再起来 —— 所以要绕到下一轮循环再提交。

        ⚠️ 三道闸门（K92：频率必须单独设限，不然「自动」就变成新的打爆源）：
          ① 这一场短于 30 秒 → 视为异常，不自动重启；
          ② 距上次自动重启不足 120 秒 → 跳过这一轮；
          ③ 真正提交前再延时 15 秒。
        最坏情况 = 每 2 分钟 1 次探测，比平时监控（30 秒一次）还慢。
        """
        if self._loop is None or not _net_enabled():
            return
        now = time.time()
        ran = now - (entry.startedAt or now)
        if ran < AUTO_RESUME_MIN_CYCLE_SEC:
            self.log(f"[recorder-manager] {entry.webRid} 这一场只跑了 "
                     f"{ran:.0f}s，判定为异常，不自动重启监控")
            return
        gap = now - (entry.lastResumeAt or 0.0)
        if gap < AUTO_RESUME_MIN_GAP_SEC:
            self.log(f"[recorder-manager] {entry.webRid} 距上次自动重启仅 "
                     f"{gap:.0f}s（<{AUTO_RESUME_MIN_GAP_SEC:.0f}s），跳过本轮")
            return
        entry.lastResumeAt = now
        self._elog(entry, "本场结束，已自动回到监控，等下一场开播")

        def _go() -> None:
            if not entry.autoMonitor or entry.manualStopped:
                return                     # 期间用户改主意了就作废
            task = entry.task
            if task is not None and not task.done():
                return
            self._submit_recording(entry)

        try:
            self._loop.call_later(AUTO_RESUME_DELAY_SEC, _go)
        except RuntimeError as exc:        # 事件循环已关闭（服务在退出）
            self.log(f"[recorder-manager] 自动重启监控已取消：{exc}")

    # ------------------------------------------------ 启停

    def start_recording(self, web_rid: str) -> tuple[int, dict]:
        entry = self._entries.get(web_rid)
        if entry is None:
            return 404, {"error": f"没有 {web_rid} 这个录制器，请先添加"}
        if entry.task is not None and not entry.task.done():
            return 200, {"ok": True, "recorder": entry.snapshot(),
                         "message": "已在运行，复用当前任务（K10：不重复启动）"}
        # v3.53：手动点「开始录制」= 明确要求继续 → 解除「手动停止」的封印
        entry.manualStopped = False
        self._submit_recording(entry)
        return 200, {"ok": True, "recorder": entry.snapshot()}

    def stop_recording(self, web_rid: str) -> tuple[int, dict]:
        entry = self._entries.get(web_rid)
        if entry is None:
            return 404, {"error": f"没有 {web_rid} 这个录制器"}
        # v3.53：**用户手动停止**要记下来 —— 之后不再自动回到监控，
        # 必须他再点一次「开始录制」（用户原话）。自动重启前会检查这个标志。
        entry.manualStopped = True
        self._elog(entry, "用户手动停止，已退出监控（再点「开始录制」才会继续）")
        self._stop_recording_sync(entry)
        return 200, {"ok": True, "recorder": entry.snapshot()}

    def probe_now(self, web_rid: str = "") -> tuple[int, dict]:
        """**立刻**重新探一次是否开播（界面录制器区块的「刷新状态」，v3.45）。

        平时是「监控中」的房间每 ``PROBE_INTERVAL_SEC``（30 秒）自动探一次；
        用户点刷新却要干等半分钟才看到变化，体感上等于按钮没反应。

        按房间当前状态分两条路走，都不做无用功：
          · **正在监控中**（task 在跑）→ 只给录制器**置位**，让它的轮询循环提前唤醒去探；
            这里立刻返回（不占 HTTP 线程，也不与它抢同一个 DouyinClient）；
          · **其它情况**（没启动 / 已经录着）→ 在请求线程里直接探一次
            （``asyncio.run``，几秒内返回），结果照同一份 ``_apply_probe`` 记进去。

        返回 ``{"ok", "requested", "probed", "failed"}`` —— 让界面能如实说刷了几个。
        v3.50：这是用户显式动作 → 顺带解除风控冷却（见 :meth:`_clear_cooldown`）。
        """
        self._clear_cooldown("刷新状态")
        with self._lock:
            targets = [e for e in self._entries.values()
                       if not web_rid or e.webRid == web_rid]
        if not targets:
            return 404, {"error": "没有可探测的直播间（先在「添加直播间」里加一个）"}

        requested: list[str] = []
        probed: list[str] = []
        failed: list[dict] = []
        for entry in targets:
            task = entry.task
            if task is not None and not task.done():
                rec = entry.recorder
                if rec is not None:
                    rec.request_probe()          # 让它的轮询循环立刻重探
                requested.append(entry.webRid)
                continue
            try:
                info = asyncio.run(DouyinClient().room_info(entry.webRid))
                with self._lock:
                    _apply_probe(entry, info)
                probed.append(entry.webRid)
            except Exception as exc:             # noqa: BLE001  单个房间失败不影响其余
                failed.append({"rid": entry.webRid,
                               "error": f"{type(exc).__name__}: {exc}"})
        msg = f"已请求 {len(requested)} 个监控中的房间立刻重探"
        if probed:
            msg += f"；另外直接探了 {len(probed)} 个"
        if failed:
            msg += f"；{len(failed)} 个探测失败"
        return 200, {"ok": True, "requested": requested, "probed": probed,
                     "failed": failed, "message": msg}

    def _submit_recording(self, entry: RecorderEntry) -> None:
        """把一场录制提交到后台事件循环。"""
        if self._loop is None:
            self.log("[recorder-manager] 事件循环未启动，无法提交")
            entry.state = "error"
            entry.lastError = "调度器未启动"
            return
        if entry.task is not None and not entry.task.done():
            return   # K10：已在跑，不重复启动

        def rec_log(msg: str) -> None:
            entry.recentLog.append(f"{time.strftime('%H:%M:%S')} {msg}")
            if len(entry.recentLog) > 60:
                del entry.recentLog[:20]

        def on_hour_file(path: Path) -> None:
            # D24「边录边切」的挂点（v3.35 真正接通）：一小段录满 → 落一个成片 →
            # **立刻交给自动化流水线**去转写→切片→渲染；录制**不停**，继续下一段。
            # ⚠️ 这里跑在录制器的事件循环里，只能做「登记」这种瞬间动作
            # （sink 内部是入队，不阻塞；见 AutoPipeline.nudge 的说明）。
            entry.recentLog.append(
                f"{time.strftime('%H:%M:%S')} 分段落盘：{path.name} → 交自动化流水线")
            if self._hour_file_sink:
                try:
                    self._hour_file_sink(path)
                except Exception as exc:      # 流转失败绝不能影响录制
                    self.log(f"[recorder-manager] 小时文件流转失败：{exc}")

        def on_probe(info: dict) -> None:
            # v3.35：把每次轮询到的开播状态记到 entry 上 —— 录制器卡片靠它显眼显示
            # 「是否正在直播」。字段映射抽到模块级 _apply_probe（界面「刷新状态」共用，K44）。
            _apply_probe(entry, info)

        rec = LiveRecorder(
            entry.webRid,
            quality=entry.quality,
            log=rec_log,
            on_hour_file=on_hour_file,
            on_probe=on_probe,
            segment_seconds=SEGMENT_CHOICES.get(
                entry.segmentKey, DEFAULT_SEGMENT_SECONDS),   # v3.35：分段时长
            aspect_mode=entry.aspectMode,     # v3.25：录制画幅模式
            screen_mode=entry.screenMode,     # v4.47：整屏录屏模式
        )
        entry.recorder = rec
        entry.state = "waiting"
        entry.startedAt = time.time()
        entry.endedAt = 0.0
        entry.lastError = ""

        async def _run() -> None:
            try:
                stats = await rec.run()
                entry.owner = stats.owner or entry.owner
                entry.title = stats.title or entry.title
                entry.state = "finished"
                # 录过一场就把主播/标题补进历史（添加时还没开播，这两项是空的）
                self._touch_history(entry.webRid, owner=entry.owner,
                                    title=entry.title,
                                    recordCount=(self._history.get(entry.webRid, {})
                                                 .get("recordCount", 0) or 0) + 1)
            except asyncio.CancelledError:
                entry.state = "idle"
            except Exception as exc:        # 兜底：任何异常都不许带崩调度器
                entry.state = "error"
                entry.lastError = f"{type(exc).__name__}: {exc}"
                self.log(f"[recorder-manager] {entry.webRid} 异常：{entry.lastError}")
            finally:
                entry.endedAt = time.time()
                # v3.53：一场结束（主播下播）→ 若仍勾着「自动监听开播」，
                # 自动回到监控等下一场；**用户手动停的那场不会走到这里**
                # （`stop_recording` 已置 manualStopped）。闸门见
                # :meth:`_schedule_auto_resume`（最小间隔 + 最短时长 + 延时）。
                # v3.63：**出错（error）也算**（原来只认 finished）——拉流/解析途中一次
                # 真故障会让监控**永久消失**，而界面那条状态条当时还写着「监控中」，
                # 用户完全没有可判断的依据（用户报的「开播了却没录」）。闸门不变。
                if entry.autoMonitor and not entry.manualStopped \
                        and entry.state in ("finished", "error"):
                    self._schedule_auto_resume(entry)

        entry.task = asyncio.run_coroutine_threadsafe(_run(), self._loop)
        rec_log(f"提交录制任务（档位 {entry.quality}）")

    def _stop_recording_sync(self, entry: RecorderEntry) -> None:
        rec = entry.recorder
        if rec is None:
            return
        # LiveRecorder.stop 是同步的（设标志 + 给 ffmpeg stdin 写 'q'），
        # 但 stdin 写必须在事件循环线程里做才绝对安全。
        if self._loop is not None and entry.task is not None and not entry.task.done():
            self._loop.call_soon_threadsafe(rec.stop, "手动停止")
        else:
            rec.stop("手动停止")

    # ------------------------------------------------ 录制库

    def recordings(self) -> tuple[int, dict]:
        """录制库：`live_raw` 下所有成片（含每个文件时长），与总量统计。"""
        files: list[dict] = []
        for pattern in ("*.mp4", "*.ts"):
            for p in sorted(ws.live_source_dir.rglob(pattern)):
                if p.is_dir():
                    continue
                try:
                    stat = p.stat()
                except OSError:
                    continue
                files.append({
                    "name": p.name,
                    "rel": str(p.relative_to(ws.live_source_dir)),
                    # v3.25：画幅后缀是文件名的一部分（_916/_34…），反查出来给 UI 打标签
                    "aspect": aspect_of_tag(_aspect_suffix(p.stem)),
                    "sizeBytes": stat.st_size,
                    "sizeMb": round(stat.st_size / 1024 / 1024, 2),
                    "mtime": round(stat.st_mtime, 1),
                    "durationSec": probe_duration(p),
                })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        total = sum(f["sizeBytes"] for f in files)
        return 200, {
            "files": files,
            "count": len(files),
            "totalBytes": total,
            "totalGb": round(total / 1024 ** 3, 2),
            "dir": str(ws.live_source_dir),
        }

    def delete_recordings(self, rels: list) -> tuple[int, dict]:
        """删除录制库里的成片（**只允许删 `live_raw` 内的文件**）。

        规则已抽到 :func:`app.common.file_ops.delete_files_in`（录制/转写/候选三处共用）：
        路径越界拒绝、目录拒绝、不存在的如实报 failed、单个失败不影响其余。
        UI 侧另有二次确认；这里是最后一道防线。
        """
        return delete_files_in(ws.live_source_dir, rels,
                               log=lambda m: self.log(f"[recorder-manager] {m}"))

    # ------------------------------------------------ 立即解析（"解析并添加"按钮）

    async def resolve_room(self, raw: str) -> tuple[int, dict]:
        """立即解析一个直播间（不录制），供 UI 的「解析并添加」按钮用。

        v3.26：吃直播间号 / 直播间链接 / 分享短链 / 主播主页链接；抖音号解析不出来时
        把**能照做的提示**原样带回去（``hint`` 字段），不要只甩一个「解析失败」。
        """
        from .douyin import DouyinClient
        try:
            info = await DouyinClient().resolve_input(raw)
        except LiveRecorderError as exc:
            return 502, {"error": str(exc), "input": str(raw or ""),
                         "inputKind": parse_room_input(raw)["kind"]}
        return 200, info

    def resolve_room_sync(self, raw: str) -> tuple[int, dict]:
        """从 HTTP 线程安全地调 resolve_room。

        v3.50：这是**用户显式**发起的解析（点了「解析并添加」），所以先把风控冷却解除
        —— 冷却的提示原文就是"去浏览器过一次验证，再回来重试"，
        用户重试这个动作本身就是在告诉我们"我过完了"。
        **只有人类动作能解**；自动轮询永远不解（那才是继续敲门的元凶）。
        """
        self._clear_cooldown("解析并添加")
        if self._loop is None:
            return 503, {"error": "调度器未启动"}
        future = asyncio.run_coroutine_threadsafe(self.resolve_room(raw), self._loop)
        try:
            return future.result(timeout=30)
        except Exception as exc:
            return 502, {"error": f"{type(exc).__name__}: {exc}"}

    def _clear_cooldown(self, why: str) -> None:
        """用户显式动作 → 解除风控冷却（唯一实现，别在各处各写一遍）。"""
        try:
            from .douyin import cooldown_left, reset_cooldown
        except ImportError:                                        # pragma: no cover
            return
        left = cooldown_left()
        if left > 0:
            reset_cooldown()
            self.log(f"[recorder-manager] 用户操作「{why}」→ 解除风控冷却"
                     f"（原还剩 {left / 60:.0f} 分钟）")
