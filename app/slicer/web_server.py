#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「直播切片」服务（slicer）—— 标准库 http.server 实现。

对应文档 §6.1 工程结构、§7.4 启动编排：
- 由启动脚本以 ``python -m app.slicer.web_server`` 拉起，隐藏窗口，日志重定向；
- 端口来自 ``SLICE_PORT``；HOST 强制 127.0.0.1；
- 与 editor 服务**刻意解耦**（§4.1.1）：录制器独立运行，只把完成文件落到素材目录，
  editor 侧通过目录监控消费。

M1-C 阶段职责：服务骨架 + 健康端点 + 运行时探测。
M2/M3 会在此加入：录制调度、转写调度、LLM 切片、打分、切片导出、目录监控。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.common.file_ops import delete_files_in, is_safe_stem
from app.common.net_gate import net_enabled
from app.common.paths import APP_TITLE, APP_VERSION, ws
from app.common.runtime_probe import probe_system, runtime_summary
from app.slicer.douyin import RID_SEGMENT as _RID_SEG
from app.slicer.recorder_manager import RecorderManager
from app.slicer.recheck import ReanalyzeManager
from app.slicer.transcriber import TranscriberManager
from app.slicer import transcriber as transcriber_mod  # 模块级函数（模型下载）
from app.slicer.renderer import RenderManager
from app.slicer.auto_pipeline import AutoPipeline
from app.slicer import tuning as tuning_engine
from app.slicer.render import (ORIENT_CHOICES, TARGET_LANDSCAPE, TARGET_PORTRAIT,
                               gpu_memory_used_mib)
from app.slicer import slicing as slicing_engine
from app.slicer import renderer as renderer_engine
from app.slicer import jianying as jianying_engine   # v3.75：剪映草稿导出
from app.slicer import cancel_flags             # v3.159：切片「停止」令牌
from app.slicer import media_streams            # v3.158：媒体流的可撤销注册表
from app.slicer.llm import decode_api_key, load_config as load_llm_config
from app.slicer.llm import test_connection as test_llm_connection
from app.slicer.llm import list_models as llm_list_models
from app.slicer.llm import providers_view as llm_providers_view
from app.slicer import gpu as gpu_engine
from app.slicer import ffmpeg_setup        # v3.81：显卡体检 / 实测 / CUDA 修复
# v4.6（）：看板的**自动采集**（浏览器通道取数 → 生成看板能读的快照）

SERVICE = "slicer"
TOKEN_ENV = "JACK_LOCAL_SESSION_TOKEN"
TOKEN_HEADER = "X-Session-Token"

# 流式端点按扩展名给 Content-Type（K54：music 试听 / 封面图预览都不能写死 video/mp4）
# v3.34：表搬到 `app.common.media_types`（唯一出处，K44）—— 本文件只转出，别再本地重写。
from app.common.media_types import MEDIA_TYPES as _MEDIA_TYPES  # noqa: E402

# 不校验 token 的路径（启动编排要探测 /version）
PUBLIC_PATHS = {"/version", "/api/health", "/api/runtime"}

_STARTED_AT = time.time()
_LOG_LOCK = threading.Lock()

# 路由表：精确匹配 + 正则（带路径参数）
ROUTES: dict[tuple[str, str], callable] = {}
PATTERN_ROUTES: list[tuple[str, re.Pattern, callable]] = []


def route(method: str, path: str):
    def decorator(func):
        ROUTES[(method.upper(), path)] = func
        return func
    return decorator


def pattern_route(method: str, pattern: str):
    """正则路由：捕获组按顺序作为参数传给处理函数。"""
    compiled = re.compile(pattern)
    def decorator(func):
        PATTERN_ROUTES.append((method.upper(), compiled, func))
        return func
    return decorator


# ---------------------------------------------------------------- 录制调度（M2-B）

MANAGER = RecorderManager(
    log=lambda m: print(f"[slicer] {m}", flush=True))

# 转写调度（M3-A）：单 worker 队列，子进程跑 faster-whisper
TRANSCRIBER = TranscriberManager(
    log=lambda m: print(f"[slicer] {m}", flush=True))

# M4：渲染调度（单 worker 串行 + 显存守卫，见 app/slicer/renderer.py 顶部说明）
RENDERER = RenderManager(
    log=lambda m: print(f"[slicer] {m}", flush=True))

# M6：自动化流水线（目录监控 → 转写 → 切片 → 渲染；见 auto_pipeline.py 顶部说明）
AUTO = AutoPipeline(
    TRANSCRIBER, RENDERER,
    log=lambda m: print(f"[slicer] {m}", flush=True))

# v3.24：高级剪辑页「重新识别字幕与 AI 方案」（针对单条候选重跑 Whisper + 重算钩子）
RECHECK = ReanalyzeManager(
    log=lambda m: print(f"[slicer] {m}", flush=True))


def default_whisper_model() -> str:
    """默认模型名：显式环境变量 > ``SLICE_WHISPER_MODEL`` 目录名 > ``small``。"""
    explicit = os.environ.get("SLICE_WHISPER_MODEL_NAME", "").strip()
    if explicit:
        return explicit
    model_path = os.environ.get("SLICE_WHISPER_MODEL", "").strip()
    if model_path:
        return Path(model_path).name
    return "small"


@route("POST", "/api/transcribe")
def h_transcribe_submit(handler, _params):
    """提交转写任务（仅允许 live_raw 内的文件）。"""
    body = handler._read_json_body()
    file_ref = body.get("file") or body.get("video") or body.get("rel") or ""
    max_seconds = body.get("maxSeconds")
    return TRANSCRIBER.submit(
        str(file_ref),
        model=str(body.get("model") or default_whisper_model()),
        language=str(body.get("language") or "zh"),
        max_seconds=float(max_seconds) if max_seconds else None,
    )



@route("GET", "/api/transcribe/status")
def h_transcribe_status(_handler, params):
    return TRANSCRIBER.status(params.get("id") or None)


@route("POST", "/api/models/whisper/download")
def h_model_dl_start(handler, _params) -> tuple[int, dict]:
    """一键下载 Whisper 模型（后台线程；开源版 2026-11-19）。"""
    body = handler._read_json_body()
    return 200, transcriber_mod.start_model_download(str(body.get("name") or "small"))


@route("GET", "/api/models/whisper/download")
def h_model_dl_state(_handler, _params) -> tuple[int, dict]:
    return 200, transcriber_mod.model_download_state()


@route("POST", "/api/transcribe/cancel")
def h_transcribe_cancel(handler, _params):
    body = handler._read_json_body()
    return TRANSCRIBER.cancel(body.get("id") or None)


@route("GET", "/api/transcripts")
def h_transcripts(_handler, _params):
    """已产出的转写文件列表。"""
    return TRANSCRIBER.transcripts()


# ---------------------------------------------------------------- v3.42 素材流转阶段

# 素材流转的六个阶段（id 供程序判断、label 直接显示在界面上）。
FLOW_STAGES = [
    {"id": "recording_detect", "label": "发现录屏"},
    {"id": "slicing", "label": "自动切片"},
    {"id": "candidates", "label": "候选筛选"},
    {"id": "editing", "label": "自动剪辑"},
    {"id": "review", "label": "人工确认"},
    {"id": "completed", "label": "导出成片"},
]
_FLOW_ORDER = [s["id"] for s in FLOW_STAGES]


def _flow_stage() -> dict:
    """从服务内单例的状态推导「素材现在流转到哪一步」。

    推导优先级（照原版的「谁在前谁说了算」）：
    正在处理（AUTO 忙）> 有失败待重试（attention）> 已产出待确认（review）
    > 正在录制 / 监控开播（recording_detect）。
    ``stageStates`` 派生规则也照原版：active 之前的全部 complete，之后的全部 pending。
    """
    flow = AUTO.status()
    _code, rec_payload = MANAGER.list()
    recs = (rec_payload or {}).get("recorders") or []
    pulling = [r for r in recs
               if r.get("pulling") or (r.get("recording") or {}).get("pulling")]
    cur = flow.get("current") or {}
    # v3.53：生产线「重置」只是把**界面计数**折成"本轮"（基线由 /api/auto/flow-reset 记下）。
    # 真实状态（busy / processed / failed）一个都不动 —— 所以正在跑的转写切片剪辑
    # 照旧显示，只有"上一轮留下的已完成/失败"不再占着生产线的位置。
    base = flow.get("flowView") or {}
    # v3.58：本轮 = 累计 − 基线，且分**三档**：
    #   cycle_film    —— 真的产出成片（只有它才把生产线停在「人工确认」）；
    #   cycle_skipped —— 走过一遍但没成片（没语音 / 分数不够 / 老版本失败）；
    #   cycle_failed  —— 真故障待重试。
    # 以前只算一个「已处理」，跳过和老失败也算进去 → 明明没出成片却停在人工确认（实测）。
    cycle_total = max(0, int(flow.get("processedCount") or 0)
                      - int(base.get("baselineProcessed") or 0))
    cycle_film = max(0, int(flow.get("processedFilmCount") or 0)
                     - int(base.get("baselineFilm") or 0))
    cycle_processed = cycle_film
    cycle_skipped = max(0, cycle_total - cycle_film)
    cycle_failed = max(0, int(flow.get("failedCount") or 0)
                       - int(base.get("baselineFailed") or 0))
    if flow.get("busy") and cur:
        stage, video = str(cur.get("stage") or ""), str(cur.get("video") or "")
        if stage == "transcribe":
            active, message = "slicing", f"正在转写 {video}"
        elif stage == "slice":
            active, message = "candidates", f"正在自动切片并筛选候选 {video}"
        elif stage == "render":
            active, message = "editing", f"正在自动剪辑渲染 {video}"
        elif stage == "done":
            active, message = "completed", f"{video} 已产出成片"
        else:
            active, message = "slicing", f"正在处理 {video}"
    elif cycle_failed:
        first = (flow.get("lastFailures") or [{}])[0]
        active, message = "attention", (
            f"有素材自动流转失败：{first.get('reason') or '原因未知'}"
            f"（本轮 {cycle_failed} 条，会自动重试，也可手动重扫）")
    elif cycle_processed:
        # v3.58：把**是谁的素材**说出来 —— 用户报「没有展示对应的素材的主播名字」。
        # 名字由后端给（`filmOwners`，解析规则在 recorder.owner_of_hour_file，只有一份），
        # 界面不另算一份（K44）。
        owners = [o for o in (flow.get("filmOwners") or [])[-cycle_processed:] if o]
        seen = list(dict.fromkeys(owners))          # 去重且保序
        who = "、".join(seen[:3]) + ("等" if len(seen) > 3 else "")
        active, message = "review", (
            f"本轮已自动产出 {cycle_processed} 个素材的成片"
            + (f"（{who}）" if who else "")
            + "，等待人工确认（在「剪辑渲染」页查看）")
    elif pulling:
        names = "、".join(
            (r.get("owner") or r.get("note") or r.get("webRid") or "?")
            for r in pulling[:3])
        active, message = "recording_detect", f"正在录制 {names}，录满一段自动流转"
    elif recs:
        from app.slicer.recorder import PROBE_INTERVAL_SEC
        # v3.63（）：把**真实生效的间隔**说出来，不再写死 PROBE_INTERVAL_SEC。
        # 退避一生效（未开播久了 / 风控冷却中）就会大于 30 秒，而界面还写着"每 30 秒"，
        # 用户只会觉得"监控坏了"（K69：状态不许撒谎）。多个录制器各退各的 → 显示区间。
        secs = sorted({int(r.get("pollIntervalSec") or PROBE_INTERVAL_SEC)
                       for r in recs})
        every = (f"每 {secs[0]} 秒检测一次开播" if len(secs) == 1
                 else f"每 {secs[0]}~{secs[-1]} 秒检测一次开播")
        active, message = ("recording_detect",
                           f"监控中：等待新的录屏素材，{every}")
    else:
        active, message = "recording_detect", "等待添加直播间"

    if cycle_skipped and active == "recording_detect":
        # K69：「没产出成片」和「没处理」是两件事 —— 跳过的素材得说出来，
        # 否则用户看到生产线停在发现录屏，会以为那段录像压根没被看到。
        message += (f"（本轮有 {cycle_skipped} 个素材没产出成片："
                    "没语音 / 分数不够，已跳过）")

    if active == "attention":
        # 原版：attention 落在出问题的那一环（我们是切片环节），之前算 complete
        pos = _FLOW_ORDER.index("slicing")
        stage_states = {s: ("complete" if i < pos else "attention" if i == pos
                            else "pending") for i, s in enumerate(_FLOW_ORDER)}
    else:
        pos = _FLOW_ORDER.index(active)
        stage_states = {s: ("complete" if i < pos else "active" if i == pos
                            else "pending") for i, s in enumerate(_FLOW_ORDER)}
    return {
        "activeStage": active,
        "message": message,
        # 原版用 semi_automatic/automatic 两个词，这里沿用同词表：开关开=automatic
        "automationMode": "automatic" if flow.get("enabled") else "semi_automatic",
        "stageStates": stage_states,
        "stages": [dict(s) for s in FLOW_STAGES],
        # v3.53：本轮计数（重置后从 0 起算）与重置时间，界面据此显示「本轮」并
        # 让「重置」按钮知道自己有没有生效（K75：后端加字段，界面才有得显示）。
        "cycleProcessed": cycle_processed,
        "cycleFailed": cycle_failed,
        # v3.58：走过一遍但**没产出成片**的条数（界面把它显示在发现录屏那一档的说明里）。
        "cycleSkipped": cycle_skipped,
        "resetAt": float(base.get("resetAt") or 0.0),
    }


@route("GET", "/api/recorders")
def h_recorders_list(_handler, _params):
    """录制器列表。

    v3.35：顺手把「分段时长」的可选项与自动化流水线开关一起带出去 —— 录制页要用它们
    渲染下拉与开关，**别让前端自己写死一套秒数/键名**（K44：同一条事实两处写必然漂移）。
    v3.42：再带上「素材流转阶段」（自动内容生产线）—— 录制页顶部要显示
    录制的素材现在流转到了哪一步。
    """
    code, payload = MANAGER.list()
    if code == 200 and isinstance(payload, dict):
        from app.slicer.recorder import SEGMENT_CHOICES, SEGMENT_LABELS
        payload["segmentOptions"] = [
            {"value": k, "label": SEGMENT_LABELS.get(k, k),
             "seconds": int(v)} for k, v in SEGMENT_CHOICES.items()]
        payload["autoFlow"] = AUTO.status()
        payload["flow"] = _flow_stage()
        # v3.50：风控冷却剩余秒数（命中抖音验证后我们会主动停手）。
        # 界面据此如实提示"已暂停请求，去浏览器过一次验证"，而不是让用户对着
        # 一个反复失败的按钮猜为什么。0 = 正常。
        from app.slicer.douyin import cooldown_left
        payload["douyinCooldownSec"] = int(cooldown_left())
    return code, payload


@route("POST", "/api/recorders")
def h_recorders_add(handler, _params):
    body = handler._read_json_body()
    return MANAGER.add(
        str(body.get("input") or body.get("webRid") or ""),
        quality=str(body.get("quality") or "FULL_HD1"),
        note=str(body.get("note") or ""),
        auto_monitor=bool(body.get("autoMonitor")),
        aspect_mode=str(body.get("aspectMode") or "auto"),
        segment_key=str(body.get("segmentKey") or "1h"),   # v3.35：分段时长档
        screen_mode=bool(body.get("screenMode")),          # v4.47：录屏模式（整屏录制）
    )


