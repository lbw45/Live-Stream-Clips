#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高级剪辑页「重新识别字幕与 AI 方案」（对齐参考实现顶栏同名按钮）。

为什么单独一个模块，而不是复用 ``transcriber.py``：
- 转写页那是**整条视频**级别的（产物落到 ``transcripts/<stem>.json``），而这里只
  转「当前这一条候选」的几十秒；**绝不能覆盖整份转写** —— 同一场直播的其它候选
  还在用那份文件取字幕（v3.24：recues 存在草稿里，渲染时才覆盖）。
- 所以结果存在**剪辑草稿**里（``recues`` 字段，源视频时间轴），渲染时由
  ``renderer._execute`` 优先取用；草稿删了就自动退回原转写。

耗时操作走后台线程 + 轮询状态（与其他 Manager 一致），进度分四段：
``cutting（抽音轨） → transcribing（Whisper） → planning（重算钩子） → done``。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.common import proc
from app.common.paths import ws
from app.slicer import cut as cut_engine

DEFAULT_MODEL = "medium"          # 对齐参考实现按钮上的提示语「Medium 正在重新识别…」

_STAGES = {"queued": 0.0, "cutting": 10.0, "transcribing": 40.0,
           "planning": 82.0, "done": 100.0}


def _default_runner(wav: Path, out_dir: Path, model: str) -> list[dict]:
    """真跑 Whisper（GPU 优先）。抽出来的产物 json 里才是带时间戳的分段。"""
    from app.slicer import whisper_run
    res = whisper_run.run(str(wav), str(out_dir), model=model, language="zh")
    data = json.loads(Path(res["jsonPath"]).read_text(encoding="utf-8"))
    return data.get("segments") or []


