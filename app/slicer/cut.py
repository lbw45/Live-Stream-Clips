#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预览剪辑 / 高级剪辑页（M4.5，对齐参考实现 LIVE PREVIEW + 高级调整）的 plan / 草稿层。

职责边界：
- **plan_cut**：候选 + 转写 → 剪辑页需要的全部静态信息（源视频、区间、字幕段、
  已有草稿、**六个工具的决策 decisions**、可选 BGM 列表、封面/成片是否已存在）。
  纯读取，不改任何文件。
- **save_draft / load_draft / clear_draft**：keep 段 + 六个工具的决策存草稿 JSON
  （`runtime_data/slicer/cut_drafts/<stem>__<idx>.json`）。草稿只是"没点生成"的
  中间态；点「保存并覆盖成片」走 renderer 的 manual_ranges 通路，成片落 edits 后
  草稿依然存在（下次进来还能接着改）。
- **normalize_decisions**：工具决策的唯一收敛入口（补默认、夹取越界值）。
- **extract_cover**：封面工具，从源素材截一帧存 `edits/<成片同名>_cover.jpg`。
- 实际渲染在 `app/slicer/renderer.py`（队列 + 显存守卫）；本模块只在 extract_cover
  里碰 ffmpeg（抽一帧），除此之外不碰。
- **mark_applied**：成片落 edits 后给草稿打标记（**不删**，见 K55）。
- 实际渲染在 `app/slicer/renderer.py`（队列 + 显存守卫），本模块**不碰 ffmpeg**。

目录参数全部可注入（K36：测试必须完全隔离，不碰真实数据）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.common import file_ops as FO
from app.common.media_types import (AUDIO_EXT, IMAGE_EXT,  # noqa: F401
                                    IMPORT_EXT, VIDEO_EXT)
from app.common.paths import source_kind_of, ws
from app.slicer.editplan import STRATEGY_MANUAL, normalize_keep_ranges
from app.slicer.renderer import (list_bgm_files, resolve_film_stem,
                                 resolve_source_video)
from app.slicer.subtitles import normalize_cue_shifts

CUT_DRAFT_SUFFIX = ".cut.json"
COVER_SUFFIX = "_cover.jpg"

# 媒体流白名单：前缀 → 目录 + 允许的扩展名（防目录穿越，只在 /api/media/stream 用）
#
# ⚠️ v3.34：扩展名改从 `app.common.media_types` 取（唯一出处，K44）。
#   原来这里与素材库 `library.IMPORT_EXT` 各写一份，「我的上传」列得出 mp3 而这里不放 mp3 →
#   试听那条 mp3 被端点回 400「文件名或类型不合法」→ **静默没声音**（用户报障）。
#   不变量（冒烟有断言）：**素材库里列得出来的，就必须能在媒体流端点上播**
#   —— 即 `library.KINDS[kind]["ext"] ⊆ STREAM_ROOTS[kind][1]`。
STREAM_ROOTS: dict[str, tuple] = {
    "live":    (lambda: ws.live_source_dir,   VIDEO_EXT),
    # `IMAGE_EXT`：剪辑页「封面」工具截出来的封面图要走同一条流式端点才能在前端显示
    "edits":   (lambda: ws.editor_output_dir, VIDEO_EXT | IMAGE_EXT),
    # 我的上传：视频 / 音频 / 图片都能播 —— 用户的 BGM 常常就是从这儿导入的
    "uploads": (lambda: ws.uploads_dir,       IMPORT_EXT),
    "music":   (lambda: ws.music_dir,         AUDIO_EXT),
}