@route("GET", "/api/recorders/history")
def h_recorders_history(_handler, _params):
    """历史直播间（v3.25）：移除过的房间也留着；v3.26 起带主播名与上一场直播类型。

    一键复用改为**前端把这条填进上方「添加直播间」表单**（用户 2026-09-23：
    历史与录制器两块功能重复，复用就该走同一个添加入口），所以后端不再有 use 端点。
    """
    return MANAGER.history()


@route("POST", "/api/recorders/history/type")
def h_recorders_history_type(handler, _params):
    """手动指定一条历史的「上一场直播类型」（自动识别错了就靠它纠正）。"""
    body = handler._read_json_body()
    return MANAGER.set_history_type(
        str(body.get("webRid") or body.get("input") or ""),
        str(body.get("contentType") or ""),
    )


@pattern_route("DELETE", "^/api/recorders/history/(" + _RID_SEG + ")$")
def h_recorders_history_forget(_handler, _params, rid: str):
    """把一条历史彻底忘掉（不影响当前录制器列表）。"""
    return MANAGER.forget_history(rid)


@route("POST", "/api/recorders/resolve")
def h_recorders_resolve(handler, _params):
    """「解析并添加」按钮的第一步：立即解析，返回房间信息（不录制）。"""
    body = handler._read_json_body()
    return MANAGER.resolve_room_sync(str(body.get("input") or ""))


@route("POST", "/api/recorders/probe")
def h_recorders_probe(handler, _params):
    """**立刻**重探一次是否开播（录制器区块的「刷新状态」按钮，v3.45）。

    body 可带 ``webRid`` 只刷一个；不带就刷全部 —— 就是参考实现直播间监控面板
    右上角那个「刷新状态」的行为。平时后端每 30 秒（`recorder.PROBE_INTERVAL_SEC`）
    自动探一次，这个按钮让用户不必干等。
    """
    body = handler._read_json_body()
    return MANAGER.probe_now(str(body.get("webRid") or ""))


@pattern_route("DELETE", "^/api/recorders/(" + _RID_SEG + ")$")
def h_recorders_remove(_handler, _params, rid: str):
    return MANAGER.remove(rid)


@pattern_route("PUT", "^/api/recorders/(" + _RID_SEG + ")$")
def h_recorders_options(handler, _params, rid: str):
    body = handler._read_json_body()
    return MANAGER.set_options(
        rid,
        quality=body.get("quality") or None,
        note=body.get("note") if "note" in body else None,
        auto_monitor=body.get("autoMonitor") if "autoMonitor" in body else None,
        aspect_mode=body.get("aspectMode"),
        segment_key=body.get("segmentKey") if "segmentKey" in body else None,
        # v4.47：录屏模式开关（勾选=整屏录制：含弹幕/头像/连线，屏蔽送礼特效，原样比例）
        screen_mode=(bool(body.get("screenMode"))
                     if "screenMode" in body else None),
    )


@pattern_route("GET", "^/api/recorders/(" + _RID_SEG + ")/status$")
def h_recorders_status(_handler, _params, rid: str):
    return MANAGER.status(rid)


@pattern_route("POST", "^/api/recorders/(" + _RID_SEG + ")/start$")
def h_recorders_start(_handler, _params, rid: str):
    # 开源版 2026-11-20：FFmpeg 是录制的硬依赖，缺了直接给可执行的指引，
    # 不要等 ffmpeg 进程起不来再报「退出码」那种看不懂的错。
    if not ws.ffmpeg.is_file():
        return 400, {"ok": False,
                     "error": "缺少 FFmpeg（workspace\\bin\\ffmpeg.exe）。"
                              "打开「设置 → 显卡与加速」点「下载 FFmpeg」即可自动补齐。",
                     "needFfmpeg": True}
    return MANAGER.start_recording(rid)


@pattern_route("POST", "^/api/recorders/(" + _RID_SEG + ")/stop$")
def h_recorders_stop(_handler, _params, rid: str):
    return MANAGER.stop_recording(rid)


@route("GET", "/api/recordings")
def h_recordings(_handler, _params):
    """录制库：`live_raw` 下所有成片 + 总量统计。"""
    return MANAGER.recordings()


@route("POST", "/api/recordings/delete")
def h_recordings_delete(handler, _params):
    """删除勾选的录制成片（仅限 `live_raw` 内，路径越界一律拒绝）。"""
    body = handler._read_json_body()
    files = body.get("files")
    if not isinstance(files, list) or not files:
        return 400, {"error": "请提供要删除的文件列表（files）"}
    return MANAGER.delete_recordings(files)


# ---------------------------------------------------------------- 切片引擎（M3-B）

def _safe_join(root: Path, name: str) -> Path | None:
    """把用户给的名字解析到 ``root`` 内；越界返回 ``None``（防路径穿越）。"""
    raw = str(name or "").strip()
    if not raw:
        return None
    target = (root / raw).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


@route("GET", "/api/slice/config")
def h_slice_config(_handler, _params) -> tuple[int, dict]:
    """切片引擎的当前配置：走哪个 planner、LLM 是否配好、默认参数、内容类型。"""
    from app.slicer import prompts

    cfg = load_llm_config()
    return 200, {
        "planner": "llm" if cfg.configured else "local_fallback",
        "llm": cfg.redacted(),
        "defaultParams": slicing_engine.DEFAULT_PARAMS,
        "storyMinSec": slicing_engine.DEFAULT_PARAMS["storyMinSec"],
        "commerceMinSec": slicing_engine.DEFAULT_PARAMS["commerceMinSec"],
        # v3.19：内容类型（决定用哪套 prompt 与三维权重）。**auto 排第一**：它是默认档，
        # 也是"换一场直播不用改设置"的那条路（顺序在服务端定，前端不重排）
        "contentType": load_content_type(),
        "contentTypes": [{"value": "auto", "label": "自动识别（按转写内容判定）",
                          "weights": prompts.weights_of("general")}]
        + [{"value": t, "label": prompts.label_of(t), "weights": prompts.weights_of(t)}
           for t in prompts.TYPE_ORDER if t != "auto"],
        "candidatesDir": str(ws.candidates_dir),
        "transcriptsDir": str(ws.transcripts_dir),
        "sensitiveRuleCount": len(slicing_engine.SENSITIVE_PATTERNS),
        "maskWordCount": len(slicing_engine.BLOCK_WORDS),
    }


def load_content_type() -> str:
    """读切片内容类型偏好（默认 ``auto``：按转写内容自动判定）。

    真正实现在 :func:`app.slicer.prompts.load_content_type_pref`（**唯一出处**，K44）；
    这里只是保留旧名字，避免服务层以外的调用方各写一份。
    """
    from app.slicer import prompts

    return prompts.load_content_type_pref()


@route("POST", "/api/slice/content-type")
def h_slice_content_type(handler, _params) -> tuple[int, dict]:
    """保存内容类型偏好（``auto`` 或具体类型）。

    直播间内容会变，所以这是一个**可改的偏好**而不是写死的常量：换主播/换品类时改一次即可。
    """
    from app.slicer import prompts

    body = handler._read_json_body()
    value = str(body.get("contentType") or "").strip()
    if value not in prompts.TYPE_ORDER:
        return 400, {"error": f"contentType 只能是 {' / '.join(prompts.TYPE_ORDER)}"}

    saved = prompts.save_content_type_pref(value)
    return 200, {"ok": True, "contentType": saved, "label": prompts.label_of(saved)}


@route("POST", "/api/slice/llm-config")
def h_slice_llm_config(handler, _params) -> tuple[int, dict]:
    """写入 AI 服务配置（**临时入口**：M1-D 设置页落地后迁走）。

    Key 一律以 ``dpapi:<base64>`` 形态落 ``settings.json``（§5.1 要求 DPAPI 加密）；
    传 ``clearKey=true`` 可清除。返回脱敏视图，绝不回显完整 Key。
    """
    from app.slicer.llm import dpapi_protect

    body = handler._read_json_body()
    path = ws.settings_path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    if body.get("provider"):
        data["aiProvider"] = str(body["provider"])
    if body.get("baseUrl"):
        data["aiBaseUrl"] = str(body["baseUrl"]).strip()
    if body.get("model"):
        data["aiModel"] = str(body["model"]).strip()
    if body.get("clearKey"):
        data.pop("aiApiKey", None)
    elif str(body.get("apiKey") or "").strip():
        try:
            data["aiApiKey"] = dpapi_protect(str(body["apiKey"]).strip())
        except Exception as exc:      # DPAPI 不可用时明确报错，不静默存明文
            return 500, {"error": f"DPAPI 加密失败，未保存：{exc}"}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

    cfg = load_llm_config()
    return 200, {"ok": True, "llm": cfg.redacted(),
                 "planner": "llm" if cfg.configured else "local_fallback"}



# ---------------------------------------------------------------- v3.132 本场备注
# ⚠️ handler 一律返回 (status, payload)。

@route("GET", "/api/note/history")
def h_note_history(_handler, _params) -> tuple[int, dict]:
    """v3.141：历史备注列表（「本场备注」弹窗的一键填入源）。

    来源三处，按内容去重后**时间倒序**：
      ① **预设**（v3.143：用户「保存为预设」的常用备注，跨素材可用）；
      ② 素材 sidecar（``<素材>.note.json``，跟着素材走的那份）；
      ③ 历史直播间里写过的本场备注（录制页开播前写的那份）。
    响应同时带 fields/units —— 与 ``/api/note`` 同源（K44），前端不手抄字段。

    ⚠️ 注册在这个位置（`/api/note` 之前）：路由是按精确 path 匹配的，但把更具体的
    路径放前面永远是安全的，免得以后改成前缀匹配时被 `/api/note` 抢走。
    """
    from app.common.paths import source_roots
    from app.slicer import notes as notes_mod

    def _sig(n: dict) -> tuple:
        return tuple(str(n.get(k) or "") for k in
                     ("contentType", "title", "unit", "note", "strict", "splitHighlight"))

    def _meaningful(n: dict) -> bool:
        return any(str(n.get(k) or "").strip() for k in ("contentType", "title", "note"))

    items: list[dict] = []
    seen: set[tuple] = set()
    # ① 预设优先（用户明确保存过的常用备注，标签要能看出来）
    for pr in _load_note_presets():
        sig = _sig(pr["note"])
        if sig in seen:
            continue
        seen.add(sig)
        items.append({"source": "预设", "name": pr["name"], "note": pr["note"],
                      "updatedAt": pr.get("updatedAt") or ""})
    for _kind, root in source_roots():
        try:
            files = sorted(Path(root).glob(f"*{notes_mod.NOTE_SUFFIX}"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:80]
        except OSError:
            continue
        for f in files:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            note = notes_mod._clean(raw)
            if not _meaningful(note):
                continue
            sig = _sig(note)
            if sig in seen:
                continue
            seen.add(sig)
            items.append({
                "source": "素材",
                "name": f.name[:-len(notes_mod.NOTE_SUFFIX)],
                "note": note,
                "updatedAt": str(raw.get("updatedAt") or ""),
            })
    for row in MANAGER.content_notes():
        note = row.get("note") or {}
        sig = _sig(note)
        if sig in seen:
            continue
        seen.add(sig)
        items.append({"source": "直播间", "name": row.get("owner") or "",
                      "note": note, "updatedAt": row.get("updatedAt") or ""})
    items.sort(key=lambda x: x.get("updatedAt") or "", reverse=True)
    return 200, {"ok": True, "items": items[:50],
                 "fields": [dict(f) for f in notes_mod.NOTE_FIELDS],
                 "units": _note_units()}


# ---------------------------------------------------------------- v3.143 备注预设
# 「保存为预设」= 把当前这份备注存成**跨素材可复用**的模板（不挂在任何素材上）。
# 存 hc\runtime_data\slicer\note-presets.json，最多 30 条，同名覆盖。

NOTE_PRESETS_FILE = "note-presets.json"
NOTE_PRESET_MAX = 30


def _note_presets_path():
    return ws.transcripts_dir.parent / NOTE_PRESETS_FILE


def _builtin_note_presets() -> list[dict]:
    """内置备注预设（v4.68，`notes.BUILTIN_NOTE_PRESETS` 的只读视图）。

    为什么不写进 note-presets.json：那是**用户文件**，写进去等于替用户做决定
    （还会占掉 30 条额度、被一键清空后彻底消失）。内置的走这里合并下发，
    用户能用**同名保存**覆盖，但删不掉 —— 与"内置预设"这个语义一致。
    """
    from app.slicer import notes as notes_mod
    return [{"name": str(it.get("name") or "")[:24],
             "note": notes_mod._clean(it.get("note") or {}),
             "updatedAt": "", "builtin": True}
            for it in notes_mod.BUILTIN_NOTE_PRESETS if str(it.get("name") or "").strip()]


def _user_note_presets() -> list[dict]:
    """只读**用户文件**里的预设（保存/删除必须只动这一层）。"""
    from app.slicer import notes as notes_mod
    p = _note_presets_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("items") if isinstance(raw, dict) else raw
    out: list[dict] = []
    for it in (rows or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:24]
        if not name:
            continue
        out.append({"name": name,
                    "note": notes_mod._clean(it.get("note") or {}),
                    "updatedAt": str(it.get("updatedAt") or ""),
                    "builtin": False})
    return out


def _load_note_presets() -> list[dict]:
    """**内置 + 用户**合并（下发/下拉用）。用户同名 ⇒ 覆盖内置那一条（放在用户的顺序里）。

    v4.68：用户要"每个类型提前备好两份预设" ⇒ 内置 10 条在这里并进下拉；
    以前这个函数只读文件，现在读两层，但**写入路径仍然只碰用户文件**（见上面两个函数）。
    """
    user = _user_note_presets()
    mine = {it["name"] for it in user}
    return [it for it in _builtin_note_presets() if it["name"] not in mine] + user


def _save_note_presets(items: list[dict]) -> None:
    p = _note_presets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)                      # 先写临时文件再替换：半截文件会让下拉整个读空


@route("POST", "/api/note/presets")
def h_note_presets_save(handler, _params) -> tuple[int, dict]:
    """v3.143：把当前备注存成预设。v3.145：**名称留空也能存**。

    命名规则（用户 2026-10-26）：「没填预设名称就默认用具体名称，重名就 01/02 递增；
    填了就按填的来」。
      · 请求带 ``name`` ⇒ 用它（同名 = 明确覆盖，返回 ``overwritten: true``）；
      · 不带 ⇒ base = 备注的「具体名称」，再不行用「预设」；已被占用就找第一个空位
        ``base01``、``base02``…（两位数，够用且排序稳定）。
    不弹输入框（WebView2 的 `window.prompt` 不可靠）—— 名称由弹窗里的输入框给。
    """
    from app.slicer import notes as notes_mod
    body = handler._read_json_body()
    note = notes_mod._clean(body.get("note") or {})
    if not any(str(note.get(k) or "").strip() for k in ("contentType", "title", "note")):
        return 400, {"ok": False, "error": "这份备注是空的，没什么可存"}

    rows = _load_note_presets()
    names = {it["name"] for it in rows}
    typed = str(body.get("name") or "").strip()[:24]
    overwritten = False
    if typed:
        name = typed
        overwritten = name in names
    else:
        base = str(note.get("title") or "").strip()[:20] or "预设"
        name = base
        if name in names:                     # 重名 ⇒ 找第一个空位（01/02/03…）
            name = next((f"{base}{i:02d}" for i in range(1, 100)
                         if f"{base}{i:02d}" not in names), f"{base}{len(names) + 1:02d}")

    # v4.68：内置预设**不写进用户文件**（否则用户一键清空后仍会"复活"，且白占 30 条额度）
    kept = [it for it in rows if it["name"] != name and not it.get("builtin")]
    kept.insert(0, {"name": name, "note": note,
                    "updatedAt": time.strftime("%Y-%m-%d %H:%M")})
    kept = kept[:NOTE_PRESET_MAX]
    try:
        _save_note_presets(kept)
    except OSError as exc:
        return 500, {"ok": False, "error": f"预设保存失败：{exc}"}
    return 200, {"ok": True, "name": name, "overwritten": overwritten, "count": len(kept)}


