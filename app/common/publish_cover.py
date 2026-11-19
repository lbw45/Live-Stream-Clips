#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布封面：已有封面优先，没有就用成片首帧，派生 3:4 / 4:3 两份。

为什么单独放 ``app/common``：
- 发布页在 **editor 服务**，封面派生却要读 slicer 的产物；按 D29 不允许 editor
  直接 import slicer 服务模块。这里只依赖 ``paths.ws``（ffmpeg/ffprobe）和 PIL，
  两边都能安全调用，规则只有一份（K44）。
- 文件名跟成片走（<stem>_cover.jpg），**不共用固定文件名** —— 否则 edits 目录里
    多支成片会互相覆盖。

优先级（用户 2026-11-03）：
1. 已经有封面：``<stem>_cover.jpg``；
2. 否则从**成片视频本身**抽首帧，存为 ``cover_first.jpg`` / ``<stem>_cover_auto.jpg``；
3. 两种比例固定 3:4（1080×1440）与 4:3（1440×1080），统一由本模块生成。
"""
from __future__ import annotations
import json

import uuid
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from app.common import proc
from app.common.paths import ws

#: 上传封面比例与尺寸的**唯一出处**（发布预览、上传、测试都读这里）
COVER_RATIOS: tuple[dict, ...] = (
    {"key": "3x4", "label": "3:4 竖版", "width": 1080, "height": 1440,
     "suffix": "_3x4.jpg"},
    {"key": "4x3", "label": "4:3 横版", "width": 1440, "height": 1080,
     "suffix": "_4x3.jpg"},
    # v4.76（用户 2026-11-12）：加 **9:16 与 16:9** 两档（原方案只有 3:4/4:3）
    {"key": "9x16", "label": "9:16 竖屏", "width": 1080, "height": 1920,
     "suffix": "_9x16.jpg"},
    {"key": "16x9", "label": "16:9 横屏", "width": 1920, "height": 1080,
     "suffix": "_16x9.jpg"},
)
JPEG_QUALITY = 92


def _is_fresh(src: Path, dst: Path, cfg: Path | None = None) -> bool:
    """dst 存在且不比 src 旧 → 可复用（幂等：重开/反复预览不做重复转码）。

    v4.76（方案 §9.1）：封面**设计配置**是第三个输入 —— cover.json 改了也必须重渲染，
    否则用户改完文案点保存，图还是旧的。取三者的最新 mtime 比较。
    """
    try:
        if not (dst.is_file() and dst.stat().st_size > 0):
            return False
        newest = src.stat().st_mtime
        if cfg is not None and cfg.is_file():
            newest = max(newest, cfg.stat().st_mtime)
        return dst.stat().st_mtime + 0.01 >= newest
    except OSError:
        return False


def _save_jpeg(src: Image.Image, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + f".{uuid.uuid4().hex}.tmp")
    try:
        src.convert("RGB").save(tmp, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        tmp.replace(dst)
    finally:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


def _derive(src: Path, dst: Path, width: int, height: int) -> None:
    """按目标比例居中裁切（不拉伸）。源图不够大时按现有尺寸放大到平台要求。"""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        fitted = ImageOps.fit(im.convert("RGB"), (int(width), int(height)),
                              method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        _save_jpeg(fitted, dst)


def _probe_duration(video: Path) -> float:
    """成片时长（秒）；拿不到返回 0（调用方按 0 处理：退化为只抽首帧）。"""
    try:
        r = proc.run(
            [str(ws.ffprobe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return max(0.0, float(str(getattr(r, "stdout", "") or "").strip() or 0))
    except (OSError, TypeError, ValueError):
        return 0.0


def _sharpness(p: Path) -> float:
    """**清晰度**评分：拉普拉斯/边缘能量方差（越大越清晰，糊帧/黑场/转场明显偏低）。"""
    try:
        with Image.open(p) as im:
            g = im.convert("L").resize((320, 180))
            edge = g.filter(ImageFilter.FIND_EDGES)
            return float(ImageStat.Stat(edge).var[0])
    except (OSError, ValueError):
        return -1.0


def _grab_frame(video: Path, sec: float, out: Path) -> bool:
    """在 ``sec`` 秒处抽一帧（-ss 放在 -i 前 = 快速定位）。"""
    try:
        r = proc.run(
            [str(ws.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{max(0.0, float(sec)):.2f}", "-i", str(video),
             "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        return int(getattr(r, "returncode", 1)) == 0 and out.is_file() and out.stat().st_size > 0
    except OSError:
        return False


def _first_frame(video: Path, dst: Path) -> None:
    """抽**最清晰**的一帧当封面底图（v4.80 起不再只取第一帧）。

    为什么改：首帧常落在**转场/淡入/虚焦/黑场**上（用户实测"首帧模糊"）。
    做法：按成片时长取 5 个采样点（10%–90%）各抽一帧，用边缘能量方差挑最清晰的那张；
    时长探测失败 / 全部抽帧失败 ⇒ 回退原来的「抽首帧」行为（绝不空手）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex

    def _fallback() -> None:
        tmp = dst.with_name(dst.name + f".{tag}.tmp.jpg")
        try:
            ok = _grab_frame(video, 0.0, tmp)
            if not ok:
                raise RuntimeError("抽首帧失败（ffmpeg 没产出画面）")
            tmp.replace(dst)
        finally:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass

    dur = _probe_duration(video)
    if dur <= 1.0:
        _fallback()
        return
    points = [dur * r for r in (0.1, 0.3, 0.5, 0.7, 0.9)]
    best: tuple[float, Path] | None = None
    cands: list[Path] = []
    try:
        for i, sec in enumerate(points):
            p = dst.with_name(dst.name + f".{tag}.f{i}.jpg")
            cands.append(p)
            if not _grab_frame(video, sec, p):
                continue
            sc = _sharpness(p)
            if best is None or sc > best[0]:
                best = (sc, p)
        if best is None:
            _fallback()
            return
        best[1].replace(dst)
    finally:
        for p in cands:
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass


