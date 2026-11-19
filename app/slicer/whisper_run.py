#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写引擎（M3-A）：视频 → 16k 单声道 WAV → faster-whisper → SRT + JSON。

**既可作为独立子进程运行**（``python -m app.slicer.whisper_run --video ... --out ...``），
**也可作为模块被 import**（:func:`run`）。slicer 服务用前者，冒烟测试用后者。

设计要点（对应文档 §2.1 / §6.4）：

1. **GPU 优先，失败回退 CPU** —— ``cuda + int8_float16`` → ``cpu + int8``；
   CUDA 运行时用工作区自带的 ``runtime\\cuda12``（D20），不依赖系统安装。
2. **音频先落盘到 ASCII 临时目录**（``runtime_data\\tmp``）——K8/K24：
   中文路径会让 av 库读 WAV 失败；ffmpeg 读中文视频路径没问题，所以"抽音频"这一步
   正好把中文路径**转换掉**，之后 whisper 只面对纯 ASCII 路径。
3. **进度以 ``@@PROGRESS@@ {json}`` 行打到 stdout**，父进程逐行解析
   （子进程不能用 return code 之外的东西传进度，而日志里混普通行会干扰解析）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

try:
    from app.common import proc
    from app.common.paths import ws
except ImportError:      # 直接以脚本方式运行：python app\slicer\whisper_run.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.common import proc
    from app.common.paths import ws

PROGRESS_MARKER = "@@PROGRESS@@"

# 进度分配：抽音频 0–8、加载模型 8–12、转写 12–98、写产物 98–100
_P_EXTRACT = (0.0, 8.0)
_P_LOAD = (8.0, 12.0)
_P_TRANSCRIBE = (12.0, 98.0)
_P_WRITE = (98.0, 100.0)


# ---------------------------------------------------------------- 进度上报

def emit_progress(stage: str, percent: float, message: str = "", **extra) -> None:
    """打印一行进度（父进程解析用）。普通日志走 stderr，不污染 stdout 协议。"""
    payload = {
        "stage": stage,
        "percent": round(percent, 2),
        "message": message,
        "at": round(time.time(), 2),
    }
    payload.update(extra)
    print(f"{PROGRESS_MARKER} {json.dumps(payload, ensure_ascii=False)}", flush=True)


def _log(message: str) -> None:
    print(f"[whisper] {message}", file=sys.stderr, flush=True)


def _lerp(band: tuple[float, float], ratio: float) -> float:
    """把 0–1 的 ratio 映射到 [lo, hi] 区间。"""
    lo, hi = band
    ratio = max(0.0, min(1.0, ratio))
    return lo + (hi - lo) * ratio


# ---------------------------------------------------------------- 探测与抽音频

def probe_duration(path: str | Path) -> float:
    """用 ffprobe 取媒体时长（秒）；失败返回 0.0（调用方按"未知"处理）。"""
    cmd = [str(ws.ffprobe), "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", env=ws.subprocess_env())
        return round(float((r.stdout or "").strip()), 3)
    except (OSError, ValueError):
        return 0.0


def extract_audio(video: str | Path, wav: str | Path,
                  max_seconds: float | None = None) -> Path:
    """ffmpeg 抽音频：16kHz 单声道 pcm_s16le（faster-whisper 的输入格式）。

    ``max_seconds`` 不为空时只抽前 N 秒（预览/冒烟测试用）。
    """
    wav = Path(wav)
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ws.ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-i", str(video), "-vn", "-acodec", "pcm_s16le",
            "-ar", "16000", "-ac", "1", str(wav)]
    r = proc.run(cmd, capture_output=True, text=True, encoding="utf-8",
                 errors="replace", env=ws.subprocess_env())
    if r.returncode != 0 or not wav.is_file():
        raise RuntimeError(f"抽取音频失败：{(r.stderr or '').strip()[:400]}")
    return wav


# ---------------------------------------------------------------- 模型加载