@route("POST", "/api/note/presets/delete")
def h_note_preset_delete(handler, _params) -> tuple[int, dict]:
    """v3.160（用户 2026-10-28）：「预设没有删除功能，加上」。

    只删**这一条预设**（note-presets.json 里的一行），不碰任何素材上的 sidecar 备注；
    名字不存在时回 404 让人知道点错了，而不是假成功。
    """
    body = handler._read_json_body() or {}
    name = str(body.get("name") or "").strip()[:24]
    if not name:
        return 400, {"ok": False, "error": "请指定要删除的预设名称"}
    rows = _load_note_presets()
    hit = next((it for it in rows if it["name"] == name), None)
    if hit is None:
        return 404, {"ok": False, "error": f"没有这个预设：{name}（可能已经被删掉了）"}
    if hit.get("builtin"):
        return 400, {"ok": False,
                     "error": f"「{name}」是内置预设，删不掉；想改就用**同名保存**覆盖它"}
    kept = [it for it in rows if it["name"] != name]
    try:
        _save_note_presets(kept)
    except OSError as exc:
        return 500, {"ok": False, "error": f"预设删除失败：{exc}"}
    return 200, {"ok": True, "name": name, "count": len(kept)}


@route("GET", "/api/note")
def h_note_get(_handler, params) -> tuple[int, dict]:
    """读某条素材的本场备注（顺带把 UI 要的字段与单元选项一起下发，前端不手抄）。"""
    from app.slicer import notes as notes_mod
    name = str(params.get("name") or params.get("video") or "").strip()
    stem = str(params.get("transcript") or "").strip()
    if not name and stem:
        # 切片页只有转写稿的 stem —— 从产物里拿 videoName 再找备注（素材名才是身份键）
        tp = _safe_join(ws.transcripts_dir,
                        stem if stem.endswith(".json") else f"{stem}.json")
        if tp is not None and tp.is_file():
            try:
                name = str(json.loads(tp.read_text(encoding="utf-8")).get("videoName") or "")
            except (OSError, ValueError):
                name = ""
    if name and not notes_mod.note_path_for(Path(name)).is_file():
        # 没自己的 sidecar ⇒ 用「录它的主播」的本场备注视作兜底（录制页开播前写的那份）
        live = MANAGER.note_for_source(name)
        if (live or {}).get("updatedAt"):
            return 200, {"ok": True, "name": name, "note": live,
                         "fromLive": True,
                         "fields": [dict(f) for f in notes_mod.NOTE_FIELDS],
                         "units": _note_units()}
    note = notes_mod.note_of(name) if name else dict(notes_mod.DEFAULT_NOTE)
    return 200, {
        "ok": True, "name": name, "note": note,
        "fields": [dict(f) for f in notes_mod.NOTE_FIELDS],
        "units": _note_units(),
    }


def _note_units() -> list[dict]:
    from app.slicer import notes as notes_mod
    return [{"id": k, "label": v["label"], "hint": v["hint"],
             "minSec": v["minSec"], "maxSec": v["maxSec"],
             "unitMaxSec": v.get("unitMaxSec", v["maxSec"])}
            for k, v in notes_mod.UNIT_PRESETS.items()]


@route("GET", "/api/live/note")
def h_live_note_get(_handler, params) -> tuple[int, dict]:
    """读某个直播间的本场备注（录制页「备注」按钮用）。"""
    from app.slicer import notes as notes_mod
    rid = str(params.get("rid") or "").strip()
    if not rid:
        return 400, {"ok": False, "error": "请提供 rid"}
    note = MANAGER.note_for_rid(rid)
    return 200, {"ok": True, "rid": rid, "note": note,
                 "fields": [dict(f) for f in notes_mod.NOTE_FIELDS],
                 "units": _note_units()}


@route("POST", "/api/live/note")
def h_live_note_save(handler, _params) -> tuple[int, dict]:
    """写某个直播间的本场备注（开播前就能写，录完的文件切片时兜底用它）。"""
    body = handler._read_json_body()
    rid = str(body.get("rid") or "").strip()
    if not rid:
        return 400, {"ok": False, "error": "请提供 rid"}
    return MANAGER.set_live_note(rid, body.get("note") or {})


@route("POST", "/api/note")
def h_note_save(handler, _params) -> tuple[int, dict]:
    from app.slicer import notes as notes_mod
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    if not name:
        stem = str(body.get("transcript") or "").strip()
        tp = _safe_join(ws.transcripts_dir,
                        stem if stem.endswith(".json") else f"{stem}.json") if stem else None
        if tp is not None and tp.is_file():
            try:
                name = str(json.loads(tp.read_text(encoding="utf-8")).get("videoName") or "")
            except (OSError, ValueError):
                name = ""
    if not name:
        return 400, {"ok": False, "error": "请指定素材文件名（name）"}
    target = _find_source_by_name(name)
    if target is None:
        # v3.160：前端在切片页传的是 stem；万一它还对不上，就用转写稿里的真文件名再试一次
        stem = str(body.get("transcript") or "").strip()
        tp = _safe_join(ws.transcripts_dir,
                        stem if stem.endswith(".json") else f"{stem}.json") if stem else None
        if tp is not None and tp.is_file():
            try:
                vname = str(json.loads(tp.read_text(encoding="utf-8")).get("videoName") or "")
            except (OSError, ValueError):
                vname = ""
            if vname:
                name = vname
                target = _find_source_by_name(vname)
    if target is None:
        return 404, {"ok": False,
                     "error": f"找不到素材：{name}（若文件确实在，请确认它位于录制目录里）"}
    try:
        saved = notes_mod.save_note(target, body.get("note") or {})
    except notes_mod.NoteError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "name": name, "note": saved}


MEDIA_NAME_EXTS = (".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm",
                   ".m4a", ".mp3", ".wav", ".aac")


def _find_source_by_name(name: str):
    """按文件名在源根里找素材（备注 sidecar 跟着素材走）。

    v3.139：改走 ``paths.source_roots()``（源根**唯一出处**，K44）。
    旧实现写的是 ``getattr(ws, "live_dir")`` —— **Workspace 根本没有这个属性**
    （真名是 ``live_source_dir``），``getattr`` 默认给 None ⇒ ``live_raw`` 被静默跳过
    ⇒ 直播素材在「本场备注 / 边界双重验证」上全部报「找不到素材」
    （K155 同族：写错一个属性名就静默退化；冒烟没抓到是因为 smoke_notes 只测了
    notes 模块本身、没测 HTTP 这层，K149 的远亲）。
    """
    from app.common.paths import source_roots

    def _roots():
        for _kind, root in source_roots():
            yield Path(root)
        for attr in ("candidates_dir", "editor_output_dir"):
            root = getattr(ws, attr, None)
            if root:
                yield Path(root)

    # v3.160（用户 2026-10-28「选了可用素材却说找不到」）：切片页只有**转写稿的 stem**
    # （没有 .mp4 后缀），而 sidecar 是挂在**真文件名**旁的 ⇒ 原来精确匹配必然 404。
    # 这里按「原名 → 补扩展名 → 同名 stem（大小写不敏感）」三级退化，都是**只读查找**。
    raw = str(name or "").strip()
    if not raw:
        return None
    tried = [raw]
    stem = Path(raw).stem
    if Path(raw).suffix.lower() not in MEDIA_NAME_EXTS:
        tried = [raw + ext for ext in MEDIA_NAME_EXTS] + [raw]
    for root in _roots():
        for cand in tried:
            try:
                p = root / cand
            except (TypeError, ValueError):
                continue
            if p.is_file():
                return p
    # 最后一级：同名 stem（用户可能传 "xxx.mp4" 但盘上是 .mkv，或反过来）
    for root in _roots():
        try:
            for p in root.iterdir():
                if (p.is_file() and p.stem.lower() == stem.lower()
                        and p.suffix.lower() in MEDIA_NAME_EXTS):
                    return p
        except OSError:
            continue
    # 备注也可能挂在候选/成片旁（保留旧能力；注意 Workspace 没有 "edits_dir"，
    # 成片的真属性是 editor_output_dir —— 又是一个 getattr 静默退化的坑）
    for attr in ("candidates_dir", "editor_output_dir"):
        root = getattr(ws, attr, None)
        if root and (Path(root) / name).is_file():
            return Path(root) / name
    return None



# ---------------------------------------------------------------- v3.132 边界双重验证 / 完整性复核

@route("GET", "/api/units/ocr")
def h_units_ocr_status(_handler, _params) -> tuple[int, dict]:
    """OCR 是否可用（界面要如实显示，不许假装验过）。"""
    from app.slicer import ocr_verify
    info = ocr_verify.available()
    return 200, {"ok": True, **info}


@route("POST", "/api/units/verify")
def h_units_verify(handler, _params) -> tuple[int, dict]:
    """**双重验证**：转写文本找边界 → OCR 读画面复核这些时刻。

    请求：``{"videoName": "...", "unit": "match"}``（视频名在源根里找）。
    """
    from app.slicer import notes as notes_mod, ocr_verify, segment_rules
    body = handler._read_json_body()
    name = str(body.get("name") or body.get("videoName") or "").strip()
    unit = str(body.get("unit") or "match")
    if not name:
        return 400, {"ok": False, "error": "请提供素材文件名（name）"}
    video = _find_source_by_name(name)
    if video is None:
        return 404, {"ok": False, "error": f"找不到素材：{name}"}

    # 转写产物按同名找（transcripts/<stem>.json）
    stem = video.stem
    tpath = ws.transcripts_dir / f"{stem}.json"
    if not tpath.is_file():
        return 404, {"ok": False, "error": f"还没有转写产物：{stem}.json（先转写一次）"}
    try:
        transcript = json.loads(tpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"ok": False, "error": f"转写产物读取失败：{exc}"}

    # v4.75 P-C：录制报告里的**内容死区**（gaps）——切片据此剔除跨断层候选
    try:
        _rec_path = ws.candidates_dir.parent / "live_raw" / f"{tpath.stem}.record.json"
        if _rec_path.is_file():
            _rec = json.loads(_rec_path.read_text(encoding="utf-8"))
            if isinstance(_rec.get("gaps"), list):
                transcript["gaps"] = _rec["gaps"]
    except Exception:                                      # noqa: BLE001
        pass
    points = segment_rules.detect_boundaries(transcript.get("segments") or [], unit)
    try:
        result = ocr_verify.verify_points(video, points, unit)
    except ocr_verify.OcrError as exc:
        return 500, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "name": name, "unit": unit,
                 "unitLabel": notes_mod.UNIT_PRESETS.get(unit, {}).get("label", unit),
                 "textPoints": points, **result}


@route("POST", "/api/slice/integrity")
def h_slice_integrity(handler, _params) -> tuple[int, dict]:
    """切片之后的**AI 完整性复核**（不自动改候选，只给判定与建议）。"""
    from app.slicer import integrity
    body = handler._read_json_body()
    stem = str(body.get("transcript") or body.get("stem") or "").strip()
    if not stem:
        return 400, {"ok": False, "error": "请提供转写产物（transcript）"}
    name = stem if stem.endswith(".json") else f"{stem}.json"
    tpath = _safe_join(ws.transcripts_dir, name)
    if tpath is None or not tpath.is_file():
        return 404, {"ok": False, "error": f"转写产物不存在：{name}"}
    cpath = ws.candidates_dir / f"{tpath.stem}.candidates.json"
    if not cpath.is_file():
        return 404, {"ok": False, "error": "还没有候选产物，请先切片"}
    try:
        transcript = json.loads(tpath.read_text(encoding="utf-8"))
        cand = json.loads(cpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"ok": False, "error": f"产物读取失败：{exc}"}
    # v4.75 P-C：录制报告里的**内容死区**（gaps）——切片据此剔除跨断层候选
    try:
        _rec_path = ws.candidates_dir.parent / "live_raw" / f"{tpath.stem}.record.json"
        if _rec_path.is_file():
            _rec = json.loads(_rec_path.read_text(encoding="utf-8"))
            if isinstance(_rec.get("gaps"), list):
                transcript["gaps"] = _rec["gaps"]
    except Exception:                                      # noqa: BLE001
        pass
    res = integrity.judge(transcript, cand.get("candidates") or [])
    return 200, res


