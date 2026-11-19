#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面选帧（v4.85）：从成片里挑「画面上最没有字幕/字样」的帧做封面底图。

需求（用户 2026-11-14，带截图圈了位置）：
「新增选择其他帧为封面图，去找尽量画面中没有字幕和字样的帧为图，备选多几帧给我选择」。

做法（全本地、不加新依赖）：
1. ffmpeg **一次解码**均匀抽 ``pool`` 帧（跳过首尾 3% —— 片头有钩子大花字、片尾常是黑场）；
2. 逐帧跑 OCR（**复用** :mod:`app.slicer.ocr_verify` 的引擎，rapidocr 为可选依赖），
   统计**文字块数**与**文字面积占比**（box 面积 / 画面面积）—— 越大越"脏"；
3. 顺带算平均亮度：过暗/过曝的帧降分（转场黑场、闪光）；
4. 脏分 → 干净分 0–100，取最干净的 ``count`` 帧，且彼此间隔 ≥ ``min_gap`` 秒
   （同一秒挑两张没有意义）；按时序返回，方便左侧竖排看"哪一段的帧"；
5. 结果按 ``mtime+size`` 缓存到 ``runtime_data/cover_frames/<stem>/analysis.json``
   —— 同一成片反复开子窗不重复跑 OCR（24 帧约 3–8 秒，首次之后瞬时）；
6. OCR 不可用 ⇒ 降级为「字幕常驻区边缘密度」启发式，并**如实**标 ``degraded=True``
   （K69：不许假装验过）。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from app.common import proc
from app.common.paths import ws

#: 抽多少帧做海选：按**每秒 2 帧**算（字幕句间空隙常只有 0.3–0.6 秒，1 fps 会大片错过），
#: 夹在 [24, 96] 之间 —— 20 秒片 ≈ 40 帧，60 秒片封顶 96 帧。
POOL_PER_SEC = 2.0
POOL_MIN, POOL_MAX = 24, 96
#: 默认给几张候选
COUNT = 6
#: 候选之间的最小时间间隔（秒）—— 避免同一秒/同一姿势挑两张
MIN_GAP = 2.0
#: 跳过首尾的比例（片头花字、片尾黑场）
EDGE_SKIP = 0.03
#: 抽帧宽度（选中后要当封面底图，必须够大）
FRAME_W = 1080
#: OCR 用的宽度：640 宽对中文识别足够，速度约 1080 的 2.5 倍
OCR_W = 640
#: 字幕带：y 中心超过这个比例（% ）就算"我们烧的字幕"，权重比画面原生字更高
SUBTITLE_BAND_PCT = 65.0
#: 分析算法版本：**分区统计/采样密度**一变就 +1 —— 老缓存里的字段结构不同，
#: 不校验版本会把「没有 subBlocks 字段」误读成"无字幕"（假干净帧）。
ALGO_VERSION = 2
#: 亮度合理区间：低于/高于都降分（黑场、闪白）
LUMA_LO, LUMA_HI = 20.0, 235.0


def frames_dir(video: Path) -> Path:
    """某条成片的候选帧目录（runtime_data，不进素材库、不进 edits）。

    v4.97：成片的封面配置跟成片走（同目录、同 stem），
    互相覆盖候选帧与分析缓存。所以带上父目录名（做一次文件名安全化）区分来源。
    """
    video = Path(video)
    if video.parent.name in ("edits",) or video.parent.parent.name in ("", None):
        tag = video.stem
    else:
        import re as _re
        safe = _re.sub(r"[^\w一-鿿.\-]+", "_", video.parent.name)[:40]
        tag = f"{safe}__{video.stem}"
    return ws.slicer_runtime / "cover_frames" / tag


def _analysis_path(video: Path) -> Path:
    return frames_dir(video) / "analysis.json"


