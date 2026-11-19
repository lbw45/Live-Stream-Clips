#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直播录制器（D23 三层容错 + 每小时一个文件）。

需求来源（用户原话）：
    ① 网络中断/异常中断后**继续录**，中断前后的内容**合并为一个视频**；
    ② 内容较长时**每小时存为一个视频**，再继续录，直到直播结束。

对应实现（§6.8，全部有实测依据）：
- **L0 透明重连**：ffmpeg ``-reconnect*`` —— 网络抖动时 ffmpeg 自己重连，**不丢内容**。
  这是第一道防线，绝大多数"网络问题"在这一层就消掉了。
- **L1 进程级续录**：ffmpeg 非 0 退出 → 重新解析房间与流地址 → 确认仍在播 → 继续录。
  ⚠️ **L1 期间的内容是真实丢失的，合片补不回来**（网络断了就是没录到，必须如实告知）。
- **L2 合片**：同一小时段内的中断分片合并为一个视频（见 :mod:`app.slicer.merge`）。

为什么不靠「每小时重启 ffmpeg」分段（K20）：重启会产生 0.56–1.52s 墙钟死区，
实测虽被 CDN 缓冲追帧补回（净损失≈0），但依赖两个不可控变量，属**静默失效**。
所以分段必须用 ``-f segment`` 在**同一个 ffmpeg 进程内**完成。

分段档（v3.35 / v3.78）：`SEGMENT_CHOICES` 一份（整场不切 / 1 小时 / 3 小时）。
    「整场不切」= `segment_seconds == 0` 的哨兵值，见该常量的说明与它的代价。

分段"已落盘"的判定（K21/K23）：
    segment muxer 只在**关键帧**处切分，段长不精确等于 ``segment_time``；
    因此**不能用定时器去猜**，而是看**文件出现**——
    除当前正在写的最后一个文件外，其余全部视为已关闭（muxer 已写出 moov）。

画面健康检测（v3.77，用户 2026-10-09「录屏为什么会画面抖动…检测到就及时暂停录制、
再重新开始录制直播」）：
    每 :data:`HEALTH_CHECK_SEC` 秒抽**正在写的那个分片**尾部 3 秒画面（8fps、96×54 灰度）
    算统计量，判 黑屏 / 画面卡住不动 / 画面抖动闪屏 三类故障（判据与阈值见
    :mod:`app.slicer.frame_health`）。判到坏 **连续** :data:`HEALTH_CONFIRM_TIMES` 次才动作
    —— 优雅退出 ffmpeg（分片正常收尾）再由主循环重新拉流。**整个过程只读本地文件，
    一个额外网络请求都不发**（这一点很重要：外呼次数直接关系到风控，见 K119）。
    两道闸：频率闸门（:data:`HEALTH_RATE_*`）与总开关（``YJ_DISABLE_FRAME_HEALTH=1``）。

画幅（v3.25，用户要求「真重编码到目标画幅」）：
    开录前 ffprobe 探一次源流尺寸 → 归到 16:9 / 9:16 / 3:4 / 4:3 → 按目标尺寸重编码输出；
    录制途中每 30s 再探一次，**画幅一变就把上一段封存成独立素材**（文件名带 ``_916``/``_34``
    这类后缀），再按新画幅重新拉流继续录。判定与滤镜全部走 :mod:`app.slicer.aspect`
    （唯一出处），本文件只负责"什么时候切段"。