@route("POST", "/api/slice")
def h_slice_run(handler, _params) -> tuple[int, dict]:
    """把一份转写产物切成候选片段（§6.4）。无 Key 时自动走本地规则规划器。"""
    body = handler._read_json_body()
    stem = str(body.get("transcript") or body.get("stem") or "").strip()
    mode = str(body.get("mode") or "story").strip()
    planner = str(body.get("planner") or "auto").strip()
    params = body.get("params") if isinstance(body.get("params"), dict) else None
    # v3.19：内容类型。请求里没给就用偏好值（默认 auto → 本地识别），
    # 这样「换一种直播内容」不需要改代码，也不需要每次手动传。
    from app.slicer import prompts
    content_type = str(body.get("contentType") or load_content_type()).strip()

    if mode not in ("story", "commerce"):
        return 400, {"error": "mode 只能是 story（故事）或 commerce（带货）"}
    if planner not in ("auto", "local_fallback", "llm", "deepseek"):
        return 400, {"error": "planner 只能是 auto / local_fallback / llm"}
    if content_type not in prompts.TYPE_ORDER:
        return 400, {"error": f"contentType 只能是 {' / '.join(prompts.TYPE_ORDER)}"}
    if not stem:
        return 400, {"error": "请提供要切片的转写产物（transcript）"}

    name = stem if stem.endswith(".json") else f"{stem}.json"
    tpath = _safe_join(ws.transcripts_dir, name)
    if tpath is None:
        return 400, {"error": "转写文件路径越界，已拒绝"}
    if not tpath.is_file():
        return 404, {"error": f"转写产物不存在：{name}（先到「转写」页转一次）"}

    try:
        transcript = json.loads(tpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"error": f"转写产物读取失败：{exc}"}
    if not isinstance(transcript, dict) or not transcript.get("segments"):
        return 400, {"error": "转写产物里没有 segments，请重新转写"}
    # v4.75 P-C：录制报告里的**内容死区**（gaps）——切片据此剔除跨断层候选
    try:
        _rec_path = ws.candidates_dir.parent / "live_raw" / f"{tpath.stem}.record.json"
        if _rec_path.is_file():
            _rec = json.loads(_rec_path.read_text(encoding="utf-8"))
            if isinstance(_rec.get("gaps"), list):
                transcript["gaps"] = _rec["gaps"]
    except Exception:                                      # noqa: BLE001
        pass

    # v3.132：本场备注（「这是什么内容 / 按什么单元切」）—— 请求里给了就用它，
    # 否则按**素材文件名**从 sidecar（<视频>.note.json）读。
    from app.slicer import notes as notes_mod
    note = body.get("note") if isinstance(body.get("note"), dict) else None
    if note is None:
        src_name = str(transcript.get("videoName") or transcript.get("video") or "")
        if not src_name:
            src_name = str(body.get("videoName") or "")
        note = notes_mod.note_of(src_name) if src_name else None

    # v3.140：「按复核意见重新拆分」—— 前端把上一版完整性复核的判定与建议拼成文本传上来
    review_hint = str(body.get("reviewHint") or "").strip()

    # v3.159：这次切片挂一个「停止」令牌；前端点停止 → /api/slice/cancel 置位，
    # 主流程在各关键点抛 SliceCancelled ⇒ 到这里直接回 409 且**不写任何候选文件**
    # （上一版转写产物与上次切好的候选都原样保留，重跑不受影响）。
    token = cancel_flags.begin("slice")
    try:
        result = slicing_engine.slice_transcript(
            transcript, planner=planner, mode=mode, params=params,
            content_type=content_type, note=note,
            review_hint=review_hint or None,
            should_cancel=lambda: token.is_set())
    except slicing_engine.SliceCancelled as exc:
        return 409, {"ok": False, "cancelled": True,
                     "error": f"已停止：{exc}（本次切片结果不保存，转写产物与已有候选未动）"}
    except Exception as exc:      # 引擎异常转 500，不让服务挂掉
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        cancel_flags.end("slice")

    # v3.26：把识别出的内容类型回填到「录了这个文件的主播」的历史直播间上
    # （录制素材命名 = <主播>_<时间戳>.mp4，按主播名前缀匹配；认不准就不写）
    try:
        rid = MANAGER.note_content_type_for_source(
            result.get("videoName") or tpath.stem, result.get("contentType"))
        if rid:
            result.setdefault("notes", []).append(
                f"内容类型已记到历史直播间 {rid} 名下（「上一场直播类型」）")
    except Exception:            # 回填失败绝不能影响切片结果
        pass

    out = ws.candidates_dir / f"{tpath.stem}.candidates.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
        result["outputPath"] = str(out)
    except OSError as exc:
        result["outputPath"] = ""
        result.setdefault("notes", []).append(f"候选落盘失败（结果仍有效）：{exc}")

    # v4.69 P2-D（方案主线 D）：切片质量报告独立落盘 —— 改参数后拿两份对比即可，
    # 不用肉眼判断"这次切得好不好"。失败不影响切片结果本身。
    q = result.get("quality")
    if isinstance(q, dict):
        try:
            qpath = ws.candidates_dir / f"{tpath.stem}.quality.json"
            qtmp = qpath.with_suffix(".tmp")
            qtmp.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
            qtmp.replace(qpath)
        except OSError as exc:
            result.setdefault("notes", []).append(f"质量报告落盘失败（不影响结果）：{exc}")
    return 200, result


@route("POST", "/api/slice/cancel")
def h_slice_cancel(_handler, _params) -> tuple[int, dict]:
    """v3.159：停止正在跑的切片（协作式）。

    只置停止位，不杀线程 —— 切片会在**下一个检查点**（每个 AI 窗口 / 落盘前）退出，
    并保证不写候选文件。返回 ``running`` 说明当时是否真有一轮在跑（空点不谎报成功）。
    """
    running = cancel_flags.request("slice")
    return 200, {"ok": True, "running": running}


@route("GET", "/api/candidates")
def h_candidates_list(_handler, _params) -> tuple[int, dict]:
    """候选产物列表（扫描目录，不依赖内存状态）。"""
    root = ws.candidates_dir
    files: list[dict] = []
    if root.is_dir():
        for p in sorted(root.glob("*.candidates.json")):
            try:
                stat = p.stat()
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cands = data.get("candidates") or []
            top = max((float((c.get("scores") or {}).get("overall") or 0) for c in cands),
                      default=0.0)
            files.append({
                "stem": p.name.replace(".candidates.json", ""),
                "file": p.name,
                "path": str(p),
                "videoName": data.get("videoName") or data.get("video", ""),
                "planner": data.get("planner", ""),
                "model": data.get("model", ""),
                "mode": data.get("mode", ""),
                # v3.19：这次切片是按哪套 prompt/权重做的（列表页与详情页都要能看出来）
                "contentType": data.get("contentType", ""),
                "typeConfidence": data.get("typeConfidence", 1.0),
                "typeEvidence": data.get("typeEvidence") or [],
                "candidateCount": data.get("candidateCount", len(cands)),
                "rejectedCount": len(data.get("rejected") or []),
                "topScore": round(top, 1),
                "audioSec": data.get("audioSec", 0),
                "elapsedSec": data.get("elapsedSec", 0),
                "sizeKb": round(stat.st_size / 1024, 1),
                "mtime": round(stat.st_mtime, 1),
                # v3.60（）：这份产物的**源素材还在不在**。不在 = 预览与进剪辑
                # 都必然失败（源没了，区间标记就无从播放）。列表页必须提前说出来，
                # 否则用户点下去只看到「没反应」（真窗口取证的原始症状）。
                # 判据与 /api/cut/plan 同源（renderer.candidate_source_exists）。
                "sourceExists": renderer_engine.candidate_source_exists(
                    p.name.replace(".candidates.json", "")),
            })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return 200, {"files": files, "count": len(files), "dir": str(root)}


@route("GET", "/api/candidates/detail")
def h_candidates_detail(_handler, params) -> tuple[int, dict]:
    """某份候选产物的完整内容（UI 表格里点开看详情用）。"""
    stem = str(params.get("stem") or "").strip()
    if not stem:
        return 400, {"error": "缺少 stem 参数"}
    name = stem if stem.endswith(".candidates.json") else f"{stem}.candidates.json"
    path = _safe_join(ws.candidates_dir, name)
    if path is None:
        return 400, {"error": "路径越界，已拒绝"}
    if not path.is_file():
        return 404, {"error": f"候选产物不存在：{name}"}
    try:
        return 200, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"error": f"读取失败：{exc}"}


# ---------------------------------------------------------------- 产物删除
# 三处删除共用 app\common\file_ops.delete_files_in（越界/目录/受保护文件一律拒绝）。
# 转写产物比较特殊：一个 stem 对应 **.json + .srt 两个文件**，而且可能只产出了其中一个，
# 所以先按 stem 展开成实际存在的文件，再交给共享实现 —— 否则"缺失的那一半"会被误报成失败。

def _delete_by_stems(root: Path, stems, exts: tuple[str, ...],
                     label: str) -> tuple[int, dict]:
    wanted: list[str] = []
    failed: list[dict] = []
    for stem in (stems or []):
        stem = str(stem).strip()
        if not is_safe_stem(stem):
            failed.append({"rel": stem, "error": "非法名称，已拒绝"})
            continue
        found = [f"{stem}{ext}" for ext in exts if (root / f"{stem}{ext}").is_file()]
        if not found:
            failed.append({"rel": stem, "error": "文件不存在"})
            continue
        wanted.extend(found)
    status, payload = delete_files_in(
        root, wanted, log=lambda m: print(f"[slicer] {label} {m}", flush=True))
    if failed:
        payload["failed"] = failed + payload["failed"]
        payload["ok"] = False
    return status, payload


@route("POST", "/api/transcripts/delete")
def h_transcripts_delete(handler, _params) -> tuple[int, dict]:
    """删除转写产物（按 stem 连带删 .json 与 .srt）。"""
    body = handler._read_json_body()
    stems = body.get("stems") or body.get("files")
    if not isinstance(stems, list) or not stems:
        return 400, {"error": "请提供要删除的转写产物列表（stems）"}
    return _delete_by_stems(ws.transcripts_dir, stems, (".json", ".srt"), "删除转写产物")


@route("POST", "/api/candidates/delete")
def h_candidates_delete(handler, _params) -> tuple[int, dict]:
    """删除切片候选产物（整份元数据 JSON）。"""
    body = handler._read_json_body()
    stems = body.get("stems") or body.get("files")
    if not isinstance(stems, list) or not stems:
        return 400, {"error": "请提供要删除的候选产物列表（stems）"}
    return _delete_by_stems(ws.candidates_dir, stems, (".candidates.json",),
                            "删除候选产物")


@route("POST", "/api/candidates/remove-item")
def h_candidates_remove_item(handler, _params) -> tuple[int, dict]:
    """**从一份候选里删掉单个片段**（人工筛片），重新编号并原子写回。

    与"删整份产物"不同：这里改的是产物**内容**，所以要留下痕迹 ——
    记 `manuallyEdited` 并把被删片段追加进 `rejected`（原因标「人工移除」），
    将来 M7 统计「人工修改率」时就有依据。
    """
    body = handler._read_json_body()
    stem = str(body.get("stem") or "").strip()
    idx = body.get("idx")
    if not is_safe_stem(stem):
        return 400, {"error": "非法名称，已拒绝"}
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return 400, {"error": "缺少要移除的片段编号（idx）"}

    name = stem if stem.endswith(".candidates.json") else f"{stem}.candidates.json"
    path = _safe_join(ws.candidates_dir, name)
    if path is None:
        return 400, {"error": "路径越界，已拒绝"}
    if not path.is_file():
        return 404, {"error": f"候选产物不存在：{name}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"error": f"读取失败：{exc}"}

    cands = list(data.get("candidates") or [])
    removed = next((c for c in cands if int(c.get("idx") or -1) == idx), None)
    if removed is None:
        return 404, {"error": f"这份候选里没有编号 {idx} 的片段"}
    cands = [c for c in cands if int(c.get("idx") or -1) != idx]

    # 重新编号：idx 与 publishPriority 都按新顺序刷新，避免留空洞
    for i, c in enumerate(cands):
        c["idx"] = i + 1
        c["publishPriority"] = i + 1
    data["candidates"] = cands
    data["candidateCount"] = len(cands)
    data["manuallyEdited"] = True
    rejected = list(data.get("rejected") or [])
    rejected.append({"start": removed.get("start"), "end": removed.get("end"),
                     "reason": "人工移除"})
    data["rejected"] = rejected
    notes = list(data.get("notes") or [])
    notes.append(f"人工移除了原 {idx} 号片段（{removed.get('title') or '无标题'}）")
    data["notes"] = notes

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return 500, {"error": f"写回失败：{exc}"}
    print(f"[slicer] 人工移除候选：{name} 第 {idx} 条 → 剩 {len(cands)} 条", flush=True)
    # M7：记录人工干预（评分闭环数据）
    tuning_engine.record_feedback("candidate_removed",
                                  f"{stem}#{idx} {removed.get('title') or ''}")
    return 200, {"ok": True, "removedIdx": idx, "candidateCount": len(cands), "data": data}


# ------------------------------------------------- v4.70 P4（方案主线 E2/E3）
# 人工干预入口：E2「重跑本窗」+ E3「合并两条相邻候选」。
# 都改的是候选**内容**（与 remove-item 同一套原子写回 / manuallyEdited / rejected 记录），
# 不删任何视频文件，也不碰发布链路。


@route("POST", "/api/slice/replan-window")
def h_slice_replan_window(handler, _params) -> tuple[int, dict]:
    """**E2：只重跑某一窗的 LLM 规划**（方案主线 E2）。

    验收口径：点一次 = **恰好 1 次 LLM 调用**；其他窗的候选一条不动。
    与整场重切（btnCutReanalyze）的区别：不重转写、其他窗候选原样保留。
    改动落在 candidates.json（manuallyEdited 记录），恢复 = revert 文件。
    """
    body = handler._read_json_body()
    stem = str(body.get("stem") or "").strip()
    if not is_safe_stem(stem):
        return 400, {"error": "非法名称，已拒绝"}
    try:
        w_start = float(body.get("start"))
        w_end = float(body.get("end"))
    except (TypeError, ValueError):
        return 400, {"error": "缺少本窗的 start / end"}
    if w_end <= w_start:
        return 400, {"error": "本窗 end 必须大于 start"}

    tr_path = _safe_join(ws.transcripts_dir, f"{stem}.json")
    cand_path = _safe_join(ws.candidates_dir, f"{stem}.candidates.json")
    if tr_path is None or cand_path is None:
        return 400, {"error": "路径越界，已拒绝"}
    if not tr_path.is_file():
        return 404, {"error": f"找不到转写产物：{stem}.json（重跑本窗需要原始转写）"}
    if not cand_path.is_file():
        return 404, {"error": f"候选产物不存在：{stem}.candidates.json"}
    try:
        transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        data = json.loads(cand_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"error": f"读取失败：{exc}"}

    segments = list(transcript.get("segments") or [])
    if not segments:
        return 400, {"error": "这份转写里没有句子，没法重跑"}

    from .slicing import (DEFAULT_PARAMS, build_windows, finalize_candidates,
                          params_for, total_of)
    saved_params = dict(data.get("params") or {})
    resolved = {**DEFAULT_PARAMS, **saved_params}
    resolved["contentType"] = data.get("contentType") or "general"
    resolved["mode"] = data.get("mode") or "story"
    note = data.get("note") or {}
    if note:
        resolved["note"] = dict(note)

    total_sec = total_of(segments)

    # 情绪雷达：重跑也要带上 [高能] 标记（有缓存，代价约等于零）。失败静默降级。
    video_path = str(transcript.get("video") or "")
    if video_path:
        try:
            from . import audio_energy
            segments, energy_info = audio_energy.enrich(video_path, segments)
            if energy_info and energy_info.get("peaks"):
                resolved["_energyPeaks"] = energy_info["peaks"]
        except Exception:                                     # noqa: BLE001
            pass

    windows = build_windows(segments, resolved["windowSec"], resolved["overlapSec"])
    if not windows:
        return 400, {"error": "这份转写切不出窗口"}

    # 找与 [w_start, w_end] 重叠最大的那一窗（前端传的是行所属窗的范围）
    def _overlap(win: dict) -> float:
        return min(w_end, win["end"]) - max(w_start, win["start"])

    win = max(windows, key=_overlap)
    if _overlap(win) <= 0:
        return 400, {"error": (f"给出的范围 {w_start:.0f}-{w_end:.0f}s 与任何窗口都不重叠"
                               f"（这份素材共 {len(windows)} 窗）")}

    from .llm import (LLMError, build_messages as _llm_build_messages,
                      chat_json, extract_json, load_config,
                      validate_clips_payload)
    cfg = load_config()
    if not cfg.configured:
        return 400, {"error": "未配置 API Key —— 重跑本窗需要 AI 规划"}

    w = dict(win)
    w["_totalSec"] = total_sec
    q_old = data.get("quality") or {}
    # v4.68：任务参数覆盖段按**上次切片的档位**重建（素材没变，档位也不会变）
    tier = params_for(total_sec)
    resolved["_taskParams"] = {"totalSec": round(total_sec, 1), "tier": tier["tier"],
                               "suggestLo": tier["suggestLo"], "suggestHi": tier["suggestHi"],
                               "minSec": resolved.get("minSec") or tier["minSec"],
                               "maxSec": resolved.get("maxSec") or tier["maxSec"]}
    messages = _llm_build_messages(w, resolved, win["index"], len(windows))
    try:
        content = chat_json(cfg, messages)                 # <- 恰好 1 次 LLM 调用
        new_clips, new_rej, warns = validate_clips_payload(
            extract_json(content), resolved)
    except LLMError as exc:
        return 502, {"error": f"重跑本窗失败（候选未动）：{exc}"}
    except Exception as exc:                               # noqa: BLE001
        return 500, {"error": f"重跑本窗异常（候选未动）：{type(exc).__name__}: {exc}"}

    fin, fin_rej = finalize_candidates(new_clips, segments, resolved, "llm",
                                       snap_stats=[], stats_out={})

    # 替换：旧候选中心点落在本窗的移除；其他窗一条不动（验收口径）
    def _in_win(c: dict) -> bool:
        center = (float(c.get("start") or 0) + float(c.get("end") or 0)) / 2
        return win["start"] <= center < win["end"]

    old_in_win = [c for c in (data.get("candidates") or []) if _in_win(c)]
    kept = [c for c in (data.get("candidates") or []) if not _in_win(c)]
    merged = kept + fin
    merged.sort(key=lambda c: (-float((c.get("scores") or {}).get("overall") or 0),
                               float(c.get("start") or 0)))
    for i, c in enumerate(merged):
        c["idx"] = i + 1
        c["publishPriority"] = i + 1

    data["candidates"] = merged
    data["candidateCount"] = len(merged)
    data["manuallyEdited"] = True
    rejected = list(data.get("rejected") or [])
    rejected.append({"start": win["start"], "end": win["end"],
                     "reason": f"人工重跑本窗：旧 {len(old_in_win)} 条已替换为新 {len(fin)} 条"
                               + (f"；另有 {len(new_rej)} 条 rejected" if new_rej else "")})
    data["rejected"] = rejected[-80:]
    notes = list(data.get("notes") or [])
    notes.append(f"人工重跑第 {win['index'] + 1}/{len(windows)} 窗"
                 f"（{win['start']:.0f}-{win['end']:.0f}s）：旧 {len(old_in_win)} 条 -> 新 {len(fin)} 条")
    data["notes"] = notes[-80:]

    q = dict(q_old)
    q["candidates"] = len(merged)
    q["finalCandidates"] = len(merged)
    q["replanCount"] = int(q.get("replanCount") or 0) + 1
    q["lastReplannedWindow"] = {"index": win["index"],
                                "start": win["start"], "end": win["end"],
                                "oldCount": len(old_in_win), "newCount": len(fin)}
    data["quality"] = q
    try:
        qpath = ws.candidates_dir / f"{stem}.quality.json"
        qpath.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:                                        # noqa: BLE001
        pass

    try:
        tmp = cand_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cand_path)
    except OSError as exc:
        return 500, {"error": f"写回失败：{exc}"}
    print(f"[slicer] 人工重跑本窗：{stem} 窗{win['index'] + 1} "
          f"旧 {len(old_in_win)} -> 新 {len(fin)}（LLM 1 次调用）", flush=True)
    return 200, {"ok": True, "window": {"index": win["index"],
                                        "start": win["start"], "end": win["end"]},
                 "replacedOld": len(old_in_win), "newCandidates": len(fin),
                 "candidateCount": len(merged), "data": data}


