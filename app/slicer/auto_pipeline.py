#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6 自动化流水线：目录监控 → 转写 → 切片 → 渲染 → 入队（editor 兜底）。

无人值守链路（D8）：监控 ``live_raw`` 新出现的录制文件，自动走完
转写 → 切片 → 渲染，成片落 ``edits\\``；editor 的 ``publish.scan`` 会兜底扫成片入队
（slicer 不直连 editor，D29 解耦）。

设计要点：
- **串行**：一次只处理一个文件（一个文件走完 转写→切片→渲染 再下一个）。
- **稳定判定**：连续两次扫描 mtime+size 不变才算「录完」，避免边录边切把半截文件当素材。
- **指纹去重 + 状态持久化**：``auto_pipeline.json`` 记已处理/处理中的指纹，服务重启不重复处理。
- **显存守卫**：渲染器本身已有 >3200 MiB 守卫（renderer），这里只保证不并发。
- 切片走 ``planner="auto"``：无 Key 时自动 local_fallback（与「AI 切片」页一致）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..common.paths import ws
from . import slicing as slicing_engine
from . import tuning as tuning_engine
# v3.58：小时文件名 → 主播名的解析只有一份（在 recorder 里，紧挨着写文件名的那段）。
from .recorder import owner_of_hour_file

SCAN_INTERVAL_SEC = 10.0
STABLE_CHECKS = 2          # 连续 2 次 mtime+size 不变才算稳定
STATE_FILE_NAME = "auto_pipeline.json"

# v3.35：真故障最多重试几次。为什么需要「有限重试」：原来失败一次就把指纹写进
# `processed` = **永远不再试**，于是「服务恰好被重启/GPU 恰好被占」这种临时故障会让
# 这段录像**永久不流转**，而用户只看到「没自动流转」、看不到任何原因（K70）。
MAX_TRIES = 3

# 允许用环境变量整体停掉自动化（冒烟测试**必须**带上它，否则起真服务时会去处理
# 用户 live_raw 里的真实录像 —— 2026-09-24 实测踩到过）。
DISABLE_ENV = "SLICE_DISABLE_AUTO"

# v3.58：`_processed` 里**不只有成片** —— 跳过（没语音/分数不够）记 "skipped: …"，
# 老版本（v3.35 之前）失败过一次就写死 "failed: …"。它们都**没有产出成片**，
# 拿 `_processed` 的总数当「已产出 N 个成片」会虚高（实测 10 条里 3 条是老 failed）。
# 这里集中判定，别在界面/接口里各写一份（K44）。
_NON_FILM_PREFIXES = ("skipped:", "failed:")

# 界面要显示「本轮是谁的素材」→ 只带最近这么多个主播名就够了（按处理顺序取尾部）。
FLOW_FILM_WINDOW = 20


def is_film(value: object) -> bool:
    """这条 `_processed` 记录**真的产出成片了**吗（跳过/老失败不算）。"""
    return not str(value or "").startswith(_NON_FILM_PREFIXES)


class SkippedMaterial(RuntimeError):
    """这块素材**本身**不值得做（没有效语音 / 分数不够）—— 不是故障，不该重试。"""

# v3.35：自动化流水线的开关**持久化到 settings.json**，并且**默认开**。
# 为什么改默认：用户报「录了一小时没有自动流转到转写」—— 根因就是这个开关默认关着、
# 而且只存在内存里（服务一重启又变回关），开关还藏在设置页，录制页根本看不到。
# 现在「录完就自动流转」是默认行为，`/api/auto/toggle` 会把它记下来。
SETTING_KEY = "autoFlowEnabled"
DEFAULT_ENABLED = True