# 高级剪辑页六个工具的状态，**只有这一份**默认表（K44：不要在各处各自写默认值）。
# 字段命名对齐参考实现 decision 对象（musicVolume / speechVolume / hookFadeSec …）。
DECISION_DEFAULTS: dict = {
    "visualMode": "original",   # original / game_background / montage（后两者本版未支持）
    "orientation": "auto",      # auto / portrait / landscape（D30 画幅跟随源）
    "bgm": "",                  # `music/` 下的文件名；空串 = 不用 BGM
    "musicVolume": 22,          # BGM 音量百分比（0–150）
    "speechVolume": 100,        # 人声音量百分比（0–150）
    "hookFadeSec": 0.0,         # 钩子衔接淡化秒数（0–1）
    "hideFillers": False,       # 字幕：隐藏独立语气词
    "coverMs": -1,              # 封面取自源素材的第几毫秒；-1 = 还没截封面
    # v3.41 字幕样式（「字幕调整」面板）：字号百分比（50–200，100=原样）、
    # 水平位置 X（0–100，50=居中）、垂直位置 Y = 距底部百分比（0–80，20≈现行默认边距）。
    # 三项默认值 = 不改样式；渲染侧只有偏离默认才进 ASS（见 subtitles.build_ass）。
    "subFontScale": 100,
    "subPosX": 50,
    "subPosY": 20,
}

_VISUAL_MODES = ("original", "game_background", "montage")
_ORIENTATIONS = ("auto", "portrait", "landscape")


def normalize_decisions(raw: dict | None) -> dict:
    """把任意来源的工具决策收敛成**完整且合法**的一份（缺字段补默认，越界夹取值）。

    这是 decisions 的唯一入口：草稿读写、plan 下发、渲染入参全部走它，
    免得出现「前端给了 musicVolume=abc，后端直接拿去渲染」这类事故。
    """
    src = raw if isinstance(raw, dict) else {}
    out = dict(DECISION_DEFAULTS)

    mode = str(src.get("visualMode") or out["visualMode"])
    out["visualMode"] = mode if mode in _VISUAL_MODES else "original"
    orient = str(src.get("orientation") or out["orientation"])
    out["orientation"] = orient if orient in _ORIENTATIONS else "auto"

    bgm = str(src.get("bgm") or "").strip()
    out["bgm"] = Path(bgm).name if bgm else ""      # 只认文件名，杜绝 ../../ 穿越

    def _pct(key: str) -> int:
        try:
            v = int(round(float(src.get(key))))
        except (TypeError, ValueError):
            return int(out[key])
        return max(0, min(150, v))

    out["musicVolume"] = _pct("musicVolume")
    out["speechVolume"] = _pct("speechVolume")

    try:
        fade = float(src.get("hookFadeSec"))
    except (TypeError, ValueError):
        fade = 0.0
    out["hookFadeSec"] = round(max(0.0, min(1.0, fade)), 3)

    out["hideFillers"] = bool(src.get("hideFillers"))

    try:
        cover_ms = float(src.get("coverMs"))
    except (TypeError, ValueError):
        cover_ms = -1.0
    out["coverMs"] = cover_ms if cover_ms >= 0 else -1

    # v3.41：字幕样式三项（越界夹取；解析失败一律回默认 = 不改样式）
    def _int_in(key: str, lo: int, hi: int) -> int:
        try:
            v = int(round(float(src.get(key))))
        except (TypeError, ValueError):
            return int(out[key])
        return max(lo, min(hi, v))

    out["subFontScale"] = _int_in("subFontScale", 50, 200)
    out["subPosX"] = _int_in("subPosX", 0, 100)
    out["subPosY"] = _int_in("subPosY", 0, 80)
    return out


def _candidate_file(candidate: str, candidates_dir: Path) -> Path:
    raw = str(candidate or "").strip()
    if not raw:
        raise ValueError("请指定候选产物（candidate）")
    if not FO.is_safe_stem(raw):
        raise ValueError("候选产物名不合法")
    name = raw if raw.endswith(".candidates.json") else f"{raw}.candidates.json"
    p = candidates_dir / name
    if not p.is_file():
        raise FileNotFoundError(f"候选产物不存在：{name}")
    return p


def _pick_candidate(data: dict, idx: int | None) -> dict:
    cands = data.get("candidates") or []
    if not cands:
        raise ValueError("这份候选产物里没有可用的片段")
    if idx is None:
        return max(cands, key=lambda c: float((c.get("scores") or {}).get("overall") or 0))
    for c in cands:
        if int(c.get("idx", -1)) == int(idx):
            return c
    raise FileNotFoundError(f"候选产物里没有第 {idx} 条片段")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"读取失败 {path.name}：{exc}")