@route("POST", "/api/candidates/merge")
def h_candidates_merge(handler, _params) -> tuple[int, dict]:
    """**E3：把两条相邻候选合并为一条**（方案主线 E3）。

    相邻 = 按 start 排序后两条紧挨着（中间没有第三条候选的中心点），且 gap ≤ 90 秒
    或时间上重叠。合并规则：start 取前者、end 取后者，三维分数按时长加权平均，
    scoreSource 保留（混源取低者），notes 写明"合并自 #A+#B，建议重新评分"。
    """
    body = handler._read_json_body()
    stem = str(body.get("stem") or "").strip()
    if not is_safe_stem(stem):
        return 400, {"error": "非法名称，已拒绝"}
    try:
        idx_a = int(body.get("idxA"))
        idx_b = int(body.get("idxB"))
    except (TypeError, ValueError):
        return 400, {"error": "缺少要合并的两条编号（idxA / idxB）"}
    if idx_a == idx_b:
        return 400, {"error": "两条编号相同，没法合并"}

    name = stem if stem.endswith(".candidates.json") else f"{stem}.candidates.json"
    path = _safe_join(ws.candidates_dir, name)
    if path is None:
        return 400, {"error": "路径越界，已拒绝"}
    if not path.is_file():
        return 404, {"error": f"候选产物不存在：{name}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 400, {"error": f"读取失败：{exc}"}

    cands = list(data.get("candidates") or [])
    a = next((c for c in cands if int(c.get("idx") or -1) == idx_a), None)
    b = next((c for c in cands if int(c.get("idx") or -1) == idx_b), None)
    if a is None or b is None:
        return 404, {"error": "这份候选里没有这两条编号"}

    first, second = ((a, b) if float(a.get("start") or 0) <= float(b.get("start") or 0)
                     else (b, a))
    for c in cands:                       # 相邻：中间不能夹第三条候选的中心点
        if c is first or c is second:
            continue
        center = (float(c.get("start") or 0) + float(c.get("end") or 0)) / 2
        if float(first.get("end") or 0) < center < float(second.get("start") or 0):
            return 400, {"error": f"这两条中间还夹着 #{c.get('idx')}，不是相邻候选"}

    gap = float(second.get("start") or 0) - float(first.get("end") or 0)
    if gap > 90:
        return 400, {"error": f"两条之间空了 {gap:.0f} 秒，合并出来的片段会太长"
                              "（先删掉中间不要的内容，或分开保留）"}

    dur_a = max(float(first.get("end") or 0) - float(first.get("start") or 0), 0.1)
    dur_b = max(float(second.get("end") or 0) - float(second.get("start") or 0), 0.1)
    sa, sb = first.get("scores") or {}, second.get("scores") or {}
    merged_scores = {}
    for k in ("value", "comedy", "completeness", "overall"):
        va, vb = sa.get(k), sb.get(k)
        try:
            va = float(va) if va is not None else None
            vb = float(vb) if vb is not None else None
        except (TypeError, ValueError):
            va = vb = None
        if va is None and vb is None:
            merged_scores[k] = 50.0
        elif va is None:
            merged_scores[k] = vb
        elif vb is None:
            merged_scores[k] = va
        else:
            merged_scores[k] = round((va * dur_a + vb * dur_b) / (dur_a + dur_b), 1)
    from .slicing import weighted_overall as _wo
    merged_scores["overall"] = _wo(str(data.get("contentType") or "general"), merged_scores)

    merged = dict(first)
    merged["start"] = min(float(first.get("start") or 0), float(second.get("start") or 0))
    merged["end"] = max(float(first.get("end") or 0), float(second.get("end") or 0))
    merged["durationSec"] = round(merged["end"] - merged["start"], 2)
    merged["scores"] = merged_scores
    sources = {str(first.get("scoreSource") or "llm"),
               str(second.get("scoreSource") or "llm")}
    merged["scoreSource"] = (sources.pop() if len(sources) == 1
                             else ("fallback" if "fallback" in sources else "local"))
    merged.pop("hook", None)           # 钩子跨了合并边界多半越界 => 删掉（渲染端按无钩子处理）
    merged["keywords"] = (first.get("keywords") or [])[:4]
    merged["summary"] = str(first.get("summary") or "")[:200]
    merged.setdefault("notes", []).append(
        f"合并自 #{idx_a} 与 #{idx_b}（分数为时长加权均分），建议重新评分")

    merged_list = [c for c in cands if c is not first and c is not second]
    merged_list.append(merged)
    merged_list.sort(key=lambda c: float(c.get("start") or 0))
    for i, c in enumerate(merged_list):
        c["idx"] = i + 1
        c["publishPriority"] = i + 1
    data["candidates"] = merged_list
    data["candidateCount"] = len(merged_list)
    data["manuallyEdited"] = True
    rejected = list(data.get("rejected") or [])
    rejected.append({"start": first.get("start"), "end": second.get("end"),
                     "reason": f"人工合并 #{idx_a} + #{idx_b}，合并后时长 "
                               f"{merged['durationSec']:.0f}s"})
    data["rejected"] = rejected[-80:]
    notes = list(data.get("notes") or [])
    notes.append(f"人工合并原 #{idx_a} 与 #{idx_b} -> {merged['durationSec']:.1f}s"
                 "（分数为时长加权均分，建议重新评分）")
    data["notes"] = notes[-80:]

    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return 500, {"error": f"写回失败：{exc}"}
    print(f"[slicer] 人工合并候选：{name} #{idx_a} + #{idx_b} -> "
          f"{merged['durationSec']:.1f}s", flush=True)
    tuning_engine.record_feedback("candidate_merged", f"{stem} #{idx_a}+#{idx_b}")
    return 200, {"ok": True, "merged": merged, "candidateCount": len(merged_list),
                 "data": data}


# ---------------------------------------------------------------- AI 服务连通性

@route("POST", "/api/slice/llm-config/test")
def h_slice_llm_test(handler, _params) -> tuple[int, dict]:
    """测试 AI 服务是否可用（§5.1「测试连接」）。

    body 里可带 **尚未保存** 的 ``baseUrl`` / ``model`` / ``apiKey`` —— 先试再存，
    免得为了测一下就把可能写错的 Key 落盘。返回体**绝不含完整 Key**。
    """
    import dataclasses

    body = handler._read_json_body()
    cfg = load_llm_config()
    overrides: dict = {}
    if body.get("baseUrl"):
        overrides["base_url"] = str(body["baseUrl"]).strip()
    if body.get("model"):
        overrides["model"] = str(body["model"]).strip()
    if body.get("provider"):
        overrides["provider"] = str(body["provider"]).strip()
    raw_key = str(body.get("apiKey") or "").strip()
    if raw_key:
        try:
            overrides["api_key"] = decode_api_key(raw_key)
        except Exception as exc:
            return 400, {"error": f"API Key 无法解析（dpapi 密文可能不是本机生成的）：{exc}"}
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)
    result = test_llm_connection(cfg)
    print(f"[slicer] AI 连通性测试：ok={result['ok']} "
          f"延迟={result['latencyMs']}ms 错误={result['error'][:80]}", flush=True)
    return 200, result


@route("POST", "/api/slice/llm-models")
def h_slice_llm_models(handler, _params) -> tuple[int, dict]:
    """列出**这个 Key 真正可用**的模型名（v3.44，照 ``GET /models``）。

    为什么需要这个端点：模型名是服务端的自由文本，用户不可能背下来。
    2026-09-24 用户把模型填成 ``deepseek-v41-flash``（该账号没这个名字），
    只在「测试连通性」看到一个 400，界面上完全看不出该填什么 ——
    实测该账号可用的只有 ``deepseek-flash`` / ``deepseek-v4-pro``。

    与测试端点同样的「可先试再存」语义：body 里可带**尚未保存**的
    ``baseUrl`` / ``apiKey``；**返回体绝不含完整 Key**。
    """
    import dataclasses

    body = handler._read_json_body()
    cfg = load_llm_config()
    overrides: dict = {}
    if body.get("baseUrl"):
        overrides["base_url"] = str(body["baseUrl"]).strip()
    if body.get("provider"):
        overrides["provider"] = str(body["provider"]).strip()
    raw_key = str(body.get("apiKey") or "").strip()
    if raw_key:
        try:
            overrides["api_key"] = decode_api_key(raw_key)
        except Exception as exc:                              # noqa: BLE001
            return 400, {"error": f"API Key 无法解析（dpapi 密文可能不是本机生成的）：{exc}"}
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)
    result = llm_list_models(cfg)
    print(f"[slicer] 列出可用模型：ok={result['ok']} "
          f"数量={len(result['models'])} 错误={result['error'][:80]}", flush=True)
    return 200, result


@route("GET", "/api/slice/llm-providers")
def h_slice_llm_providers(_handler, _params) -> tuple[int, dict]:
    """**厂商预设清单**（v3.81）—— 设置页「服务商」下拉的唯一数据来源。

    为什么要有这个端点：这份清单若写死在 `index.html` 里，就会变成「第 2 份厂商表」——
    加一个厂商要改两处、改漏一处就是**下拉里有选不出值**（K44 同源）。
    桌面桥放行「去哪拿 Key」链接也用这张表做白名单，所以只能有一份。
    """
    return 200, {"providers": llm_providers_view()}


# ---------------------------------------------------------------- 显卡与加速（v3.81）

@route("GET", "/api/gpu/busy")
def h_gpu_busy(_handler, _params) -> tuple[int, dict]:
    """显卡**当前被谁占着**（v4.61，给界面做并发提示用）。

    只读内存里的任务表：不加锁、不 import torch、不调 nvidia-smi，单次 < 1ms。
    前端只查这一个接口 —— 别各页去调对方的重接口（K44）。
    """
    return 200, gpu_engine.busy_owner()


@route("GET", "/api/system/gpu")
def h_gpu_probe(_handler, _params) -> tuple[int, dict]:
    """显卡 / CUDA / NVENC / Whisper 的**体检汇总**（毫秒级，可以放心轮询）。

    ⚠️ 它回答的是「**该具备的条件齐不齐**」，不是「**真的能用吗**」——
    后者要看 `/api/system/gpu/selftest` 的实测结果。
    """
    return 200, gpu_engine.probe()


@route("POST", "/api/system/gpu/selftest")
def h_gpu_selftest_start(_handler, _params) -> tuple[int, dict]:
    """启动一次实测（NVENC 真编 1 秒 + Whisper 真加载一次 GPU 模型）。

    为什么要做成 submit 模式：Whisper 那次要加载模型、占显存，最快也要几秒，
    **绝不能放在每 4 秒一次的轮询路径上**（K65 同源：慢活在后台，前端轮询状态）。
    """
    return 200, gpu_engine.start_selftest()


@route("GET", "/api/system/gpu/selftest")
def h_gpu_selftest_state(_handler, _params) -> tuple[int, dict]:
    return 200, gpu_engine.selftest_state()


@route("GET", "/api/system/ffmpeg")
def h_ffmpeg_state(_handler, _params) -> tuple[int, dict]:
    """FFmpeg 状态（缺文件时前端给出「下载 FFmpeg」按钮）。"""
    return 200, ffmpeg_setup.state()


@route("POST", "/api/system/ffmpeg/download")
def h_ffmpeg_download(handler, _params) -> tuple[int, dict]:
    """一键下载 FFmpeg（后台线程，状态结构与 CUDA 修复一致）。"""
    handler._read_json_body()
    return 200, ffmpeg_setup.start_download()


@route("POST", "/api/system/cuda/repair")
def h_cuda_repair_start(_handler, _params) -> tuple[int, dict]:
    """缺 CUDA DLL 时的一键修复：`pip download` → 解 DLL → 校验（后台跑）。

    ⚠️ 只下**缺的那几个** nvidia wheel，版本号从本机 ctranslate2 的依赖声明里读，
    不装 CUDA Toolkit、不动系统 —— 详见 `app/slicer/gpu.py` 的模块注释。
    """
    return 200, gpu_engine.start_repair()


@route("GET", "/api/system/cuda/repair")
def h_cuda_repair_state(_handler, _params) -> tuple[int, dict]:
    return 200, gpu_engine.repair_state()


# ---------------------------------------------------------------- 渲染（M4，§6.5/§6.6）