def _duration(video: Path) -> float:
    """ffprobe 时长（秒）；读不出返回 0。"""
    cmd = [str(ws.ffprobe), "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(video)]
    try:
        r = proc.run(cmd, capture_output=True, timeout=30)
        return max(0.0, float((r.stdout or b"0").decode("utf-8", "ignore").strip() or 0))
    except Exception:                                          # noqa: BLE001
        return 0.0


def _extract_pool(video: Path, out_dir: Path, pool: int, dur: float) -> list[dict]:
    """一次解码均匀抽 pool 帧：返回 ``[{path, tSec}]``（按时间先后）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("p_*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    start = dur * EDGE_SKIP
    span = max(dur * (1 - 2 * EDGE_SKIP), 1.0)
    step = max(span / max(pool, 1), 0.4)
    cmd = [str(ws.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", str(video),
           "-vf", f"fps=1/{step:.4f},scale={FRAME_W}:-2",
           "-q:v", "3", str(out_dir / "p_%03d.jpg")]
    proc.run(cmd, timeout=180)
    files = sorted(out_dir.glob("p_*.jpg"))
    return [{"path": f, "tSec": round(start + i * step, 2)} for i, f in enumerate(files)]


def _ocr_engine():
    """复用 ocr_verify 的引擎（唯一出处），拿不到返回 None。"""
    try:
        from app.slicer import ocr_verify
        return ocr_verify._engine()                            # noqa: SLF001 — 同项目复用
    except Exception:                                          # noqa: BLE001
        return None


def _ocr_dirt(eng, img: Path) -> dict:
    """OCR 分区脏度：**字幕带**（y>SUBTITLE_BAND_PCT，我们烧的字）与其他区域分开统计。

    返回 ``{blocks, subBlocks, area, subArea, texts}``；识别失败按全 0（不算脏）。
    OCR 前把帧缩到 ``OCR_W``（640）—— 实测对中文识别没差别，速度快一倍多。
    """
    out = {"blocks": 0, "subBlocks": 0, "area": 0.0, "subArea": 0.0, "texts": []}
    try:
        from PIL import Image
        import numpy as np
        with Image.open(img) as im:
            w0, h0 = im.size
            h = max(1, round(h0 * OCR_W / max(w0, 1)))
            arr = np.array(im.convert("RGB").resize((OCR_W, h), Image.LANCZOS))
        res, _ = eng(arr)
    except Exception:                                          # noqa: BLE001
        return out
    if not res:
        return out
    total = float(OCR_W * h)
    for row in res:
        if not (isinstance(row, (list, tuple)) and len(row) >= 2):
            continue
        try:
            box = row[0]
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except Exception:                                      # noqa: BLE001
            continue
        a = max(max(xs) - min(xs), 0) * max(max(ys) - min(ys), 0)
        yc = ((min(ys) + max(ys)) / 2) / max(h, 1) * 100.0
        out["blocks"] += 1
        out["area"] += a
        if yc >= SUBTITLE_BAND_PCT:
            out["subBlocks"] += 1
            out["subArea"] += a
        txt = str(row[1] or "").strip()
        if txt:
            out["texts"].append(txt[:12])
    out["area"] = round(min(out["area"] / total, 1.0), 4)
    out["subArea"] = round(min(out["subArea"] / total, 1.0), 4)
    out["texts"] = out["texts"][:4]
    return out


def _fallback_dirt(img: Path) -> dict:
    """OCR 不可用时的兜底：字幕常驻带的边缘密度 → 归一成"疑似字幕"。

    只是启发式，调用方会把 ``degraded=True`` 如实带给界面。
    """
    out = {"blocks": 0, "subBlocks": 0, "area": 0.0, "subArea": 0.0, "texts": []}
    try:
        from PIL import Image, ImageFilter, ImageStat
        with Image.open(img) as im:
            g = im.convert("L")
            w, h = g.size
            band = g.crop((0, int(h * 0.70), w, h))
            edges = band.filter(ImageFilter.FIND_EDGES)
            mean = ImageStat.Stat(edges).mean[0]
        if mean > 11:                                  # 经验阈值：有字的底部带边缘均值明显更高
            out["blocks"] = 1
            out["subBlocks"] = 1
            out["subArea"] = round(min(mean / 40.0, 1.0) * 0.05, 4)
        out["area"] = out["subArea"]
    except Exception:                                          # noqa: BLE001
        pass
    return out


def _brightness(img: Path) -> float:
    try:
        from PIL import Image, ImageStat
        with Image.open(img) as im:
            return round(ImageStat.Stat(im.convert("L")).mean[0], 1)
    except Exception:                                          # noqa: BLE001
        return 128.0


def _clean_score(ocr: dict, luma: float) -> int:
    """干净分 0–100：**没有字幕最好**；画面原生字轻扣；过暗过曝再扣。"""
    score = 100.0
    sub = int(ocr.get("subBlocks") or 0)
    other = max(int(ocr.get("blocks") or 0) - sub, 0)
    score -= min(sub, 4) * 18.0              # 烧的字幕：一块就扣 18（最多 72）
    score -= min(other, 6) * 5.0             # 画面原生贴纸/背景字：一块 5
    score -= min(float(ocr.get("area") or 0) * 200.0, 26.0)
    if luma < LUMA_LO:
        score -= min((LUMA_LO - luma) * 2.0, 40.0)
    elif luma > LUMA_HI:
        score -= min((luma - LUMA_HI) * 2.0, 40.0)
    return int(max(0, min(100, round(score))))


def _thumb_data_uri(img: Path, w: int = 168) -> str:
    """左侧竖排缩略图：直接给 data URI（候选帧不进媒体流白名单，省一条通道）。"""
    import base64
    from io import BytesIO

    from PIL import Image
    try:
        with Image.open(img) as im:
            im = im.convert("RGB")
            h = max(1, round(im.height * w / max(im.width, 1)))
            im = im.resize((w, h), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, "JPEG", quality=78)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:                                          # noqa: BLE001
        return ""


def analyze(video: str | Path, *, count: int = COUNT, pool: int | None = None,
            force: bool = False) -> dict:
    """挑干净帧。返回 ``{"ok", "frames":[…], "cached", "degraded", "note"}``。

    ``frames[]``：``{name, tSec, clean, blocks, subBlocks, areaPct, subAreaPct,
    luma, texts, thumb}``（按时序）。
    失败抛 ``RuntimeError``（调用方转成接口错误文本）。
    """
    video = Path(video)
    if not video.is_file():
        raise RuntimeError(f"成片不存在：{video.name}")
    st = video.stat()
    cp = _analysis_path(video)
    if not force and cp.is_file():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            if (data.get("algo") == ALGO_VERSION
                    and data.get("mtime") == round(st.st_mtime, 3)
                    and data.get("size") == st.st_size
                    and data.get("frames")):
                out = dict(data)
                out["cached"] = True
                for f in out["frames"]:
                    p = frames_dir(video) / f["name"]
                    f["thumb"] = _thumb_data_uri(p) if p.is_file() else ""
                return out
        except (OSError, ValueError):
            pass

    dur = _duration(video)
    if dur <= 1.0:
        raise RuntimeError("读不出成片时长（ffprobe 失败）")
    # 采样密度按秒算：字幕句间空隙只有 0.3–0.6 秒，1 fps 会大片错过
    n_pool = int(pool) if pool else min(max(int(dur * POOL_PER_SEC), POOL_MIN), POOL_MAX)
    d = frames_dir(video)
    shots = _extract_pool(video, d, max(n_pool, count), dur)
    if not shots:
        raise RuntimeError("抽帧失败（ffmpeg 没产出图片）")

    eng = _ocr_engine()
    degraded = eng is None
    rows: list[dict] = []
    for s in shots:
        ocr = _ocr_dirt(eng, s["path"]) if eng is not None else _fallback_dirt(s["path"])
        luma = _brightness(s["path"])
        rows.append({"name": s["path"].name, "tSec": s["tSec"],
                     "blocks": int(ocr.get("blocks") or 0),
                     "subBlocks": int(ocr.get("subBlocks") or 0),
                     "areaPct": round(float(ocr.get("area") or 0) * 100, 2),
                     "subAreaPct": round(float(ocr.get("subArea") or 0) * 100, 2),
                     "texts": ocr.get("texts") or [],
                     "luma": luma,
                     "clean": _clean_score(ocr, luma)})

    # 贪心：**无字幕的帧永远排在前面**（用户要的就是"没有字幕和字样"），
    # 同组内按干净分降序；都不够 count 张时才用"字幕最少"的补，且与已选间隔 ≥ MIN_GAP。
    picked: list[dict] = []
    for r in sorted(rows, key=lambda x: (x["subBlocks"] > 0, -x["clean"], x["tSec"])):
        if all(abs(r["tSec"] - p["tSec"]) >= MIN_GAP for p in picked):
            picked.append(r)
        if len(picked) >= count:
            break
    picked.sort(key=lambda x: (x["subBlocks"] > 0, -x["clean"], x["tSec"]))  # 展示也按此序
    clean_cnt = sum(1 for r in picked if r["subBlocks"] == 0)

    data = {"version": 1, "algo": ALGO_VERSION, "video": str(video), "mtime": round(st.st_mtime, 3),
            "size": st.st_size, "durationSec": round(dur, 2),
            "degraded": degraded, "pool": len(rows), "frames": picked,
            # 全场有多少帧"完全没有字幕"（不是候选数，给界面说人话用）
            "cleanCount": clean_cnt,
            "cleanTotal": sum(1 for r in rows if r["subBlocks"] == 0),
            "generatedAt": round(time.time(), 1),
            "note": ("OCR 不可用，按画面边缘密度估算（结果仅供参考）" if degraded else "")}
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: v for k, v in data.items() if k != "frames"}
                                  | {"frames": picked}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, cp)
    except OSError:
        pass
    out = dict(data)
    out["cached"] = False
    for f in picked:
        f["thumb"] = _thumb_data_uri(d / f["name"])
    return out


def choose(video: str | Path, frame_name: str) -> dict:
    """选中一帧 → 覆盖成该成片的封面底图 ``<stem>_cover.jpg``。

    为什么覆盖 `_cover.jpg`：`/api/edits/cover/base` 的取图优先级是
    `_cover.jpg`（已有封面）→ `_cover_auto.jpg`（自动首帧）。写到 auto 会被
    已有的 base 遮住，看到的就是"选了没变"。
    """
    video = Path(video)
    if not video.is_file():
        raise RuntimeError(f"成片不存在：{video.name}")
    name = Path(str(frame_name or "")).name
    if not name.startswith("p_") or not name.lower().endswith(".jpg") or "/" in name or "\\" in name:
        raise RuntimeError("帧文件名不合法")
    src = frames_dir(video) / name
    if not src.is_file():
        raise RuntimeError(f"候选帧不在（请重新点「选其他帧」）：{name}")
    # v4.97：正式封面统一是 <stem>_cover.jpg（跟成片同名不同扩展名）
    from app.common.publish_cover import cover_files_for
    dst, _fallback, _kind = cover_files_for(video)
    try:
        shutil.copyfile(src, dst)
    except OSError as exc:
        raise RuntimeError(f"写入封面底图失败：{exc}") from exc
    # 记一笔，便于事后追（不参与渲染决策）
    try:
        meta = dst.with_name(dst.stem + "_pick.json")
        meta.write_text(json.dumps({"frame": name, "pickedAt": round(time.time(), 1)},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "frame": name, "cover": dst.name}