"""
from __future__ import annotations
import json

import asyncio
import base64
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common.paths import ws
from .aspect import (MODE_AUTO, MODE_COPY, UNKNOWN, classify, encoder_args,
                     normalize_mode, plan as aspect_plan, tag_of)
from .douyin import DouyinClient, LiveRecorderError, cooldown_left
from . import frame_health as health
from .merge import probe, probe_duration, probe_stream_size

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# v3.25：录制途中探测源画幅的间隔（秒）。太频繁等于额外拉一路流（K57）
DEFAULT_ASPECT_CHECK = 30.0
# v3.42：开播状态轮询间隔（秒）——「每 30 秒检测一下是否开播」的需求就是它。
# 唯一出处：界面文案（「每 30 秒自动检测」）与快照字段 ``pollIntervalSec`` 都引用这里，
# 以后要改间隔只改这一个数（K44：别让后端默认值和界面文案各自写死）。
PROBE_INTERVAL_SEC = 30.0

# ---- v3.50：探测退避（A3 里一直挂着「后续加失败退避」，这次必须做，因为出了真事故）----
# ⚠️ v3.63（）**下调上限**：用户报「主播开播了却没开始录，要停止监控再点
#    开始录制才录上」。取证：退避上限原来是 **300s（未开播）/ 900s（出错）**，而
#    `_wait_for_live` 是「睡完这一觉才探下一次」——挂机越久，探测间隔越大，于是开播后
#    最长要等 5 分钟（出错时 15 分钟）才发现，且中途进过风控冷却就会更久；用户手动
#    「停止监控→开始录制」会**新建一个 LiveRecorder**（退避复位成 30s）→ 立刻就录上了。
#    这正是他看到的现象。上限改成：未开播 60s、出错 180s —— 仍然比 30s 基础间隔客气
#    （长时间不播时请求量减半以上），但**承诺必须与实测一致**：界面写的「每 N 秒检测一次」
#    用的就是这个真实值（见 snapshot.pollIntervalSec）。
BACKOFF_CAP_SEC = 60.0
# 出**错误**（网络/被风控）时的最长间隔。900s 那种"沉默一刻钟"对"开播就要录"是致命的：
# 网络抖动这类瞬时错误不该让监控瞎 15 分钟。**风控冷却另走一条路**（见 _wait_for_live：
# 冷却期内不敲请求，只按冷却剩余时间睡，到点立刻探），所以这里不需要靠"睡很久"来停手。
BACKOFF_ERROR_CAP_SEC = 180.0
# 连续多少次「没开播」升一档（10 次 ≈ 5 分钟）
BACKOFF_OFFLINE_STEP = 10

# v3.63：出错后最多睡多久。冷却期内的睡眠由 `douyin.cooldown_left()` 决定（到点即醒），
# 非冷却类的错误（网络抖动等）用这个上限，保证不至于长时间失去探测能力。
ERROR_SLEEP_CAP_SEC = 60.0


class ProbeBackoff:
    """等开播阶段的探测间隔退避。

    ⚠️ 三种结果**必须分开计数**（合成一个计数器会让一种状态拖累另一种）：
      · :meth:`live`    已开播 → 复位
      · :meth:`offline` 正常拿到"未开播" → **缓慢升档**（30→45→60→…→300s）：
        刚开播前通常离得很近，前几分钟保持 30s 就够了；挂了很久则没必要继续高频。
      · :meth:`error`   请求失败/被风控 → **2 倍快速退避**（60→120→…→900s）。
    三个方法都**返回下一轮该等多少秒**，调用方直接拿去 sleep。
    """

    # v4.74 P-A（方案《录制模块优化方案》优化项 A）：**秒退判据** ——
    # ffmpeg 拉起后撑不过这个时长就按「秒退」处理（源此刻彻底不可用），与
    # 「拉起 5 分钟后断流（源没问题，只是抖了一下）」分开计数。
    # 参考实现用 5s（bililive-go fastFailThreshold），只取设计不抄代码。
    FAST_FAIL_SEC = 5.0

    def __init__(self, base: float, *, cap: float = BACKOFF_CAP_SEC,
                 cap_error: float = BACKOFF_ERROR_CAP_SEC) -> None:
        self.base = max(5.0, float(base))
        self.cap = float(cap)
        self.cap_error = float(cap_error)
        self.offline_streak = 0
        self.errors = 0
        self.fast_fail_streak = 0        # 连续「拉起就崩」的次数（秒退连击）

    def attempt(self, elapsed: float, got_stream: bool) -> float:
        """一次**录制尝试**结束后调用（L1 续录段，v4.74 P-A）。

        · got_stream=True（真拉到过流）⇒ 全部复位，回到基础间隔
          —— 保断流后的内容连续性（立刻重试由 L1 的 15 秒封顶保证，见 run()）；
        · got_stream=False 且 elapsed ≥ FAST_FAIL_SEC ⇒ 正常失败，走普通 error 退避；
        · got_stream=False 且 elapsed < FAST_FAIL_SEC ⇒ **秒退**：连续计数，
          下一次等开播探测更快翻倍（见 interval() 的位移）。
        """
        if got_stream:
            self.fast_fail_streak = 0
            return self.live()
        if elapsed < self.FAST_FAIL_SEC:
            self.fast_fail_streak += 1
        else:
            self.fast_fail_streak = 0
        return self.error()

    def live(self) -> float:
        self.offline_streak = 0
        self.errors = 0
        return self.base

    def offline(self) -> float:
        self.errors = 0                       # 能拿到房间信息 = 通道是好的
        self.offline_streak += 1
        return self.interval()

    def error(self) -> float:
        self.errors += 1
        self.offline_streak = 0
        return self.interval()

    def interval(self) -> float:
        """当前生效的间隔（给界面展示用，不改变状态）。

        v4.74 P-A：error 分支带**秒退位移** —— 连续「拉起就崩」说明源此刻彻底不可用，
        比普通失败更快翻倍（shift = min(errors-1+fast_fail_streak, 4)，最多多翻 4 档）。
        """
        if self.errors:
            # 普通失败（无秒退）保持原序列 base×2^errors（60→120→180 封顶）；
            # 秒退连击把 streak 作为**额外位移**（shift=min(errors+streak, 8)），
            # 第 2 次秒退就直接顶到 cap_error —— 源彻底不可用时少白跑几趟。
            shift = min(self.errors + self.fast_fail_streak, 8)
            return min(self.cap_error, self.base * (2 ** shift))
        # 前 BACKOFF_OFFLINE_STEP 次没开播保持基础间隔（刚开播前通常离得很近），
        # 之后每满一档 +50% 基础间隔，封顶 cap。`-1` 是为了让"第 10 次"仍算第一档。
        step = max(0, self.offline_streak - 1) // BACKOFF_OFFLINE_STEP
        return min(self.cap, self.base * (1 + 0.5 * step))


# v3.57：画幅切换的**去抖 + 频率闸门**。
# 教训（K56/K92）：抖音同一场直播的流会在 1920x1080 / 1080x1920 / 1080x1440 / 544x960
# 之间跳，探测本身还会失败（K57）。
# 「一看到不一样就切」一场能切出几十个碎片 —— 这是**频率**问题，
# 只验"功能对不对"的测试永远发现不了，必须单独写断言（K92）。
PROBE_CONFIRM_TIMES = 2             # 轮询通道：同一种新画幅连续几次才认（去抖）
ASPECT_RATE_WINDOW_SEC = 120.0      # 频率闸门的统计窗口
ASPECT_RATE_MAX = 6                 # 窗口内最多切几次（含）
ASPECT_RATE_COOLDOWN_SEC = 180.0    # 触顶后的冷静期
# v3.57：已解析的流地址可以在这段时间内复用（K99）。
# 以前每重启一次 ffmpeg 都重新解析一次房间流地址 —— 一场切 20 次就是 20 次请求，
# 这是人机验证（A10）的风险源。真失败（L1 续录）时调用方会主动把缓存作废。
STREAM_URL_TTL_SEC = 300.0
# 录制途中检查一次「ffmpeg 有没有报分辨率变化」的频率（秒）。
WATCH_TICK_SEC = 0.5


class SwitchRateGuard:
    """画幅切换的**频率闸门**（v3.57 闸 1/闸 2 共用的一道保险）。

    窗口内切够 :data:`ASPECT_RATE_MAX` 次就直接进冷静期：宁可漏切，也不让
    一场直播被切成几十个碎片 —— 碎片之间的 1~2 秒空档是**真实丢内容**的。

    ⚠️ 测它时必须**复用同一个实例**（K96）：每次 new 一个新对象，
    闸门永远不会触发，三条断言会一起红，而错在测试不在代码。
    """

    def __init__(self, window: float = ASPECT_RATE_WINDOW_SEC,
                 limit: int = ASPECT_RATE_MAX,
                 cooldown: float = ASPECT_RATE_COOLDOWN_SEC) -> None:
        self.window = max(1.0, float(window))
        self.limit = max(1, int(limit))
        self.cooldown = max(1.0, float(cooldown))
        self._hits: list[float] = []
        self._until = 0.0

    def allow(self) -> bool:
        now = time.time()
        if now < self._until:
            return False
        self._hits = [t for t in self._hits if now - t <= self.window]
        return len(self._hits) < self.limit

    def note(self) -> None:
        """记一次已发生的切换；达到上限就立刻进冷静期。"""
        now = time.time()
        self._hits = [t for t in self._hits if now - t <= self.window]
        self._hits.append(now)
        if len(self._hits) >= self.limit:
            self._until = now + self.cooldown
            self._hits = []

    @property
    def cooling(self) -> bool:
        return time.time() < self._until

    def remaining(self) -> float:
        return max(0.0, self._until - time.time())


CHANNEL_PROBE = "probe"     # ffprobe 轮询（延迟信号，会多发一次请求）
CHANNEL_EVENT = "event"     # ffmpeg stderr 事件（零额外请求，秒级）


class AspectGuard:
    """画幅"什么时候该切段"的**唯一出处**（v3.57）。

    两个通道喂同一种观测，差别只在要不要去抖：
      · ``probe``：一次 ffprobe 轮询。可能失败、也可能读到 jitter 的中间值，
        所以**连续 :data:`PROBE_CONFIRM_TIMES` 次一致**才算数（去抖）；
        探不到尺寸（网络抖）**清零 pending、绝不动作**（K57）。
      · ``event``：ffmpeg 自己的 stderr 报"画面尺寸变了"，这是一次真实的帧级变化，
        且**零额外请求**，所以一次就算数。

    两个通道都过同一道 :class:`SwitchRateGuard`。
    """

    def __init__(self, mode: str = MODE_AUTO,
                 confirm_times: int = PROBE_CONFIRM_TIMES,
                 rate: SwitchRateGuard | None = None) -> None:
        self.mode = normalize_mode(mode)
        self.confirm_times = max(1, int(confirm_times))
        self.rate = rate or SwitchRateGuard()
        self.current = UNKNOWN
        self.pending = UNKNOWN
        self.pendingSrc: tuple[int, int] = (0, 0)
        self.switchCount = 0
        self.blocked = 0
        self.lastReason = ""

    def set_current(self, aspect: str) -> None:
        """把"当前画幅"同步进来（每段开局都要同步一次，别让它漂移）。"""
        self.current = aspect or UNKNOWN
        self.pending = UNKNOWN
        self.pendingSrc = (0, 0)
        self._streak = 0

    def plan_for(self, width: int, height: int, pk: bool = False) -> dict:
        return aspect_plan(width, height, self.mode, pk=pk)

    def observe(self, width: int, height: int, *, pk: bool = False,
                channel: str = CHANNEL_PROBE) -> str:
        """喂一次观测。返回**要切到的目标画幅**；不需要动作返回 ``""``。"""
        self.lastReason = ""
        if self.mode == MODE_COPY:
            return ""
        try:
            w, h = int(width), int(height)
        except (TypeError, ValueError):
            w = h = 0
        if not w or not h:
            # 探不到 = 什么都不知道。**必须把 pending 清零**：
            # 上一次的 pending 是几十秒前的事，凭它切段等于凭旧证据动手（K57）。
            self._streak = 0
            self.pending = UNKNOWN
            return ""
        new = self.plan_for(w, h, pk=pk).get("aspect") or UNKNOWN
        if new == UNKNOWN or new == self.current:
            self._streak = 0
            self.pending = UNKNOWN
            return ""
        if channel == CHANNEL_PROBE:
            self._streak = self._streak + 1 if new == self.pending else 1
            self.pending = new
            if self._streak < self.confirm_times:
                self.lastReason = (f"第 {self._streak}/{self.confirm_times} 次观测到 "
                                   f"{self.current} → {new}，还没确认，不动")
                return ""
        else:
            self.pending = new
            self._streak = self.confirm_times
        if not self.rate.allow():
            self.blocked += 1
            self.lastReason = (f"频率闸门冷却中（剩 {self.rate.remaining():.0f}s），"
                               f"本次 {self.current} → {new} 不切段")
            return ""
        self.pendingSrc = (w, h)
        self.rate.note()
        self.switchCount += 1
        self.lastReason = (f"确认切换到 {new}（通道 {channel}）"
                           if channel == CHANNEL_PROBE else f"ffmpeg 上报 {w}x{h} → {new}")
        return new


# ---- 闸 1：从 ffmpeg 的 stderr 里认出「分辨率变了」----
#
# 本机 ffmpeg（N-126308-gd411d9e752-20260828）**实测**的原文只有这一条，
# 且只在 **info 及以上** loglevel 才会出现（warning 什么都报不出来）：
#     [vf#0:0 @ 0x...] Reconfiguring filter graph because video parameters
#                      changed to yuv420p(...), 480x720, unspecified alpha
# ⚠️ 两条**不能**拿来当信号的线（实测证据，别凭印象加回来）：
# "Reinit context to 640x368" —— 起流时就会报一次，而且 368≠360（crop 后的编码尺寸），
#       拿它当信号会在**开局就误切一次**；copy 模式下它也是唯一会出现的一条。
# copy 模式（``-c copy``、不过滤镜）**完全不报任何线** —— 见 _spawn 里的说明。
_SIZE_EVENT_PATTERNS = (
    re.compile(r"video parameters changed to .*?\b(\d{2,5})x(\d{2,5})\b", re.I),
    re.compile(r"frame changed from size:\s*(\d+)x(\d+).*?to size:\s*(\d+)x(\d+)", re.I),
    re.compile(r"frame size changed to\s*(\d+)x(\d+)\b", re.I),
)


def size_change_from_line(line: str) -> tuple[int, int] | None:
    """从一行 ffmpeg stderr 里认出"分辨率变了"，返回新的 ``(w, h)``；不是则返回 ``None``。

    纯函数、无 IO —— 频率类断言（K92）可以直接喂一串日志行来验证"会不会切疯了"。
    """
    if not line:
        return None
    for pat in _SIZE_EVENT_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        groups = [int(g) for g in m.groups() if g.isdigit()]
        # 有些格式是 "from old to new"，**一定取最后一组**（= 变化后的尺寸）
        if len(groups) >= 2:
            w, h = groups[-2], groups[-1]
            if 0 < w <= 20000 and 0 < h <= 20000:
                return (w, h)
    return None


class _StderrWatcher(threading.Thread):
    """盯 ffmpeg 的 stderr —— 它同时干**两件缺一不可**的事：

    1. **排干**管道。以前 stderr=PIPE 却没人读，日志一多就能把管道写满、
       把 ffmpeg 顶死（隐性致命 bug，平时看不出来）。
    2. 从中抓分辨率变化事件，塞进队列交给 :meth:`LiveRecorder._on_size_event`。
    """

    daemon = True

    def __init__(self, proc: subprocess.Popen, events: "queue.Queue",
                 keep_lines: int = 40) -> None:
        super().__init__(daemon=True)
        self.proc = proc
        self.events = events
        self.keep_lines = keep_lines
        self.tail: list[str] = []
        self.hits = 0

    def run(self) -> None:
        try:
            stream = self.proc.stderr
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                self.tail.append(line)
                if len(self.tail) > self.keep_lines:
                    self.tail.pop(0)
                hit = size_change_from_line(line)
                if hit:
                    self.hits += 1
                    self.events.put(hit)
        except (OSError, ValueError):
            pass
        finally:
            stream = self.proc.stderr
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


# K25：录制容器必须是 MPEG-TS（崩溃安全），出片时才 remux 成 MP4
SEGMENT_FORMAT = "mpegts"
SEGMENT_EXT = "ts"
# D23：每小时一个文件
DEFAULT_SEGMENT_SECONDS = 3600

# v3.35：可选的**分段时长**（用户要「选择每段存多久」）。默认 1 小时；3 小时档给
# 「不想频繁流转」的场景。**唯一出处** —— 调度器按这里的值校验，前端选项是它的镜像
# （`index.html` 的 `SEGMENT_MODES`），别在两处各写一套秒数。
#: v3.78：**0 秒 = 哨兵**，表示「全程不中断、整场录成一个文件」（用户 2026-10-09）。
#: 为什么用 0 而不是一个大数字：0 在任何地方都好判（`> 0` 即"要分段"），
#: 而"很大的数字"会在超长直播里悄悄失效 —— 那种失效没人会发现。
SEGMENT_WHOLE_KEY = "whole"
# ---------------------------------------------------------------- v4.47 录屏模式
#: 录屏窗口的**逻辑尺寸**（CSS px）；device_scale_factor=2 ⇒ 输出 1080x1920，
#: 与手机竖屏一致。**原样 9:16，不强制 3:4**（用户 2026-11-06 明确要求）。
SCREEN_VIEWPORT = (540, 960)
#: 录屏窗口标题前缀（gdigrab 按窗口标题抓；注入唯一标题，避免抓到别的 Edge 窗口）
SCREEN_TITLE_PREFIX = "PB-SCREEN-"
#: 连续多少次抓不到画面就收手（防死循环刷日志）
SCREEN_MAX_EMPTY_ATTEMPTS = 3
#: v4.49 后台录屏的**截图视口**：540x960（手机比例），由 `scale=1080:1920` 放大——
#: 视口越大截图越慢（page.screenshot 是同步 CDP 往返），540 宽实测 ~12fps 稳定。
SCREEN_SHOT_VIEWPORT = (540, 960)
#: 截图轮询的目标帧率（实测 12fps；编码跟不上会自动丢帧，不阻塞）
SCREEN_SHOT_FPS = 12.0
#: CDP 截帧的 JPEG 质量（80 在文字/弹幕清晰度与码率之间比较均衡）
SCREEN_BG_JPEG_QUALITY = 80
#: 后台录屏等首帧的上限（抖音直播间从打开到起播实测 20~30s，12s 太短 —— v4.49）
SCREEN_BG_FIRST_FRAME_SEC = 45.0
#: 无头模式能不能拿到画面？实测**抖音播放器在 headless 下不起播**（无帧）⇒ 改为
#: 「有头但窗口开到屏幕外 + 关闭遮挡节流」——用户看不见窗口，渲染/截帧照常（v4.49）。
#: 兜底的「可见窗口抓屏」只在显式设 REC_SCREEN_ALLOW_WINDOW=1 时才启用（排障用）。
SCREEN_ALLOW_WINDOW_FALLBACK = os.environ.get("REC_SCREEN_ALLOW_WINDOW", "") == "1"
#: 屏幕外窗口坐标（远在可见桌面之外；gdigrab 抓不到没关系，我们走 CDP 截帧）
SCREEN_OFFSCREEN_X = -3200

def screen_fit_viewport() -> tuple[int, int]:
    """按**屏幕工作区**算一个 9:16 的 CSS 视口，保证录屏窗口完整可见。

    为什么必须自适应：区域抓取要求窗口矩形完全落在屏幕内，否则 gdigrab 直接
    ``I/O error``（实测：1080p + 125% 缩放下固定 540x960 视口的窗口高出屏幕）。
    留 90px 给标题栏/任务栏，再按 9:16 反推宽度。
    """
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)   # SPI_GETWORKAREA
        work_w = int(rect.right - rect.left) or 1920
        work_h = int(rect.bottom - rect.top) or 1040
        hdc = user32.GetDC(0)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)) or 96       # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        scale = max(1.0, dpi / 96.0)
        css_h = int((work_h - 90) / scale)
        css_w = max(240, int(round(css_h * 9 / 16)))
        # 宽度也要塞得下（超宽屏比例时宽度先到上限）
        max_w = int((work_w - 40) / scale)
        if css_w > max_w:
            css_w = max_w
            css_h = int(round(css_w * 16 / 9))
        return css_w, css_h
    except Exception:                                     # noqa: BLE001
        return SCREEN_VIEWPORT


def screen_window_rect(prefix: str = SCREEN_TITLE_PREFIX) -> dict | None:
    """找到标题含 ``prefix`` 的窗口，置顶并返回它**客户区**的屏幕矩形（物理像素）。

    为什么不用 ``gdigrab -i title=``：实测 Edge 的窗口标题是
    「PB-SCREEN-xxx - 用户配置 1 - Microsoft​ Edge」（带零宽空格与配置名后缀），
    ``title=`` 精确匹配必然失败（Invalid argument / 无输出）。
    改成 EnumWindows 找窗口 + 取客户区矩形，再抓桌面对应区域 —— 稳且不依赖标题。
    """
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # 物理像素坐标（每屏 DPI 感知）
    except Exception:                                     # noqa: BLE001
        pass
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if prefix in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    if not found:
        return None
    hwnd = found[0]
    # 置顶 + 激活（避免被其它窗口盖住；只在前台抢一次，录制期间不反复抢）
    try:
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)   # HWND_TOPMOST, NOSIZE|NOMOVE
        user32.SetForegroundWindow(hwnd)
    except Exception:                                     # noqa: BLE001
        pass
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w, h = int(rect.right - rect.left), int(rect.bottom - rect.top)
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    if w <= 0 or h <= 0:
        return None
    return {"hwnd": int(hwnd), "x": int(pt.x), "y": int(pt.y), "w": w, "h": h}


#: 屏蔽送礼特效的 CSS（v4.47）。礼物动画/横幅都是 DOM/Canvas 图层：
#: 按 gift/present/reward/lottie 关键字兜（只按组合匹配，绝不碰播放器 video）。
GIFT_BLOCK_CSS = """
[class*="gift" i], [id*="gift" i], [data-e2e*="gift" i],
[class*="Gift"], [class*="present" i], [class*="reward" i],
[class*="lottie" i], [class*="Lottie"],
[class*="giftEffect" i], [class*="giftBanner" i],
[class*="gift"] canvas, [class*="Gift"] canvas { display: none !important; }
"""

#: 再兜一层（SPA 动态插入的节点 CSS 拦不住）：MutationObserver 直接隐藏命中节点。
SCREEN_BLOCK_JS = """
(() => {
  const HIT = /gift|present|reward|lottie/i;
  const nuke = (root) => {
    try {
      const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const el of all) {
        if (el.tagName === 'VIDEO' || el.querySelector('video[src]')) continue;
        const key = (el.className && el.className.toString ? el.className.toString() : '')
          + ' ' + (el.id || '') + ' '
          + (el.getAttribute ? (el.getAttribute('data-e2e') || '') : '');
        if (HIT.test(key)) el.style.setProperty('display', 'none', 'important');
      }
    } catch (e) { /* 忽略：屏蔽失败也不能影响播放 */ }
  };
  const start = () => {
    nuke(document);
    new MutationObserver((ms) => {
      for (const m of ms) for (const n of (m.addedNodes || []))
        if (n.nodeType === 1) nuke(n);
    }).observe(document.documentElement, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
"""

#: ffmpeg 的 `-segment_time` 不接受 0（会报错），所以整档送这个值 —— 7 天，等价于"不切"。
NO_SPLIT_SECONDS = 7 * 24 * 3600.0
SEGMENT_CHOICES: dict[str, float] = {"whole": 0.0, "1h": 3600.0, "3h": 10800.0}
SEGMENT_LABELS: dict[str, str] = {
    "whole": "全程不中断（整场录成一个视频；直播结束后才自动流转）",
    "1h": "每 1 小时一段（推荐）",
    "3h": "每 3 小时一段",
}
DEFAULT_SEGMENT_KEY = "1h"


def normalize_segment_key(value) -> str:
    """把任意输入归一成合法的分段档名（认不出就落默认，不写坏值）。"""
    key = str(value or "").strip()
    if key in SEGMENT_CHOICES:
        return key
    # 也接受直接给秒数（老前端/命令行）
    try:
        secs = float(key)
    except (TypeError, ValueError):
        return DEFAULT_SEGMENT_KEY
    for k, v in SEGMENT_CHOICES.items():
        if abs(v - secs) < 1.0:
            return k
    return DEFAULT_SEGMENT_KEY
# ---------------------------------------------------------------- v3.77 画面健康检测
#: 总开关（默认开）。要临时关掉用环境变量 ``YJ_DISABLE_FRAME_HEALTH=1``
#: —— 冒烟/调试时空跑一场录制不想被它插一脚时用。
HEALTH_ENABLED = os.environ.get("YJ_DISABLE_FRAME_HEALTH", "") not in ("1", "true", "True")
#: 每隔几秒检一次（抽 3 秒画面≈100-300ms，8 秒一次对 CPU 可忽略）
HEALTH_CHECK_SEC = 8.0
#: **连续**判坏几次才动作（去抖：一次可能是瞬时花屏/切场景，重启反而多丢 1~2 秒）
HEALTH_CONFIRM_TIMES = 2
#: 频率闸门：窗口秒数 / 窗口内最多重启几次 / 触顶后的冷静期
HEALTH_RATE_WINDOW_SEC = 1800.0
HEALTH_RATE_MAX = 5
HEALTH_RATE_COOLDOWN_SEC = 600.0
#: 出事的那一段短于它 ⇒ 说明刚拉流就坏、几乎没有可用内容 ⇒ 从合片里撤回并删掉
#: （长于一小时段的话整段丢掉损失太大，保留并把异常写进日志/统计）
HEALTH_DROP_MAX_SEC = 120.0
#: 重启前的短暂冷却（给抖音侧一点时间，别刚断就贴上去）
HEALTH_RESTART_SLEEP_SEC = 3.0
#: 等 ffmpeg 优雅退出的上限（超时就 terminate；TS 分片杀了也能读，K25）
HEALTH_EXIT_WAIT_SEC = 5.0
#: v4.29：**用户点停止**后等 ffmpeg 优雅退出的上限。实测 `q` 写进 stdin 后，若 ffmpeg
#: 正卡在网络读（-rw_timeout 30s）里，它可能几十秒都不退出 —— 卡片就一直「停止中」。
#: TS 分片被杀也能读（K25），所以这里给个短窗口，超时 terminate，不再无限等。
STOP_EXIT_WAIT_SEC = 8.0

# K21：段长只对齐关键帧，判定"满一小时"要留容错（实测 8s 段偏差 ≤0.04s，3600s 按 0.5% 计）
HOUR_TOLERANCE = 0.98
# A6：磁盘告警线（v4.74 P-B 起**缺省值**，实际阈值可在设置页配：
#     recordMinFreeGb / recordMinFreePct —— 双条件任一触发即停）
LOW_DISK_GB = 50.0
LOW_DISK_PCT = 5.0


def _read_disk_limits_from(cfg: dict) -> tuple[float, float]:
    """从 settings dict 解析磁盘停机阈值（缺省 50GB / 5%，非法值回缺省）。"""
    gb, pct = LOW_DISK_GB, LOW_DISK_PCT
    try:
        gb = max(0.0, float(cfg.get("recordMinFreeGb", LOW_DISK_GB)))
    except (TypeError, ValueError):
        gb = LOW_DISK_GB
    try:
        pct = min(100.0, max(0.0, float(cfg.get("recordMinFreePct", LOW_DISK_PCT))))
    except (TypeError, ValueError):
        pct = LOW_DISK_PCT
    return gb, pct


def disk_state_now(root=None, cfg: dict | None = None) -> dict:
    """**模块级**磁盘水位（v4.74 P-B）：录制器内部与 manager.snapshot / 设置端点共用一份（K44）。

    ``root`` 缺省用 ``ws.root``；``cfg`` 缺省现场读 ``ws.settings_path``。
    """
    import json as _json
    import shutil as _shutil
    root = root or ws.root          # 顶层 import 的 ws（模块级单例，K44）
    if cfg is None:
        try:
            cfg = _json.loads(Path(ws.settings_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    gb, pct = _read_disk_limits_from(cfg or {})
    try:
        usage = _shutil.disk_usage(str(root))
        free_gb = usage.free / 1024 ** 3
        free_pct = (usage.free / usage.total * 100.0) if usage.total else 0.0
    except OSError:
        free_gb = free_pct = 0.0
    return {"freeGb": round(free_gb, 1), "freePct": round(free_pct, 2),
            "limitGb": gb, "limitPct": pct,
            "low": bool(free_gb < gb or free_pct < pct)}


@dataclass
class RecordStats:
    webRid: str = ""
    owner: str = ""
    title: str = ""
    quality: str = ""
    startedAt: float = 0.0
    # v4.50（用户 2026-11-06）：「已录」只算**真的在录**的时间 ——
    #   recordedSec   = 已结束的各轮拉流时长累计
    #   pullStartedAt = 当前这轮 ffmpeg 起拉的时刻（0 = 当前没在拉流）
    recordedSec: float = 0.0
    pullStartedAt: float = 0.0
    endedAt: float = 0.0
    attempts: int = 0
    interruptions: int = 0
    segments: int = 0
    outputFiles: list[str] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)
    stopReason: str = ""
    # v3.25 画幅：mode=录制画幅模式；aspects=各段实际画幅（按段顺序）；
    # switches=录制途中切了几次画幅（>0 说明这场直播中途改过版式，例如进了 PK）
    aspectMode: str = MODE_AUTO
    aspects: list[str] = field(default_factory=list)
    aspectSwitches: int = 0
    # v3.57：被「频率闸门」拦下的切换次数（>0 说明这场流在疯狂抖，值得看一眼）
    aspectSwitchBlocked: int = 0
    # v3.77 画面健康：查了几次 / 判到异常几次 / 因此重启拉流几次 / 被频率闸门拦下几次
    healthChecks: int = 0
    healthRestarts: int = 0
    healthBlocked: int = 0
    # v4.75 P-C（方案《录制模块优化方案》优化项 C）：**录制报告**数据 ——
    #   recordSegments = 每次「拉流段」（有重启才有边界）的 reason 与相对起止（秒，相对 firstPullStart）
    #   gaps/gapSec/gapCount = L1 重连等造成的**内容死区**（真实丢失的内容，切片据此剔除跨断层候选）
    recordSegments: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    gapSec: float = 0.0
    gapCount: int = 0
    firstPullStart: float = 0.0
    #: 每次判到异常的明细（时间 + 原因 + 指标 + 出事的分片名），UI 与日志都用它
    healthEvents: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            **self.__dict__,
            "elapsedSec": round(self.endedAt - self.startedAt, 1) if self.endedAt else 0,
        }


class LiveRecorder:
    """单个直播间的录制器。**必须在服务进程里跑**，不能跑在桌面窗口进程里。

    :param on_hour_file: 每落一个"小时文件"回调一次（``Path`` → 最终文件）。
        上层用它触发「边录边切」（D24）—— 回调里不要做耗时操作。
    :param on_segment:   每检测到一个已关闭的分片回调一次（调试/监控用）。
    :param on_probe:     **每次轮询房间信息后回调一次**（v3.35，参数是房间信息 dict）。录制器
        卡片要靠它显示「此刻是否正在直播」—— 调度器自己不轮询，只能由这里把结果递上去。
        回调同样**不许耗时**（跑在事件循环里）。
    """

    def __init__(self,
                 web_rid: str,
                 quality: str = "FULL_HD1",
                 poll_interval: float = PROBE_INTERVAL_SEC,
                 segment_seconds: float = DEFAULT_SEGMENT_SECONDS,
                 out_dir: str | os.PathLike | None = None,
                 final_dir: str | os.PathLike | None = None,
                 log=print,
                 on_hour_file=None,
                 on_segment=None,
                 on_probe=None,
                 aspect_mode: str = MODE_AUTO,
                 aspect_check_interval: float = DEFAULT_ASPECT_CHECK,
                 screen_mode: bool = False) -> None:
        self.web_rid = str(web_rid)
        self.quality = quality
        self.poll_interval = max(10.0, poll_interval)
        # v3.50：**当前实际**生效的探测间隔（退避后会变大）。界面显示「每 N 秒检测」用它，
        # 否则用户看到"每 30 秒"却等了几分钟，会以为功能坏了。
        self.current_interval = self.poll_interval
        # v3.78：整档（whole）是 0 秒 —— 归一到这里，后面一律用 `> 0` 判"要不要分段"
        self.segment_seconds = max(0.0, float(segment_seconds or 0.0))
        # v4.47：录屏模式（整屏录制，含头像/弹幕/连线；屏蔽送礼特效；原样比例）
        self.screen_mode = bool(screen_mode)
        self.log = log or (lambda *a, **k: None)
        self.on_hour_file = on_hour_file
        self.on_segment = on_segment
        self.on_probe = on_probe
        # --- v3.25 画幅 ---
        self.aspect_mode = normalize_mode(aspect_mode)
        self.aspect_check_interval = max(10.0, aspect_check_interval)
        self.aspect = UNKNOWN          # 当前段画幅（unknown = 还没探到/认不出）
        self.aspect_plan: dict = {}    # 当前段的重编码方案（UI 展示用）
        # v3.57：画幅切换的判定核心（去抖 + 频率闸门），闸 1/闸 2 共用
        self._guard = AspectGuard(self.aspect_mode)
        self._pending_aspect: str = UNKNOWN   # 探测到的新画幅
        self._pending_plan: dict = {}   # 事件给出的新尺寸算好的方案（省一次探测）
        self._switch_pending = False   # 是否为切画幅而主动结束了 ffmpeg
        # v3.77 画面健康：_health_bad 是「连续判坏」计数；_health_pending 表示
        # 「本轮是被健康检查叫停的，主循环要去重新拉流」
        self._health_bad = 0
        self._health_pending = False
        self._health_part: Path | None = None
        self._health_guard = SwitchRateGuard(HEALTH_RATE_WINDOW_SEC, HEALTH_RATE_MAX,
                                            HEALTH_RATE_COOLDOWN_SEC)
        self._pk = False               # 房间是否正在连线/PK（画幅判定的辅助信号）
        # v4.56：后台录屏的 ffmpeg 输入是 **image2pipe（JPEG 帧管道）**——stdin 被
        # 数据流占用，`_request_graceful_exit` 往里写 'q' 不是交互命令而是**帧数据**
        # （污染流且退不了）。这类进程只能靠「关 stdin（EOF）+ terminate」收口，
        # 见 `_record_loop_screen_bg` 的收尾与 `_finish_screen_pipe`。
        self._pipe_input = False

        ws.ensure_dirs()
        self.parts_dir = Path(out_dir) if out_dir else (ws.records_dir / self.web_rid)
        self.final_dir = Path(final_dir) if final_dir else ws.live_source_dir
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self.final_dir.mkdir(parents=True, exist_ok=True)

        self.client = DouyinClient()
        self.stats = RecordStats(webRid=self.web_rid, aspectMode=self.aspect_mode)
        self._stop = False
        # v3.45：界面可以要求「**立刻**重探一次是否开播」（录制器区块的「刷新状态」按钮）。
        # 用 threading.Event 而不是 asyncio.Event —— 请求来自 HTTP 线程，
        # threading.Event.is_set() 跨线程读是安全的，也不必操心把 set() 投到哪个 loop。
        self._probe_now = threading.Event()
        self._proc: subprocess.Popen | None = None

    # ---------------------------------------------------------------- 控制

    @property
    def pulling(self) -> bool:
        """**此刻是否真在拉流**（ffmpeg 进程活着）= 正在录。

        v3.33 为什么需要它：调度器里「监控中等开播」与「正在录制」都落在
        ``state="waiting"``（``recording`` 这个值其实从未被赋值），**光看 state 分不开**。
        而编排器要在「代码更新过、旧服务还活着」时决定能不能安全重启服务 ——
        重启会打断正在录的这场，必须有一个**直接**的判据。ffmpeg 进程是唯一可靠的。
        """
        proc = self._proc
        return bool(proc is not None and proc.poll() is None)

    @property
    def stopping(self) -> bool:
        """**已经要求停止、正在收尾**（v4.15）。

        `pulling` 只说明 ffmpeg 进程还在 —— 但写了 'q' 之后的这几秒里
        **没有新画面进来**，它正在把最后一个分片封好。这两件事混在一起，
        界面就会在点完「停止录制」后继续显示「录制中」，用户以为按钮没反应
        （K69：不能把「正在收尾」说成「还在录」）。
        """
        return bool(self._stop)

    def stop(self, reason: str = "用户停止") -> None:
        self._stop = True
        self.stats.stopReason = reason
        self._request_graceful_exit()

    def request_probe(self) -> None:
        """要求**立刻**重探一次是否开播（界面「刷新状态」按钮用，v3.45）。

        只对「正在等开播」的阶段有意义（那个循环里每 ``poll_interval`` 秒才探一次，
        默认 30 秒）—— 用户点了刷新却要干等半分钟才看到变化，体验上等于没反应。
        置位后由 ``_wait_for_live`` 的等待循环接手，下一次 ≤0.25s 就会真的去探。
        **跨线程调用安全**（threading.Event）。
        """
        self._probe_now.set()

    def _pull_begin(self) -> None:
        """v4.50：**真的开始录制**的时刻（ffmpeg 刚起拉）—— 监控等待不算「已录」。

        v4.75 P-C：同时记全场**第一个拉流起点**（gaps 相对化基准），并把与上一段之间
        的**内容死区**（间隔 > 0.5s，即 L1 重连/重启空档）记进 gaps —— 这是真实丢失的
        内容，切片阶段据此剔除跨断层候选。
        """
        now = time.time()
        s = self.stats
        if not s.firstPullStart:
            s.firstPullStart = now
        if getattr(s, "lastPullEndedAt", 0.0):
            gap = now - s.lastPullEndedAt
            if gap > 0.5:
                s.gaps.append({"from": round(s.lastPullEndedAt - s.firstPullStart, 1),
                               "to": round(now - s.firstPullStart, 1),
                               "sec": round(gap, 1)})
                s.gapSec = round(s.gapSec + gap, 1)
                s.gapCount += 1
        self.stats.pullStartedAt = now

    def _pull_end(self) -> None:
        """v4.50：这轮拉流结束，把时长累计进 `recordedSec`。

        v4.75 P-C：同时记**段的相对起止**（reason 在调用方分支确定后再 push，
        见 :meth:`_push_pull_segment`）。
        """
        if self.stats.pullStartedAt:
            self._pull_seg_from = self.stats.pullStartedAt
            self._pull_seg_to = time.time()
            self.stats.recordedSec += max(0.0, self._pull_seg_to - self._pull_seg_from)
            self.stats.lastPullEndedAt = time.time()
            self.stats.pullStartedAt = 0.0

    def _push_pull_segment(self, reason: str, detail: str = "") -> None:
        """v4.75 P-C：把刚结束的拉流段按**确定的原因**记进 reportSegments。

        调用点 = run() 里各结束分支（health/aspect/crash/stop）——只有那里才知道
        "这一段为什么结束"。残留段（没走到分支的）由 run() 收尾兜底 push。
        """
        frm = float(getattr(self, "_pull_seg_from", 0.0) or 0.0)
        to = float(getattr(self, "_pull_seg_to", 0.0) or 0.0)
        if frm <= 0 or to <= frm:
            return
        base = float(self.stats.firstPullStart or frm)
        item = {"reason": reason, "from": round(frm - base, 1), "to": round(to - base, 1)}
        if detail:
            item["detail"] = detail[:80]
        self.stats.recordSegments.append(item)
        self._pull_seg_from = self._pull_seg_to = 0.0

    def recorded_seconds(self) -> float:
        """v4.50：**实际录制秒数**（已结束各轮累计 + 当前这轮已录）。界面「已录」只认它。"""
        s = self.stats
        live = max(0.0, time.time() - s.pullStartedAt) if s.pullStartedAt else 0.0
        return float(getattr(s, "recordedSec", 0.0) or 0.0) + live

    def _request_graceful_exit(self) -> None:
        """让 ffmpeg 优雅退出（写 'q' 到 stdin）。

        直接 terminate 会让最后一个分片的 moov 没写出来 → 文件不可读。
        写 'q' 能让 segment muxer 正常收尾。

        ⚠️ v4.56：**只对"stdin 是交互通道"的进程有效**（gdigrab 抓屏那条）。
        后台录屏（`_spawn_screen_pipe`）的 stdin 是 **image2pipe 数据流**——往里写 'q'
        是往帧流里塞 2 个脏字节，ffmpeg 当数据吃掉，根本不退出。
        那条路的收口在 `_finish_screen_pipe`（关 stdin 触发 EOF → terminate 兜底）。
        """
        if getattr(self, "_pipe_input", False):
            return
        proc = self._proc
        if not proc or proc.poll() is not None or not proc.stdin:
            return
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            pass

    # ---------------------------------------------------------------- 工具

    def _disk_state(self) -> dict:
        """当前磁盘水位与判定（v4.74 P-B）：低水位停机时要**给出数字与阈值**（K69）。

        实现委托模块级 :func:`disk_state_now`（manager.snapshot / 设置端点共用一份，K44）。
        """
        return disk_state_now(ws.root)

    def _run_dir(self, attempt: int) -> Path:
        """每一轮拉流用**独立子目录**。

        不能共用同一个输出 pattern：`-f segment` 每次启动都从 ``%05d`` 的 0 开始编号，
        续录时会**覆盖/混入上一轮的文件**，而「除最后一个都算已关闭」的判定
        正是基于目录内文件列表 —— 一旦混入旧文件，判定就失效了。
        """
        return self.parts_dir / f"run{attempt:03d}"

    def _list_parts(self, run_dir: Path) -> list[Path]:
        """按**分片序号（数字序）**排序。

        v4.74 P-E：字典序在分片号进位时会乱（`100000.ts` 排在 `99999.ts` 前面，
        3 小时档 × 高码率真有可能到 6 位数）⇒ 按 int(stem) 排。非数字名（理论不该有）
        沉到最后，不参与合片顺序。
        """
        def _num(p: Path) -> int:
            try:
                return int(p.stem)
            except ValueError:
                return -1
        return sorted(run_dir.glob(f"[0-9]*.{SEGMENT_EXT}"), key=_num)

    # ---------------------------------------------------------------- 主流程

    async def run(self) -> RecordStats:
        """录制一场直播，返回统计。**不抛异常**，所有失败都记进 stats。"""
        self.stats.startedAt = time.time()
        try:
            info = await self._wait_for_live()
            if not info:
                return self._finish()
            self.stats.owner = info["owner"]
            self.stats.title = info["title"]
            self._pk = bool(info.get("pk"))     # v3.25：PK 是 3:4 的辅助判据
            if self.screen_mode:
                await self._record_loop_screen()
            else:
                await self._record_loop()
            self.stats.merges = getattr(self, "_last_merges", [])
        except asyncio.CancelledError:
            self.stats.stopReason = "任务被取消"
        except Exception as exc:                      # 兜底：不让录制线程带崩服务
            self.stats.stopReason = f"异常终止：{type(exc).__name__}: {exc}"
            self.log(f"[recorder] {self.stats.stopReason}")
        return self._finish()

    def _finish(self) -> RecordStats:
        self.stats.endedAt = time.time()
        return self.stats

    async def _wait_for_live(self) -> dict | None:
        """阶段一：轮询到开播为止（D19：``status==2``）。

        ⚠️ v3.50 修一个**严重事故级**的 bug：原来这里的
        ``await self._sleep_or_probe(self.poll_interval)`` 被缩进在
        ``if info and info["live"]:`` 里面、而且写在 ``return info`` **之后** ——
        是**永远执行不到的死代码**。于是这个循环**一次都不睡**：请求一返回就发下一个，
        约每秒 1~5 次持续敲抖音，直到主播开播为止。
        这正是「抖音要求人机验证（A10）」的真凶 —— 不是"30 秒探测太频繁"，
        而是**压根没有间隔**。教训见 K91：死代码不报错，只在这里安静地刷请求。
        """
        self.log(f"[recorder] 开始监控 {self.web_rid}"
                 f"（每 {self.poll_interval:.0f}s 轮询，档位 {self.quality}）")
        backoff = ProbeBackoff(self.poll_interval)
        self.current_interval = backoff.interval()
        info: dict | None = None
        while not self._stop:
            err: LiveRecorderError | None = None
            try:
                info = await self.client.room_info(self.web_rid)
            except LiveRecorderError as exc:
                info = None
                err = exc
                self.log(f"[recorder] 轮询失败：{exc}")
            self._emit_probe(info)          # v3.35：把「是否在播」递给调度器/界面
            if info and info["live"]:
                self.log(f"[recorder] ★ 开播：{info['owner']} — {info['title']}")
                backoff.live()
                self.current_interval = backoff.interval()
                return info
            if err is not None:
                # v3.63（）：**风控冷却期内不盲目加倍**。
                # 冷却是一个**已知的截止时刻**（douyin.cooldown_left），这期间
                # `_before_request` 连请求都不发 —— 所以"睡很久"没有任何收益，只会让
                # 冷却一结束我们还睡着（原来上限 900s：冷却 15 分钟结束，我们还要再睡
                # 15 分钟才探下一下 ⇒「开播了却迟迟不录」的真凶之一）。
                # 现在：按冷却剩余时间睡（至少 5s，最多 ERROR_SLEEP_CAP_SEC），到点立刻探。
                left = cooldown_left()
                if left > 0:
                    wait = max(5.0, min(left, ERROR_SLEEP_CAP_SEC))
                    self.current_interval = int(wait)
                    self.log(f"[recorder] 风控冷却中，{wait:.0f}s 后再探"
                             f"（冷却还剩 {left:.0f}s）")
                    await self._sleep_or_probe(wait)
                    continue
                wait = min(backoff.error(), ERROR_SLEEP_CAP_SEC)
            else:
                if info:
                    self.log(f"[recorder] 未开播（status={info['status']}）…")
                wait = backoff.offline()
            self.current_interval = wait
            await self._sleep_or_probe(wait)
        return None

    async def _sleep_or_probe(self, seconds: float) -> None:
        """等 ``seconds``，但被 :meth:`request_probe` 打断时立刻返回（v3.45）。

        0.25 秒一跳地查 ``threading.Event``：不阻塞事件循环，也不引入
        「把 asyncio.Event 从别的线程 set 到别的 loop 上」那类难查的坑。
        """
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            if self._probe_now.is_set():
                self._probe_now.clear()
                self.log("[recorder] 收到「刷新状态」请求 → 立刻重探一次")
                return
            await asyncio.sleep(0.25)

    # ---------------------------------------------------------------- v4.47 录屏

    def _screen_profile_dir(self) -> Path:
        """录屏用的独立浏览器配置目录（不与人机验证/登录态搅在一起）。"""
        d = ws.runtime_data / "screen_profile" / self.web_rid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _spawn_screen(self, run_dir: Path, audio_url: str) -> subprocess.Popen:
        """抓**窗口客户区画面** + 直播间**音轨**（画面带弹幕/头像/连线，声音是干净的）。

        v4.47b：改用「EnumWindows 定位窗口 → gdigrab 抓桌面 ClientRect 区域」。
        抓到的客户区通常就是窗口在屏幕上的物理像素（如 540x960），统一
        ``scale=1080:1920`` 放大到手机竖屏尺寸（**原样 9:16**，不裁不改成 3:4）。
        """
        rect = screen_window_rect(f"{SCREEN_TITLE_PREFIX}{self.web_rid}")
        out = str(run_dir / f"%05d.{SEGMENT_EXT}")
        seg = str(int(self.segment_seconds if self.segment_seconds > 0 else NO_SPLIT_SECONDS))
        if rect:
            grab = ["-f", "gdigrab", "-draw_mouse", "0", "-framerate", "30",
                    "-offset_x", str(rect["x"]), "-offset_y", str(rect["y"]),
                    "-video_size", f'{rect["w"]}x{rect["h"]}', "-i", "desktop"]
        else:
            grab = ["-f", "gdigrab", "-draw_mouse", "0", "-framerate", "30",
                    "-i", f"title={SCREEN_TITLE_PREFIX}{self.web_rid}"]
        cmd = [
            str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "info", "-nostats",
            *grab,
            # 声音：直接复用直播流（避免系统回环设备依赖）
            "-i", audio_url,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=1080:1920:flags=bicubic",       # 原样 9:16，只放大不改画幅
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-f", "segment", "-segment_time", seg,
            "-segment_format", SEGMENT_FORMAT, "-reset_timestamps", "1", out,
        ]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=ws.subprocess_env(),
                                creationflags=CREATE_NO_WINDOW)

    def _spawn_screen_pipe(self, run_dir: Path, audio_url: str) -> subprocess.Popen:
        """后台录屏的 ffmpeg：画面从 **stdin 管道**喂 JPEG 帧，声音取直播流。

        ``-use_wallclock_as_timestamps 1`` + ``-fps_mode vfr``：帧到达时间就是真实时间，
        CDP 截帧的帧率会随画面变化上下浮动，用墙钟时间戳才不会变速/丢时长。
        """
        out = str(run_dir / f"%05d.{SEGMENT_EXT}")
        seg = str(int(self.segment_seconds if self.segment_seconds > 0 else NO_SPLIT_SECONDS))
        cmd = [
            str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "image2pipe", "-framerate", "15", "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
            "-i", audio_url,
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=1080:1920:flags=bicubic",       # 原样 9:16 放大到竖屏尺寸
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-r", "15", "-fps_mode", "cfr",
            "-c:a", "aac", "-b:a", "160k",
            # v4.56：**必须 -shortest** —— 收口靠「关 stdin（视频 EOF）」；
            # 音频是直播流（不会自己结束），没有 -shortest 的话 ffmpeg 会在视频结束后
            # **继续只录音频**（segment 还会继续切纯音频段，进程永不退出）。
            "-shortest",
            "-f", "segment", "-segment_time", seg,
            "-segment_format", SEGMENT_FORMAT, "-reset_timestamps", "1", out,
        ]
        # v4.56：标记「stdin 是数据流」—— stop() 的优雅退出对它无效（见 _request_graceful_exit）
        self._pipe_input = True
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=ws.subprocess_env(),
                                creationflags=CREATE_NO_WINDOW)

    def _finish_screen_pipe(self, proc: subprocess.Popen,
                            q: "queue.Queue[bytes | None]",
                            wt: threading.Thread) -> None:
        """v4.56：后台录屏 ffmpeg 的**收口**（修复「停止后没有产物进录制库」）。

        停止时旧代码只 `q.put(None)` 让 writer 线程退出——**stdin 一直开着**：
        image2pipe 读不到 EOF，ffmpeg 就永远等下一帧 → 最后一个 ts 永远不封口 →
        进程变成孤儿继续写盘（实测一次挂了 7 分钟，直到直播流自己断）；
        而 `assembler.close(force=True)` 只合并槽位里的分片，槽位空的/不完整的
        都不会产出 ⇒ 界面上「已录 0:46」却什么都没进录制库。

        收口顺序（每一步都有理由）：
          ① `q.put(None)` → writer 把队列里**剩余的帧**写完再退出（不能先关 stdin，
             否则排队中的帧丢在管道里）；
          ② `proc.stdin.close()` → image2pipe 读到 **EOF**，视频输入正式结束；
          ③ `proc.wait(timeout)`：视频已结束但**音频还是直播流**，没设 -shortest 的
             ffmpeg 会继续只录音频 → 等不住就 terminate。
             MPEG-TS 没有 moov/trailer（K25），被 terminate 的 ts 照样可读，
             时长就是已写数据 —— 这是有意选择，不是偷懒。
        """
        try:
            q.put_nowait(None)
        except Exception:                                  # noqa: BLE001  队列满了说明帧还没排空
            pass
        if wt is not None:
            wt.join(timeout=5.0)
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()                         # EOF：视频输入结束
        except Exception:                                  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:                  # 音频流还在推 → 直接收口（TS 可杀，K25）
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:                              # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                          # noqa: BLE001
                    pass

    async def _record_loop_screen(self) -> None:
        """v4.47~49 录屏模式**总调度**：先试后台（屏幕外窗口 + 定时截图），按开关决定是否回退窗口抓屏。

        ⚠️ 这个函数是 `run()` 唯一的录屏入口 —— v4.49 改后台实现时整块替换把它一起删过一次，
        真机表现为「开播即异常终止：'LiveRecorder' object has no attribute '_record_loop_screen'」。
        冒烟里已有断言守着（见 smoke_recorder_state 的三方法齐备检查）。
        """
        assembler = HourAssembler(
            final_dir=self.final_dir,
            anchor=self.stats.owner or self.web_rid,
            segment_seconds=self.segment_seconds,
            log=self.log,
            on_hour_file=self._emit_hour_file,
            on_segment=self._emit_segment,
        )
        self._set_aspect("screen")
        assembler.aspect = "screen"
        self.stats.quality = "screen"

        try:
            ok = await self._record_loop_screen_bg(assembler)
        except Exception as exc:                       # noqa: BLE001  后台失败绝不带崩录制
            ok = False
            self.log(f"[recorder] 后台录屏异常（{type(exc).__name__}: {exc}）→ 回退窗口抓屏")
        if not ok and not self._stop:
            if SCREEN_ALLOW_WINDOW_FALLBACK:
                self.log("[recorder] 后台录屏没拿到画面 → 按 REC_SCREEN_ALLOW_WINDOW=1 "
                         "回退**窗口抓屏**（窗口会置顶，别遮挡）")
                await self._record_loop_screen_window(assembler)
            else:
                # v4.49：默认**不弹窗**——用户要的是后台录；如实结束并说明原因。
                self.log("[recorder] 后台录屏没拿到画面；已停止（要排障可设 "
                         "REC_SCREEN_ALLOW_WINDOW=1 用窗口抓屏对比）")
                if not self.stats.stopReason:
                    self.stats.stopReason = "录屏页面没有画面"

        self.stats.aspectSwitchBlocked = self._guard.blocked
        # v4.75 P-C：兜底 push 残留段（没走到任何分支就退出的，比如下播前最后一次确认）
        self._push_pull_segment("stop", "录制流程结束")
        self._write_record_reports(assembler)
        if self._stop:
            self.log("[recorder] 录制已停止；分片正在后台合片（大文件要等一会）")
            self._finalize_detached(assembler)
        else:
            await assembler.close_async(force=True, aspect=self.aspect)
        self._last_merges = assembler.merges
        self.stats.aspects = assembler.aspects

    async def _record_loop_screen_bg(self, assembler: "HourAssembler") -> bool:
        """v4.49 后台录屏：**屏幕外窗口 + 定时截图**（用户看不见，截帧不依赖窗口可见性）。

        实测（2026-11-06 真机）：
          · `headless=True` ⇒ 抖音播放器不起播；
          · CDP `Page.startScreencast` ⇒ 只出 1~2 帧（视频叠加层不产生 damage）；
          · 窗口放**屏幕外** + `page.screenshot` 轮询 ⇒ 稳定出帧（假页面 6.3s / 72 帧）。
        因此：窗口开到屏幕外、关闭遮挡节流，按 ~12fps 主动截图喂给 ffmpeg。
        返回 True = 正常走完；False = 拿不到画面（如实结束，不弹窗）。
        """
        from playwright.async_api import async_playwright

        vw, vh = SCREEN_SHOT_VIEWPORT
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(self._screen_profile_dir()),
                channel="msedge",
                headless=False,                        # 需要真渲染（headless 不起播）
                viewport={"width": vw, "height": vh},
                device_scale_factor=1,
                args=[f"--window-position={SCREEN_OFFSCREEN_X},0",
                      f"--window-size={vw + 20},{vh + 80}",
                      "--autoplay-policy=no-user-gesture-required",
                      "--mute-audio",
                      "--disable-backgrounding-occluded-windows",
                      "--disable-renderer-backgrounding",
                      "--disable-background-timer-throttling",
                      "--disable-features=CalculateNativeWinOcclusion"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await ctx.add_init_script(script=SCREEN_BLOCK_JS)
                self.log("[recorder] 录屏：正在打开直播间页面（窗口在屏幕外，不会显示）…")
                await page.goto(f"https://live.douyin.com/{self.web_rid}",
                                wait_until="domcontentloaded", timeout=60000)
                await page.add_style_tag(content=GIFT_BLOCK_CSS)
                # 等播放器起播（最多 15s；起不来也照样开录，画面先录加载页，不空等）
                ready = False
                for _ in range(30):
                    try:
                        ready = bool(await page.evaluate(
                            "() => { const v = document.querySelector('video');"
                            "return !!v && v.readyState >= 2 && v.videoWidth > 0; }"))
                    except Exception:                  # noqa: BLE001
                        ready = False
                    if ready:
                        break
                    await page.wait_for_timeout(500)
                self.log("[recorder] 录屏：播放器" + ("已起播，开始录制" if ready
                                                     else "未确认起播（先开录，边录边等）"))

                attempt = 0
                empty = 0
                first = True
                while not self._stop:
                    try:
                        urls = await self.client.stream_urls(self.web_rid)
                    except LiveRecorderError as exc:
                        self.log(f"[recorder] 录屏：解析流失败（{exc}），稍后重试")
                        await self._sleep_or_probe(self.poll_interval)
                        continue
                    if not urls:
                        self.log("[recorder] 直播间已下播，结束录屏")
                        self.stats.stopReason = "主播下播"
                        break
                    _, audio_url = self.client.pick(urls, self.quality)
                    attempt += 1
                    self.stats.attempts = attempt
                    run_dir = self._run_dir(attempt)
                    run_dir.mkdir(parents=True, exist_ok=True)
                    proc = self._spawn_screen_pipe(run_dir, audio_url)
                    self._proc = proc
                    # v4.56：stderr **必须有人排干** —— `_StderrWatcher` 的注释原话：
                    # 「stderr=PIPE 却没人读，日志一多就能把管道写满、把 ffmpeg 顶死」。
                    # v4.49 的后台循环漏了这一条 ⇒ ffmpeg 随时可能被自己的日志卡死。
                    # 这里只排干不处理事件（录屏画幅固定 screen，没有切画幅一说）。
                    _StderrWatcher(proc, queue.Queue()).start()
                    self._pull_begin()              # v4.50：录屏这轮开始记时
                    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=8)

                    def _writer(proc=proc, q=q) -> None:
                        while True:
                            item = q.get()
                            if item is None:
                                break
                            try:
                                proc.stdin.write(item)
                            except Exception:          # noqa: BLE001  管道断了就收手
                                return

                    wt = threading.Thread(target=_writer, daemon=True)
                    wt.start()
                    got = 0
                    interval = 1.0 / SCREEN_SHOT_FPS
                    nxt = time.monotonic()
                    # v4.56：**分片收割**（后台路径原本完全没接 assembler ——
                    # slot 一直空 ⇒ 停止后 close(force=True) 无东西可合 ⇒ 录制库永远没有文件）。
                    # 规则与 `_watch.harvest` 一致：除「正在写的最后一个」外都算已关闭；
                    # 最后一个在收口（_finish_screen_pipe）之后补收。
                    seen_parts: set[str] = set()
                    last_harvest = 0.0
                    while proc.poll() is None and not self._stop:
                        try:
                            jpg = await page.screenshot(type="jpeg",
                                                        quality=SCREEN_BG_JPEG_QUALITY,
                                                        animations="allow")
                            if jpg:
                                try:
                                    q.put_nowait(jpg)
                                    got += 1
                                except queue.Full:
                                    pass                    # 编码跟不上就丢帧，绝不阻塞
                        except Exception:              # noqa: BLE001  截图偶发失败不算致命
                            pass
                        if got == 1 and first:
                            first = False
                            self.log("[recorder] 录屏：已开始录制（后台，窗口不可见）")
                        nxt += interval
                        delay = nxt - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        else:
                            nxt = time.monotonic()
                        # v4.56：收割「已关闭」的分片（segment_time 到点切出下一段后，
                        # 上一段才算关）。满一小时也在这里触发封存 ——
                        # 不然录屏模式下小时切片永远不会落库。
                        if time.monotonic() - last_harvest >= 0.5:
                            last_harvest = time.monotonic()
                            parts = self._list_parts(run_dir)
                            if len(parts) > 1:
                                for path in parts[:-1]:
                                    if path.name in seen_parts:
                                        continue
                                    seen_parts.add(path.name)
                                    if await asyncio.to_thread(self._accept, path):
                                        await assembler.add_async(path)
                                if assembler.take_hour_due():
                                    await assembler.close_async()
                    self._pull_end()                # v4.50：录屏这轮结束，累计时长
                    self._proc = None
                    # v4.56：先收口 ffmpeg（封口 ts、进程退出），**再**补收最后的分片 ——
                    # 顺序反了就会把「还在写的 ts」拿去合片（ffprobe 半路读，结果不可靠）。
                    self._finish_screen_pipe(proc, q, wt)
                    for path in self._list_parts(run_dir):
                        if path.name in seen_parts:
                            continue
                        seen_parts.add(path.name)
                        if await asyncio.to_thread(self._accept, path):
                            await assembler.add_async(path)
                        else:
                            self.log(f"[recorder] 分片不可读，已丢弃：{path.name}")
                    self.stats.segments = len(seen_parts)
                    if self._stop:
                        break
                    empty = empty + 1 if got == 0 else 0
                    if empty >= SCREEN_MAX_EMPTY_ATTEMPTS:
                        self.log("[recorder] 录屏：连续多次截不到画面 → 停止"
                                 "（直播间需登录？被风控？）")
                        if not self.stats.stopReason:
                            self.stats.stopReason = "录屏页面没有画面"
                        # 最后一次尝试仍失败 ⇒ 明确告诉调用方"没画面"
                        await assembler.close_async(force=True, aspect=self.aspect)
                        return False
                    await asyncio.sleep(min(5.0, self.poll_interval))
                return True
            finally:
                try:
                    await ctx.close()
                except Exception:                      # noqa: BLE001
                    pass

    async def _record_loop_screen_window(self, assembler: "HourAssembler") -> None:
        """v4.47 兜底：开一个手机比例的直播间窗口 → 注入屏蔽脚本 → 抓窗口客户区。

        · 画面：整屏（含主播头像、弹幕流动、连线人头像），**原样 9:16 不强制 3:4**；
        · 送礼特效：CSS + MutationObserver 双保险屏蔽；
        · 声音：直播流音轨；中断：ffmpeg 退出就重启一轮（L1 同款）。
        """
        from playwright.async_api import async_playwright

        self.log("[recorder] 录屏模式：整屏录制（含弹幕/头像，屏蔽送礼特效，原样比例）")

        attempt = 0
        empty = 0
        vw, vh = screen_fit_viewport()
        self.log(f"[recorder] 录屏窗口 {vw}x{vh}（9:16，原样比例；屏幕工作区自适应）")
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(self._screen_profile_dir()),
                channel="msedge",
                headless=False,
                viewport={"width": vw, "height": vh},
                device_scale_factor=2,
                args=["--window-position=0,0",
                      f"--window-size={vw + 16},{vh + 90}",
                      "--autoplay-policy=no-user-gesture-required"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await ctx.add_init_script(script=SCREEN_BLOCK_JS)
                await page.goto(f"https://live.douyin.com/{self.web_rid}",
                                wait_until="domcontentloaded", timeout=60000)
                await page.add_style_tag(content=GIFT_BLOCK_CSS)
                await page.evaluate(
                    "t => { document.title = t; }",
                    f"{SCREEN_TITLE_PREFIX}{self.web_rid}")
                await page.wait_for_timeout(4000)          # 等播放器起播
                while not self._stop:
                    try:
                        urls = await self.client.stream_urls(self.web_rid)
                    except LiveRecorderError as exc:
                        self.log(f"[recorder] 录屏：解析流失败（{exc}），稍后重试")
                        await self._sleep_or_probe(self.poll_interval)
                        continue
                    if not urls:
                        self.log("[recorder] 直播间已下播，结束录屏")
                        self.stats.stopReason = "主播下播"
                        break
                    _, audio_url = self.client.pick(urls, self.quality)
                    attempt += 1
                    self.stats.attempts = attempt
                    run_dir = self._run_dir(attempt)
                    run_dir.mkdir(parents=True, exist_ok=True)
                    proc = self._spawn_screen(run_dir, audio_url)
                    self._proc = proc
                    self._pull_begin()              # v4.50：窗口录屏这轮开始记时
                    n = await self._watch(proc, assembler, run_dir, "")
                    self._pull_end()                # v4.50：累计实际录制时长
                    # v4.56：停止/退出时 ffmpeg 可能还活着（_watch 的循环随 _stop 退出）。
                    # 不收口的话最后一片 ts 永远「正在写」、不会被收割 ⇒ 录制库少文件。
                    # gdigrab 这条 stdin 是交互通道，写 'q' 有效（与后台路径不同）。
                    self._request_graceful_exit()
                    try:
                        proc.wait(timeout=5.0)
                    except Exception:              # noqa: BLE001
                        try:
                            proc.terminate()
                        except Exception:          # noqa: BLE001
                            pass
                    self._proc = None
                    # v4.56：补收最后一片（_watch 的 harvest 不收「正在写的最后一个」，
                    # 退出后就再也没人收了）。ffmpeg 已退出 ⇒ 文件已封口，ffprobe 可读。
                    for path in self._list_parts(run_dir):
                        if path not in assembler.slot:
                            if await asyncio.to_thread(self._accept, path):
                                await assembler.add_async(path)
                    self.stats.segments += n
                    if self._stop:
                        break
                    empty = empty + 1 if n == 0 else 0
                    if empty >= SCREEN_MAX_EMPTY_ATTEMPTS:
                        self.log("[recorder] 录屏连续多次抓不到画面（窗口被遮挡/最小化？）→ 停止")
                        self.stats.stopReason = "录屏抓不到画面"
                        break
                    await asyncio.sleep(min(5.0, self.poll_interval))
            finally:
                try:
                    await ctx.close()
                except Exception:                      # noqa: BLE001
                    pass

    async def _record_loop(self) -> None:
        """阶段二：拉流录制。中断自动续录（L1），分片交给 :class:`HourAssembler`。"""
        assembler = HourAssembler(
            final_dir=self.final_dir,
            anchor=self.stats.owner or self.web_rid,
            segment_seconds=self.segment_seconds,
            log=self.log,
            on_hour_file=self._emit_hour_file,
            on_segment=self._emit_segment,
        )

        attempt = 0
        # v3.57（K99）：解析出来的流地址在有效期内**复用**。
        # 以前每重启一次 ffmpeg 都要重新解析一次房间流地址 —— 画幅切一次就多敲一次
        # 抖音的房间接口，一场切 20 次 = 20 次请求，这正是人机验证（A10）的风险源。
        # 只有在「没解析过 / 缓存过期 / 上一次是真失败」时才重新问一遍。
        cached_url = ""
        cached_quality = self.quality
        cached_at = 0.0

        while not self._stop:
            disk_st = self._disk_state()
            if disk_st["low"]:
                why = (f"磁盘剩余 {disk_st['freeGb']:.0f} GB（{disk_st['freePct']:.1f}%）"
                       f"低于阈值（{disk_st['limitGb']:.0f} GB 或 {disk_st['limitPct']:.0f}%）")
                self.log(f"[recorder] {why}，停止录制（A6）")
                self.stats.stopReason = f"{why}，停止录制（A6）"
                break

            if not cached_url or (time.time() - cached_at) >= STREAM_URL_TTL_SEC:
                try:
                    urls = await self.client.stream_urls(self.web_rid)
                    if not urls:
                        self.log("[recorder] 直播间已下播，结束录制")
                        self.stats.stopReason = "主播下播"
                        break
                    cached_quality, cached_url = self.client.pick(urls, self.quality)
                    cached_at = time.time()
                except LiveRecorderError as exc:
                    self.log(f"[recorder] 解析流失败：{exc}，{self.poll_interval:.0f}s 后重试")
                    await asyncio.sleep(self.poll_interval)
                    continue
            quality, url = cached_quality, cached_url

            attempt += 1
            self.stats.attempts = attempt
            self.stats.quality = quality

            # v3.25：开录/续录前先探一次源画幅，按它决定输出画幅与编码方式
            planned, size = await self._plan_aspect(url)
            new_aspect = planned.get("aspect") or self.aspect
            # 续录期间画幅也变了（进程挂了的空档里主播改了版式）→ 先把旧画幅那段封存
            if (self.aspect != UNKNOWN and new_aspect != UNKNOWN
                    and new_aspect != self.aspect and assembler.slot):
                await self._close_segment(assembler, self.aspect,
                                          f"续录时发现画幅 {self.aspect} → {new_aspect}")
                self.stats.aspectSwitches += 1
            self._set_aspect(new_aspect)
            self.aspect_plan = planned
            assembler.aspect = new_aspect
            self.log(f"[recorder] 第 {attempt} 次拉流（档位 {quality}）｜画幅 "
                     f"{self.aspect}（{size[0]}x{size[1]}，"
                     f"{'重编码 ' + planned['encode'] if planned.get('vf') else '原样 copy'}）")

            run_dir = self._run_dir(attempt)
            run_dir.mkdir(parents=True, exist_ok=True)
            proc = self._spawn(url, run_dir, planned)
            self._proc = proc
            self._pull_begin()                      # v4.50：这一轮真的开始录了
            new_segments = await self._watch(proc, assembler, run_dir, url)
            self._pull_end()                        # v4.50：累计实际录制时长
            self._proc = None
            self.stats.segments += new_segments

            if self._stop:
                self._push_pull_segment("stop")
                break

            # v3.25：画幅切换导致的主动重启 —— 上一段已封存，按新画幅继续
            # v3.77：本轮是被画面健康检查叫停的 ⇒ 处理完这一段的去留，重新拉流继续录
            if self._health_pending:
                self._health_pending = False
                part = self._health_part
                self._health_part = None
                self._maybe_drop_bad_part(assembler, part)
                self._push_pull_segment("health", "画面异常已处置，重新拉流")
                await asyncio.sleep(HEALTH_RESTART_SLEEP_SEC)
                self.log("[recorder] 重新拉流继续录制（画面异常已处置）")
                continue

            if self._switch_pending:
                self._switch_pending = False
                old = self.aspect
                target = self._pending_aspect or old
                self._push_pull_segment("aspect", f"画幅 {old} → {target}")
                await self._close_segment(assembler, old, f"画幅 {old} → {target}")
                self._set_aspect(target)
                assembler.aspect = target
                self.stats.aspectSwitches += 1
                self.log(f"[recorder] 已封存 {old} 段，按 {self.aspect} 继续录制")
                continue

            rc = proc.returncode
            if rc == 0:
                self._push_pull_segment("stop", "ffmpeg 正常结束")
                self.log("[recorder] ffmpeg 正常结束")
                self.stats.stopReason = self.stats.stopReason or "ffmpeg 正常结束"
                break

            # L1：确认还在播才续录，否则判定下播。
            # 真失败 → 作废地址缓存（复用的地址可能过期了），下一轮重新解析。
            cached_url = ""
            self.stats.interruptions += 1
            try:
                probe = await self.client.room_info(self.web_rid)
                self._emit_probe(probe)      # v3.35：续录前这次探测也上报（下播要立刻反映到界面）
                if not probe["live"]:
                    self._push_pull_segment("stop", "确认已下播")
                    self.log("[recorder] 确认已下播，结束录制")
                    self.stats.stopReason = "主播下播"
                    break
            except LiveRecorderError as exc:
                self.log(f"[recorder] 续录前校验失败：{exc}")
            tail = getattr(self, "_last_ffmpeg_tail", None)
            if tail:
                self.log("[recorder] ffmpeg 最后几行输出：" + " | ".join(tail[-5:])[:400])
            self.log(f"[recorder] ffmpeg 退出 rc={rc}，流中断 → 续录（L1）")
            self._push_pull_segment("crash", f"ffmpeg 退出 rc={rc}，续录前等待 ≤15s")
            # v4.74 P-A（方案优化项 A）：把这次尝试的「撑了多久 / 有没有真拉到流」
            # 喂给退避器记账 —— 拉起就崩（秒退）与断流（抖动）从此分开计数，
            # 秒退连击会让下一次等开播探测更快翻倍（普通失败序列不变）。
            elapsed = max(0.0, time.time() - float(self.stats.pullStartedAt or 0.0))
            got_stream = bool(self._list_parts(self._run_dir(attempt))) or                 elapsed >= self.backoff.FAST_FAIL_SEC
            self.backoff.attempt(elapsed, got_stream)
            # v4.74 P-D（方案《录制模块优化方案》优化项 D）：L1 续录前的等待**封顶 15 秒，
            # 不跟随 poll_interval 退避**。理由（对照 bililive-go 的
            # maxRecordRetryIntervalAfterHold=15s，只取设计不抄代码）：
            # 从没录上过 ⇒ 源此刻根本不可用，慢慢试（本来也没在丢内容），等开播阶段
            #     自己有退避封顶 60s；
            # 已经录上过 ⇒ 优先保断流后的内容连续性，只封顶 15s。
            # 把这个 15 改成 poll_interval（30-60s）会让每次抖动多丢 15-45 秒内容。
            await asyncio.sleep(min(self.poll_interval, 15))

        # v4.29：收尾合片。**手动停止**时把合片丢给后台线程 —— 录制任务立刻结束，
        # 卡片从「停止中」秒级回到「已结束」，界面与其它操作不再被合片拖住；
        # 其它结束（下播/正常结束）仍在任务内异步合片（状态语义与以前一致）。
        self.stats.aspectSwitchBlocked = self._guard.blocked
        if self._stop:
            self.log("[recorder] 录制已停止；分片正在后台合片，"
                     "完成后自动进录制库（大文件要等几分钟）")
            self._finalize_detached(assembler)
            self.stats.aspects = list(assembler.aspects)
        else:
            await assembler.close_async(force=True, aspect=self.aspect)
            self._last_merges = assembler.merges
            self.stats.aspects = assembler.aspects

    def build_record_report(self) -> dict:
        """**v4.75 P-C：录制报告**（方案《录制模块优化方案》优化项 C）。

        回答"这场录得好不好"：切了几段、每次为什么切、L1 重连真实丢了多少内容
        （gapSec/gapCount —— **切片据此剔除跨断层的候选**）、合片成败、画幅切换次数。

        时间语义：``recordSegments`` / ``gaps`` 的 from/to 都是**相对秒**
        （相对全场第一个拉流起点 ``firstPullStart``）—— 与切片候选的时间轴同源，
        读取方可以直接比对区间。
        """
        s = self.stats
        merged = [m for m in (s.merges or []) if m.get("ok")]
        unmerged = [m for m in (s.merges or []) if not m.get("ok")]
        segs = list(s.recordSegments or [])
        # 残留段兜底（分支 push 之外还有没 push 的）
        frm = float(getattr(self, "_pull_seg_from", 0.0) or 0.0)
        to = float(getattr(self, "_pull_seg_to", 0.0) or 0.0)
        if frm > 0 and to > frm:
            base = float(s.firstPullStart or frm)
            segs.append({"reason": "stop", "from": round(frm - base, 1), "to": round(to - base, 1)})
        segs.sort(key=lambda x: float(x.get("from") or 0))
        st = disk_state_now(ws.root)
        import datetime as _dt
        # ISO 时间用 local tz；startedAt/endedAt = 0（stub/未结束）时不生成，避免 OSError
        try:
            started = (_dt.datetime.fromtimestamp(s.startedAt).astimezone()
                       .isoformat(timespec="seconds") if s.startedAt else "")
        except (OSError, OverflowError, ValueError):
            started = ""
        try:
            ended = (_dt.datetime.fromtimestamp(s.endedAt).astimezone()
                     .isoformat(timespec="seconds") if s.endedAt else "")
        except (OSError, OverflowError, ValueError):
            ended = ""
        return {
            "webRid": s.webRid,
            "owner": s.owner,
            "startedAt": started,
            "endedAt": ended,
            "totalSec": round(float(getattr(self, "recorded_seconds", lambda: 0.0)()), 1),
            "segments": segs,
            "gaps": list(s.gaps or []),
            "gapSec": round(float(s.gapSec or 0.0), 1),
            "gapCount": int(s.gapCount or 0),
            "hours": [{"file": Path(m.get("output") or "").name,
                       "ok": bool(m.get("ok")), "startAt": m.get("startAt"),
                       "endAt": m.get("endAt"), "durationSec": m.get("mergedDurationSec"),
                       "aspect": m.get("aspect")}
                      for m in (s.merges or []) if (m.get("output") or m.get("reason"))],
            "merged": len(merged),
            "unmerged": len(unmerged),
            "aspectSwitches": int(s.aspectSwitches or 0),
            "healthRestarts": int(s.healthRestarts or 0),
            "diskFreeGbAtEnd": st.get("freeGb", 0.0),
            "generatedAt": round(time.time(), 2),
        }

    def _write_record_reports(self, assembler: "HourAssembler") -> None:
        """把录制报告写到**每个 hour 文件旁**（`<hour名>.record.json`，live_raw 目录）。

        与 `<素材>.note.json` 同模式（跟素材走）；同一场多个 hour 文件写多份相同内容 ——
        切片按 videoName（=hour 文件名）任取一份即可命中。写失败只记日志（不影响录制）。
        """
        try:
            report = self.build_record_report()
        except Exception as exc:                           # noqa: BLE001
            self.log(f"[recorder] 录制报告组装失败（不影响录制）：{type(exc).__name__}: {exc}")
            return
        self.stats.recordReport = report
        for out in self.stats.outputFiles:
            try:
                p = Path(out)
                p.with_name(p.stem + ".record.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:                       # noqa: BLE001
                # 写盘失败**绝不能弄死后台合片线程**（否则 closed 不 set、完成日志缺失，
                # smoke_recorder_stop 的"产出照常登记"就会假失败——v4.75 实测）。
                self.log(f"[recorder] 录制报告写盘失败（不影响录制）：{type(exc).__name__}: {exc}")

    def _finalize_detached(self, assembler: "HourAssembler") -> None:
        """手动停止后的收尾合片**丢后台线程**（v4.29）。

        为什么：合片要跑 ffprobe 三点采样 + ffmpeg remux，大分片是分钟级操作；
        以前它同步占着录制任务/事件循环，用户点完停止还要对着「停止中」干等。
        现在录制任务立刻结束（卡片秒级回「已结束」），合片在后台完成：
        产出照常走 ``on_hour_file`` → 进录制库、交自动化流水线，
        完成后把统计补回 stats 并写一行卡片日志（K69：不能悄悄做完不说）。
        线程用 daemon：服务退出时跟进程一起走（此时 ffmpeg 也活不成，见 stop()）。
        """
        def _work() -> None:
            t0 = time.time()
            try:
                assembler.close(force=True, aspect=self.aspect)
            except Exception as exc:               # noqa: BLE001  收尾绝不带崩服务
                self.log(f"[recorder] 停止后的后台合片失败：{type(exc).__name__}: {exc}")
                return
            self._last_merges = assembler.merges
            self.stats.aspects = assembler.aspects
            self.stats.merges = list(assembler.merges)[-3:]
            # v4.75 P-C：合片完成 ⇒ outputFiles 齐了，**最终版**录制报告落盘
            # （此前 _emit_hour_file 时写过快照，这里覆盖为完整版）
            try:
                self._write_record_reports(assembler)
            except Exception:                      # noqa: BLE001
                pass
            n = sum(1 for m in assembler.merges if m.get("ok"))
            self.log(f"[recorder] 后台合片完成（耗时 {time.time() - t0:.0f}s，"
                     f"产出 {n} 个文件，已进录制库）")

        threading.Thread(target=_work, name=f"finalize-{self.web_rid}",
                         daemon=True).start()

    # ---------------------------------------------------------------- 画幅

    def _set_aspect(self, aspect: str) -> None:
        """改当前画幅的**唯一入口**（别直接赋值 self.aspect，否则会和判定层漂移）。"""
        self.aspect = aspect or UNKNOWN
        self._guard.set_current(self.aspect)
        self._pending_aspect = UNKNOWN

    async def _plan_aspect(self, url: str) -> tuple[dict, tuple[int, int]]:
        """给出本轮拉流的画幅方案 + 源尺寸（必须异步：探测要阻塞 ffprobe 几秒）。

        v3.57：ffmpeg 的 stderr 事件已经把**新尺寸**送到手上了，
        这时再去 ffprobe 一次纯属多发一次请求 —— 直接用事件算好的方案。
        只有没有先验信息时才真的去探（K57：探不到就返回空方案，绝不瞎猜）。
        """
        if self._pending_plan:
            pl = self._pending_plan
            self._pending_plan = {}
            return pl, (int(pl.get("srcWidth") or 0), int(pl.get("srcHeight") or 0))
        size = await asyncio.to_thread(
            probe_stream_size, url, DouyinClient.headers_for(self.web_rid))
        if not size[0] or not size[1]:
            return {}, (0, 0)
        return aspect_plan(size[0], size[1], self.aspect_mode, pk=self._pk), size

    async def _check_aspect(self, url: str) -> None:
        """闸 2：低频 ffprobe 轮询交叉验证（连带做去抖与频率闸门）。

        只在**真的确认变了**时才动作 —— 探不到（网络抖一下）一律不动（K57），
        单次读到不一样也不动（要连续 :data:`PROBE_CONFIRM_TIMES` 次一致），
        否则一场直播会被切成几十个碎片（K56/K92）。
        """
        if self._stop or self._switch_pending or self.aspect_mode == MODE_COPY:
            return
        w, h = await asyncio.to_thread(
            probe_stream_size, url, DouyinClient.headers_for(self.web_rid))
        if not w or not h:
            self._guard.observe(0, 0, pk=self._pk)      # 明确记一次"探不到"
            return
        target = self._guard.observe(w, h, pk=self._pk, channel=CHANNEL_PROBE)
        if self._guard.lastReason:
            self.log(f"[recorder] 画幅轮询：{self._guard.lastReason}")
        if not target:
            return
        self._mark_switch(target, w, h, f"轮询确认 {self.aspect} → {target}（源 {w}x{h}）")

    def _on_size_event(self, width: int, height: int) -> None:
        """闸 1：ffmpeg 自己在 stderr 里报「画面尺寸变了」—— 零额外请求、秒级。

        ⚠️ 这条信号**只在重编码（有滤镜）时才有**：``-c copy`` 不解码，ffmpeg 什么都不报。
        那种情况下新的画面尺寸会一路透进同一个分片，只能靠闸 3（合片前头/中/尾校验）兜。
        """
        if self._stop or self._switch_pending or self.aspect_mode == MODE_COPY:
            return
        target = self._guard.observe(width, height, pk=self._pk, channel=CHANNEL_EVENT)
        if self._guard.lastReason:
            self.log(f"[recorder] ffmpeg 上报分辨率变化：{self._guard.lastReason}")
        if not target:
            return
        self._mark_switch(target, width, height,
                          f"ffmpeg 上报画幅变化 → {target}（源 {width}x{height}）")

    def _mark_switch(self, target: str, width: int, height: int, why: str) -> None:
        """确认了一次切换：存下目标方案，让 ffmpeg 优雅退出，下一轮按新画幅重开。"""
        self._pending_aspect = target
        self._pending_plan = self._guard.plan_for(width, height, pk=self._pk)
        self._pending_plan["srcWidth"] = int(width)
        self._pending_plan["srcHeight"] = int(height)
        self._switch_pending = True
        self.log(f"[recorder] {why} → 封存当前段后按 {target} 续录")
        self._request_graceful_exit()

    def _drain_size_events(self, events: "queue.Queue") -> int:
        """把 stderr 监视线程抓到的事件全部喂给闸 1，返回处理条数。"""
        got = 0
        while True:
            try:
                w, h = events.get_nowait()
            except queue.Empty:
                break
            got += 1
            try:
                self._on_size_event(w, h)
            except Exception as exc:            # 绝不能让解析异常带崩录制
                self.log(f"[recorder] 处理画幅事件异常：{exc}")
        return got

    async def _close_segment(self, assembler: "HourAssembler", aspect: str, why: str) -> None:
        """封存一个画幅段（文件名带画幅后缀）。合片在线程池里跑（v4.29）。"""
        self.log(f"[recorder] 画幅分段：{why}")
        await assembler.close_async(force=True, aspect=aspect)

    def _spawn(self, url: str, run_dir: Path, aspect: dict | None = None) -> subprocess.Popen:
        """起 ffmpeg。**同一个进程内**用 ``-f segment`` 切小时文件（K20）。

        刻意**不加** ``-movflags +faststart``：分片是中间产物，还要再合片一次，
        faststart 的额外一趟重写没有意义，反而让「进程被强杀时 moov 未写出」更常见。
        moov 在分片关闭时正常写出即可，faststart 留给合片阶段（见 :mod:`merge`）。

        v3.25：``aspect`` 是 :func:`app.slicer.aspect.plan` 的结论 ——
        需要变换时**只重编码视频**（``-vf`` + 编码器），音频一律 copy。
        """
        cmd = [
            str(ws.ffmpeg), "-y", "-hide_banner",
            # v3.57：必须是 ``info`` —— 本机 ffmpeg 的"画面尺寸变了"这条日志
            # 只在 info 及以上才出现（-loglevel warning 什么都报不出来，实测）；
            # ``-nostats`` 去掉周期性的进度行（它不是日志，长期录制会一直往 stderr 里灌）。
            # stderr 由 _StderrWatcher 排干。
            "-loglevel", "info", "-nostats",
            # L0 透明重连：网络抖动时 ffmpeg 自己重连，录制不中断
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "30",
            "-reconnect_at_eof", "1",
            "-rw_timeout", "30000000",
            "-headers", DouyinClient.headers_for(self.web_rid),
            "-i", url,
        ]
        vf = (aspect or {}).get("vf") or ""
        if vf:
            # 真重编码到目标画幅（用户 v3.25 选定）：只动视频，音频保持 copy
            cmd += ["-vf", vf]
            cmd += encoder_args((aspect or {}).get("encode") or "x264")
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c", "copy"]
        cmd += [
            "-f", "segment",
            # v3.78：整档（0 秒）送 NO_SPLIT_SECONDS（ffmpeg 不接受 0）
            "-segment_time", str(int(self.segment_seconds
                                     if self.segment_seconds > 0 else NO_SPLIT_SECONDS)),
            # K25：必须用 MPEG-TS。实测 mp4（含 +faststart 与 fragmented 两种写法）
            # 在进程被强杀时 **moov 未写出、文件完全不可读**，中断那一段整段丢失；
            # TS 无需最终化，杀掉也能读出已写入的全部内容（实测 12s 录制恢复 13.28s）。
            "-segment_format", SEGMENT_FORMAT,
            "-reset_timestamps", "1",
            str(run_dir / f"%05d.{SEGMENT_EXT}"),
        ]
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=ws.subprocess_env(),
            creationflags=CREATE_NO_WINDOW,
        )

    # ---------------------------------------------------------------- v3.77 画面健康

    async def _check_health(self, run_dir: Path) -> bool:
        """看一眼**正在写的那个分片**的尾部画面；判到坏（去抖够了）返回 True。

        ⚠️ 只读本地文件，**不发任何网络请求** —— 这条很重要：重启录制会重新解析流地址，
        所以闸门要卡在"真的坏了"上，不能因为检查本身把请求数打上去（K119）。
        """
        if not HEALTH_ENABLED or self._stop:
            return False
        parts = self._list_parts(run_dir)
        if not parts:
            return False                       # 还没落第一个文件（刚起 ffmpeg）
        target = parts[-1]                     # 最后一个 = 正在写的那个
        try:
            if target.stat().st_size < 200_000:
                return False                   # 太短，抽帧没意义
        except OSError:
            return False
        res = await asyncio.to_thread(health.probe, target)
        self.stats.healthChecks += 1
        if res["ok"]:
            self._health_bad = 0
            return False
        self._health_bad += 1
        self.log(f"[recorder] 画面健康检查：{health.describe(res)}"
                 f"（连续第 {self._health_bad}/{HEALTH_CONFIRM_TIMES} 次）")
        if self._health_bad < HEALTH_CONFIRM_TIMES:
            return False
        if not self._health_guard.allow():
            # 窗口内已经重启够多次了 ⇒ 宁可继续录这段有毛病的画面，也别和坏流无休止地对着干
            self._health_bad = 0
            self.stats.healthBlocked += 1
            self.log(f"[recorder] 画面异常，但频率闸门已触发（{HEALTH_RATE_WINDOW_SEC / 60:.0f} 分钟"
                     f"内已重启 {HEALTH_RATE_MAX} 次）⇒ 暂不重启，"
                     f"冷静 {HEALTH_RATE_COOLDOWN_SEC / 60:.0f} 分钟")
            return False
        self._health_guard.note()
        self._health_bad = 0
        self._health_part = target
        self._health_pending = True
        reason = "、".join(res["reasons"])
        self.stats.healthRestarts += 1
        self.stats.healthEvents.append({
            "at": round(time.time(), 1),
            "reason": reason,
            "detail": health.describe(res),
            "part": target.name,
        })
        self.log(f"[recorder] ★ 画面异常（{reason}）⇒ 立刻停止这一段，稍后重新拉流继续录制")
        return True

    async def _health_stop(self, proc: subprocess.Popen) -> None:
        """优雅收尾 ffmpeg（让当前分片正常关闭），必要时兜底强杀。"""
        self._request_graceful_exit()
        deadline = time.monotonic() + HEALTH_EXIT_WAIT_SEC
        while proc.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if proc.poll() is None:
            self.log("[recorder] 画面异常：ffmpeg 没在预期内退出，直接终止"
                     "（TS 分片杀进程也能读，见 K25）")
            try:
                proc.terminate()
            except OSError:
                pass

    def _maybe_drop_bad_part(self, assembler: "HourAssembler", part: Path | None) -> None:
        """出事的那一段**本身就很短**时把它从合片里撤回来（刚拉流就坏，留着没价值）。

        长于一小时段的**不撤** —— 一个小时里坏几秒，整段丢掉是更大的损失；
        那种情况只记在 ``stats.healthEvents`` 与日志里，让用户知道哪一段哪一刻有问题。
        """
        if part is None:
            return
        dur = probe_duration(part)
        if dur <= 0 or dur > HEALTH_DROP_MAX_SEC:
            self.log(f"[recorder] 异常分片 {part.name}（{dur:.1f}s）保留"
                     f"（超过 {HEALTH_DROP_MAX_SEC:.0f}s，整段丢弃损失更大）")
            return
        if assembler.discard(part, f"画面异常且整段仅 {dur:.1f}s"):
            self._drop(part, f"画面异常（整段 {dur:.1f}s 都不可用）")

    async def _hard_stop(self, proc: subprocess.Popen) -> None:
        """优雅退出没等到 → 强制终止（v4.29）。

        TS 分片被杀也能读出已写入的内容（K25），代价只是**正在写的**最后一段
        可能少一两秒 —— 比让用户对着「停止中」干等一分钟好得多。
        """
        try:
            proc.terminate()
        except OSError:
            pass
        for _ in range(15):                    # terminate 后最多再等 3 秒
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.2)
        self.log("[recorder] terminate 也没退，直接 kill")
        try:
            proc.kill()
        except OSError:
            pass
        for _ in range(10):
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.2)

    async def _watch(self, proc: subprocess.Popen, assembler: "HourAssembler",
                     run_dir: Path, url: str = "") -> int:
        """盯 ffmpeg：把「已关闭且可读」的分片交给 assembler，返回本轮新增分片数。

        顺带做两件事：
        - 每 ``aspect_check_interval`` 秒 ffprobe 一次源画幅（v3.25 闸 2 兜底），
          连续两次一致才承认（去抖），过了频率闸门才真的切；
        - 每 :data:`WATCH_TICK_SEC` 秒检查一次 ffmpeg 的 stderr（v3.57 闸 1 主信号），
          抓到「画面尺寸变了」就秒级切段 —— **零额外网络请求**。

        两条判定规则：
        - **已关闭**（K21/K23）：segment muxer 只在切到下一段时才写完上一段的 moov，
          所以**除当前正在写的最后一个文件外**，其余都已关闭。绝不用定时器去猜段边界。
        - **可读**：进程被强杀时最后一个分片没有 moov，ffprobe 读不出来。
          这类文件必须**丢弃并如实记录**，不能送进合片 —— 否则整个槽位合片失败，
          还会被误判成 K22「编码参数不一致」。

        v4.29：收割（ffprobe）与满段封存（合片）全部走线程池 —— 大 TS 的全盘扫描
        和合片前的三点采样都是**分钟级**操作，以前同步跑在事件循环里，会把整个
        录制调度器（含其它录制器的轮询、用户点停止的响应）一起卡住。
        """
        seen: set[str] = set()
        count = 0

        async def harvest() -> int:
            """把 run_dir 里"已关闭"的新文件收进来（最后一个不算）。"""
            got = 0
            files = self._list_parts(run_dir)
            if len(files) <= 1:
                return 0
            for path in files[:-1]:
                if path.name in seen:
                    continue
                seen.add(path.name)
                if await asyncio.to_thread(self._accept, path):
                    await assembler.add_async(path)
                    got += 1
            return got

        # 闸 1：起一个后台线程盯着 stderr（顺便把管道排干 —— 不排会顶死 ffmpeg）
        events: "queue.Queue" = queue.Queue()
        watcher = _StderrWatcher(proc, events)
        watcher.start()
        last_check = time.time()
        last_health = time.time()
        stop_deadline = 0.0
        try:
            while proc.poll() is None:
                count += await harvest()
                if assembler.take_hour_due():
                    # 满一段就封存：合片在线程池里跑，ffmpeg 继续录下一段，循环不卡
                    await assembler.close_async()
                self._drain_size_events(events)
                if url and time.time() - last_check >= self.aspect_check_interval:
                    last_check = time.time()
                    await self._check_aspect(url)     # 闸 2：内部只在真确认了才动作
                # v3.77：画面健康（纯本地读文件；判到坏 ⇒ 收尾 ffmpeg 并跳出，交给主循环重启）
                if (HEALTH_ENABLED and not self.screen_mode and not self._switch_pending
                        and time.time() - last_health >= HEALTH_CHECK_SEC):
                    last_health = time.time()
                    if await self._check_health(run_dir):
                        await self._health_stop(proc)
                        break
                # v4.29：用户点了停止 → 给优雅退出一个**有上限**的窗口，超时强杀
                if self._stop:
                    if not stop_deadline:
                        stop_deadline = time.monotonic() + STOP_EXIT_WAIT_SEC
                    elif time.monotonic() >= stop_deadline:
                        self.log(f"[recorder] 停止收尾：优雅退出 {STOP_EXIT_WAIT_SEC:.0f}s "
                                 "没等到 → 直接终止（TS 分片被杀也能读，K25）")
                        await self._hard_stop(proc)
                        break
                await asyncio.sleep(WATCH_TICK_SEC)
        finally:
            # ffmpeg 退出后再把剩下的事件收干净（最后几行往往最有价值）
            self._drain_size_events(events)
            watcher.join(timeout=3.0)
            self._last_ffmpeg_tail = list(watcher.tail)

        # 进程退出后，剩余文件全部视为已关闭
        for path in self._list_parts(run_dir):
            if path.name in seen:
                continue
            seen.add(path.name)
            if await asyncio.to_thread(self._accept, path):
                await assembler.add_async(path)
                count += 1
        if assembler.take_hour_due():
            await assembler.close_async()      # 最后一刀正好满段：先按段封存，余下交给收尾
        return count

    def _accept(self, path: Path) -> bool:
        """分片是否可用：非空 + ffprobe 读得出时长。不可用的直接删掉并记日志。"""
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size < 100_000:
            self._drop(path, f"体积过小（{size}B），疑似空壳")
            return False
        info = probe(path)
        if "error" in info:
            self._drop(path, f"ffprobe 读不出（moov 未写出，多半是进程被强杀）：{info['error']}")
            return False
        if not info.get("duration"):
            self._drop(path, "时长为 0")
            return False
        return True

    def _drop(self, path: Path, why: str) -> None:
        self.log(f"[recorder] 丢弃分片 {path.name}：{why}")
        try:
            path.unlink()
        except OSError:
            pass

    def _emit_segment(self, path: Path) -> None:
        if self.on_segment:
            try:
                self.on_segment(path)
            except Exception as exc:
                self.log(f"[recorder] on_segment 回调异常：{exc}")

    def _emit_hour_file(self, path: Path) -> None:
        self.stats.outputFiles.append(str(path))
        if self.on_hour_file:
            try:
                self.on_hour_file(path)
            except Exception as exc:
                self.log(f"[recorder] on_hour_file 回调异常：{exc}")

    def _emit_probe(self, info: dict | None) -> None:
        """把一次轮询结果递给上层（v3.35：录制器卡片靠它显示「是否正在直播」）。

        ``info`` 为 None 表示这次没探到（网络失败）—— 也要上报，好让界面能显示
        「上次检查失败」而不是一直停在旧状态（宁可显示"不确定"，也不要假装还活着）。
        """
        if not self.on_probe:
            return
        try:
            self.on_probe(info or {})
        except Exception as exc:                 # 回调异常绝不能影响录制
            self.log(f"[recorder] on_probe 回调异常：{exc}")


# ---------------------------------------------------------------- 小时段装配

# v3.58：小时文件名 → 主播名的**唯一解析规则**（命名规则就在下面
# :meth:`HourAssembler.close` —— ``<主播名>_<YYYYmmdd-HHMMSS>[_画幅][_gN].mp4``）。
# 为什么按**时间戳**切而不是按第一个 ``_`` 切：实测主播名里可能带空格/括号/emoji，
# 也可能带下划线（``倪珈_``、``陈伯(全能王)``、``迪士尼在逃公主👸``），按第一个 ``_``
# 切会把名字切坏。取不到就退回整个文件名（宁可显示全名，也不显示「未知」，K69）。
_HOUR_OWNER_RE = re.compile(r"^(?P<owner>.+?)_\d{8}-\d{6}")


def owner_of_hour_file(name: str) -> str:
    """从小时文件名里取主播名（拿不到就退回去掉扩展名的文件名）。"""
    stem = Path(name or "").stem
    m = _HOUR_OWNER_RE.match(stem)
    return (m.group("owner") if m else stem).strip()


class HourAssembler:
    """把分片装配成「小时文件」（D23 第 ② 条 + L2 合片）。

    规则：
    - 分片按顺序累积；当出现一个**满一小时**的分片（时长 ≥ ``segment_seconds × 0.98``）
      时，把当前槽位里的全部分片合并为一个文件落 ``live_raw``，然后开新槽位。
    - 未出现满段（中断/下播）时，``close()`` 会把剩余分片合并为一个文件。
    - 于是：不间断 → 每小时一个文件；中断 → 中断前后的分片**合并在同一个文件里**。
    """

    def __init__(self, final_dir: Path, anchor: str, segment_seconds: float,
                 log=print, on_hour_file=None, on_segment=None) -> None:
        self.final_dir = Path(final_dir)
        self.anchor = re.sub(r'[\\/:*?"<>|]+', "_", anchor or "anchor").strip() or "anchor"
        # v3.78：0 = 整档「全程不中断」（见 SEGMENT_WHOLE_KEY 的说明）
        self.segment_seconds = max(0.0, float(segment_seconds or 0.0))
        self.log = log or (lambda *a, **k: None)
        self.on_hour_file = on_hour_file
        self.on_segment = on_segment
        self.slot: list[Path] = []
        # 每次合片的明细（供 UI 显示「本段曾中断 N 次，缺失约 X 秒」，§5.2 第 4b 条）
        self.merges: list[dict] = []
        # v3.25：每个已封存段的画幅（与 merges 一一对应，供 stats 汇总）
        self.aspects: list[str] = []
        self.aspect: str = ""      # 当前槽位画幅（由 LiveRecorder 同步进来）
        # v3.57（坑 2）：每个分片的**精确时间区间**（墙钟）。
        # 分段之间重启 ffmpeg 会丢 1~2 秒，下游切片必须能看出"这两段之间是个断层"，
        # 而"用文件时长去推"推不出来 —— 只能在这里，趁分片刚落盘时记下来。
        self.slot_stamps: dict[str, tuple[float, float]] = {}
        self.slot_started_at: float = 0.0
        # v4.29：合片要跑 ffmpeg 子进程（大分片的采样+remux 可达分钟级），
        # 现在全部在线程池/后台线程里跑 —— 这把锁保证**同一时刻只有一个合片**，
        # 不然「满段封存」和「停止收尾」撞在一起会互相踩文件。
        self._close_lock = threading.Lock()
        # v4.29：add() 里发现"这一段满了"只**置位**，由调用方（_watch 循环）
        # 在下一个节拍用 close_async() 异步封存 —— 边界不变，事件循环不再被卡。
        self._hour_due = False

    def take_hour_due(self) -> bool:
        """取出「满段待封存」标志（取走即清零，幂等）。"""
        due, self._hour_due = self._hour_due, False
        return due

    def add(self, path: Path) -> None:
        self._add_with_duration(path, probe_duration(path))

    async def add_async(self, path: Path) -> None:
        """add() 的异步版（v4.29）：ffprobe 对大 TS 是**全盘扫描**，多 GB 的分片
        一次可读几十秒 —— 放线程池，别让事件循环陪跑。"""
        duration = await asyncio.to_thread(probe_duration, path)
        self._add_with_duration(path, duration)

    def _add_with_duration(self, path: Path, duration: float) -> None:
        if not self.slot:
            self.slot_started_at = time.time()
        # 分片此刻刚关闭（seed harvest 每隔 0.5s 收一次），把它认定为 [now-时长, now]
        now = time.time()
        self.slot_stamps[path.name] = (round(now - max(0.0, duration), 3), round(now, 3))
        self.slot.append(path)
        self.log(f"[assembler] 分片落盘 {path.name}（{duration:.1f}s）")
        if self.on_segment:
            try:
                self.on_segment(path)
            except Exception:
                pass
        # K21：段长只对齐关键帧，用 0.98 容差判定"满一小时"。
        # v3.78：`segment_seconds == 0` 是「全程不中断」档 —— **不按段封存**，
        # 只在 close()（下播/停止/画幅切段）时落一个文件。
        # ⚠️ 少了这个 `> 0`，`duration >= 0` 恒真 ⇒ 每落一个分片就封存一次，
        #    表现成"每段都是几分钟的小片"，与"整场一个视频"正好相反。
        # v4.29：满段只**置位**（take_hour_due），合片由 _watch 循环异步做 ——
        # 段边界不变（add 与取位在同一轮循环里先后发生），事件循环不再被合片卡住。
        if self.segment_seconds > 0 and duration >= self.segment_seconds * HOUR_TOLERANCE:
            self._hour_due = True

    def discard(self, path: Path, why: str = "") -> bool:
        """把一个**刚加进来**的分片撤回去（v3.77：画面异常且整段不可用时用）。

        连它记下的**时间区间**一起撤 —— 留着的话合片后的时间轴会与文件对不上
        （这与 K113「别拿目录顺序当时间顺序」是同一类问题：戳与文件必须一一对应）。
        找不到就直接返回 False（不抛：这只是一次尽力而为的清理）。
        """
        try:
            self.slot.remove(path)
        except ValueError:
            return False
        self.slot_stamps.pop(path.name, None)
        if not self.slot:
            self.slot_started_at = 0.0
        self.log(f"[assembler] 撤回分片 {path.name}：{why or '按调用方要求'}")
        return True

    def close(self, force: bool = False, aspect: str = "") -> Path | None:
        """合并当前槽位。``force=True`` 时即使不足一小时也合并（下播/停止）。

        v4.29：对外入口只做**串行化**（同一时刻只允许一个合片在跑），真正的活儿在
        :meth:`_close_unlocked`。合片本身建议用 :meth:`close_async` 在线程池里跑。
        """
        with self._close_lock:
            return self._close_unlocked(force=force, aspect=aspect)

    async def close_async(self, force: bool = False, aspect: str = "") -> Path | None:
        """close() 的异步版（v4.29）：合片（含大分片三点采样 + remux）是分钟级操作，
        放线程池跑，事件循环继续伺候别的录制器和用户动作。"""
        return await asyncio.to_thread(self.close, force, aspect)

    def _close_unlocked(self, force: bool = False, aspect: str = "") -> Path | None:
        """合并当前槽位。``force=True`` 时即使不足一小时也合并（下播/停止）。

        v3.57：一个槽位**可以产出多个文件** —— 中途变了分辨率时按分组各自成品，
        每个成品内部只有一种分辨率（K100：素材库里不许出现被压扁的文件）。
        ``aspect`` 会写进文件名后缀（``_916``/``_34``…）——
        用户要的「改动画幅的素材在命名上简单备注一下」。
        """
        if not self.slot:
            return None
        aspect = aspect or self.aspect
        parts, self.slot = self.slot, []
        stamps, self.slot_stamps = self.slot_stamps, {}
        started_at = self.slot_started_at or time.time()
        ended_at = time.time()
        self.slot_started_at = 0.0
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tag = tag_of(aspect)
        base = self.final_dir / f"{self.anchor}_{stamp}{'_' + tag if tag else ''}.mp4"

        from .merge import merge_parts_grouped          # 局部导入：避免循环依赖

        def _rename(index: int, width: int, height: int, output: Path) -> Path:
            """每组的画幅后缀跟着**这组自己的尺寸**走（不是跟着槽位走）。"""
            if index <= 0 and (not width or not height):
                return output
            group_aspect = classify(width, height) or aspect
            gtag = tag_of(group_aspect) or tag
            stem = output.stem
            m = re.search(r"_(169|916|34|43)$", stem)
            if m:
                stem = stem[: m.start()]
            if index > 0:
                stem += f"_g{index + 1}"
            return output.with_name(f"{stem}{'_' + gtag if gtag else ''}{output.suffix}")

        results, rejected, notes = merge_parts_grouped(parts, base, rename=_rename)
        if notes:
            self.log(f"[assembler] {notes}")

        primary: Path | None = None
        for res in results:
            group_aspect = (classify(res.width, res.height) or aspect) if res.width else aspect
            rec = res.as_dict()
            rec["aspect"] = group_aspect or ""
            # 坑 2：精确起止时间（墙钟）—— 下游切片据此知道段与段之间有断层。
            # 用这一组实际包含的分片时间戳，而不是整个槽位（一组可能只占槽位一部分）。
            span = [stamps[n] for n in res.partNames if n in stamps]
            rec["startAt"] = round(min(s[0] for s in span), 3) if span else round(started_at, 3)
            rec["endAt"] = round(max(s[1] for s in span), 3) if span else round(ended_at, 3)
            rec["slotCount"] = len(results)
            rec["size"] = f"{res.width}x{res.height}" if res.width else ""
            self.merges.append(rec)
            if not res.ok or not res.output:
                self.log(f"[assembler] 合片失败：{res.reason}")
                continue
            self.aspects.append(group_aspect or "")
            self.log(f"[assembler] 小时文件 {res.output.name}"
                     f"（{res.mergedDurationSec:.1f}s，{res.partCount} 个分片，"
                     f"{res.width}x{res.height}）")
            if self.on_hour_file:
                try:
                    self.on_hour_file(res.output)
                except Exception:
                    pass
            primary = primary or res.output

        if rejected:
            # K55：**移走**而不是删除 —— 内容还在，只是没合进任何 MP4
            # v4.46（用户 2026-11-06）：移走时**按成品命名规则改名**（锚点名_时间_unmerged_N），
            # 以前直接沿用分片名 00000.ts —— 录制库里出现「00000.ts」谁也认不出是哪场。
            keep_dir = self.final_dir / "unmerged"
            keep_dir.mkdir(parents=True, exist_ok=True)
            for idx, p in enumerate(rejected):
                stem = f"{self.anchor}_{stamp}_unmerged" + (f"_{idx + 1}" if len(rejected) > 1 else "")
                target = keep_dir / f"{stem}{p.suffix}"
                try:
                    p.replace(target)
                    self.log(f"[assembler] 分片 {p.name} 内部有分辨率变化，未合片（已改名保留："
                             f"{target.name}）")
                except OSError as exc:
                    self.log(f"[assembler] 移动分片失败 {p.name}：{exc}")

        if primary is None and results:
            # 有成果文件（才会走到这）但没有一个是 ok 的旧分支：保持 unmerged 语义
            keep_dir = self.final_dir / "unmerged"
            keep_dir.mkdir(parents=True, exist_ok=True)
            for p in parts:
                if not p.is_file():
                    continue
                try:
                    p.replace(keep_dir / p.name)
                except OSError as exc:
                    self.log(f"[assembler] 移动分片失败 {p.name}：{exc}")
        return primary