@route("GET", "/api/render/options")
def h_render_options(_handler, _params) -> tuple[int, dict]:
    """渲染页需要的下拉数据：候选产物、BGM、默认渲染参数、当前显存。"""
    # v3.19：BGM 列表**走 renderer 那一份实现**（音乐素材 + 我的上传），
    # 不在这里自己扫目录 —— 导出统一落到 uploads，只扫 music 会让下拉是空的（K44）
    music = renderer_engine.list_bgm_files()
    return 200, {
        "strategies": ["conservative", "aggressive"],
        "strategydesc": {
            "conservative": "保守：整段保留，只把钩子剪到开头",
            "aggressive": "激进：按停顿剪掉空档（留太少会自动放弃剪）",
        },
        # D30：画幅默认**跟随录制源**（v3.137 起保持源比例：4:3 就出 4:3，不再塞进 16:9）
        "orientations": list(ORIENT_CHOICES),
        "orientationdesc": {
            "auto": "自动：跟随录制的比例（16:9 / 9:16 / 4:3 / 3:4 都原样出片，不裁剪）",
            "portrait": "竖屏 9:16（1080×1920；源是横屏时会居中裁剪）",
            "landscape": "横屏 16:9（1920×1080；源是竖屏时会居中裁剪）",
        },
        "targetSize": {
            "portrait": {"width": TARGET_PORTRAIT[0], "height": TARGET_PORTRAIT[1]},
            "landscape": {"width": TARGET_LANDSCAPE[0], "height": TARGET_LANDSCAPE[1]},
        },
        "music": music,
        "editsDir": str(ws.editor_output_dir),
        "musicDir": str(ws.music_dir),
        "musicRoots": [{"label": label, "dir": str(d)}
                       for label, d in renderer_engine.bgm_roots()],
        "vramUsedMib": gpu_memory_used_mib(),
        "vramLimitMib": RENDERER.VRAM_LIMIT_MIB,
    }


@route("POST", "/api/render")
def h_render_submit(handler, _params) -> tuple[int, dict]:
    """提交渲染任务。**串行排队 + 显存守卫**。

    两种模式（v3.39）：
    - 带 ``minScore`` → **按分数批量**：把这份候选产物里综合评分 ≥ 该分的**所有小片段**
      各排一个任务（用户：「渲染任务改为把指定分数的切片小片段都进行操作」）；
    - 否则 → 单条（``idx`` 省略 = 挑最高分那条），保持原来的行为。
    """
    body = handler._read_json_body()
    common = dict(
        candidate=str(body.get("candidate") or "").strip(),
        source=(str(body["source"]).strip() if body.get("source") else None),
        strategy=str(body.get("strategy") or "conservative").strip(),
        burn_subtitles=bool(body.get("burnSubtitles", True)),
        bgm=(str(body["bgm"]).strip() if body.get("bgm") else None),
        params=body.get("params") if isinstance(body.get("params"), dict) else None,
    )
    min_score = body.get("minScore")
    if min_score not in (None, ""):
        try:
            threshold = float(min_score)
        except (TypeError, ValueError):
            return 400, {"error": "分数阈值不合法（minScore）"}
        return RENDERER.submit_batch(min_score=threshold, **common)
    return RENDERER.submit(idx=body.get("idx"), **common)


@route("GET", "/api/render/status")
def h_render_status(_handler, params) -> tuple[int, dict]:
    return RENDERER.status(str(params.get("job") or "").strip() or None)


# ---------------------------------------------------------------- 预览剪辑（M4.5）

@route("GET", "/api/cut/plan")
def h_cut_plan(_handler, params) -> tuple[int, dict]:
    """剪辑页加载：候选 + 转写 → 源视频/区间/字幕段/已有草稿。

    v3.79：候选产物可能**已经被删**（用户在素材库里清候选时常见）。那时不让这条路断掉 ——
    成片自己带着 keepRanges（`.edit.json`），按它重建一份计划照样能改这条成片。
    所以前端要把**成片文件名**一起传上来（``film=``）。
    """
    from app.slicer import cut as cut_engine
    idx = params.get("idx")
    film = str(params.get("film") or "").strip()
    try:
        return 200, cut_engine.plan_cut(
            str(params.get("candidate") or ""),
            int(idx) if idx not in (None, "") else None)
    except FileNotFoundError as exc:
        if film:
            try:
                plan = cut_engine.plan_cut_from_film(film)
                plan["primaryError"] = str(exc)      # 原始失败原因也带上（界面要说清）
                return 200, plan
            except (FileNotFoundError, ValueError) as exc2:
                return 404, {"error": f"{exc}；按成片重建也失败：{exc2}"}
        return 404, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}


def _draft_base(candidate: str) -> str:
    """候选名（可带 ``.candidates.json``）→ 草稿用的 stem。"""
    raw = str(candidate or "").strip()
    return raw[: -len(".candidates.json")] if raw.endswith(".candidates.json") else raw


def _fallback_ranges(candidate: str, idx: int) -> list[dict]:
    """没草稿时的默认 keep 段 = AI 给的完整区间（「恢复 AI 原方案」也用它）。"""
    from app.slicer import cut as cut_engine
    _base, picked, _src, _d = cut_engine.resolve_target(_draft_base(candidate), int(idx))
    return [{"startMs": round(float(picked["start"]) * 1000.0, 1),
             "endMs": round(float(picked["end"]) * 1000.0, 1)}]


def decisions_to_render_params(dec: dict) -> dict:
    """六个工具的决策 → ``RenderParams`` 字段（**唯一换算入口**）。

    音量在 UI 上是百分比（与参考实现一致：0–100%），渲染层要的是倍数。
    v3.41：字幕样式（大小 / XY 位置）偏离默认时随 params 下发
    ``subtitleLayout``，渲染 worker 在写 ASS 前套用（见 subtitles.build_ass）。
    """
    params: dict = {
        "orientation": str(dec.get("orientation") or "auto"),
        "voice_volume": round(float(dec.get("speechVolume") or 0) / 100.0, 3),
        "bgm_volume": round(float(dec.get("musicVolume") or 0) / 100.0, 3),
        "hook_fade_sec": float(dec.get("hookFadeSec") or 0.0),
        "hide_fillers": bool(dec.get("hideFillers")),
    }
    layout: dict = {}
    try:
        fs = int(dec.get("subFontScale") or 100)
        if fs != 100:
            layout["fontScale"] = round(fs / 100.0, 3)
        px, py = int(dec.get("subPosX") or 50), int(dec.get("subPosY") or 20)
        if px != 50 or py != 20:
            layout["posX"], layout["posY"] = px, py
    except (TypeError, ValueError):
        layout = {}
    if layout:
        params["subtitleLayout"] = layout
    return params


@route("POST", "/api/cut/save")
def h_cut_save(handler, _params) -> tuple[int, dict]:
    """保存手动剪辑草稿（keep 段 + 工具决策 + 字幕条偏移；不渲染）。"""
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    try:
        return 200, {"ok": True, "draft": cut_engine.save_draft(
            str(body.get("candidate") or ""), int(body.get("idx") or 0),
            body.get("ranges"), decisions=body.get("decisions"),
            cue_shifts=body.get("cueShifts"))}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}

@route("POST", "/api/cut/cue-shifts")
def h_cut_cue_shifts(handler, _params) -> tuple[int, dict]:
    """只存「字幕条挪动」（拖动结束就落盘，不必等点保存）。

    keep 段与决策沿用草稿里已有的；没草稿时用 AI 原区间 + 默认决策。
    """
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    try:
        idx = int(body.get("idx") or 0)
        base = _draft_base(candidate)
        try:
            old = cut_engine.load_draft(base, idx) or {}
            ranges = old.get("keepRanges") or _fallback_ranges(candidate, idx)
        except FileNotFoundError:
            ranges = _fallback_ranges(candidate, idx)
        draft = cut_engine.save_draft(candidate, idx, ranges,
                                      cue_shifts=body.get("cueShifts"))
        return 200, {"ok": True, "cueShifts": draft["cueShifts"], "draft": draft}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


@route("POST", "/api/cut/decisions")
def h_cut_decisions(handler, _params) -> tuple[int, dict]:
    """只存工具决策（面板上改一个开关就立刻存下来，不碰渲染）。

    keep 段沿用草稿里已有的；没草稿就用 AI 原区间。
    """
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    try:
        idx = int(body.get("idx") or 0)
        base = _draft_base(candidate)
        dec = cut_engine.normalize_decisions(body.get("decisions"))
        try:
            old = cut_engine.load_draft(base, idx) or {}
            ranges = old.get("keepRanges") or _fallback_ranges(candidate, idx)
        except FileNotFoundError:
            ranges = _fallback_ranges(candidate, idx)
        draft = cut_engine.save_draft(candidate, idx, ranges, decisions=dec)
        return 200, {"ok": True, "decisions": draft["decisions"], "draft": draft}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


@route("POST", "/api/cut/restore")
def h_cut_restore(handler, _params) -> tuple[int, dict]:
    """恢复 AI 原方案：清掉草稿（keep 段 + 工具决策 + 重新识别结果）。

    不做任何"猜"—— 直接删草稿，下次 plan 拉到的就是 AI 原始区间与默认决策。
    """
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    try:
        idx = int(body.get("idx") or 0)
        ranges = _fallback_ranges(candidate, idx)
        cut_engine.clear_draft(_draft_base(candidate), idx)
        return 200, {"ok": True, "ranges": ranges,
                     "decisions": cut_engine.normalize_decisions(None),
                     "message": "已恢复到 AI 原方案"}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


@route("POST", "/api/cut/cover")
def h_cut_cover(handler, _params) -> tuple[int, dict]:
    """封面工具：从源素材的指定位置截一帧，存成与成片同名的 JPEG。"""
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    try:
        info = cut_engine.extract_cover(str(body.get("candidate") or ""),
                                        int(body.get("idx") or 0),
                                        body.get("ms"))
        return 200, {"ok": True, **info}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


@route("POST", "/api/cut/cover-prompt")
def h_cut_cover_prompt(handler, _params) -> tuple[int, dict]:
    """封面工具（v3.37）：给**外部 AI 生图工具**的可复制提示词（3:4 / 4:3 两版）。

    照参考实现：本软件**不在本地生成封面**，只出「参考帧 + 提示词」，由用户带走使用。
    """
    from app.slicer import cut as cut_engine
    from app.slicer import cover_prompt as cover_engine
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    try:
        idx = int(body.get("idx") or 0)
        base, picked, src, _data = cut_engine.resolve_target(candidate, idx)
        draft = cut_engine.load_draft(base, idx) or {}
        dec = cut_engine.normalize_decisions(draft.get("decisions"))
        _film, cover_name = cut_engine.film_names(base, idx, str(picked.get("title") or ""))
        cover_path = ws.editor_output_dir / cover_name
        kit = cover_engine.build_cover_kit(
            picked,
            strategy=str(draft.get("strategy") or ""),
            reference_rel=f"edits/{cover_name}" if cover_path.is_file() else "",
            cover_ms=float(dec.get("coverMs") if dec.get("coverMs") is not None else -1))
        return 200, {"ok": True, **kit}
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}


@route("POST", "/api/cut/reanalyze")
def h_cut_reanalyze(handler, _params) -> tuple[int, dict]:
    """重新识别字幕与 AI 方案（对该片段单独重跑 Whisper + 重算开头钩子）。"""
    body = handler._read_json_body()
    model = str(body.get("model") or "medium").strip() or "medium"
    return RECHECK.submit(str(body.get("candidate") or ""),
                          int(body.get("idx") or 0), model)


@route("GET", "/api/cut/reanalyze/status")
def h_cut_reanalyze_status(_handler, params) -> tuple[int, dict]:
    return RECHECK.status(str(params.get("job") or "").strip() or None)


@route("POST", "/api/cut/reanalyze/cancel")
def h_cut_reanalyze_cancel(handler, _params) -> tuple[int, dict]:
    body = handler._read_json_body()
    return RECHECK.cancel(str(body.get("job") or "").strip() or None)


@route("POST", "/api/cut/render")
def h_cut_render(handler, _params) -> tuple[int, dict]:
    """手动剪辑 → 渲染/覆盖成片（复用六步管线；字幕随 keep 段重映射）。

    ``ranges`` 省略时用已存草稿。**工具决策决定 BGM / 音量 / 淡化 / 画幅 / 语气词**，
    ``recues``（重新识别出的字幕）存在时优先于整份转写。
    """
    from app.slicer import cut as cut_engine
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    idx = body.get("idx")
    idx_val = int(idx) if idx is not None else 0
    draft = None
    try:
        draft = cut_engine.load_draft(_draft_base(candidate), idx_val)
    except (ValueError, TypeError):
        draft = None
    ranges = body.get("ranges")
    if ranges is None:
        if not draft:
            return 400, {"error": "没有可用的剪辑草稿（先分割/删除分段并保存）"}
        ranges = draft.get("keepRanges")
    if not isinstance(ranges, list):
        return 400, {"error": "ranges 必须是 [{startMs,endMs}] 数组"}
    dec = cut_engine.normalize_decisions(
        body.get("decisions") or (draft or {}).get("decisions"))
    params = decisions_to_render_params(dec)
    params["recues"] = (draft or {}).get("recues") or []
    # v3.37：字幕条偏移必须随渲染一起带上（不带上就是"改了没生效"）
    params["cueShifts"] = (draft or {}).get("cueShifts") or {}
    return RENDERER.submit(candidate=candidate, idx=idx, manual_ranges=ranges,
                           burn_subtitles=bool(body.get("burnSubtitles", True)),
                           bgm=dec["bgm"] or None, params=params)


@route("POST", "/api/render/cancel")
def h_render_cancel(_handler, params) -> tuple[int, dict]:
    return RENDERER.cancel(str(params.get("job") or "").strip() or None)


# v4.74 P-B（方案优化项 B）：录制磁盘水位阈值（设置页可配，双条件任一触发即停）
@route("GET", "/api/settings/record-disk")
def h_record_disk_get(_handler, _params) -> tuple[int, dict]:
    from .recorder import _read_disk_limits_from
    cfg = {}
    try:
        cfg = json.loads(ws.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    gb, pct = _read_disk_limits_from(cfg or {})
    return 200, {"ok": True, "recordMinFreeGb": gb, "recordMinFreePct": pct}


@route("POST", "/api/settings/record-disk")
def h_record_disk_set(handler, _params) -> tuple[int, dict]:
    """写 `recordMinFreeGb` / `recordMinFreePct`（范围：GB 0-5000、% 0-100）。

    读-改-写原子落盘；两个都设 0 = 关闭磁盘闸门（不推荐，界面上要说明）。
    """
    body = handler._read_json_body()
    try:
        gb = float(body.get("recordMinFreeGb"))
        pct = float(body.get("recordMinFreePct"))
    except (TypeError, ValueError):
        return 400, {"error": "请填数字（GB 与 % 两项）"}
    if not (0 <= gb <= 5000 and 0 <= pct <= 100):
        return 400, {"error": "超出范围：GB 应在 0-5000，% 应在 0-100"}
    try:
        cfg = json.loads(ws.settings_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg["recordMinFreeGb"] = gb
    cfg["recordMinFreePct"] = pct
    try:
        tmp = ws.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ws.settings_path)
    except OSError as exc:
        return 500, {"error": f"设置保存失败：{exc}"}
    return 200, {"ok": True, "recordMinFreeGb": gb, "recordMinFreePct": pct}


@route("GET", "/api/edits")
def h_edits_list(_handler, _params) -> tuple[int, dict]:
    """成片列表（扫描 `edits` 目录，伴生 `.edit.json` 提供时长/编码器等元信息）。"""
    return RENDERER.edits()


@route("POST", "/api/edits/cover")
def h_edits_cover(handler, _params) -> tuple[int, dict]:
    """**v4.72：为成片生成封面图**（渲染页成片行的「封面图」按钮，用户 2026-11-12）。

    以该成片的一帧（视频首帧）为封面 —— **格式由 publish_cover 统一管**（K44，别在这
    重复实现裁切）：3:4 竖版 1080×1440 与 4:3 横版 1440×1080 两份 JPEG（居中裁切不拉伸，
    质量 92），落在成片同目录（`<名>_cover_auto_3x4.jpg / _4x3.jpg`）——
    发布页的「封面」按钮与自动上传用的就是这些文件。
    已有封面且没过期时**复用**（generated=false），不重复抽帧；成片重渲后封面自动跟上。
    """
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"error": "非法文件名，已拒绝"}
    # 成片解析统一走 resolve_film（kind 由路径推断）
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"error": f"成片不存在：{name}"}
    try:
        from app.common.publish_cover import assets_for_file, cover_files_for
        _existing, _fallback, kind = cover_files_for(path)
        return 200, assets_for_file(path, kind, generate=True)
    except FileNotFoundError as exc:
        return 404, {"error": str(exc)}
    except Exception as exc:                               # noqa: BLE001
        return 500, {"error": f"封面生成失败：{type(exc).__name__}: {exc}"}