def load_model(model_name: str = "small", prefer_gpu: bool = True):
    """加载 faster-whisper 模型，返回 ``(model, device, compute_type)``。

    先试 GPU，抛异常再回退 CPU（§2.1）。模型目录取自工作区 ``runtime\\models``（D16）。
    """
    from faster_whisper import WhisperModel   # 延迟导入：先注册 CUDA DLL 再 import ct2

    model_dir = ws.models_dir / model_name
    if not model_dir.is_dir():
        raise RuntimeError(f"模型目录不存在：{model_dir}")

    attempts: list[tuple[str, str]] = []
    if prefer_gpu:
        attempts.append(("cuda", "int8_float16"))
    attempts.append(("cpu", "int8"))

    errors: list[str] = []
    for device, compute_type in attempts:
        t0 = time.time()
        try:
            _log(f"加载模型 {model_name} device={device} compute_type={compute_type}")
            model = WhisperModel(str(model_dir), device=device,
                                 compute_type=compute_type,
                                 cpu_threads=4, num_workers=1)
            _log(f"模型就绪，用时 {time.time() - t0:.1f}s")
            return model, device, compute_type
        except Exception as exc:                      # 任何加载失败都回退下一档
            errors.append(f"{device}/{compute_type}: {type(exc).__name__}: {exc}")
            _log(f"加载失败：{errors[-1]}")
    raise RuntimeError("模型加载全部失败 —— " + " ｜ ".join(errors))


# ---------------------------------------------------------------- 产物序列化

def _srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    blocks = []
    for seg in segments:
        blocks.append(
            f"{seg['idx']}\n"
            f"{_srt_time(seg['start'])} --> {_srt_time(seg['end'])}\n"
            f"{seg['text']}\n"
        )
    return "\n".join(blocks)