def _spec_for(source_kind: str, video: Path) -> tuple[Path, Path, Path, str]:
    """返回 ``(已有封面, 首帧兜底, 比例文件名前缀, 媒体逻辑前缀)``。

    封面与成片同目录，正式封面叫 ``<stem>_cover.jpg``，首帧兜底叫
    ``<stem>_cover_auto.jpg``。``source_kind`` 参数保留以维持接口形状。
    """
    base = video.with_name(video.stem + "_cover.jpg")
    return (base, video.with_name(video.stem + "_cover_auto.jpg"),
            video.parent, "edits")


def cover_files_for(video: Path) -> tuple[Path, Path, str]:
    """按**路径本身**返回 ``(正式封面, 首帧兜底, kind)``。

    所有调用方都走这里算文件名，别各自拼（K44）。
    """
    video = Path(str(video))
    existing, fallback, _folder, _rel = _spec_for("edit", video)
    return existing, fallback, "edit"


def _paths_for(base: Path, existing: bool) -> list[tuple[dict, Path]]:
    out = []
    if existing:
        # foo_cover.jpg → foo_cover_3x4.jpg；cover.jpg → cover_3x4.jpg
        stem = base.stem
        for spec in COVER_RATIOS:
            out.append((spec, base.with_name(stem + spec["suffix"])))
    else:
        # foo_cover_auto.jpg → foo_cover_auto_3x4.jpg；cover_first.jpg → cover_first_3x4.jpg
        stem = base.stem
        for spec in COVER_RATIOS:
            out.append((spec, base.with_name(stem + spec["suffix"])))
    return out