def resolve_target(candidate: str, idx: int | None, *,
                    candidates_dir: Path | None = None):
    """候选 → (base, picked, 源视频路径, 候选文件数据)：**唯一解析入口**（K44）。

    剪辑 plan / 草稿 / 封面 / 重新识别四条路都从这里拿，避免各写一份找文件逻辑。
    """
    cand_dir = candidates_dir or ws.candidates_dir
    cpath = _candidate_file(candidate, cand_dir)
    data = _load_json(cpath)
    base = cpath.name[: -len(".candidates.json")]
    picked = _pick_candidate(data, idx)
    src = resolve_source_video(None, data, base)
    if isinstance(src, tuple):
        raise FileNotFoundError(src[1].get("error", "找不到源视频"))
    return base, picked, src, data


def film_names(ident, idx: int, title: str = "") -> tuple[str, str]:
    """(成片文件名, 封面文件名) —— 命名规则的**唯一出处**。

    v3.39：成片名改成**该小切片标题**（用户：渲染出来要分得清哪个视频），
    具体规则与去重都委托给 ``renderer.resolve_film_stem()`` —— 那是渲染真正落盘时用的
    同一个函数，所以这里预测出来的名字与出片名**必然一致**（K44：两处各写一份必漂移，
    而漂移的后果是「保存并覆盖成片」变成另存一份）。

    ``ident`` = **候选身份**（候选产物名，与伴生 json 的 candidateName 同口径）。
    封面与成片同名（只换后缀），所以两者一起返回。
    """
    stem = resolve_film_stem(Path(str(ident)).name, int(idx), STRATEGY_MANUAL, title)
    return f"{stem}.mp4", f"{stem}{COVER_SUFFIX}"