@route("GET", "/api/edits/cover/base")
def h_edits_cover_base(handler, params) -> tuple[int, dict]:
    """v4.80：**无字底图**（子窗画布用）——按比例裁切 + 模糊 + 压暗，**不叠任何文字**。

    为什么需要：之前画布底图拿的是已烧字的四档封面，再叠可拖文字 ⇒ 出现"两份文案"。
    返回 data URI（1080 宽 JPEG q85 ≈ 100KB，按比例缓存一次）。
    """
    import base64
    from app.common import cover_design
    from app.common.publish_cover import COVER_RATIOS
    name = str((params or {}).get("name") or "").strip()
    key = str((params or {}).get("key") or "3x4").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"error": f"成片不存在：{name}"}
    ratio = next((r for r in COVER_RATIOS if r["key"] == key), COVER_RATIOS[0])
    # 底图源：已有封面图（首帧/最佳帧）→ 若不存在先抽一帧
    # v4.97：文件名统一按成片 stem 取（<stem>_cover.jpg）
    from app.common.publish_cover import cover_files_for
    base_img, auto_img, _kind = cover_files_for(path)
    src = base_img if base_img.is_file() else auto_img
    if not src.is_file():
        try:
            from app.common.publish_cover import _first_frame
            _first_frame(path, auto_img)
            src = auto_img
        except Exception as exc:                       # noqa: BLE001
            return 500, {"error": f"取底图失败：{type(exc).__name__}: {exc}"}
    # v4.82：模糊/压暗可由前端滑块实时指定（还没保存也能看到效果）；
    # 不传就按该成片 cover.json 里的 background（老配置缺字段 ⇒ 22 / 0.47，出图不变）。
    cfg = cover_design.load_cfg(path) or {}
    blur, shade = cover_design.backdrop_params(cfg)
    q = params or {}
    if q.get("blur") not in (None, ""):
        try:
            blur = max(0, min(cover_design.BLUR_MAX, int(float(q.get("blur")))))
        except (TypeError, ValueError):
            pass
    if q.get("shade") not in (None, ""):
        try:
            shade = max(0.0, min(cover_design.SHADE_MAX, float(q.get("shade"))))
        except (TypeError, ValueError):
            pass
    # v4.98：取景焦点（拖动底图改的就是它）——默认读 cfg，也允许前端用 &fx=&fy= 临时覆盖
    # v5.0：按**当前比例**取（各比例各存一份，避免切比例后取景串味）
    fx, fy = cover_design.focus_params(cfg, key)
    for _k, _cur in (("fx", fx), ("fy", fy)):
        if q.get(_k) not in (None, ""):
            try:
                v = max(0.0, min(1.0, float(q.get(_k))))
                if _k == "fx":
                    fx = v
                else:
                    fy = v
            except (TypeError, ValueError):
                pass
    # v4.99：zoom（底图放大倍率）同样默认读 cfg、可 &zoom= 覆盖
    zoom = 1.0
    if q.get("zoom") not in (None, ""):
        try:
            zoom = max(1.0, min(3.0, float(q.get("zoom"))))
        except (TypeError, ValueError):
            pass
    else:
        zoom = cover_design.zoom_params(cfg, key)
    try:
        data = cover_design.render_base_jpeg(src, ratio, blur=blur, shade=shade,
                                             focus=(fx, fy), zoom=zoom)
    except Exception as exc:                           # noqa: BLE001
        return 500, {"error": f"生成底图失败：{type(exc).__name__}: {exc}"}
    return 200, {"ok": True, "key": ratio["key"], "blur": blur, "shade": round(shade, 3),
                 "url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}


@route("GET", "/api/edits/cover/photo")
def h_edits_cover_photo(handler, params) -> tuple[int, dict]:
    """v4.98：**未裁切的整张底图**（给子窗拖动取景用）。

    需求（2026-11-14）：「封面设计的画布中的图片能不能改为选中的，由我调整图片的画面位置，
    画布固定比例就行，就是由我拖动图片调整到最佳位置」。

    为什么不复用 `/cover/base`：那里已经按比例裁切 + 模糊 + 压暗，裁完就没有"多余画面"可拖了。
    这里返回**最长边 ≤ 1400 的整图**（JPEG q88，≈150–400KB 的 data URI），
    前端用 `object-fit: cover` 同款公式摆放，拖动只改 cfg 里的 `background.focus`。
    """
    import base64
    from io import BytesIO

    from PIL import Image

    from app.common.publish_cover import cover_files_for
    name = str((params or {}).get("name") or "").strip()
    if not name.lower().endswith(".mp4"):
        return 400, {"error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"error": f"成片不存在：{name}"}
    existing, fallback, _kind = cover_files_for(path)
    src = existing if existing.is_file() else fallback
    if not src.is_file():
        return 404, {"error": "还没有可用的封面底图（请先生成/选一帧）"}
    try:
        with Image.open(str(src)) as im:
            im = im.convert("RGB")
            w0, h0 = im.size
            k = min(1.0, 1400 / max(w0, h0, 1))
            if k < 1.0:
                im = im.resize((max(1, int(w0 * k)), max(1, int(h0 * k))),
                               Image.Resampling.LANCZOS)
            buf = BytesIO()
            im.save(buf, "JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return 200, {"ok": True, "url": "data:image/jpeg;base64," + b64,
                     "width": im.width, "height": im.height, "file": src.name}
    except Exception as exc:                           # noqa: BLE001
        return 500, {"error": f"读取底图失败：{type(exc).__name__}: {exc}"}


@route("GET", "/api/edits/cover/design")
def h_edits_cover_design_get(handler, params) -> tuple[int, dict]:
    """读一份成片的**封面设计配置**（没有就返回模板 A 的空文案版，前端操作台直接可改）。"""
    from app.common import cover_design
    name = str((params or {}).get("name") or "").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"error": f"成片不存在：{name}"}
    cfg = cover_design.load_cfg(path)
    if not cfg:
        cfg = cover_design.template_cfg("A")
    return 200, {"ok": True, "config": cfg,
                 "templates": [{"key": k, "label": v["label"]} for k, v in
                               cover_design.TEMPLATES.items()],
                 "emojiCatalog": list(cover_design.EMOJI_CATALOG),
                 "coverJson": str(ws.cover_json_for(path))}


@route("POST", "/api/edits/cover/design")
def h_edits_cover_design_set(handler, _params) -> tuple[int, dict]:
    """存封面设计配置（cover.json）并**立刻重渲染四档封面**（v4.76 P2）。

    前端操作台（文案/模板/emoji）改完点保存调这里；返回新的 covers 给抽屉刷新。
    """
    from app.common import cover_design
    from app.common.publish_cover import assets_for_file
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"error": f"成片不存在：{name}"}
    cfg = body.get("config")
    if not isinstance(cfg, dict) or not str((cfg.get("copy") or {}).get("line1") or "").strip():
        return 400, {"error": "至少要填第一行文案（line1）才能保存"}
    cover_design.save_cfg(path, cfg)                   # 保存配置
    from app.common.publish_cover import cover_files_for
    _existing, _fallback, kind = cover_files_for(path)
    r = assets_for_file(path, kind, generate=True)      # 重渲染（cfg mtime 触发重建）
    return 200, {"ok": True, **r}


@route("POST", "/api/edits/cover/copy")
def h_edits_cover_copy(handler, _params) -> tuple[int, dict]:
    """**AI 生成/简化封面文案**（v4.76 P1，方案 §7；v4.84 改为基于用户当前文案压缩）。

    body：
      - ``text``（v4.84）：用户填在「封面文案」框里的文字 —— **有它就以它为源做压缩**
        （用户：「这个 AI 优化文案的按钮，是优化上方的文案内容的」）；
      - ``title`` / ``summary``：片名与背景，压缩时只当语境，不当事实来源；
        没有 ``text`` 时退回「按标题+摘要生成」。

    失败一律 200 + 空候选 + note（不阻断封面设计，K69）。
    """
    from app.common.cover_copy import generate_candidates
    body = handler._read_json_body()
    title = str(body.get("title") or "").strip()
    summary = str(body.get("summary") or "").strip()
    text = str(body.get("text") or "").strip()[:200]
    try:
        cands = generate_candidates(title, summary, text=text or None)
    except Exception as exc:                                    # noqa: BLE001
        # 路径写错/依赖缺失这种曾经静默成「点了没反应」——现在如实说原因
        return 200, {"ok": True, "candidates": [],
                     "note": f"文案生成异常：{type(exc).__name__}: {exc}"}
    if not cands:
        return 200, {"ok": True, "candidates": [],
                     "note": "文案生成失败或未配置 AI Key——可手填文案"}
    return 200, {"ok": True, "candidates": cands, "source": "simplify" if text else "generate"}


@route("POST", "/api/edits/cover/frames")
def h_edits_cover_frames(handler, _params) -> tuple[int, dict]:
    """v4.85：**找"没有字幕/字样"的帧**当封面底图候选（用户 2026-11-14）。

    body ``{name, count?, force?}`` → ``{ok, frames:[{name,tSec,clean,subBlocks,…,thumb}],
    cleanCount, cleanTotal, degraded, cached, note}``。
    首次要抽帧 + 逐帧 OCR（约 15–25 秒），同一成片之后再点走缓存（瞬时）。
    """
    from app.common import cover_frames
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"ok": False, "error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"ok": False, "error": f"成片不存在：{name}"}
    try:
        count = int(body.get("count") or cover_frames.COUNT)
    except (TypeError, ValueError):
        count = cover_frames.COUNT
    try:
        data = cover_frames.analyze(path, count=max(3, min(count, 12)),
                                    force=bool(body.get("force")))
    except Exception as exc:                                    # noqa: BLE001
        return 200, {"ok": False, "frames": [],
                     "error": f"取帧失败：{type(exc).__name__}: {exc}"}
    data["ok"] = True
    return 200, data


@route("POST", "/api/edits/cover/frame")
def h_edits_cover_frame(handler, _params) -> tuple[int, dict]:
    """v4.85：把选中的那一帧写成该成片的封面底图（`<stem>_cover.jpg`）。"""
    from app.common import cover_frames
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    frame = str(body.get("frame") or "").strip()
    if not name.lower().endswith(".mp4") or not is_safe_stem(Path(name).stem):
        return 400, {"ok": False, "error": "非法文件名，已拒绝"}
    path = ws.resolve_film(name)
    if path is None:
        return 404, {"ok": False, "error": f"成片不存在：{name}"}
    try:
        res = cover_frames.choose(path, frame)
    except Exception as exc:                                    # noqa: BLE001
        return 200, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return 200, res


@route("POST", "/api/media/detach")
def h_media_detach(handler, _params):
    """v3.158：掐断正在读这些文件的媒体流（删文件前调用）。

    前端在「删素材/删成片」前会先调它；``file_ops.delete_files_in`` 里还有一层兜底调用，
    所以就算某个入口忘了，也不会再出现「WinError 32 文件被占用」。
    """
    body = handler._read_json_body() or {}
    rels = body.get("rels")
    if not isinstance(rels, list):
        one = str(body.get("rel") or "").strip()
        rels = [one] if one else []
    paths = []
    from app.slicer.cut import STREAM_ROOTS
    for raw in rels[:50]:
        rel = str(raw or "").strip()
        parts = rel.split("/", 1)
        if len(parts) != 2 or parts[0] not in STREAM_ROOTS or not parts[1]:
            continue
        root_fn, _exts = STREAM_ROOTS[parts[0]]
        try:
            target = (root_fn() / parts[1]).resolve()
            target.relative_to(root_fn().resolve())
        except (ValueError, OSError):
            continue
        paths.append(target)
    n = media_streams.detach(paths, timeout=2.0) if paths else 0
    return 200, {"ok": True, "detached": n, "requested": len(paths)}


@route("POST", "/api/edits/delete")
def h_edits_delete(handler, _params) -> tuple[int, dict]:
    """删除成片，**连带删伴生的 `.edit.json`**。删除规则走共享实现 `file_ops`。

    `edits\\index.json`（渲染任务索引）在受保护名单里，勾不到也删不掉。
    """
    body = handler._read_json_body()
    names = body.get("names") if isinstance(body.get("names"), list) else None
    if names is None and body.get("name"):
        names = [str(body["name"])]
    if not names:
        return 400, {"error": "没有指定要删除的成片（names）"}
    return RENDERER.delete_edits(names)


# ------------------------------------------------------------ 剪映草稿导出（v3.75）

@route("GET", "/api/jianying/status")
def h_jianying_status(_handler, _params) -> tuple[int, dict]:
    """剪映草稿导出**能不能用**（依赖装没装 + 草稿目录在不在）。

    为什么要单独给一个状态端点：依赖可能没装、剪映草稿目录可能不在，
    前端必须**先问再决定给不给按钮**，而不是给一个点了才报错的死按钮（K69）。
    """
    return 200, jianying_engine.status()


@route("GET", "/api/jianying/draft-dir")
def h_jianying_draft_dir_get(_handler, _params) -> tuple[int, dict]:
    """v3.163：当前剪映草稿箱（设置里可改；没改过 = 默认 E 盘那个）。"""
    st = jianying_engine.status()
    return 200, {"ok": True, "path": st.get("draftDir") or "",
                 "exists": st.get("dirExists") is True,
                 "isCustom": st.get("draftDirCustom") is True,
                 "default": st.get("defaultDraftDir") or ""}


@route("POST", "/api/jianying/draft-dir")
def h_jianying_draft_dir_set(handler, _params) -> tuple[int, dict]:
    """v3.163：保存剪映草稿箱。

    ``path`` 为空 = 恢复默认；目录必须存在且可写（在这里拦，别让「导出到剪映」必失败）。
    """
    body = handler._read_json_body() or {}
    ok, err = jianying_engine.set_draft_dir_setting(str(body.get("path") or ""))
    if not ok:
        return 400, {"ok": False, "error": err}
    st = jianying_engine.status()
    return 200, {"ok": True, "path": st.get("draftDir") or "",
                 "exists": st.get("dirExists") is True,
                 "isCustom": st.get("draftDirCustom") is True}