def _cleanup_auto_series(fallback: Path) -> int:
    """删掉「首帧兜底」那一整套（`xxx_cover_auto.jpg` + 四个比例），返回删了几个。

    v4.95（用户 2026-11-14）：「每次修改封面图后都是覆盖原有的，不要每次都存为一次，
    这样很占位置」——产物名本来就是固定覆盖，真正占空间的是**并存的两套**：
    设计器生成的 `_cover*.jpg`（正式）与早期自动抽首帧的 `_cover_auto*.jpg`（约 0.9MB/片）。
    一旦有了正式封面，后者**永远用不到**（`_spec_for` 优先 `_cover.jpg`），这里顺手清掉。
    ⚠️ 只允许删 `_cover_auto` 这一族文件名 —— 传进来的 fallback 若不是它就直接返回 0。
    """
    name = fallback.name
    if "_cover_auto" not in name and not name.startswith("cover_first"):
        return 0
    cands = [fallback]
    stem = fallback.stem
    for spec in COVER_RATIOS:
        cands.append(fallback.with_name(stem + spec["suffix"]))
    n = 0
    for p in cands:
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            pass                                    # 删不掉（占用）不影响主流程
    return n


def _img_info(path: Path) -> dict:
    try:
        with Image.open(path) as im:
            w, h = im.size
        st = path.stat()
        return {"exists": True, "width": int(w), "height": int(h),
                "mtime": round(st.st_mtime, 3), "bytes": int(st.st_size)}
    except (OSError, ValueError):
        return {"exists": False, "width": 0, "height": 0, "mtime": 0, "bytes": 0}


def assets_for_file(video_path: str | Path, source_kind: str = "edit",
                    *, generate: bool = True) -> dict:
    """解析一份成片的发布封面。

    ``generate=False`` 只检查现有文件，不触发抽帧/裁切（列表轮询用）。
    返回结构供前端和发布执行器共用，路径与逻辑前缀均由本模块算好（K44）。
    """
    video = Path(str(video_path))
    if not video.is_file():
        raise FileNotFoundError(f"成片文件已不在：{video}")

    existing, fallback, folder, rel_prefix = _spec_for("edit", video)
    # 0 字节/损坏的“封面”不算有封面（is_file() 会把它当成存在，PIL 随后才炸）
    existing_ok = _img_info(existing).get("exists", False)
    base = existing if existing_ok else fallback
    source_label = "已有封面" if existing_ok else "视频首帧"
    if existing_ok:
        # v4.95：已有正式封面 ⇒ 首帧兜底那套用不到了，顺手清掉（省 ~0.9MB/片，幂等）
        _cleanup_auto_series(fallback)
    refreshed = False

    # v4.76（方案 §3/§9.2）：封面**设计配置**（`<stem>_cover.json`）存在才叠字，
    # 没有配置走原路径（老素材行为完全不变）。配置 mtime 参与 _is_fresh 判定。
    cfg_path = ws.cover_json_for(video)
    cfg = {}
    if generate and cfg_path.is_file():
        try:
            _maybe = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(_maybe, dict) and (_maybe.get("copy") or {}).get("line1"):
                cfg = _maybe
        except (OSError, ValueError):
            cfg = {}

    if generate:
        # 已有封面不重建；首帧兜底只在缺失或视频比图更新时重建（成片重渲后自动跟上）
        if not base.is_file() or (
                base == fallback and _is_fresh(video, base, cfg_path) is False):
            _first_frame(video, base)
            refreshed = True
        for spec, target in _paths_for(base, base == existing):
            if not _is_fresh(base, target, cfg_path):
                # v4.76：有设计配置 ⇒ cover_design 叠字渲染；失败回退原裁切（不阻断）
                from app.common import cover_design as _cd
                if cfg and _cd.render(base, cfg, spec, target):
                    refreshed = True
                    continue
                _derive(base, target, spec["width"], spec["height"])
                refreshed = True

    covers = []
    for spec, target in _paths_for(base, base == existing):
        info = _img_info(target)
        covers.append({
            "key": spec["key"], "label": spec["label"], "file": target.name,
            "path": str(target), "rel": f"{rel_prefix}/{target.name}",
            "keyName": f"{spec['key']}", "targetWidth": spec["width"],
            "targetHeight": spec["height"], **info,
        })
    base_info = _img_info(base)
    return {
        "ok": True,
        "sourceKind": source_kind,
        "source": source_label,
        "sourceFile": base.name,
        "baseRel": f"{rel_prefix}/{base.name}",
        "base": {**base_info, "file": base.name, "path": str(base),
                 "rel": f"{rel_prefix}/{base.name}"},
        "covers": covers,
        "generated": bool(generate and refreshed),
    }
