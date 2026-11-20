#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写调度器（M3-A）—— 跑在 slicer 服务进程里，把转写请求排队交给子进程。

为什么是**子进程**而不是线程（§2.1 的原版实证结论）：

- faster-whisper 会吃满显存/CPU，跑在服务进程里会把 HTTP 线程一起拖慢；
- 子进程可以被**强杀**（取消转写），线程不能；
- 子进程崩溃不会带崩服务。

单 worker 串行：转写是重活，并发跑两路只会互相抢 GPU 反而更慢，
而且录音（ffmpeg + NVENC）已经在抢资源了。

进度：子进程按 ``@@PROGRESS@@ {json}`` 行上报（见 ``whisper_run``），本模块逐行解析。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..common import proc
from ..common.paths import PROJECT_ROOT, source_kind_of, ws
from .whisper_run import PROGRESS_MARKER

# 历史保留条数（超出后丢最旧的已完成任务；运行中的绝不丢）
MAX_HISTORY = 50


class TranscriberManager:
    """转写任务的排队与执行（服务内单例）。"""

    def __init__(self, log=print) -> None:
        self.log = log or (lambda *a, **k: None)
        self._lock = threading.RLock()      # K26：持锁时会调 _save()，必须可重入
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []         # 任务 id 顺序（新的在前）
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._running = False
        self._cancel_ids: set[str] = set()
        self._seq = 0

    # ------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._load_index()
        self._thread = threading.Thread(
            target=self._worker, name="transcriber", daemon=True)
        self._thread.start()
        pending = sum(1 for j in self._jobs.values() if j["status"] == "queued")
        self.log(f"[transcriber] 已启动，历史 {len(self._jobs)} 条，待执行 {pending} 条")

    def stop(self) -> None:
        self._running = False
        child = self._proc
        if child is not None and child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass
        self._queue.put("__stop__")     # 唤醒 worker 让它退出
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------ 持久化索引

    @property
    def _index_path(self) -> Path:
        return ws.transcripts_dir / "index.json"

    def _load_index(self) -> None:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        for item in jobs if isinstance(jobs, list) else []:
            try:
                job = dict(item)
                jid = str(job["id"])
            except (KeyError, TypeError):
                continue
            if job.get("status") in ("queued", "running"):
                # 上次服务退出时没跑完 → 标记中断（不自动重跑，避免重启风暴）
                job["status"] = "error"
                job["error"] = "服务重启，任务中断"
            job.setdefault("recentLog", [])
            self._jobs[jid] = job
            self._order.append(jid)
        self._seq = len(self._order)

    def _save(self) -> None:
        with self._lock:
            jobs = [self._jobs[j] for j in self._order
                    if j in self._jobs and self._jobs[j]["status"] != "running"]
            payload = {"jobs": jobs[:MAX_HISTORY], "savedAt": round(time.time(), 1)}
        path = self._index_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------ 提交与查询

    def submit(self, video_rel: str, *, model: str = "small", language: str = "zh",
               max_seconds: float | None = None) -> tuple[int, dict]:
        """提交一个转写任务。``video_rel`` 相对**任一源根**（也可传绝对路径）。

        源根取自 ``paths.source_roots()``（录制素材 + 我的上传）；
        归属判定只有一处（``source_kind_of``），不要再在这里写死 live_raw。
        """
        raw = str(video_rel or "").strip()
        if not raw:
            return 400, {"error": "请提供要转写的视频（file）"}
        candidate = Path(raw)
        # 允许调用方传「逻辑名/相对路径」两段式（下载来的素材用 network/xxx.mp4 表达）
        roots = ws.source_roots()
        target = None
        if not candidate.is_absolute():
            head, _, rest = raw.replace("\\", "/").partition("/")
            for name, root in roots:
                if head == name and rest:
                    target = (root / rest).resolve()
                    break
        if target is None:
            for name, root in roots:
                probe = (root / raw).resolve()
                try:
                    probe.relative_to(root.resolve())
                except ValueError:
                    continue
                if probe.is_file():
                    target = probe
                    break
        if target is None:
            fallback = Path(raw)
            target = (fallback.resolve() if fallback.is_absolute()
                      else (ws.live_source_dir / raw).resolve())
        kind = source_kind_of(target)
        if not kind:
            return 400, {"error": "只能转写源根目录内的视频（直播录制 / 网络下载 / 我上传的素材；路径越界已拒绝）"}
        if not target.is_file():
            return 404, {"error": f"文件不存在：{raw}"}
        root = dict(roots)[kind].resolve()

        # 同一个文件已有排队中/运行中的任务 → 复用，不重复排队（K10 同思路）
        with self._lock:
            for jid in self._order:
                job = self._jobs.get(jid)
                if (job and job.get("videoPath") == str(target)
                        and job.get("status") in ("queued", "running")):
                    return 200, {"ok": True, "job": dict(job),
                                 "message": "该文件已在队列中，复用当前任务"}

            self._seq += 1
            jid = f"t{time.strftime('%Y%m%d-%H%M%S')}-{self._seq:03d}"
            job = {
                "id": jid,
                "video": str(target.relative_to(root)),
                # v3.59（）：源根逻辑名（live / network）——「这个视频从哪来的」
                # 必须显式带着走，否则下游只能猜（猜错就是预览/渲染找不到源）。
                "sourceKind": kind,
                "videoName": target.name,
                "videoPath": str(target),
                "status": "queued",
                "stage": "queued",
                "percent": 0.0,
                "message": "排队中",
                "model": model,
                "language": language,
                "maxSeconds": max_seconds,
                "queuedAt": round(time.time(), 1),
                "startedAt": 0.0,
                "endedAt": 0.0,
                "elapsedSec": 0.0,
                "audioSec": 0.0,
                "segmentCount": 0,
                "srtPath": "",
                "jsonPath": "",
                "error": "",
                "recentLog": [],
            }
            self._jobs[jid] = job
            self._order.insert(0, jid)
            self._trim()
            self._save()
        self._queue.put(jid)
        self.log(f"[transcriber] 入队 {jid} ← {job['video']}")
        return 200, {"ok": True, "job": dict(job)}

    def status(self, job_id: str | None = None) -> tuple[int, dict]:
        with self._lock:
            current = next((self._jobs[j] for j in self._order
                            if self._jobs.get(j, {}).get("status") == "running"), None)
            if job_id:
                job = self._jobs.get(job_id)
                if job is None:
                    return 404, {"error": f"没有任务 {job_id}"}
                return 200, {"job": dict(job), "current": current}
            jobs = [dict(self._jobs[j]) for j in self._order if j in self._jobs]
            queued = sum(1 for j in jobs if j["status"] == "queued")
        return 200, {
            "jobs": jobs,
            "count": len(jobs),
            "current": current,
            "queued": queued,
            "busy": current is not None,
            "now": round(time.time(), 1),
        }

    def cancel(self, job_id: str | None = None) -> tuple[int, dict]:
        """取消任务：排队中的直接作废；运行中的杀掉子进程。"""
        with self._lock:
            target = job_id or next(
                (j for j in self._order
                 if self._jobs.get(j, {}).get("status") in ("queued", "running")), None)
            if not target:
                return 404, {"error": "没有可取消的任务"}
            job = self._jobs.get(target)
            if job is None:
                return 404, {"error": f"没有任务 {target}"}
            status = job["status"]
            if status not in ("queued", "running"):
                return 409, {"error": f"任务已是 {status} 状态，无法取消", "job": dict(job)}
            self._cancel_ids.add(target)
            child = self._proc
            if status == "running" and child is not None and child.poll() is None:
                try:
                    child.kill()
                except OSError:
                    pass
        self.log(f"[transcriber] 请求取消 {target}（原状态 {status}）")
        return 200, {"ok": True, "job": dict(job), "message": "已请求取消"}

    @staticmethod
    def _source_rel(stem: str, video_name: str) -> tuple[str, bool]:
        """这条转写对应的**源素材**媒体流地址（两段式 ``逻辑名/相对路径``）与「源还在不在」。

        v3.63（）：转写产物列表要能点「预览」（用户报「转写产物没有预览按钮」），
        而预览播的是它的**源素材** —— 所以地址必须由服务端给出（K71：前端不拼媒体流地址），
        源已不在时也要如实说（K108：不给死按钮）。

        定位与判定都走 `renderer.resolve_source_video`（「找源视频」的唯一实现，与
        `/api/cut/plan` 同源，K44：两处各写一份必然漂移）；逻辑名走 `paths.source_kind_of`
        （源根表唯一出处）。返回 ``("", False)`` 表示源素材不在了。

        局部导入 renderer：避免模块级循环（renderer 侧也会用到转写产物）。
        """
        try:
            from .renderer import resolve_source_video
            got = resolve_source_video(None, {"videoName": video_name or ""}, stem)
        except Exception:                                        # noqa: BLE001
            return "", False
        if not isinstance(got, Path):
            return "", False
        kind = source_kind_of(got)
        root = dict(ws.source_roots()).get(kind)
        if not kind or root is None:
            return "", False
        try:
            rel = got.relative_to(Path(root)).as_posix()
        except ValueError:
            return "", False
        return f"{kind}/{rel}", True

    def transcripts(self) -> tuple[int, dict]:
        """已产出的转写文件列表（扫描目录，不依赖内存状态）。"""
        root = ws.transcripts_dir
        files: list[dict] = []
        if root.is_dir():
            for srt in sorted(root.glob("*.srt")):
                json_path = srt.with_suffix(".json")
                try:
                    stat = srt.stat()
                except OSError:
                    continue
                meta: dict = {}
                if json_path.is_file():
                    try:
                        meta = json.loads(json_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        meta = {}
                src_rel, src_ok = self._source_rel(srt.stem, meta.get("videoName", ""))
                files.append({
                    "stem": srt.stem,
                    "srt": srt.name,
                    "srtPath": str(srt),
                    "jsonPath": str(json_path) if json_path.is_file() else "",
                    "videoName": meta.get("videoName", ""),
                    "segmentCount": meta.get("segmentCount", 0),
                    "audioSec": meta.get("audioSec", 0),
                    "device": meta.get("device", ""),
                    "model": meta.get("model", ""),
                    "elapsedSec": meta.get("elapsedSec", 0),
                    "sizeKb": round(stat.st_size / 1024, 1),
                    "mtime": round(stat.st_mtime, 1),
                    # v3.63：界面「预览」按钮用它 —— 空串＝源素材已不在（界面显示文字，不给按钮）
                    "sourceRel": src_rel,
                    "sourceExists": src_ok,
                })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return 200, {"files": files, "count": len(files), "dir": str(root)}

    # ------------------------------------------------ worker

    def _trim(self) -> None:
        """调用方须已持锁：历史超限时丢最旧的**已结束**任务。"""
        while len(self._order) > MAX_HISTORY:
            for i in range(len(self._order) - 1, -1, -1):
                job = self._jobs.get(self._order[i])
                if job and job["status"] not in ("queued", "running"):
                    self._jobs.pop(self._order.pop(i), None)
                    break
            else:
                return      # 全是活任务，不丢

    def _worker(self) -> None:
        while self._running:
            try:
                jid = self._queue.get()
            except Exception:
                continue
            if jid == "__stop__" or not self._running:
                return
            job = self._jobs.get(jid)
            if job is None:
                continue
            if jid in self._cancel_ids:
                self._cancel_ids.discard(jid)
                self._finish(job, "cancelled", error="已取消")
                continue
            try:
                self._execute(job)
            except Exception as exc:      # 兜底：worker 绝不允许死掉
                self.log(f"[transcriber] {jid} 异常：{type(exc).__name__}: {exc}")
                self._finish(job, "error", error=f"{type(exc).__name__}: {exc}")

    def _execute(self, job: dict) -> None:
        out_dir = ws.transcripts_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(job["videoPath"]).stem

        cmd = [
            str(ws.python_exe), "-X", "utf8", "-u",
            "-m", "app.slicer.whisper_run",
            "--video", job["videoPath"],
            "--out", str(out_dir),
            "--model", job["model"],
            "--language", job["language"],
        ]
        if job.get("maxSeconds"):
            cmd += ["--max-seconds", str(job["maxSeconds"])]

        with self._lock:
            job["status"] = "running"
            job["stage"] = "starting"
            job["startedAt"] = round(time.time(), 1)
            job["message"] = "启动转写子进程"
        self.log(f"[transcriber] 开始 {job['id']}（{job['videoName']}）")

        # stderr 合并进 stdout：whisper 的日志行不含标记，解析时跳过即可，
        # 同时避免 stderr 管道没人读把子进程堵死。
        # 走 proc.popen（不弹黑窗）：服务进程自己没有可见控制台，
        # 直接 subprocess.Popen 会让转写全程挂一个黑窗（见 common/proc.py）。
        child = proc.popen(
            cmd, cwd=str(PROJECT_ROOT), env=ws.subprocess_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._proc = child

        cancelled = False
        try:
            assert child.stdout is not None
            for line in child.stdout:
                line = line.rstrip("\n")
                if line.startswith(PROGRESS_MARKER):
                    self._apply_progress(job, line[len(PROGRESS_MARKER):].strip())
                elif line.strip():
                    self._append_log(job, line.strip())
                    if job["id"] in self._cancel_ids:
                        cancelled = True
            code = child.wait()
        finally:
            self._proc = None
            if job["id"] in self._cancel_ids:
                cancelled = True
                self._cancel_ids.discard(job["id"])

        if cancelled:
            # v3.159（用户 2026-10-28）：**停止 = 本次半成品不要**。
            # 只清这一次写出来的东西：本次运行之后才出现的 .json/.srt、以及 whisper 的临时 wav。
            # ⚠️ 同一素材**上一次**转好的产物（mtime 更早）绝不能删 —— 那是别的流程留下的，
            #    删了用户重跑还得再等一遍。源视频更不动（在 live_raw，不归这里管）。
            dropped = self._drop_partial(stem, float(job.get("startedAt") or 0))
            msg = "已停止" + ("（本次产物已丢弃）" if dropped else "（没有产出文件）")
            self._finish(job, "cancelled", error=msg)
            return
        if code != 0:
            tail = " ｜ ".join(job["recentLog"][-3:]) or f"子进程退出码 {code}"
            self._finish(job, "error", error=f"转写失败：{tail}")
            return

        srt_path = out_dir / f"{stem}.srt"
        json_path = out_dir / f"{stem}.json"
        meta: dict = {}
        if json_path.is_file():
            try:
                meta = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        with self._lock:
            job["percent"] = 100.0
            job["stage"] = "done"
            job["srtPath"] = str(srt_path) if srt_path.is_file() else ""
            job["jsonPath"] = str(json_path) if json_path.is_file() else ""
            job["segmentCount"] = int(meta.get("segmentCount") or 0)
            job["audioSec"] = float(meta.get("audioSec") or 0)
        self._finish(job, "done")
        self.log(f"[transcriber] 完成 {job['id']}：{job['segmentCount']} 段 → {srt_path.name}")

    def _drop_partial(self, stem: str, started_at: float) -> int:
        """丢掉**本次运行**写出的半成品，返回删掉的个数。

        判据用 mtime >= startedAt - 1s：同一 stem 上一次成功转好的产物 mtime 更早，
        不会被误删（用户明确要求「上一个流程留下来的素材不要删」）。
        """
        n = 0
        keep_after = max(started_at - 1.0, 0.0)
        for p in (ws.transcripts_dir / f"{stem}.json", ws.transcripts_dir / f"{stem}.srt"):
            try:
                if p.is_file() and p.stat().st_mtime >= keep_after:
                    p.unlink()
                    n += 1
            except OSError:
                pass
        # whisper 的临时 wav：whisper_<stem前40字符>_<时间戳>.wav
        try:
            prefix = f"whisper_{stem[:40]}_"
            for w in ws.tmp_dir.glob(prefix + "*.wav"):
                try:
                    if w.stat().st_mtime >= keep_after:
                        w.unlink()
                        n += 1
                except OSError:
                    pass
        except OSError:
            pass
        return n

    def _apply_progress(self, job: dict, raw_json: str) -> None:
        try:
            data = json.loads(raw_json)
        except ValueError:
            return
        with self._lock:
            job["percent"] = float(data.get("percent") or 0.0)
            job["stage"] = str(data.get("stage") or job["stage"])
            job["message"] = str(data.get("message") or job["message"])
            if data.get("device"):
                job["device"] = str(data["device"])
            if data.get("segmentCount") is not None:
                job["segmentCount"] = int(data["segmentCount"])
            if data.get("srtPath"):
                job["srtPath"] = str(data["srtPath"])
            if data.get("stage") == "error":
                job["error"] = str(data.get("message") or job["error"])
            if data.get("percent") is not None and data.get("stage") == "done":
                job["endedAt"] = job["endedAt"] or round(time.time(), 1)

    def _append_log(self, job: dict, line: str) -> None:
        with self._lock:
            job["recentLog"].append(f"{time.strftime('%H:%M:%S')} {line}")
            if len(job["recentLog"]) > 40:
                del job["recentLog"][:10]

    def _finish(self, job: dict, status: str, error: str = "") -> None:
        with self._lock:
            job["status"] = status
            job["endedAt"] = round(time.time(), 1)
            if job.get("startedAt"):
                job["elapsedSec"] = round(job["endedAt"] - job["startedAt"], 1)
            if error:
                job["error"] = error
            if status == "done":
                job["percent"] = 100.0
        self._save()


# ---------------------------------------------------------------- 自检入口

def _selftest() -> int:
    """无 HTTP 的最小自检：提交一个短转写，跑到结束。"""
    import argparse
    parser = argparse.ArgumentParser(description="转写调度器自检")
    parser.add_argument("--video", required=True, help="live_raw 内的文件名")
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    mgr = TranscriberManager(log=lambda m: print(m, flush=True))
    mgr.start()
    code, payload = mgr.submit(args.video, max_seconds=args.seconds)
    if code != 200:
        print(f"提交失败 {code}: {payload}")
        return 2
    jid = payload["job"]["id"]
    while True:
        _, st = mgr.status(jid)
        job = st["job"]
        print(f"  {job['percent']:6.2f}% {job['stage']:14s} {job['message']}")
        if job["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(1.5)
    mgr.stop()
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0 if job["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(_selftest())


# ---------------------------------------------------------------- Whisper 模型一键下载
# 开源版 2026-11-19（用户：本地没检测到模型就给个下载按钮）。状态结构与 gpu.repair
# 同一套口径（running/log/done/ok/error），前端 pollGpuTask 直接复用。

ALLOWED_MODELS: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3")
MODEL_DL_LOCK = threading.Lock()
_model_dl: dict = {"running": False, "name": "", "startedAt": 0.0, "log": [],
                   "done": False, "ok": None, "error": ""}


def model_download_state() -> dict:
    with MODEL_DL_LOCK:
        snap = dict(_model_dl)
        snap["log"] = list(_model_dl["log"])
        snap["elapsedSec"] = (round(time.time() - _model_dl["startedAt"], 1)
                              if _model_dl["startedAt"] else 0)
        name = _model_dl.get("name") or "small"
    # 进度：hf 是逐文件落盘的，用目录体积近似（前端除以 expectedMb 画进度条）
    d = ws.models_dir / name
    try:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
    except OSError:
        size = 0
    snap["dirSizeMb"] = round(size / 1024 / 1024, 1)
    snap["expectedMb"] = {"tiny": 75, "base": 145, "small": 460,
                          "medium": 1500, "large-v3": 3100}.get(name, 460)
    return snap


def start_model_download(name: str = "small") -> dict:
    """后台下载 faster-whisper 官方模型到 ``models/<name>``（已存在则直接返回）。"""
    name = (name or "small").strip().lower()
    if name not in ALLOWED_MODELS:
        return {"started": False, "error": f"不认识的模型名：{name}"}
    if (ws.models_dir / name / "model.bin").is_file():
        return {"started": False, "alreadyPresent": True,
                "state": model_download_state()}
    with MODEL_DL_LOCK:
        if _model_dl["running"]:
            return {"started": False, "busy": True, "state": model_download_state()}
        _model_dl.update({"running": True, "name": name, "startedAt": time.time(),
                          "log": [], "done": False, "ok": None, "error": ""})
    threading.Thread(target=_run_model_download, args=(name,), daemon=True,
                     name="model-dl").start()
    return {"started": True, "state": model_download_state()}


def _mlog(msg: str) -> None:
    with MODEL_DL_LOCK:
        _model_dl["log"].append(msg)
    print(f"[model-dl] {msg}", flush=True)


def _run_model_download(name: str) -> None:
    try:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        _mlog(f"开始下载 {name}（镜像 {os.environ['HF_ENDPOINT']}，约数百 MB）…")
        from huggingface_hub import snapshot_download   # noqa: PLC0415
        snapshot_download(repo_id=f"Systran/faster-whisper-{name}",
                          local_dir=str(ws.models_dir / name))
        ok = (ws.models_dir / name / "model.bin").is_file()
        with MODEL_DL_LOCK:
            _model_dl["ok"] = ok
            _model_dl["error"] = ("" if ok else
                                  "下载完成但 model.bin 不在 —— 镜像可能不完整，换官方源重试")
        _mlog("完成" if ok else "校验失败")
    except BaseException as exc:                        # noqa: BLE001
        with MODEL_DL_LOCK:
            _model_dl["ok"] = False
            _model_dl["error"] = f"{type(exc).__name__}: {exc}"
        _mlog(_model_dl["error"])
    finally:
        with MODEL_DL_LOCK:
            _model_dl["running"] = False
            _model_dl["done"] = True