@route("POST", "/api/jianying/export")
def h_jianying_export(handler, _params) -> tuple[int, dict]:
    """成片 → 剪映草稿。

    源素材还在 ⇒ 按 keepRanges 分段 + 可改的字幕文本轨；
    源素材不在 ⇒ 整段成片（字幕已烧录，不再加文本轨，避免双字幕）。
    """
    body = handler._read_json_body()
    name = str(body.get("name") or "").strip()
    if not name:
        return 400, {"error": "没有指定要导出的成片（name）"}
    return jianying_engine.export_film(name, draft_name=body.get("draftName"))


@route("POST", "/api/jianying/export-plan")
def h_jianying_export_plan(handler, _params) -> tuple[int, dict]:
    """**剪辑页**的「导出到剪映」：把**当前这一版剪辑**（不是已保存的草稿）导出。

    传的是页面上的 `ranges` —— 用户拖完还没保存也要按他看到的那一版导出，
    否则「我明明改了段，导出去还是原来的」就是最冤的那种坏（K117 同族：反馈要落在
    用户看得见的那一层）。后端与 `/api/jianying/export` 共用 `_export`（K44）。
    """
    body = handler._read_json_body()
    candidate = str(body.get("candidate") or "").strip()
    if not candidate:
        return 400, {"error": "没有指定切片候选（candidate）"}
    try:
        idx = int(body.get("idx") or 0)
    except (TypeError, ValueError):
        return 400, {"error": "idx 不是整数"}
    ranges = body.get("ranges")
    if not isinstance(ranges, list):
        return 400, {"error": "ranges 必须是数组"}
    decisions = body.get("decisions") if isinstance(body.get("decisions"), dict) else {}
    return jianying_engine.export_plan(candidate, idx, ranges, decisions,
                                       draft_name=body.get("draftName"))


# ---------------------------------------------------------------- 自动化流水线（M6）

@route("GET", "/api/auto/status")
def h_auto_status(_handler, _params) -> tuple[int, dict]:
    return 200, AUTO.status()


@route("POST", "/api/auto/toggle")
def h_auto_toggle(handler, _params) -> tuple[int, dict]:
    """开/关自动化流水线。

    v3.35：**落盘持久化**。原来只改内存标志 —— 服务一重启就变回关（而录制器卡片/服务
    重启很常见），于是「录完自动流转」时灵时不灵，用户报的就是这个（K68）。
    """
    body = handler._read_json_body()
    enabled = bool(body.get("enabled"))
    AUTO.set_enabled(enabled)
    from app.slicer.auto_pipeline import save_enabled_pref
    save_enabled_pref(enabled)
    return 200, {"ok": True, **AUTO.status()}


@route("POST", "/api/auto/trigger")
def h_auto_trigger(_handler, _params) -> tuple[int, dict]:
    return 200, AUTO.trigger()


@route("POST", "/api/auto/flow-reset")
def h_auto_flow_reset(_handler, _params) -> tuple[int, dict]:
    """生产线的「重置」按钮（v3.53）。

    **只重置显示，不打断工作**：不停线程、不清 `_processed`/`_failed`
    （清了会让同一批素材被重新转写一遍，白白烧 GPU），只记一条"本轮计数基线"，
    于是生产线回到「等待新的录屏素材」，而正在跑的转写/切片/剪辑照旧显示。
    """
    return 200, AUTO.reset_flow_view()


@route("POST", "/api/auto/flow-session")
def h_auto_flow_session(_handler, _params) -> tuple[int, dict]:
    """**本次打开程序** → 生产线回到「发现录屏」（v3.58）。

    前端每次开窗口（``pywebviewready``）调一次：用户报「每次打开都停在人工确认，
    还得手动点一下重置」。与「重置」共用同一个后端实现（``_set_view_base``），
    所以两处语义永远一致 —— 差别只在日志里的来源标记。
    """
    return 200, AUTO.mark_view_session()


# ---------------------------------------------------------------- 切片质量调优（M7）

@route("GET", "/api/tuning")
def h_tuning_get(_handler, _params) -> tuple[int, dict]:
    return 200, {
        "scoreThreshold": tuning_engine.get_score_threshold(),
        "feedback": tuning_engine.feedback_stats(),
    }


@route("POST", "/api/tuning")
def h_tuning_post(handler, _params) -> tuple[int, dict]:
    body = handler._read_json_body()
    if "scoreThreshold" in body:
        result = tuning_engine.set_score_threshold(body["scoreThreshold"])
        if not result.get("ok"):
            return 400, {"error": result.get("error")}
    return 200, {"ok": True,
                 "scoreThreshold": tuning_engine.get_score_threshold(),
                 "feedback": tuning_engine.feedback_stats()}


# ---------------------------------------------------------------- 处理函数

@route("GET", "/version")
def h_version(_handler, _params) -> tuple[int, dict]:
    """启动编排的探测端点（§7.4 第 14 步：slicer 探 /version）。**公共**。"""
    return 200, {"service": SERVICE, "version": APP_VERSION, "app": APP_TITLE}


@route("GET", "/api/health")
def h_health(_handler, _params) -> tuple[int, dict]:
    return 200, {
        "ok": True,
        "service": SERVICE,
        "app": APP_TITLE,
        "version": APP_VERSION,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "uptimeSec": round(time.time() - _STARTED_AT, 1),
        "workspace": str(ws.root),
        "port": os.environ.get("SLICE_PORT", ""),
        "tokenProtected": bool(os.environ.get(TOKEN_ENV, "").strip()),
    }


@route("GET", "/api/runtime")
def h_runtime(_handler, _params) -> tuple[int, dict]:
    return 200, runtime_summary()


@route("GET", "/api/system")
def h_system(_handler, _params) -> tuple[int, dict]:
    return 200, probe_system()


@route("GET", "/api/env/report")
def h_env_report(_handler, _params) -> tuple[int, dict]:
    """**运行环境体检报告**（v3.176）：功能 / 模型 / CUDA / 运行环境 四栏 + 汇总。

    用户 2026-10-29：「运行环境里面优化一下展示，各个功能的准备状态也显示一下，
    哪些模型准备中，哪些 CUDA 功能准备中，是否可用，哪些运行环境都展示一下」。

    ⚠️ 这一份**只查不加载**（不 import torch、不跑 GPU 实测），可以放心随「服务状态」抽屉刷新；
    真跑能力仍归「显卡与加速」里那个「实测」按钮（K144：检测 ≠ 实测）。
    """
    from app.slicer import env_report
    return 200, env_report.report()


@route("GET", "/api/config")
def h_config(_handler, _params) -> tuple[int, dict]:
    """切片服务的目录配置（供 UI 显示目录链）。"""
    return 200, {
        "liveSourceDir": str(ws.live_source_dir),
        "sliceOutputDir": str(ws.slice_dir),
        "modelDir": str(ws.models_dir),
        "runtimeData": str(ws.runtime_data),
        "sliceVideoDir": os.environ.get("SLICE_VIDEO_DIR", str(ws.live_source_dir)),
        "sliceOutputEnv": os.environ.get("SLICE_OUTPUT_DIR", str(ws.slice_dir)),
        "whisperPython": os.environ.get("SLICE_WHISPER_PYTHON", str(ws.python_exe)),
        "whisperModel": os.environ.get("SLICE_WHISPER_MODEL", ""),
        "whisperModelName": default_whisper_model(),
        "whisperDevice": os.environ.get("SLICE_WHISPER_DEVICE", ""),
        "transcriptsDir": str(ws.transcripts_dir),
        "candidatesDir": str(ws.candidates_dir),
        "autoWatch": os.environ.get("SLICE_AUTO_WATCH", "0") == "1",
    }




# ---------------------------------------------------------------- 账号数据看板（v3.105）



# ---------------------------------------------------------------- HTTP 层

class SliceHandler(BaseHTTPRequestHandler):
    server_version = "SlicerService/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # --- 工具 ---

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

    def _authorized(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        expected = os.environ.get(TOKEN_ENV, "").strip()
        if not expected:
            return True   # 开发模式
        got = (self.headers.get(TOKEN_HEADER) or "").strip()
        if not got:
            query = parse_qs(urlparse(self.path).query)
            got = (query.get("token") or [""])[0].strip()
        return got == expected

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # --- 分发 ---

    def _serve_media(self, params: dict) -> None:
        """``/api/media/stream``：视频/音频 Range 流式播放（预览面板 + 剪辑页）。

        ``rel`` 形如 ``live/<name>.mp4``，只允许 STREAM_ROOTS 白名单前缀 +
        扩展名，且 resolve 后必须仍在其根目录内（防穿越）。<video> 标签带不了
        自定义 header，token 走 query（``_authorized`` 已支持）。
        """
        from app.slicer.cut import STREAM_ROOTS
        rel = str(params.get("rel") or "").strip()
        parts = rel.split("/", 1)
        if len(parts) != 2 or parts[0] not in STREAM_ROOTS or not parts[1]:
            self._send_json({"error": "rel 必须是 <白名单前缀>/<文件名> 形式"}, 400)
            return
        prefix, name = parts
        root_fn, exts = STREAM_ROOTS[prefix]
        if not is_safe_stem(Path(name).stem) or Path(name).suffix.lower() not in exts:
            self._send_json({"error": "文件名或类型不合法"}, 400)
            return
        target = (root_fn() / name).resolve()
        try:
            target.relative_to(root_fn().resolve())
        except ValueError:
            self._send_json({"error": "路径越界已拒绝"}, 400)
            return
        if not target.is_file():
            self._send_json({"error": f"文件不存在：{prefix}/{name}"}, 404)
            return

        size = target.stat().st_size
        rng = (self.headers.get("Range") or "").strip()
        start, end = 0, size - 1
        status = 200
        m = re.match(r"bytes=(\d*)-(\d*)$", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                    # bytes=-N：末尾 N 字节
                start = max(size - int(m.group(2)), 0)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self._cors_headers()
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        # ⚠️ Content-Type 必须跟着扩展名走（K54）：流式端点早先一律写 video/mp4，
        #    于是「音乐」工具的.mp3 试听被浏览器按 mp4 解码，**点了没声音**。
        ctype = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._cors_headers()
        self.end_headers()
        # v3.138：**写超时**。原来这里是裸 `self.wfile.write`：播放器一暂停/把标签页
        # 挂到后台，浏览器就不再读，TCP 发送缓冲一满 ⇒ send 无限阻塞 ⇒ 这个 `open("rb")`
        # 的读句柄一直不释放。Windows 上 Python 的 open 不允许 delete 共享，
        # 于是「删成片」直接 WinError 32（用户 2026-10-24 报的就是这个）。
        # v3.158：从 10s 收紧到 **2s** —— 用户「预览多了删不掉」的根因就是暂停后
        # 这一轮 send 能挂 10 秒；2 秒足够正常播放/拖进度（拖动本来就会断连重连），
        # 却能让"删文件"最多等 2 秒就拿到句柄。
        try:
            self.connection.settimeout(2.0)
        except OSError:
            pass
        timed_out = False
        # v3.158：登记这条流，删除时可以由 /api/media/detach（或 file_ops 的删除兜底）掐断。
        # 读文件改成「每 1MB 开一次、读完立刻关」——播放器暂停时**不再有长命读句柄**，
        # Windows 上 unlink 也就不会再稳定报 WinError 32（用户 2026-10-28 又一次踩到）。
        st = media_streams.register(target)
        st.conn = self.connection          # detach 时要能 shutdown（只置标记踢不动阻塞中的 send）
        try:
            pos = start
            remaining = length
            while remaining > 0:
                if st.cancelled():
                    break                            # 有人在删这个文件：立刻收手
                try:
                    with target.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read(min(1 << 20, remaining))
                except FileNotFoundError:
                    break                            # 文件已被删掉 ⇒ 正常收尾
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return                           # 播放器拖动进度条会频繁断开
                pos += len(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError:                        # v3.138：客户端不读了，别一直占着文件
            timed_out = True
            self.close_connection = True
        except OSError:                             # 连接被对端重置等：同样尽快放句柄
            timed_out = True
            self.close_connection = True
        finally:
            media_streams.unregister(st)
            st.closed.set()
            if timed_out:
                try:
                    self.connection.close()
                except OSError:
                    pass

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if not self._authorized(path):
            self._send_json({"error": "本地会话令牌无效"}, 401)
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if path == "/api/media/stream":
            if method != "GET":
                self._send_json({"error": "媒体流只支持 GET"}, 405)
                return
            self._serve_media(params)
            return
        handler = ROUTES.get((method, path))
        try:
            if handler is not None:
                status, payload = handler(self, params)
            else:
                # 正则路由（带路径参数）
                for mth, pattern, fn in PATTERN_ROUTES:
                    if mth != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        status, payload = fn(self, params, *match.groups())
                        break
                else:
                    self._send_json({"error": f"未实现的接口：{method} {path}"}, 404)
                    return
        except Exception as exc:  # 任何业务异常都转成 500，不让服务挂掉
            self.log(f"处理 {method} {path} 失败: {type(exc).__name__}: {exc}")
            status, payload = 500, {
                "error": str(exc),
                "errorType": type(exc).__name__,
            }
        self._send_json(payload, status)

    def do_GET(self) -> None:      # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:      # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:   # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- 日志 ---

    def log_message(self, fmt: str, *args) -> None:
        self.log(fmt % args)

    def log(self, message: str) -> None:
        with _LOG_LOCK:
            print(f"[slicer] {message}", flush=True)


# ---------------------------------------------------------------- 入口

def build_server(port: int = 0) -> ThreadingHTTPServer:
    host = "127.0.0.1"   # 强制本机
    return ThreadingHTTPServer((host, port), SliceHandler)


def main() -> int:
    port = int(os.environ.get("SLICE_PORT", "7870"))
    server = build_server(port)
    ws.ensure_dirs()
    # M2-B：录制调度器随服务启动（跑在服务进程里 —— 关桌面窗口不能停录）
    MANAGER.start()
    # M3-A：转写调度器随服务启动（服务重启后保留任务历史，不自动重跑）
    TRANSCRIBER.start()
    # M4：渲染调度器随服务启动（串行 + 显存守卫）
    RENDERER.start()
    # M6：自动化流水线随服务启动（目录监控线程）
    # v3.35：开关**默认开且持久化**（原来的默认关 + 不落盘 = 「录完不自动流转」，见 K68）
    AUTO.start()
    # v3.35：把「分段落盘」接到流水线上 —— 录制器一小段录满落成片，立刻走 转写→切片→渲染，
    # 录制**不停**继续下一段（用户要的「边录边切」）。注入的是**非阻塞**登记函数，
    # 因为它会被从录制器的事件循环里调用（见 AutoPipeline.nudge）。
    MANAGER.set_hour_file_sink(AUTO.nudge)
    print(f"[slicer] 监听 http://127.0.0.1:{server.server_address[1]}", flush=True)
    print(f"[slicer] 工作区 {ws.root}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            AUTO.stop()
            RENDERER.stop()
            TRANSCRIBER.stop()
            MANAGER.stop()
        finally:
            server.server_close()
        print("[slicer] 已停止", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
