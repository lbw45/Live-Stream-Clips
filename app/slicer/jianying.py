#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪映草稿导出（v3.75，）—— 把工作台的「剪辑结果」导出成剪映专业版草稿。

**为什么值得做**（用户 2026-10-08 原话）：「后续需要给工作台的剪辑功能也是可以给到
剪映打开使用修改的，所以这个剪映功能是工作台复用的比较重要」。

成片在工作台里是**字幕已烧进像素**的 mp4 —— 想改一个字只能重渲染。导出成剪映草稿后，
剪映里**每一段保留区间都是一个独立片段、每一条字幕都是能改的文本**，改完直接在剪映里出片。

**两条路径（这是本模块最关键的不变量）**：

- ① **源素材还在** → 视频轨 = 源素材按 `keepRanges` 切成 N 段（每段可单独拖/删/调），
  字幕走**文本轨**（文案可改）。这是「可编辑」的那条路。
- ② **源素材不在了** → 视频轨 = 成片整段，**并且不再加文本轨** —— 成片的字幕已经烧进
  像素了，再加文本轨就是双字幕（v3.68 / K117 那条教训在这里同样成立）。此时如实说明
  「源素材不在 ⇒ 导出的是整段成片，字幕已烧录不可改」，不假装它可编辑。

依赖：**pyjianyingdraft 0.3.0**。库没装时 `status()` 如实返回不可用，
**不抛异常、不给点了才报错的死按钮**（K69）。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from ..common.paths import ws