class ReanalyzeManager:
    """「重新识别字幕与 AI 方案」的单任务调度器（同时只跑一个，后来的排队会直接拒绝）。"""

    MAX_JOBS = 5

    def __init__(self, log=print, *, drafts_dir: Path | None = None,
                 tmp_dir: Path | None = None, ffmpeg: Path | None = None,
                 runner=None) -> None:
        self.log = log
        self._drafts_dir = drafts_dir
        self._tmp_dir = tmp_dir
        self._ffmpeg = ffmpeg or ws.ffmpeg
        self._runner = runner or _default_runner
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._cancel: set[str] = set()
        self._seq = 0

    # ------------------------------------------------------- 公开 API

    def submit(self, candidate: str, idx: int, model: str = DEFAULT_MODEL) -> tuple[int, dict]:
        raw = str(candidate or "").strip()
        if not raw:
            return 400, {"error": "请指定候选产物（candidate）"}
        base = raw[: -len(".candidates.json")] if raw.endswith(".candidates.json") else raw
        try:
            _b, picked, src, _d = cut_engine.resolve_target(base, int(idx))
            start_sec = float(picked["start"])
            end_sec = float(picked["end"])
        except FileNotFoundError as exc:
            return 404, {"error": str(exc)}
        except (ValueError, KeyError, TypeError) as exc:
            return 400, {"error": f"候选参数不合法：{exc}"}
        if end_sec - start_sec < 1.0:
            return 400, {"error": "这段候选太短（不足 1 秒），没必要重新识别"}

        with self._lock:
            busy = any(j["status"] == "running" for j in self._jobs.values())
            if busy:
                return 409, {"error": "上一次重新识别还没跑完"}
            self._seq += 1
            jid = f"x{time.strftime('%Y%m%d-%H%M%S')}-{self._seq:03d}"
            job = {"id": jid, "candidate": base, "idx": int(idx), "model": model,
                   "status": "queued", "stage": "queued", "percent": 0.0,
                   "message": "排队中", "result": {}, "error": "",
                   "queuedAt": round(time.time(), 1), "endedAt": 0.0}
            self._jobs[jid] = job
            self._order.insert(0, jid)
            del self._order[self.MAX_JOBS:]
            for stale in list(self._jobs)[self.MAX_JOBS:]:
                self._jobs.pop(stale, None)

        t = threading.Thread(target=self._run, args=(job, src, start_sec, end_sec),
                             name=f"reanalyze-{jid}", daemon=True)
        t.start()
        return 200, {"job": job}

    def status(self, job_id: str | None = None) -> tuple[int, dict]:
        with self._lock:
            if job_id:
                job = self._jobs.get(job_id)
                if job is None:
                    return 404, {"error": f"任务不存在：{job_id}"}
                return 200, {"job": dict(job),
                             "busy": job["status"] == "running"}
            latest = self._jobs.get(self._order[0]) if self._order else None
            return 200, {"job": dict(latest) if latest else None,
                         "busy": bool(latest and latest["status"] == "running")}

    def cancel(self, job_id: str | None = None) -> tuple[int, dict]:
        with self._lock:
            target = job_id or (self._order[0] if self._order else None)
            job = self._jobs.get(target or "")
            if job is None:
                return 404, {"error": "没有正在进行的重新识别"}
            if job["status"] != "running":
                return 409, {"error": "任务已经不是运行状态"}
            self._cancel.add(job["id"])
        return 200, {"ok": True, "message": "已请求取消（会在当前分段转完后停下）"}

    # ------------------------------------------------------- 内部

    def _set(self, job: dict, stage: str, message: str = "") -> None:
        with self._lock:
            job["stage"] = stage
            job["percent"] = _STAGES.get(stage, job["percent"])
            if message:
                job["message"] = message

    def _run(self, job: dict, src: Path, start_sec: float, end_sec: float) -> None:
        tmp_dir = self._tmp_dir or ws.tmp_dir
        tmp_dir.mkdir(parents=True, exist_ok=True)
        clip = tmp_dir / f"re_{job['candidate']}_c{job['idx']}_{job['id']}.wav"
        try:
            with self._lock:
                job["status"] = "running"
                job["message"] = "抽取片段音轨"
            self._set(job, "cutting", "抽取片段音轨")
            dur = max(end_sec - start_sec, 0.5)
            if job["id"] in self._cancel:
                raise InterruptedError("已取消")
            cmd = [str(self._ffmpeg), "-y", "-ss", f"{start_sec:.3f}", "-t", f"{dur:.3f}",
                   "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(clip)]
            r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
            if r.returncode != 0 or not clip.is_file():
                tail = (r.stderr or "").strip()[-200:]
                raise RuntimeError(f"抽音轨失败：{tail or '未知原因'}")

            self._set(job, "transcribing", f"Whisper {job['model']} 正在重新识别…")
            if job["id"] in self._cancel:
                raise InterruptedError("已取消")
            segments = self._runner(clip, tmp_dir, job["model"])
            if not segments:
                raise RuntimeError("这段画面里没有识别出任何语音（Whisper 返回空）")
            # 音轨**立刻**删（几十 MB）：放在这里而不是 finally，是因为 done 状态一落，
            # 前端/测试可能马上就去读目录 —— 副作用要在状态之前完成（K46 同源）。
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass

            self._set(job, "planning", "重算开头钩子")
            # 转出来的时间是**片段相对时间**，加回片段起点才是源时间轴
            recues: list[dict] = []
            segs_src: list[dict] = []
            for s in segments:
                s0 = start_sec + float(s.get("start") or 0)
                e0 = start_sec + float(s.get("end") or 0)
                text = str(s.get("text") or "").strip()
                if e0 <= s0 or not text:
                    continue
                segs_src.append({"start": s0, "end": e0, "text": text})
                recues.append({"startMs": round(s0 * 1000.0, 1),
                               "endMs": round(e0 * 1000.0, 1), "text": text[:120]})

            from app.slicer.slicing import find_hook
            hook = find_hook(segs_src, start_sec, end_sec) or None

            # 落进剪辑草稿：**只动 recues，keepRanges/decisions 原样保留**
            old = cut_engine.load_draft(job["candidate"], job["idx"],
                                        drafts_dir=self._drafts_dir) or {}
            ranges = old.get("keepRanges") or [{"startMs": round(start_sec * 1000.0, 1),
                                                "endMs": round(end_sec * 1000.0, 1)}]
            cut_engine.save_draft(job["candidate"], job["idx"], ranges,
                                  recues=recues, drafts_dir=self._drafts_dir)

            with self._lock:
                job["status"] = "done"
                job["stage"] = "done"
                job["percent"] = 100.0
                job["endedAt"] = round(time.time(), 1)
                job["message"] = f"已重新分析 {len(recues)} 条字幕与开头钩子"
                job["result"] = {"cueCount": len(recues), "hook": hook,
                                 "recues": recues}
            self.log(f"[recheck] {job['id']} 完成：{len(recues)} 段")
        except InterruptedError as exc:
            self._finish_err(job, str(exc), "cancelled")
        except Exception as exc:                       # noqa: BLE001 —— 详情要回给前端
            self._finish_err(job, f"{type(exc).__name__}: {exc}", "error")
        finally:
            try:
                clip.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self._cancel.discard(job["id"])

    def _finish_err(self, job: dict, error: str, status: str) -> None:
        with self._lock:
            job["status"] = status
            job["error"] = error
            job["endedAt"] = round(time.time(), 1)
            job["message"] = error
        self.log(f"[recheck] {job['id']} {status}：{error}")