def load_enabled_pref() -> bool:
    """读持久化的开关（默认 ``True``；读不到 / 值非法都当默认）。"""
    import json

    try:
        data = json.loads(ws.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_ENABLED
    if not isinstance(data, dict) or SETTING_KEY not in data:
        return DEFAULT_ENABLED
    return bool(data.get(SETTING_KEY))


def save_enabled_pref(value: bool) -> bool:
    """写持久化的开关（原子写，与其它设置项同一个 settings.json）。"""
    import json

    path = ws.settings_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[SETTING_KEY] = bool(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return bool(value)


class AutoPipeline:
    """目录监控 + 转写→切片→渲染 的无人值守编排（服务内单例）。

    ``live_dir`` / ``transcripts_dir`` / ``candidates_dir`` / ``state_path`` 可注入：
    端点层用默认（真实目录），冒烟测试传临时目录，完全隔离（K36）。
    """

    def __init__(self, transcriber, renderer, log=print, *,
                 live_dir: Path | None = None,
                 transcripts_dir: Path | None = None,
                 candidates_dir: Path | None = None,
                 state_path: Path | None = None):
        self._tr = transcriber
        self._rd = renderer
        self.log = log
        self._live_dir = Path(live_dir) if live_dir else ws.live_source_dir
        self._transcripts_dir = (Path(transcripts_dir) if transcripts_dir
                                 else ws.transcripts_dir)
        self._candidates_dir = (Path(candidates_dir) if candidates_dir
                                else ws.candidates_dir)
        self._state_path = Path(state_path) if state_path else (
            ws.slicer_runtime / STATE_FILE_NAME)
        self._lock = threading.Lock()
        self._enabled = load_enabled_pref()   # v3.35：持久化的开关（默认开）
        self._busy = False
        self._current: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, str] = {}       # filename -> fingerprint
        self._processed: dict[str, str] = {}  # fingerprint -> "done" / "skipped: 原因"
        self._failed: dict[str, dict] = {}    # fingerprint -> {reason, at, tries}（可重试）
        self._priority: list[str] = []        # v3.35：录制器报来的「刚装配完」的成片（优先处理）
        self._last_scan = 0.0
        # v3.53：生产线**界面显示**的「本轮」计数基线（默认 0 = 不折算）。
        # 只影响界面显示，**不动**真实状态 —— 见 :meth:`reset_flow_view`。
        self._view_base: dict = {"processed": 0, "failed": 0, "at": 0.0}
        self._load_state()

    # ------------------------------------------------------------ 状态持久化
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._processed = data.get("processed") or {}
            self._failed = data.get("failed") or {}
        except (OSError, ValueError):
            self._processed = {}
            self._failed = {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(
                {"processed": self._processed, "failed": self._failed,
                 "updatedAt": round(time.time(), 1)},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _should_skip(self, fp: str) -> bool:
        """指纹是否该跳过：做完了 → 跳；失败了但重试用尽 → 也跳（别无限重试烧 GPU）。

        **失败但还有重试机会 → 不跳**，这正是修掉「临时故障 ⇒ 永久不流转」的地方。
        """
        if not fp or fp in self._processed:
            return True
        rec = self._failed.get(fp) or {}
        return int(rec.get("tries") or 0) >= MAX_TRIES

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # v3.35：启动时恢复持久化的开关（默认开）—— 服务重启后不再悄悄变成「关」
        self._enabled = load_enabled_pref()
        # v3.58：服务一启动就把「本轮」的基线定在**此刻** —— 上一轮留下的一堆
        # Completed 不该占着生产线的位置（否则每次打开都停在「人工确认」，
        # 显示的还是几天前的素材）。真实状态一个都不动，见 :meth:`_set_view_base`。
        self._set_view_base("服务启动")
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="auto-pipeline")
        self._thread.start()
        self.log(f"[auto] 目录监控线程已启动（自动化 "
                 f"{'开启' if self._enabled else '关闭'}）")

    def stop(self) -> None:
        self._stop.set()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
        self.log(f"[auto] 自动化 {'开启' if enabled else '关闭'}")

    def nudge(self, path: str | Path | None = None) -> None:
        """**外部事件**：录制器刚落了一个**已装配完成**的成片 → 登记为优先处理。

        ⚠️ **必须非阻塞**：这个方法是从录制器的事件循环里调的（`on_hour_file` 回调），
        在这里同步跑转写/渲染会**整个卡住录制**。所以只入队，真正的处理交给流水线自己的线程。

        为什么可以跳过「稳定判定」：`on_hour_file` 是在小时文件**装配完成之后**才回调的
        （`recorder.HourAssembler.close` → `_emit_hour_file`），它不可能是「还在写」的半截文件。
        定时扫描那条路仍然要稳定判定 —— 那条路面对的是「来源不明的文件」。
        """
        if path is None:
            return
        try:
            target = Path(path)
        except (TypeError, ValueError):
            return
        with self._lock:
            if not self._enabled:
                return
            self._priority.append(str(target))
            # 只留最近 50 个，防止异常情况下无限堆积
            if len(self._priority) > 50:
                del self._priority[:-50]

    def status(self) -> dict:
        with self._lock:
            # v3.35：把「失败原因」也带出去 —— 用户报「没自动流转」时，界面/接口要能说清
            # 「是哪一条、为什么没过」（原来只有一句 `failed:RuntimeError`，等于不可诊断，K70）
            fails = sorted(self._failed.items(),
                           key=lambda kv: kv[1].get("at") or 0, reverse=True)
            return {
                "enabled": self._enabled,
                "busy": self._busy,
                "current": dict(self._current) if self._current else None,
                "processedCount": len(self._processed),
                # v3.58：**真的产出成片**的条数（跳过/老失败不计，见 :func:`is_film`）——
                # 生产线「人工确认」只该停在有成片的时候，被跳过的素材不该占着那个位置。
                "processedFilmCount": sum(
                    1 for v in self._processed.values() if is_film(v)),
                # v3.58：最近若干条成片的**主播名**（按处理顺序），界面据此显示
                # 「本轮产出的是谁的素材」—— 名字的解析规则只有一份（recorder.owner_of_hour_file）。
                "filmOwners": [
                    owner_of_hour_file(fp.split("|")[0])
                    for fp, v in list(self._processed.items()) if is_film(v)
                ][-FLOW_FILM_WINDOW:],
                "failedCount": len(self._failed),
                "lastFailures": [
                    {"name": fp.split("|")[0], "reason": (v or {}).get("reason", ""),
                     "tries": int((v or {}).get("tries") or 0),
                     "at": (v or {}).get("at")}
                    for fp, v in fails[:5]],
                "lastScan": self._last_scan,
                # v3.53：生产线「本轮」的计数基线（界面据此把累计计数折算成"本轮"，
                # 让「重置」后能重新显示「等待新的录屏素材」而不动真实状态）。
                "flowView": {
                    "baselineProcessed": int(self._view_base.get("processed") or 0),
                    "baselineFilm": int(self._view_base.get("film") or 0),
                    "baselineFailed": int(self._view_base.get("failed") or 0),
                    "resetAt": float(self._view_base.get("at") or 0.0),
                },
            }

    def _set_view_base(self, note: str) -> dict:
        """把「本轮」的计数基线定在**此刻**（唯一的归零实现，两处入口共用，K44）。

        ⚠️ 这是**视图基线**，不是清空真实状态（用户原话：「不打断正在进行的
        转写切片剪辑的工作，只是重置这个生产线区域的位置的流程显示」）：
          · 不动 `_processed` / `_failed` / `_priority` —— 它们是流水线去重与重试的
            依据，清掉会让同一批素材**被重新转写一遍**，白白烧一遍 GPU；
          · 不停线程、不动队列，正在跑的转写/切片/剪辑照旧（`/api/auto/status`
            的 `busy` 仍是真状态，界面上该显示「正在处理 X」就照样显示）；
          · 只是记下「从现在起重新计数」，累计计数减去基线后为 0，
            于是生产线回到「等待新的录屏素材」。
        """
        with self._lock:
            self._view_base = {"processed": len(self._processed),
                               "film": sum(1 for v in self._processed.values()
                                           if is_film(v)),
                               "failed": len(self._failed),
                               "at": time.time()}
            base = dict(self._view_base)
        self.log(f"[auto] 生产线本轮计数已归零（{note}；基线：已处理 "
                 f"{base['processed']} / 成片 {base['film']} / 失败 {base['failed']}）")
        return {"ok": True, **self.status()}

    def reset_flow_view(self) -> dict:
        """用户点「重置」：本轮从 0 起算，重新等下一批素材。"""
        return self._set_view_base("用户点「重置」")

    def mark_view_session(self) -> dict:
        """**本次打开程序** → 生产线回到「发现录屏」（v3.58）。

        为什么必须做：用户报「每次打开都走到人工确认的位置，还得手动点一下重置」。
        真因是生产线看的是**累计**计数（持久化在 `auto_pipeline.json`，服务重启也还在），
        上一轮留下的一堆 Completed 会永远占着「人工确认」这一格。
        现在「本轮 = 本次打开程序之后」：开窗口时记一次基线（服务启动时也会记一次），
        于是打开即回到发现录屏，之后**新**产出的成片才会把它推到人工确认。

        ⚠️ 与「重置」是**同一个实现**（`_set_view_base`）—— 两处各写一份必然漂移。
        """
        return self._set_view_base("本次打开程序")

    def trigger(self) -> dict:
        """手动触发一次扫描（不等定时器）。"""
        try:
            self._scan_once()
        except Exception as exc:                             # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, **self.status()}

    # ------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:                         # noqa: BLE001
                self.log(f"[auto] 扫描异常：{exc}")
            self._stop.wait(SCAN_INTERVAL_SEC)

    def _scan_once(self) -> None:
        self._last_scan = round(time.time(), 1)
        with self._lock:
            if not self._enabled or self._busy:
                return
            priority, self._priority = self._priority, []
        # v3.35：录制器报来的成片**优先**处理（已装配完成，不必等稳定判定）
        for raw in priority:
            p = Path(raw)
            try:
                if not p.is_file() or p.parent != self._live_dir:
                    continue
            except OSError:
                continue
            if self._should_skip(self._fingerprint(p)):
                continue
            self._seen.pop(p.name, None)
            self._process(p)                      # 一次只处理一个（串行）
            return
        for mp4 in self._list_recordings():
            fp = self._fingerprint(mp4)
            if self._should_skip(fp):
                continue
            if self._seen.get(mp4.name) == fp:
                self._seen.pop(mp4.name, None)   # 稳定 → 处理
                self._process(mp4)
                return                            # 一次只处理一个
            self._seen[mp4.name] = fp

    def _list_recordings(self) -> list[Path]:
        """待流转的录屏素材，**最新录的排在前面**。

        ⚠️ v3.63（）修用户报的「开启自动流转后把**上一份**录屏素材当流动展示」：
        原来是 `sorted(glob("*.mp4"))` —— 按**文件名升序**＝**最旧的在最前**，而
        `_scan_once` 取第一个「未处理且稳定」的就处理。实测（2026-09-28）`live_raw` 里躺着
        9 个 mp4，最旧是 `倪珈_20260922-123404.mp4`、最新是 `陈伯(全能王)_20260927-002803_169.mp4`，
        于是打开自动流转后生产线先去啃 **09-22 那份**，界面上"当前素材"显示的就是上一轮的老素材。
        录屏文件名里带 `_YYYYmmdd-HHMMSS`，按文件名**降序**＝时间降序；但为了不依赖命名规则
        （素材也可能被手改过名），这里直接用 **mtime 降序**（K44：时间口径只有文件系统一处）。
        """
        root = self._live_dir
        if not root.is_dir():
            return []
        files = [p for p in root.glob("*.mp4") if not p.name.startswith("_smoke")]

        def _mt(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:                      # 正在被写/被删：当成最旧，别让它挡住别人
                return 0.0

        files.sort(key=_mt, reverse=True)        # 最新优先（同秒时保持 glob 的稳定顺序）
        return files

    @staticmethod
    def _fingerprint(mp4: Path) -> str:
        try:
            st = mp4.stat()
            return f"{mp4.name}|{int(st.st_mtime)}|{st.st_size}"
        except OSError:
            return ""

    # ------------------------------------------------------------ 处理单文件
    def _process(self, mp4: Path) -> None:
        fp = self._fingerprint(mp4)
        with self._lock:
            self._busy = True
            self._current = {"video": mp4.name, "stage": "transcribe",
                             "startedAt": round(time.time(), 1)}
        try:
            self._process_one(mp4)
            self._processed[fp] = "done"
            self._failed.pop(fp, None)
            self.log(f"[auto] {mp4.name} 全链路完成 → 成片已入 edits\\")
        except SkippedMaterial as exc:
            # 素材本身不值得做（没语音 / 分数不够）—— 记原因、不重试（重试只是白烧 GPU）
            self._processed[fp] = f"skipped: {exc}"
            self._failed.pop(fp, None)
            self.log(f"[auto] {mp4.name} 跳过：{exc}")
        except Exception as exc:                             # noqa: BLE001
            # 真故障（转写/渲染报错、服务被重启打断…）→ 记**原因** + 计次数，留着下次重试。
            # 原来这里只写 `failed:RuntimeError` 且直接当成「已处理」= 永久放弃，
            # 用户既看不到原因、那段录像也再不会流转（K70）。
            rec: dict = self._failed.get(fp) or {}
            rec["tries"] = int(rec.get("tries") or 0) + 1
            rec["reason"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["at"] = round(time.time(), 1)
            self._failed[fp] = rec
            left = MAX_TRIES - rec["tries"]
            self.log(f"[auto] {mp4.name} 处理失败（第 {rec['tries']}/{MAX_TRIES} 次）：{rec['reason']}"
                     + ("" if left <= 0 else f"；还会再试 {left} 次"))
        finally:
            self._save_state()
            with self._lock:
                self._busy = False
                self._current = None

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            if self._current:
                self._current["stage"] = stage

    def _process_one(self, mp4: Path) -> None:
        stem = mp4.stem
        # 1) 转写
        self._set_stage("transcribe")
        code, payload = self._tr.submit(mp4.name)
        if code != 200:
            raise RuntimeError(f"转写提交失败：{payload.get('error')}")
        job = self._wait(lambda: self._tr.status(payload["job"]["id"]))
        if job.get("status") != "done":
            raise RuntimeError(f"转写未完成：{job.get('status')} {job.get('error', '')[:120]}")
        # 2) 切片（无 Key 自动 local_fallback）
        self._set_stage("slice")
        tjson = self._transcripts_dir / f"{stem}.json"
        transcript = json.loads(tjson.read_text(encoding="utf-8"))
        # v3.21：必须带上内容类型偏好 —— 不传就是旧兼容路径（story→general），
        # 分型 prompt 与权重在无人值守的流水线里会**永远不生效**（K53）。
        from app.slicer.prompts import load_content_type_pref

        result = slicing_engine.slice_transcript(
            transcript, planner="auto", mode="story",
            content_type=load_content_type_pref())
        out = self._candidates_dir / f"{stem}.candidates.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not result.get("candidates"):
            raise RuntimeError("切片没有产出候选（素材可能太短 / 无有效语音）")
        # M7：score_threshold 过滤 —— 最高分候选低于阈值就不渲染（调优手段）
        top = max(float((c.get("scores") or {}).get("overall") or 0)
                  for c in result["candidates"])
        threshold = tuning_engine.get_score_threshold()
        if top < threshold:
            raise RuntimeError(f"最高分 {top:.1f} 低于阈值 {threshold:.0f}，跳过渲染（可调低阈值）")
        # 3) 渲染（挑最高分候选，保守策略 + 跟随画幅）
        self._set_stage("render")
        code, payload = self._rd.submit(candidate=stem, strategy="conservative")
        if code != 200:
            raise RuntimeError(f"渲染提交失败：{payload.get('error')}")
        rjob = self._wait(lambda: self._rd.status(payload["job"]["id"]))
        if rjob.get("status") != "done":
            raise RuntimeError(f"渲染未完成：{rjob.get('status')} {rjob.get('error', '')[:120]}")
        self._set_stage("done")

    def _wait(self, poll, timeout: float = 1800.0) -> dict:
        deadline = time.time() + timeout
        job: dict = {}
        while time.time() < deadline:
            code, data = poll()
            job = data.get("job") or {}
            if job.get("status") in ("done", "error", "cancelled", "failed"):
                return job
            time.sleep(1.0)
        return job