def write_outputs(out_dir: str | Path, stem: str, meta: dict,
                  segments: list[dict]) -> tuple[Path, Path]:
    """写出 ``<stem>.srt`` 与 ``<stem>.json``，返回两者路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    srt_path = out_dir / f"{stem}.srt"
    json_path = out_dir / f"{stem}.json"

    # 原子写：先写 .tmp 再 replace，避免 UI 读到半截文件
    tmp = srt_path.with_suffix(".srt.tmp")
    tmp.write_text(segments_to_srt(segments), encoding="utf-8")
    tmp.replace(srt_path)

    payload = dict(meta)
    payload["segmentCount"] = len(segments)
    payload["segments"] = segments
    payload["text"] = "".join(s["text"] for s in segments)
    tmp = json_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(json_path)
    return srt_path, json_path


# ---------------------------------------------------------------- 主流程

def sweep_stale_wavs(max_age_sec: float = 3600.0) -> list[str]:
    """清掉 ``tmp_dir`` 里过期的 ``whisper_*.wav``，返回被清掉的文件名。

    为什么需要它：本模块**正常结束时会自己删**临时 WAV（见 ``run()`` 末尾），
    但**被 kill 掉时（取消转写 / 服务被强停）Python 没机会跑任何清理代码** ——
    实测就是「取消长任务」那条冒烟留下的一个 WAV。``finally`` 也救不了
    （进程直接被 TerminateProcess，不执行任何 Python）。

    所以改成**下次开工前顺手扫一遍**：只删 mtime 超过 ``max_age_sec`` 的，
    免得误删另一个正在跑的转写的音频（`tmp_dir` 里正常只该有一个，保守起见按时间判断）。
    """
    removed: list[str] = []
    now = time.time()
    try:
        for wav in ws.tmp_dir.glob("whisper_*.wav"):
            try:
                if (now - wav.stat().st_mtime) > max_age_sec:
                    wav.unlink()
                    removed.append(wav.name)
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def run(video: str | Path, out_dir: str | Path, *,
        model: str = "small", language: str = "zh",
        max_seconds: float | None = None,
        prefer_gpu: bool = True) -> dict:
    """转写一个视频，返回结果字典（同时已写出 SRT/JSON）。

    ``max_seconds`` 用于"只转写前 N 秒"的预览模式，也用于冒烟测试。
    """
    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"视频不存在：{video}")

    # 顺手清上次被 kill 留下的音频（见 sweep_stale_wavs）
    sweep_stale_wavs()

    started = time.time()
    audio_duration = max_seconds if (max_seconds and max_seconds > 0) else probe_duration(video)

    # ---- 1) 抽音频（中文路径在这一步被换成 ASCII 临时路径，K8/K24）
    emit_progress("extracting", _P_EXTRACT[0], "抽取音频")
    wav = ws.tmp_dir / f"whisper_{video.stem[:40]}_{int(started)}.wav"
    extract_audio(video, wav, max_seconds=max_seconds)
    took = time.time() - started
    emit_progress("extracting", _P_EXTRACT[1], f"音频就绪（{took:.1f}s）",
                  audioBytes=wav.stat().st_size)

    # ---- 2) 加载模型（GPU 优先）
    emit_progress("loading", _P_LOAD[0], f"加载模型 {model}")
    ws.register_cuda()                     # 必须在 import ctranslate2 之前
    model_obj, device, compute_type = load_model(model, prefer_gpu=prefer_gpu)
    emit_progress("loading", _P_LOAD[1], f"模型就绪（{device}）", device=device,
                  computeType=compute_type)

    # ---- 3) 转写（按已出分段时长推进进度）
    emit_progress("transcribing", _P_TRANSCRIBE[0], "开始转写", device=device)
    seg_iter, info = model_obj.transcribe(
        str(wav),
        language=language or None,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    total = audio_duration or getattr(info, "duration", 0) or 0
    segments: list[dict] = []
    for seg in seg_iter:
        segments.append({
            "idx": len(segments) + 1,
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": (seg.text or "").strip(),
            "avgLogprob": round(float(getattr(seg, "avg_logprob", 0.0)), 4),
            "noSpeechProb": round(float(getattr(seg, "no_speech_prob", 0.0)), 4),
        })
        ratio = (segments[-1]["end"] / total) if total else 0.0
        emit_progress("transcribing", _lerp(_P_TRANSCRIBE, ratio),
                      f"已转写 {len(segments)} 段",
                      segmentCount=len(segments), transcribedSec=segments[-1]["end"])

    # ---- 4) 写产物
    emit_progress("writing", _P_WRITE[0], "写出 SRT / JSON")
    meta = {
        "video": str(video),
        "videoName": video.name,
        "audioSec": round(total, 3),
        "model": model,
        "device": device,
        "computeType": compute_type,
        "language": getattr(info, "language", language) or language,
        "languageProbability": round(float(getattr(info, "language_probability", 0.0)), 4),
        "maxSeconds": max_seconds,
        "generatedAt": round(time.time(), 2),
        "elapsedSec": round(time.time() - started, 1),
    }
    srt_path, json_path = write_outputs(out_dir, video.stem, meta, segments)
    result = dict(meta)
    result.update({
        "srtPath": str(srt_path),
        "jsonPath": str(json_path),
        "segmentCount": len(segments),
        "elapsedSec": round(time.time() - started, 1),
    })

    # 临时 WAV 用完即删（几十分钟的音频能到几十 MB，别占着工作区）
    try:
        wav.unlink(missing_ok=True)
    except OSError:
        pass

    emit_progress("done", 100.0, f"完成：{len(segments)} 段 / {result['elapsedSec']}s",
                  srtPath=str(srt_path), segmentCount=len(segments))
    return result


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.slicer.whisper_run",
        description="转写引擎：视频 → WAV → faster-whisper → SRT + JSON")
    parser.add_argument("--video", required=True, help="输入视频路径")
    parser.add_argument("--out", required=True, help="SRT/JSON 输出目录")
    parser.add_argument("--model", default="small", help="模型目录名（默认 small）")
    parser.add_argument("--language", default="zh", help="语言代码，默认 zh")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="只转写前 N 秒（预览/测试用）")
    parser.add_argument("--no-gpu", action="store_true", help="强制 CPU（调试用）")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    try:
        result = run(
            args.video, args.out,
            model=args.model, language=args.language,
            max_seconds=args.max_seconds, prefer_gpu=not args.no_gpu,
        )
    except Exception as exc:
        emit_progress("error", 0.0, f"{type(exc).__name__}: {exc}")
        _log(f"失败：{type(exc).__name__}: {exc}")
        return 1
    _log(f"完成：{result['segmentCount']} 段，用时 {result['elapsedSec']}s，"
         f"SRT={result['srtPath']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