SEC = 1_000_000          # pyJianYingDraft 的时间单位一律是**微秒**
#: 剪映草稿箱的默认位置。优先读环境变量 ``JIANYING_DRAFT_DIR``，
#: 否则按剪映的默认安装位置推导（设置页里随时可改成自己的实际路径）。
DRAFT_DIR = os.environ.get("JIANYING_DRAFT_DIR") or str(
    Path(os.environ.get("LOCALAPPDATA") or Path.home())
    / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft")
DEFAULT_DRAFT_DIR = DRAFT_DIR   # v3.163：默认值单独一个名字（设置里「恢复默认」用它）
DRAFT_FPS = 30
_DRAFT_SETTINGS_KEY = "jianyingDraftDir"   # 存在 ws.settings_path（settings.json）里


def get_draft_dir_setting() -> str:
    """设置里显式配置的草稿箱路径（空串 = 没配置，用默认）。"""
    try:
        data = json.loads(ws.settings_path.read_text(encoding="utf-8"))
        return str(data.get(_DRAFT_SETTINGS_KEY) or "").strip()
    except (OSError, ValueError):
        return ""


def draft_dir() -> str:
    """**运行时草稿箱**：设置里显式配置的优先，否则用默认（``DRAFT_DIR``）。"""
    custom = get_draft_dir_setting()
    return custom or DRAFT_DIR


def set_draft_dir_setting(path: str) -> tuple[bool, str]:
    """保存/清空草稿箱配置。空串 = 恢复默认（删掉这个键，不写文件占位）。

    返回 ``(ok, error)``；目录必须**存在且可写** —— 草稿导出要往里建子目录，
    指一个不存在的盘符等于把「导出到剪映」变成必失败（K69：在这里拦，别在导出时报）。
    """
    p = str(path or "").strip()
    data: dict = {}
    try:
        if ws.settings_path.is_file():
            data = json.loads(ws.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not p:
        data.pop(_DRAFT_SETTINGS_KEY, None)
        try:
            ws.settings_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            return False, f"写入设置失败：{exc}"
        return True, ""

    pp = Path(p)
    if not pp.is_dir():
        return False, f"目录不存在：{p}"
    try:
        probe = pp / ".slicer_write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return False, f"目录不可写：{exc}"
    data[_DRAFT_SETTINGS_KEY] = str(pp)
    try:
        ws.settings_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return False, f"写入设置失败：{exc}"
    return True, ""



# ------------------------------------------------------------------ 依赖与可用性

def _lib():
    """导入 pyJianYingDraft；没装时返回 None（不抛，交给 status() 如实上报）。"""
    try:
        import pyJianYingDraft as draft
        return draft
    except Exception:
        return None


def status() -> dict:
    """剪映草稿导出**能不能用** —— 前端据此决定给不给按钮（K69：不知道就说不知道）。"""
    lib = _lib()
    d = Path(draft_dir())
    try:
        dir_exists = d.is_dir()
    except OSError:
        dir_exists = False
    ok = lib is not None and dir_exists
    reason = ""
    if lib is None:
        reason = "缺少依赖 pyjianyingdraft（需用 uv pip 装进项目 runtime）"
    elif not dir_exists:
        reason = f"剪映草稿目录不存在：{d}"
    return {
        "ok": ok,
        "reason": reason,
        "draftDir": str(d),
        "dirExists": dir_exists,
        "libOk": lib is not None,
        "draftDirCustom": bool(get_draft_dir_setting()),
        "defaultDraftDir": DEFAULT_DRAFT_DIR,
    }


# ------------------------------------------------------------------ 素材与辅助

def _film_paths(name: str):
    """成片 mp4 + 伴生 .edit.json（路径唯一出处走 paths.ws，不在这里另拼）。"""
    root = ws.editor_output_dir
    mp4 = root / name
    side = mp4.with_suffix(".edit.json")
    return root, mp4, side


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _resolve_source(candidate: str):
    """定位源素材 —— **唯一实现**是 `renderer.resolve_source_video`（K44）。

    与 `/api/edits` 下发的 `sourceExists` 同源，避免「列表说在、导出说不在」的漂移。
    返回 Path 或 None。
    """
    from .renderer import resolve_source_video
    stem = Path(str(candidate or "").strip()).name
    if not stem:
        return None
    data: dict = {}
    side = ws.candidates_dir / f"{stem}.candidates.json"
    if side.is_file():
        data = _load_json(side)
    got = resolve_source_video(None, data if isinstance(data, dict) else {}, stem)
    return got if isinstance(got, Path) else None


def _subtitle_style_for(width: int, height: int):
    """字幕样式（与渲染同源：`SubtitleStyle().scaled(w, h)` —— 就是 `build_ass` 用的那一次缩放）。

    渲染时 ASS 的 PlayRes 用成片真实分辨率、样式也按它缩；这里要的是**同一套字号/边距**，
    所以照抄 `build_ass` 的做法，不另算一份（K44）。
    """
    from .subtitles import SubtitleStyle
    st = SubtitleStyle()
    try:
        if width > 0 and height > 0:
            st = st.scaled(width, height)
    except Exception:
        pass
    return st


def _text_size(font_px: float, height: int) -> float:
    """剪映字号 —— 用参考实现反推的标定系数。

    参考项目（已在剪映里验过）：字号 64px / 画布高 1440 → `TextStyle(size=8.0)`，
    即 ``size = font_px / height * 180``。这里照抄这个换算，不另立一套。
    """
    if height <= 0:
        return 8.0
    return round(float(font_px) / float(height) * 180.0, 2)


def _text_transform_y(style, height: int) -> float:
    """字幕垂直位置 → `ClipSettings.transform_y`。

    库的注释写着「单位为半个画布高」「剪映导入的字幕似乎取此值为 -0.8」⇒
    **负值往下**，且 -0.8 ≈ 画面 90% 高度处。于是
    ``transform_y = 1 - 2 * y_frac``（y_frac 是从顶部算的比例）。
    y_frac 取自工作台自己的字幕边距 `margin_v`（底部对齐），与渲染同口径。
    """
    try:
        margin_v = float(getattr(style, "margin_v", 380) or 380)
        font_px = float(getattr(style, "font_size", 72) or 72)
    except (TypeError, ValueError):
        margin_v, font_px = 380.0, 72.0
    h = float(height) if height > 0 else 1920.0
    y_frac = 1.0 - (margin_v + font_px) / h
    y_frac = min(max(y_frac, 0.05), 0.95)
    return round(1.0 - 2.0 * y_frac, 3)


def _pick_font():
    """中文字体（剪映字体枚举里挑一个存在的）；都没有就交给剪映默认。"""
    from pyJianYingDraft import FontType
    for name in ("思源黑体", "黑体", "微软雅黑", "宋体"):
        if hasattr(FontType, name):
            return getattr(FontType, name)
        for m in list(FontType):
            if m.name == name:
                return m
    return None


# ------------------------------------------------------------------ 导出

def export_film(name: str, *, draft_name: str | None = None) -> tuple[int, dict]:
    """**成片页**入口：成片 → 剪映草稿。

    返回 ``(http_code, payload)``，直接给 web_server 用。
    """
    lib = _lib()
    if lib is None:
        return 500, {"error": "缺少依赖 pyjianyingdraft（需装进项目 runtime 后重启服务）"}
    st = status()
    if not st["dirExists"]:
        return 500, {"error": f"剪映草稿目录不存在：{DRAFT_DIR}", "hint": st["reason"]}

    root, mp4, side = _film_paths(name)
    if not mp4.is_file():
        return 404, {"error": f"成片不存在：{name}"}
    meta = _load_json(side) if side.is_file() else {}
    if not meta:
        return 400, {"error": f"没有伴生的剪辑方案（.edit.json）：{name}。"
                              "老成片或方案被删掉时导不出可编辑的分段，只能整段导出。"}
    return _export(meta, mp4, draft_name=draft_name,
                   base_default=Path(name).stem or "Live Stream Clips")


def export_plan(candidate: str, idx: int, ranges: list[dict],
                decisions: dict | None = None, *, draft_name: str | None = None
                ) -> tuple[int, dict]:
    """**剪辑页**入口：把「当前这一版剪辑」导出成剪映草稿，**不必先出成片**。

    用户 2026-10-08：「后续需要给工作台的剪辑功能也是可以给到剪映打开使用修改的」——
    剪辑页里改的是**候选**（candidate + idx + 当前保留区间），那时还没有成片文件，
    所以这里按源录像切段，导出后在剪映里继续调。

    与 `export_film` 共用同一个 `_export`（K44）：两处各写一遍，分段口径与字幕文案必漂移。
    """
    lib = _lib()
    if lib is None:
        return 500, {"error": "缺少依赖 pyjianyingdraft（需装进项目 runtime 后重启服务）"}
    st = status()
    if not st["dirExists"]:
        return 500, {"error": f"剪映草稿目录不存在：{DRAFT_DIR}", "hint": st["reason"]}

    cand = str(candidate or "").strip()
    if not cand:
        return 400, {"error": "没有指定切片候选（candidate）"}
    keep = [r for r in (ranges or [])
            if isinstance(r, dict) and float(r.get("endMs") or 0) > float(r.get("startMs") or 0)]
    if not keep:
        return 400, {"error": "一个保留区间都没有 —— 先剪出要保留的段落再导出"}

    source = _resolve_source(cand)
    if source is None:
        return 400, {"error": f"源素材不在：{cand}。剪辑页导出要按保留区间切源录像，"
                              "源录像没了就切不了（可先在工作台里生成成片，再去成片页导出整段）。"}

    from app.slicer.render import probe_media
    info = probe_media(source) or {}
    meta = {
        "candidateName": cand,
        "candidateIdx": idx,
        "keepRanges": keep,
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "bgm": str((decisions or {}).get("bgm") or "").strip(),
    }
    stem = Path(cand).stem or "切片"
    return _export(meta, None, draft_name=draft_name,
                   base_default=f"{stem}#{int(idx or 0) + 1}")


def _export(meta: dict, film_mp4, *, draft_name: str | None,
            base_default: str) -> tuple[int, dict]:
    """成片 / 剪辑方案 → 剪映草稿（**唯一实现**，两个入口共用，K44）。

    - ``film_mp4`` 有值 ⇒ 成片页（源不在时可退成「整段成片」）；
    - ``film_mp4`` 为 None ⇒ 剪辑页（只走源素材那条路，没有退路就如实报错）。
    """
    from pyJianYingDraft import (AudioMaterial, AudioSegment, ClipSettings,
                                 DraftFolder, TextBorder, TextSegment, TextStyle,
                                 Timerange, TrackSpec, TrackType, VideoMaterial,
                                 VideoSegment)

    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    if width <= 0 or height <= 0:
        width, height = 1080, 1920

    keep = [r for r in (meta.get("keepRanges") or [])
            if isinstance(r, dict) and float(r.get("endMs") or 0) > float(r.get("startMs") or 0)]
    candidate = str(meta.get("candidateName") or "")
    source = _resolve_source(candidate) if candidate else None

    # ---------- 决定走哪条路（关键不变量，见模块 docstring） ----------
    use_source = source is not None and bool(keep)
    if not use_source and film_mp4 is None:
        return 400, {"error": "既没有源素材也没有成片文件 —— 没有可放进剪映的画面"}
    film_material = None
    if film_mp4 is not None:
        try:
            film_material = VideoMaterial(str(film_mp4))
        except Exception:
            film_material = None
    film_dur_us = int(getattr(film_material, "duration", 0) or 0)
    if film_dur_us <= 0 and meta.get("durationSec"):
        film_dur_us = int(float(meta["durationSec"]) * SEC)

    total_us = film_dur_us
    if use_source:
        total_us = int(sum(float(r["endMs"]) - float(r["startMs"]) for r in keep) * 1000)
        if film_dur_us > 0:
            total_us = min(total_us, film_dur_us)   # 以成片真实时长为准（K126：钳制）
    if total_us <= 0:
        return 400, {"error": "算不出时长（既没有 durationSec，素材也读不出时长）"}

    base_name = draft_name or base_default

    folder = DraftFolder(draft_dir())
    try:
        script = folder.create_draft(base_name, width, height,
                                     fps=DRAFT_FPS, allow_replace=True)
    except PermissionError:
        # K127：剪映正开着同名草稿（目录里 .locked）→ 另存带时间戳的新草稿，
        #        而不是把「别人正在编辑」报成失败
        alt = f"{base_name} {datetime.now():%m%d-%H%M%S}"
        script = folder.create_draft(alt, width, height,
                                     fps=DRAFT_FPS, allow_replace=True)
        base_name = alt
    except Exception as exc:                                  # 其它失败也要说真原因
        return 500, {"error": f"创建剪映草稿失败：{exc}"}

    # ---------- 轨道（后加的在上面） ----------
    script.append_track(TrackSpec(TrackType.video, name="画面"))
    bgm_name = str(meta.get("bgm") or "").strip()
    bgm_path = (ws.music_dir / bgm_name) if bgm_name else None
    has_bgm = bool(bgm_path and bgm_path.is_file())
    if has_bgm:
        script.append_track(TrackSpec(TrackType.audio, name="音乐"))
    # 字幕只在「源素材路径」下才加 —— 成片里字幕已烧进像素（见模块 docstring）
    if use_source:
        script.append_track(TrackSpec(TrackType.text, name="字幕A"))
        script.append_track(TrackSpec(TrackType.text, name="字幕B"))

    # v3.157（用户 2026-10-28）：「转场在剪映里要能自由改」「默认不导入剪映」
    #   ⇒ 草稿里放的是**剪映自己的转场素材**（叠化/闪黑/闪白），不是烤进画面的效果；
    #     什么时候导出完全由用户点「导入剪映」决定（渲染本身不建草稿）。
    n_trans = 0
    trans_label = ""
    trans_name = {"fade": "叠化", "black": "闪黑", "white": "闪白"}
    tmode = str(meta.get("transition") or "").strip().lower()
    trans_enum = None
    if use_source and tmode in trans_name:
        try:
            from pyJianYingDraft import TransitionType
            trans_enum = getattr(TransitionType, trans_name[tmode], None)
        except Exception:                                     # noqa: BLE001
            trans_enum = None
        if trans_enum is not None:
            trans_label = trans_name[tmode]

    n_video = 0
    if use_source:
        src_mat = None
        try:
            src_mat = VideoMaterial(str(source))
        except Exception as exc:
            return 500, {"error": f"读源素材失败：{source.name}（{exc}）"}
        src_dur_us = int(getattr(src_mat, "duration", 0) or 0)
        cursor = 0
        for r in keep:
            s_us = int(float(r["startMs"]) * 1000)
            d_us = int((float(r["endMs"]) - float(r["startMs"])) * 1000)
            if src_dur_us > 0:
                # K126：ffmpeg 切割有毫秒级偏差，不钳制到素材实际时长剪映会报「超出素材时长」
                s_us = min(s_us, max(src_dur_us - 1, 0))
                d_us = min(d_us, src_dur_us - s_us)
            if d_us <= 0 or cursor >= total_us:
                continue
            d_us = min(d_us, total_us - cursor)
            if d_us <= 0:
                continue
            seg = VideoSegment(src_mat,
                               target_timerange=Timerange(cursor, d_us),
                               source_timerange=Timerange(s_us, d_us))
            # 钩子（第一段）与正文（第二段）之间：草稿里挂一个**可编辑的**转场
            if trans_enum is not None and n_video == 0:
                t_us = int(max(float(meta.get("transitionSec") or 0.4), 0.08) * SEC)
                t_us = max(min(t_us, d_us - 1), 1)      # 不能长过这一段
                try:
                    seg.add_transition(trans_enum, duration=t_us)
                    n_trans = 1
                except Exception:                       # noqa: BLE001
                    n_trans = 0
            script.add_segment(seg, track="画面")
            cursor += d_us
            n_video += 1
        if n_video == 0:
            return 500, {"error": "没有任何保留区间能放进剪映（源素材时长可能比成片短）"}
    else:
        if film_material is None:
            return 500, {"error": f"读成片失败：{film_mp4}"}
        script.add_segment(
            VideoSegment(film_material, target_timerange=Timerange(0, total_us)),
            track="画面",
        )
        n_video = 1

    if has_bgm:
        try:
            a_mat = AudioMaterial(str(bgm_path))
            a_dur = int(getattr(a_mat, "duration", 0) or 0)
            d_us = min(total_us, a_dur) if a_dur > 0 else total_us
            script.add_segment(AudioSegment(a_mat, target_timerange=Timerange(0, d_us)),
                               track="音乐")
        except Exception:
            pass          # BGM 读不出来不影响主流程（视频与字幕才是主体）

    n_text = 0
    if use_source:
        cues = _cue_list(meta, candidate, keep)
        style = _subtitle_style_for(width, height)
        font = _pick_font()
        ty = _text_transform_y(style, height)
        size = _text_size(getattr(style, "font_size", 72), height)
        for i, c in enumerate(cues):
            s_us = int(float(c.get("startMs") or 0) * 1000)
            e_us = int(float(c.get("endMs") or 0) * 1000)
            e_us = min(e_us, total_us)
            d_us = e_us - s_us
            if d_us <= 100_000 or s_us >= total_us:      # 短于 0.1s 的碎片不值得放
                continue
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            seg = TextSegment(
                text,
                Timerange(s_us, d_us),
                font=font,
                style=TextStyle(size=size, bold=True, color=(1.0, 1.0, 1.0), align=1),
                clip_settings=ClipSettings(transform_y=ty),
                border=TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=25.0),
            )
            # K125：剪映**同轨文本段不可重叠** ⇒ 交替放 A/B 两条轨
            script.add_segment(seg, track="字幕A" if i % 2 == 0 else "字幕B")
            n_text += 1

    try:
        script.save()
    except Exception as exc:
        return 500, {"error": f"保存剪映草稿失败：{exc}"}

    mode = "source" if use_source else "film"
    # 剪辑页那条路没有成片文件 ⇒ 主体名取候选名（别让 payload 里出现空字符串，K69）
    label = Path(film_mp4).name if film_mp4 is not None \
        else str(meta.get("candidateName") or "")
    return 200, {
        "ok": True,
        "film": label,
        "draftName": base_name,
        "draftPath": str(Path(DRAFT_DIR) / base_name),
        "mode": mode,
        "tracks": {"video": n_video, "text": n_text, "audio": 1 if has_bgm else 0},
        "transition": trans_label if n_trans else "",
        "transitionSec": float(meta.get("transitionSec") or 0.4) if n_trans else 0.0,
        "message": ("已导出剪映草稿：%s（%d 个片段 + %d 条字幕，字幕可改）"
                    % (base_name, n_video, n_text)) if use_source else
                   ("已导出剪映草稿：%s（整段成片；源素材已不在，字幕是烧录的、改不了）"
                    % base_name),
        "exportedAt": round(time.time(), 1),
    }


def _cue_list(meta: dict, candidate: str, keep: list[dict]) -> list[dict]:
    """字幕 cue：`.edit.json` 里**只存了条数**，真文案得从转写按 keepRanges 重算。

    与渲染用的是同一个 `cues_from_segments`（K44）—— 否则草稿里的字幕会和成片错位
    （这正是 editplan 文档开头写的那个「钩子前置把时间轴整体后移」的坑）。
    """
    if not candidate:
        return []
    from .subtitles import cues_from_segments
    tpath = ws.transcripts_dir / f"{candidate}.json"
    tr = _load_json(tpath)
    segments = list(tr.get("segments") or [])
    if not segments:
        return []
    try:
        return cues_from_segments(segments, keep, None, False)
    except Exception:
        return []