def extract_cover(candidate: str, idx: int, ms: float, *,
                  candidates_dir: Path | None = None,
                  edits_dir: Path | None = None) -> dict:
    """封面工具：从**源素材**指定位置截一帧存成 JPEG（文件名与成片同名）。

    返回 ``{"coverName", "coverRel", "coverMs"}``。失败抛 ValueError（消息可直接展示）。

    ⚠️ 起 ffmpeg 一律走 ``proc.py``（K33：无控制台进程直接起控制台程序会弹黑窗）。
    """
    from app.common import proc
    e_dir = edits_dir or ws.editor_output_dir
    base, picked, src, _data = resolve_target(candidate, int(idx),
                                             candidates_dir=candidates_dir)
    try:
        at_ms = float(ms)
    except (TypeError, ValueError):
        raise ValueError("封面位置不合法")
    s_ms = float(picked["start"]) * 1000.0
    e_ms = float(picked["end"]) * 1000.0
    at_ms = max(s_ms, min(at_ms, e_ms))         # 越界先夹回片段区间内
    _film, cover = film_names(base, int(idx), str(picked.get("title") or ""))
    out = e_dir / cover
    e_dir.mkdir(parents=True, exist_ok=True)
    r = proc.run([str(ws.ffmpeg), "-y", "-ss", f"{at_ms / 1000.0:.3f}", "-i", str(src),
                  "-frames:v", "1", "-q:v", "2", str(out)],
                 capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out.is_file():
        raise ValueError(f"截帧失败：{(r.stderr or '').strip()[-160:] or '未知原因'}")
    return {"coverName": cover, "coverRel": f"edits/{cover}", "coverMs": round(at_ms, 1)}


# ---------------------------------------------------------------- plan

def plan_cut(candidate: str, idx: int | None, *,
             candidates_dir: Path | None = None,
             transcripts_dir: Path | None = None,
             drafts_dir: Path | None = None,
             edits_dir: Path | None = None) -> dict:
    """剪辑页加载时的全部静态信息。任何读取失败都抛异常（端点层转 4xx）。"""
    cand_dir = candidates_dir or ws.candidates_dir
    tr_dir = transcripts_dir or ws.transcripts_dir
    d_dir = drafts_dir or ws.cut_drafts_dir
    e_dir = edits_dir or ws.editor_output_dir

    base, picked, src, data = resolve_target(candidate, idx, candidates_dir=cand_dir)
    idx_val = int(picked.get("idx", -1))

    start_ms = float(picked["start"]) * 1000.0
    end_ms = float(picked["end"]) * 1000.0

    # v3.59（）：源来自录制素材根（live_raw），
    # 预览地址的前缀必须是它**真正所属**的逻辑名 —— 原来写死 `live/`，
    # 前缀写错会拼出一个播不出来的地址（K67：列得出来 ≠ 播得出来）。
    kind = source_kind_of(src)
    rel = f"{kind}/{src.name}" if kind else ""

    # 字幕段：只取落在候选区间内的（时间线展示用；重映射在渲染层做）
    tpath = tr_dir / f"{base}.json"
    cues: list[dict] = []
    if tpath.is_file():
        try:
            transcript = _load_json(tpath)
        except ValueError:
            transcript = {}
        for s in (transcript.get("segments") or []):
            try:
                s0 = float(s.get("start") or 0) * 1000.0
                e0 = float(s.get("end") or 0) * 1000.0
            except (TypeError, ValueError):
                continue
            if e0 <= start_ms or s0 >= end_ms:
                continue
            cues.append({"startMs": round(max(s0, start_ms), 1),
                         "endMs": round(min(e0, end_ms), 1),
                         "text": str(s.get("text") or "")[:60]})

    draft = load_draft(base, idx_val, drafts_dir=d_dir)
    decisions = normalize_decisions((draft or {}).get("decisions"))

    # 成片 / 封面文件名：命名规则见 `film_names()`（v3.39 = 小切片标题；身份用候选名）
    film_name, cover_name = film_names(base, idx_val, str(picked.get("title") or ""))
    return {
        "candidate": base,
        "idx": idx_val,
        "title": str(picked.get("title") or ""),
        "scores": picked.get("scores") or {},
        "startMs": round(start_ms, 1),
        "endMs": round(end_ms, 1),
        "durationMs": round(end_ms - start_ms, 1),
        "sourceRel": rel,
        "sourceName": src.name,
        "cues": cues,
        "hook": picked.get("hook"),
        "planner": str(data.get("planner") or ""),
        "draft": draft,
        # --- v3.24 高级剪辑六个工具（参考实现「高级调整」页）---
        "decisions": decisions,
        "bgmFiles": list_bgm_files(),
        # v3.41：film 带 `rel` —— 「成片入口」的高级剪辑预览要直接播**已烧字幕的成片文件**；
        # rel 由服务端下发（媒体根前缀别让前端自己拼，K71 同源纪律），走 /api/media/stream。
        "film": {"name": film_name, "exists": (e_dir / film_name).is_file(),
                 "rel": f"edits/{film_name}" if (e_dir / film_name).is_file() else ""},
        "coverRel": f"edits/{cover_name}" if (e_dir / cover_name).is_file() else "",
        "recues": (draft or {}).get("recues") or [],   # 「重新识别字幕」产出（空 = 用原转写）
        # v3.37：字幕条的人工挪动（键 = 该条的**原始起点毫秒**）。前端显示偏移、
        # 渲染取字幕都用同一份 —— 规则实现在 `subtitles.shift_cues()`（唯一出处）。
        "cueShifts": normalize_cue_shifts((draft or {}).get("cueShifts")),
    }


#: 成片伴生文件里那份"这条成片是怎么剪出来的"
EDIT_SUFFIX = ".edit.json"


def _merge_ranges(ranges: list, *, tol: float = 1.0) -> tuple[list[dict], int]:
    """把区间按起点排序并合并重叠/相接的部分，返回 ``(不重叠区间, 合并掉几处)``。

    为什么需要它：**成片的伴生记录里允许重叠**（渲染只是把区间顺序相接），
    而编辑页要求严格不重叠。同一段画面被放两次多半不是本意 —— 合并成并集
    既等价（覆盖的画面完全一样）又不会让用户一进页就看到"保存会失败"。
    拿不出 startMs/endMs 的条目直接跳过：脏数据不该让整条路走不通。
    """
    items: list = []
    for r in ranges:
        try:
            start, end = float(r["startMs"]), float(r["endMs"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < end:
            items.append((start, end))
    if not items:
        raise ValueError("这条成片的伴生记录里没有可用的分段，进不了剪辑页")
    items.sort()
    out: list = [list(items[0])]
    merged = 0
    for start, end in items[1:]:
        if start <= out[-1][1] + tol:               # 重叠或首尾相接 ⇒ 并起来
            if end > out[-1][1]:
                out[-1][1] = end
            merged += 1
        else:
            out.append([start, end])
    return [{"startMs": float(a), "endMs": float(b)} for a, b in out], merged


def plan_cut_from_film(film_name: str, *,
                       edits_dir: Path | None = None,
                       transcripts_dir: Path | None = None,
                       drafts_dir: Path | None = None) -> dict:
    """**候选产物已被删掉**时，用成片自己那份伴生 ``.edit.json`` 重建剪辑计划（v3.79）。

    用户 2026-10-09「剪辑渲染页面的视频进入不了高级剪辑」的真因就是候选 JSON 被删
    （实测 `/api/cut/plan` 返回 404），而界面上按钮照给、报错只写在窗口左下角。

    为什么不直接不给按钮（K69 的常规做法）：**改这条成片要的东西成片自己就有** ——
    `.edit.json` 里存着 `keepRanges`（渲染时写进去的）、标题、评分、候选身份与序号。
    缺的只有「AI 给的原始区间」和「六个工具的决策」，而这两样要么在草稿里、
    要么用默认值即可。所以这条路是真能用的，不是"硬撑"。

    与 :func:`plan_cut` 的差别（都要如实告诉上层）：
      · ``film`` 用**传进来的文件名**（不去重新推算 —— `resolve_film_stem` 带去重逻辑，
        对已存在的成片会推出 ``-2`` 这样的名字）；
      · ``decisions`` 取草稿里的，没草稿就是默认值（``.edit.json`` 不存决策）；
      · 返回值多一个 ``fallbackFrom: "edit-json"`` 与 ``filmRanges`` —— 界面上要能说明
        「候选产物已不在，按这条成片自己的分段打开」，别让用户以为是正常路径。

    读不出伴生文件 / 里面没有区间 ⇒ 抛 ``FileNotFoundError`` / ``ValueError``（端点层转 4xx）。
    """
    e_dir = Path(edits_dir) if edits_dir else ws.editor_output_dir
    tr_dir = Path(transcripts_dir) if transcripts_dir else ws.transcripts_dir
    d_dir = Path(drafts_dir) if drafts_dir else ws.cut_drafts_dir

    film = Path(str(film_name or "").strip()).name          # 只留文件名，去掉任何路径成分
    if not film.lower().endswith(".mp4"):
        film = f"{Path(film).stem}.mp4"
    stem = Path(film).stem
    if not FO.is_safe_stem(stem):
        raise ValueError("成片文件名不合法")
    meta_path = e_dir / f"{stem}{EDIT_SUFFIX}"
    if not meta_path.is_file():
        raise FileNotFoundError(f"这条成片没有伴生记录（{meta_path.name}），进不了剪辑页")
    meta = _load_json(meta_path)
    if not isinstance(meta, dict):
        raise ValueError("成片的伴生记录读不出内容")

    # ⚠️ 这里**不能**用 normalize_keep_ranges：那是**编辑草稿**的校验（要求严格不重叠），
    #    而成片的伴生记录里**允许**重叠 —— 渲染只是把区间按顺序相接（实测本机 33 条成片里
    #    27 条是两个嵌套区间，且区间之和恰好等于成片时长，说明同一段画面确实被放了两次）。
    #    拿草稿校验来验它，会把「本来能打开的成片」判成打不开 —— 第一版就是那么写的，
    #    33 条只过了 6 条。这里改成**合并重叠**：并集才是不重复的等价分段，
    #    而且合并后必然满足编辑页的校验，用户接着改、点保存都不会被自己绊住。
    raw_ranges = [r for r in (meta.get("keepRanges") or []) if isinstance(r, dict)]
    if not raw_ranges:
        raise ValueError("这条成片的伴生记录里没有分段（keepRanges），进不了剪辑页")
    film_ranges, merged_times = _merge_ranges(raw_ranges)

    base = str(meta.get("candidateName") or stem).strip() or stem
    try:
        idx_val = int(meta.get("candidateIdx"))
    except (TypeError, ValueError):
        idx_val = -1

    src = resolve_source_video(None, {}, base)               # 与 /api/edits 的 sourceExists 同源
    if isinstance(src, tuple):
        raise FileNotFoundError(src[1].get("error", "找不到源视频"))
    kind = source_kind_of(src)
    rel = f"{kind}/{src.name}" if kind else ""

    start_ms = min(float(r["startMs"]) for r in film_ranges)
    end_ms = max(float(r["endMs"]) for r in film_ranges)

    # 字幕段：转写是按**源录像 stem** 存的（与候选无关）⇒ 还在就照常取，与 plan_cut 同一套口径
    cues: list[dict] = []
    tpath = tr_dir / f"{base}.json"
    if not tpath.is_file() and (tr_dir / f"{stem}.json").is_file():
        tpath = tr_dir / f"{stem}.json"
    if tpath.is_file():
        try:
            transcript = _load_json(tpath)
        except ValueError:
            transcript = {}
        for seg in (transcript.get("segments") or []):
            try:
                s0 = float(seg.get("start") or 0) * 1000.0
                e0 = float(seg.get("end") or 0) * 1000.0
            except (TypeError, ValueError):
                continue
            if e0 <= start_ms or s0 >= end_ms:
                continue
            cues.append({"startMs": round(max(s0, start_ms), 1),
                         "endMs": round(min(e0, end_ms), 1),
                         "text": str(seg.get("text") or "")[:60]})

    draft = load_draft(base, idx_val, drafts_dir=d_dir)
    synthetic = False
    if not draft:
        # 没草稿 ⇒ 拿**这条成片自己的分段**当草稿，剪辑页一进去就是它现在的样子
        draft = {"keepRanges": [dict(r) for r in film_ranges], "decisions": {}}
        synthetic = True
    decisions = normalize_decisions((draft or {}).get("decisions"))

    cover_name = f"{stem}{COVER_SUFFIX}"
    return {
        "candidate": base,
        "idx": idx_val,
        "title": str(meta.get("title") or meta.get("coverTitle") or "").strip(),
        "scores": {"overall": meta.get("score") or 0},
        "startMs": round(start_ms, 1),
        "endMs": round(end_ms, 1),
        "durationMs": round(end_ms - start_ms, 1),
        "sourceRel": rel,
        "sourceName": src.name,
        "cues": cues,
        "hook": None,
        "planner": "",
        "draft": draft,
        "decisions": decisions,
        "bgmFiles": list_bgm_files(),
        "film": {"name": film, "exists": True, "rel": f"edits/{film}"},
        "coverRel": f"edits/{cover_name}" if (e_dir / cover_name).is_file() else "",
        "recues": (draft or {}).get("recues") or [],
        "cueShifts": normalize_cue_shifts((draft or {}).get("cueShifts")),
        # --- v3.79 兜底通路自己的标记（界面据此说明「走的不是常规路径」）---
        "fallbackFrom": "edit-json",
        "filmRanges": [dict(r) for r in film_ranges],
        # 合并掉几处重叠（>0 = 伴生记录里有重复区间，界面要如实说一句，别装没这回事）
        "rangesMerged": merged_times,
        "syntheticDraft": synthetic,
    }


# ---------------------------------------------------------------- 草稿

def _draft_path(base: str, idx: int, drafts_dir: Path) -> Path:
    if not FO.is_safe_stem(base):
        raise ValueError("候选名不合法")
    return drafts_dir / f"{base}__{int(idx)}{CUT_DRAFT_SUFFIX}"


def save_draft(candidate: str, idx: int, ranges: list, *,
               decisions: dict | None = None, recues: list | None = None,
               cue_shifts: dict | list | None = None,
               drafts_dir: Path | None = None,
               candidates_dir: Path | None = None) -> dict:
    """校验并保存 keep 段草稿（含六个工具的决策 + 字幕条偏移）。校验失败抛 ValueError。

    ``recues`` 省略时**沿用草稿里已有的重新识别结果**（先「重新识别字幕」、
    后「保存草稿」的场景不能把上次的结果抹掉）；``cue_shifts`` 同理。
    """
    d_dir = drafts_dir or ws.cut_drafts_dir
    c_dir = candidates_dir or ws.candidates_dir
    raw = str(candidate or "").strip()
    if not FO.is_safe_stem(raw):
        raise ValueError("候选产物名不合法")
    base = raw[: -len(".candidates.json")] if raw.endswith(".candidates.json") else raw

    cpath = c_dir / f"{base}.candidates.json"
    if not cpath.is_file():
        raise FileNotFoundError(f"候选产物不存在：{base}")
    data = _load_json(cpath)
    picked = _pick_candidate(data, int(idx))
    start_ms = float(picked["start"]) * 1000.0
    end_ms = float(picked["end"]) * 1000.0
    rs = normalize_keep_ranges(ranges, start_ms, end_ms)

    old = load_draft(base, int(idx), drafts_dir=d_dir) or {}
    merged = normalize_decisions({**(old.get("decisions") or {}), **(decisions or {})})
    keep_recues = recues if recues is not None else (old.get("recues") or [])
    keep_shifts = (normalize_cue_shifts(cue_shifts) if cue_shifts is not None
                   else normalize_cue_shifts(old.get("cueShifts")))

    d_dir.mkdir(parents=True, exist_ok=True)
    path = _draft_path(base, int(idx), d_dir)
    payload = {
        "candidate": base,
        "idx": int(idx),
        "keepRanges": rs,
        "totalMs": round(sum(r["endMs"] - r["startMs"] for r in rs), 1),
        "decisions": merged,
        "recues": keep_recues,
        "cueShifts": keep_shifts,
        "savedAt": round(time.time(), 1),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_draft(base: str, idx: int, *, drafts_dir: Path | None = None) -> dict | None:
    d_dir = drafts_dir or ws.cut_drafts_dir
    try:
        path = _draft_path(str(base), int(idx), d_dir)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mark_applied(base: str, idx: int, film_name: str, *,
                 drafts_dir: Path | None = None) -> bool:
    """成片已生成：在草稿上打个标记（**不删草稿**）。

    ⚠️ K55：早先这里的实现是「渲染完成 → clear_draft」，当时草稿只存 keep 段，
    删掉意味着"这次剪辑已经收官"。v3.24 起草稿还存着六个工具的决策、封面位置、
    重新识别的字幕 —— 它们是下一次「保存并覆盖成片」的输入，**不能跟着删**。
    """
    d_dir = drafts_dir or ws.cut_drafts_dir
    old = load_draft(str(base), int(idx), drafts_dir=d_dir)
    if not old:
        return False
    old["appliedAt"] = round(time.time(), 1)
    old["filmName"] = str(film_name or "")
    try:
        _draft_path(str(base), int(idx), d_dir).write_text(
            json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def clear_draft(base: str, idx: int, *, drafts_dir: Path | None = None) -> bool:
    """彻底丢掉草稿（顶栏「恢复 AI 原方案」专用）。"""
    d_dir = drafts_dir or ws.cut_drafts_dir
    try:
        path = _draft_path(str(base), int(idx), d_dir)
    except ValueError:
        return False
    if path.is_file():
        path.unlink(missing_ok=True)
        return True
    return False
