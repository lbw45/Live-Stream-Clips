#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一素材库：一个入口看全所有资源（直播录制 / 切片候选 / 剪辑成片 / 我的上传 / 音乐）。

对应文档 §5.4「素材库」、D29 解耦、**R3 删除一律走 file_ops**。

设计要点：
1. **分类 = 目录**（``KINDS`` 表），每类一个根；列表统一返回同一套字段，前端一个表格渲染；
2. **切片候选是元数据 JSON 不是视频** —— 只有这一类会**展开**，把每个候选区间变成一行，
   行上带**四维评分的 overall**（用户明确要求的"显示评分的分数"）；
3. **删除一律走 ``app.common.file_ops``**（越界拒绝 / 目录拒绝 / 索引保护 / 逐条失败明细），
   本模块自己**不写任何 unlink**；候选这种"一个 JSON 里装多条"的，
   删除动作是**移除该条候选**（改 JSON），不是删文件（否则一次误操作清掉整批）；
4. 读取只读 slicer 的产物文件，不直连 slicer 的库（D29）。

所有纯逻辑函数都带可注入的根目录参数，冒烟测试用临时目录完全隔离（K36）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter

from app.common import file_ops, proc
from app.common.paths import SOURCE_ROOT_ATTRS
from app.common.paths import ws

router = APIRouter()

# ---------------------------------------------------------------- 分类表

# ⚠️ v3.34：扩展名定义**搬到 `app.common.media_types`**（唯一出处，K44）——
# 原来这里与媒体流端点 `cut.STREAM_ROOTS` 各写一份，结果「我的上传」列得出 mp3、
# 流端点却不放 mp3 → 试听静默没声音。下面只是转出，别再在本文件重新定义。
from app.common.media_types import (AUDIO_EXT, IMAGE_EXT,  # noqa: E402
                                    IMPORT_EXT, VIDEO_EXT, is_audio)

CAND_SUFFIX = ".candidates.json"

KINDS: dict[str, dict] = {
    "live": {"label": "直播录制", "ext": VIDEO_EXT, "root": "live_source_dir",
             "desc": "原始直播录像（合并后的小时文件）"},
    "candidates": {"label": "切片候选", "ext": {".json"}, "root": "candidates_dir",
                   "desc": "AI 选出来的候选片段（元数据，展开为行，带评分）"},
    "edits": {"label": "剪辑成片", "ext": VIDEO_EXT, "root": "editor_output_dir",
              "desc": "剪辑渲染产出的成片"},
    "uploads": {"label": "我的上传", "ext": IMPORT_EXT, "root": "uploads_dir",
                "desc": "我自己导入的素材，唯一可直接写入的分类"},
    "music": {"label": "音乐素材", "ext": AUDIO_EXT, "root": "music_dir",
              "desc": "BGM 素材（也包含「我的上传」里的音频）"},
}


#: v3.77：**不是素材库分类、但有媒体根**的 kind（只给抽帧端点用）。
#: 用于把某些「独立产线」的产物目录接进解析能力，而不往 KINDS 里加条目
#: （素材库界面不该凭空多出一个页签）。本版没有这种产线，留空表保持接口形状。
EXTRA_THUMB_ROOTS: dict[str, str] = {}


def kind_root(kind: str, *, roots: dict | None = None) -> Path:
    """某分类的根目录（可注入覆盖，供测试隔离）。"""
    if roots and kind in roots:
        return Path(roots[kind])
    spec = KINDS.get(kind)
    if spec is None:
        raise KeyError(kind)
    return getattr(ws, spec["root"])


# ---------------------------------------------------------------- v3.46 跨目录收集
# 用户 2026-09-24：「音乐素材区没显示我已经上传的音乐素材，虽然我的上传区会有，
# 但应该在音乐素材区也会显示」。
#
# 做法：某些分类除了自己的根，还要**额外扫别的目录**（目前只有 music → 我的上传里的音频）。
# 额外根来的条目，rel 前面加一个源前缀 ``@<分类>/``，读文件时按前缀挑回对应根。
# ⚠️ 前缀**只在后端内部流转**：前端拿到的 rel 照原样回传即可（不用理解它），
#    另外每条都下发 ``streamRel``（形如 ``uploads/xxx.mp3``）供媒体流直接用 ——
#    谁最清楚文件放哪，就由谁给出可播路径（K44：别让前端自己拼）。
SRC_PREFIX = "@"


def extra_roots(kind: str, *, roots: dict | None = None) -> list[tuple[str, Path]]:
    """某分类要**额外**扫哪些根 —— ``[(来源分类名, 根目录)]``。

    ⚠️ 允许的扩展名用**本分类自己的**（music → 音频），**不要**照搬来源分类的：
    「我的上传」允许视频/音频/图片，照搬它会让**视频也混进「音乐素材」** ——
    这个坑是冒烟断言当场抓到的（第一版就是这么写的）。
    """
    if kind != "music" or "uploads" not in KINDS:
        return []
    try:
        return [("uploads", kind_root("uploads", roots=roots))]
    except KeyError:
        return []


def split_source(kind: str, rel: str) -> tuple[str, str]:
    """把可能带源前缀的 rel 拆成 ``(实际分类, 该分类内的相对路径)``。"""
    rel = str(rel or "")
    if rel.startswith(SRC_PREFIX):
        head, _, rest = rel[1:].partition("/")
        if head in KINDS and rest:
            return head, rest
    return kind, rel


def item_root(kind: str, rel: str, *, roots: dict | None = None) -> tuple[Path, str]:
    """按（可能带源前缀的）rel 定位**真实分类的根**与干净的相对路径。"""
    real_kind, clean = split_source(kind, rel)
    return kind_root(real_kind, roots=roots), clean


def _fmt_range(start_ms, end_ms) -> str:
    try:
        s = int(start_ms) / 1000.0
        e = int(end_ms) / 1000.0
    except (TypeError, ValueError):
        return "—"
    return f"{int(s // 60):02d}:{int(s % 60):02d}–{int(e // 60):02d}:{int(e % 60):02d}"


def _scan_media(kind: str, *, roots: dict | None = None) -> list[dict]:
    """普通媒体分类：一个文件一行。

    v3.46：支持**跨目录收集**（见上面 SRC_PREFIX 的说明）—— 主根来的 ``rel`` 原样，
    额外根来的加 ``@<来源分类>/`` 前缀；每条都带 ``streamRel`` 供媒体流直接使用。
    """
    exts = KINDS[kind]["ext"]
    items: list[dict] = []
    # (来源分类, 根目录, 是否要加源前缀)；**扩展名一律用本分类的**（见 extra_roots 的说明）
    sources = [(kind, kind_root(kind, roots=roots), False)]
    sources += [(k, r, True) for (k, r) in extra_roots(kind, roots=roots)]
    seen: set[str] = set()
    for src_kind, root, prefixed in sources:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in exts:
                continue
            try:
                clean = str(p.relative_to(root))
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            except (OSError, ValueError):
                continue
            rel = f"{SRC_PREFIX}{src_kind}/{clean}" if prefixed else clean
            if rel in seen:                  # 同一文件被两条路径收到 → 只留一条
                continue
            seen.add(rel)
            items.append({
                "kind": kind, "name": p.name, "rel": rel, "stem": p.stem,
                "sizeBytes": size, "mtime": mtime,
                "ext": p.suffix.lower().lstrip("."),
                "isMeta": False,
                # v3.34：逐条告诉前端「这是音频」—— 按钮写「试听」还是「预览」、
                # 缩略图占位写「♪ 音频」还是「无画面」都看它。**别在前端再抄一份扩展名表**
                # （K44：同一条事实两处写必然漂移；用户的 BGM 常在「我的上传」里，不在「音乐素材」）。
                "isAudio": is_audio(p.name),
                # v3.46：媒体流地址（`<分类>/<相对路径>`，前缀与 slicer 的 STREAM_ROOTS 同名）。
                # 前端直接用这个 —— 跨目录来的条目前端根本拼不出正确前缀，由最清楚
                # 文件位置的后端给出（K44），前端不必理解 `@` 前缀。
                "streamRel": f"{src_kind}/{clean}",
                "srcKind": src_kind,
            })
    items.sort(key=lambda d: d.get("mtime", 0.0), reverse=True)
    return items


def _scan_candidates(*, roots: dict | None = None) -> list[dict]:
    """切片候选：**展开**。每个候选产物 JSON 里的每条候选 → 一行，带评分、区间、标题。

    ⚠️ v3.60（）：``stem`` 是**候选产物的身份**（``xxx``，不含
    ``.candidates.json``），与切片页 `/api/candidates` 的 ``stem`` 同口径 ——
    前端要拿它去调 `/api/cut/plan?candidate=…`（素材库里给候选补的「预览」入口）。
    原来这里下发的是 `jf.stem` = ``xxx.candidates``，拼出 ``xxx.candidates.candidates.json``
    必然找不到文件。此前没人用这个字段，所以这个坑一直没暴露。
    """
    root = kind_root("candidates", roots=roots)
    items: list[dict] = []
    if not root.is_dir():
        return items
    for jf in sorted(root.glob(f"*{CAND_SUFFIX}")):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            mtime = jf.stat().st_mtime
            size = jf.stat().st_size
        except (OSError, ValueError):
            continue
        source = str(data.get("videoName") or jf.name[: -len(CAND_SUFFIX)])
        try:
            n = len(data.get("candidates") or [])
        except TypeError:
            n = 0
        if not n:
            # 空产物也要能看到（否则用户以为切片没跑）
            items.append({"kind": "candidates", "name": jf.name, "rel": jf.name,
                          "stem": jf.name[: -len(CAND_SUFFIX)], "sizeBytes": size, "mtime": mtime,
                          "ext": "json", "isMeta": True, "sourceName": source,
                          "candidateCount": 0, "score": None, "title": "",
                          "rangeText": "—", "idx": None, "note": "（无候选）"})
            continue
        for c in (data.get("candidates") or []):
            scores = c.get("scores") or {}
            hook = c.get("hook") or {}
            try:
                score = round(float(scores.get("overall") or 0.0), 1)
            except (TypeError, ValueError):
                score = None
            items.append({
                "kind": "candidates",
                # 行的身份 = 文件 + 候选编号：rel 里带上 idx，删除时据此定位到具体某一条
                "name": f"{source} #{c.get('idx')}",
                "rel": f"{jf.name}::{c.get('idx')}",
                "stem": jf.name[: -len(CAND_SUFFIX)], "file": jf.name, "idx": c.get("idx"),
                "sizeBytes": size, "mtime": mtime, "ext": "json", "isMeta": True,
                "sourceName": source, "candidateCount": n,
                "score": score,
                "title": str(c.get("title") or "").strip(),
                "category": str(c.get("category") or ""),
                # 候选区间在切片引擎里是 **float 秒**（start/end），钩子是毫秒
                "rangeText": _fmt_range(int(float(c.get("start") or 0) * 1000),
                                        int(float(c.get("end") or 0) * 1000)),
                "hookText": _fmt_range(hook.get("startMs"), hook.get("endMs")),
                "planner": str(data.get("planner") or ""),
            })
    # 评分降序 → 产出时间降序（先看最值得发的那条）
    items.sort(key=lambda d: (-(d.get("score") or 0.0), -(d.get("mtime") or 0.0)))
    return items


def list_items(kind: str, *, roots: dict | None = None) -> dict:
    """列出某分类的全部条目。返回 ``{kind, label, root, count, items}``。"""
    if kind not in KINDS:
        raise KeyError(kind)
    root = kind_root(kind, roots=roots)
    items = _scan_candidates(roots=roots) if kind == "candidates" else _scan_media(kind, roots=roots)
    # v3.46：分类可能有**额外目录**（音乐素材 = music + 我的上传里的音频）。
    # `exists` 必须把额外目录一起算上 —— 否则会对着一个真有素材的列表说「目录还不存在」。
    extra = [r for (_k, r) in extra_roots(kind, roots=roots)]
    return {
        "kind": kind, "label": KINDS[kind]["label"], "desc": KINDS[kind]["desc"],
        "root": str(root), "exists": root.is_dir() or any(r.is_dir() for r in extra),
        "extraRoots": [str(r) for r in extra],
        "count": len(items), "items": items,
    }


def delete_items(kind: str, rels, *, roots: dict | None = None, log=None) -> tuple[int, dict]:
    """删除素材。**文件类**走 file_ops；**候选类**是移除 JSON 里的某一条。"""
    if kind not in KINDS:
        return 400, {"error": f"未知分类：{kind}"}
    rels = list(rels or [])
    if not rels:
        return 400, {"error": "没有指定要删除的素材"}

    if kind == "candidates":
        return _delete_candidates(rels, roots=roots, log=log)

    # v3.46：跨目录收集来的条目（rel 带 `@<分类>/` 前缀）要回**它自己的根**去删，
    # 所以先按真实分类分组，每组一次 file_ops 调用 —— 越界检查、受保护名单、
    # 逐条明细这些规则**全在 file_ops 里**，这里不重复实现（K44）。
    grouped: dict[str, list[str]] = {}
    for rel in rels:
        real_kind, clean = split_source(kind, str(rel))
        grouped.setdefault(real_kind, []).append(clean)

    deleted: list[str] = []
    failed: list[dict] = []
    freed = 0
    for real_kind, group in grouped.items():
        code, res = file_ops.delete_files_in(kind_root(real_kind, roots=roots),
                                             group, log=log)
        if code != 200:
            return code, res
        deleted += list(res.get("deleted") or [])
        failed += list(res.get("failed") or [])
        freed += int(res.get("freedBytes") or 0)
    return 200, {
        "ok": not failed, "deleted": deleted, "deletedCount": len(deleted),
        "failed": failed, "freedBytes": freed, "freedMb": round(freed / 1024 ** 2, 2),
    }


def _delete_candidates(rels, *, roots: dict | None = None, log=None) -> tuple[int, dict]:
    """移除候选**：``rel`` 形如 ``xxx.candidates.json::3``。

    为什么不是删文件：一个 JSON 装着一整段直播的候选，删文件等于一次清掉全部，
    与界面上一行 = 一条候选的直觉相反。这里按 idx 精确剔除并重排编号。
    """
    root = kind_root("candidates", roots=roots)
    deleted: list[str] = []
    failed: list[dict] = []
    freed = 0

    # 按文件分组，一个文件只读写一次
    grouped: dict[str, list[int]] = {}
    for rel in rels:
        rel = str(rel)
        if "::" not in rel:
            failed.append({"rel": rel, "error": "候选标识格式不对（应含 ::编号）"})
            continue
        file_name, idx_s = rel.split("::", 1)
        if not file_ops.is_safe_stem(file_name):
            failed.append({"rel": rel, "error": "文件名不合法，已拒绝"})
            continue
        try:
            idx = int(idx_s)
        except ValueError:
            failed.append({"rel": rel, "error": "候选编号不是整数"})
            continue
        grouped.setdefault(file_name, []).append(idx)

    for file_name, idxs in grouped.items():
        target = (root / file_name).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            failed.append({"rel": file_name, "error": "路径越界，已拒绝"})
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except OSError:
            failed.append({"rel": file_name, "error": "候选文件不存在"})
            continue
        except ValueError:
            failed.append({"rel": file_name, "error": "候选文件不是合法 JSON"})
            continue
        cands = data.get("candidates") or []
        keep = []
        removed = 0
        for c in cands:
            try:
                hit = int(c.get("idx")) in idxs
            except (TypeError, ValueError):
                hit = False
            if hit:
                removed += 1
            else:
                keep.append(c)
        if not removed:
            for i in idxs:
                failed.append({"rel": f"{file_name}::{i}", "error": "该候选不存在"})
            continue
        for i, c in enumerate(keep):       # 重排编号，保持 idx 连续
            c["idx"] = i + 1
            c["publishPriority"] = i + 1
        data["candidates"] = keep
        data["candidateCount"] = len(keep)
        try:
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except OSError as exc:
            failed.append({"rel": file_name, "error": f"写回失败：{exc}"})
            continue
        for i in idxs:
            deleted.append(f"{file_name}::{i}")
        # 候选是元数据，删除释放的字节按占比估算（不做真实差）
        freed += max(1, target.stat().st_size // max(1, len(cands))) * removed
        if log:
            log(f"移除候选 {file_name} 第 {sorted(idxs)} 条 → 剩 {len(keep)} 条")

    payload = {
        "ok": not failed, "deleted": deleted, "deletedCount": len(deleted),
        "failed": failed, "freedBytes": freed, "freedMb": round(freed / 1024 ** 2, 2),
    }
    return 200, payload


def import_files(paths, *, kind: str = "uploads", roots: dict | None = None,
                 log=None) -> tuple[int, dict]:
    """把本机文件**复制**进素材库（默认「我的上传」）。

    安全约束：只收允许的扩展名；目标名字做净化（只留文件名，去路径）；
    同名自动加后缀不覆盖已有素材；目标必须落在分类根目录内。
    """
    if kind not in KINDS:
        return 400, {"error": f"未知分类：{kind}"}
    root = kind_root(kind, roots=roots).resolve()
    imported: list[str] = []
    failed: list[dict] = []

    root.mkdir(parents=True, exist_ok=True)

    for raw in (paths or []):
        src = Path(str(raw))
        try:
            if not src.is_file():
                failed.append({"rel": str(src), "error": "不是文件或不存在"})
                continue
            ext = src.suffix.lower()
            if ext not in KINDS[kind]["ext"]:
                failed.append({"rel": src.name, "error": f"不支持的类型 {ext}"})
                continue
            name = Path(src.name).name          # 只留文件名，去掉任何路径成分
            if name != src.name or name in (".", ".."):
                failed.append({"rel": name, "error": "文件名不合法，已拒绝"})
                continue
            dst_root = root.resolve()
            dst = dst_root / name
            if src.resolve() == dst:
                failed.append({"rel": name, "error": "该文件已在素材库里"});  continue
            n = 1
            while dst.exists():                 # 同名加后缀，绝不覆盖
                dst = dst_root / f"{Path(name).stem}-{n}{src.suffix}"
                n += 1
            dst.relative_to(dst_root)           # 兜底断言：目标必须落在根目录内
            shutil.copy2(src, dst)
            imported.append(dst.name)
        except ValueError:
            failed.append({"rel": str(src), "error": "目标路径越界，已拒绝"})
        except OSError as exc:
            failed.append({"rel": str(src), "error": f"复制失败：{exc}"})

    if imported and log:
        log(f"导入素材 {len(imported)} 个到「{KINDS[kind]['label']}」")
    return 200, {"ok": not failed, "imported": imported, "importedCount": len(imported),
                 "failed": failed}


# ---------------------------------------------------------------- 端点

@router.get("/items")
def items(kind: str = "live") -> dict:
    try:
        return {"ok": True, **list_items(kind)}
    except KeyError:
        # 约定：所有"业务性拒绝"都带 ok=False，前端无需猜 Shape（少一处判断就少一处漏）
        return {"ok": False, "error": f"未知分类：{kind}", "kinds": sorted(KINDS)}


@router.get("/kinds")
def kinds() -> dict:
    out = []
    for k, spec in KINDS.items():
        root = kind_root(k)
        out.append({"kind": k, "label": spec["label"], "desc": spec["desc"],
                    "root": str(root), "exists": root.is_dir(),
                    "writable": k == "uploads"})
    return {"kinds": out}


@router.post("/delete")
def delete(payload: dict) -> dict:
    status, body = delete_items(
        str(payload.get("kind") or ""), payload.get("rels") or payload.get("names") or [],
        log=lambda m: print(f"[editor] {m}", flush=True))
    if status != 200 and "error" in body:
        body["ok"] = False
    return body


@router.post("/import")
def _import(payload: dict) -> dict:
    """把本机已有文件复制进素材库（前端先用文件选择框挑文件，路径由桥传过来）。"""
    status, body = import_files(
        payload.get("paths") or [], kind=str(payload.get("kind") or "uploads"),
        log=lambda m: print(f"[editor] {m}", flush=True))
    return body


def guess_mime(path: Path) -> str:
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


# ---------------------------------------------------------------- 缩略图
# 用户要求「素材库里每个素材要有展示」——列表光有文件名看不出是哪一段，
# 所以给每行抽一帧缩略图。要点：
# 走 ffmpeg 抽帧 → 落到磁盘缓存（key = 身份+时间点+mtime），重复看不重复抽；
# 前端是 file:// 页面，拿不到本地文件 → 以 data URI 回传（不额外开 HTTP 端口）；
# 候选行没有自己的视频，用**它的源录像 + 候选起点**抽帧（这才看得出是哪一段）；
# 抽帧失败一律回 ok=False + 原因，前端显示占位符，绝不整页崩。

THUMB_DIR_NAME = "library_thumbs"
THUMB_WIDTH = 360          # v3.177：240 → 360（卡片约 178 CSS 像素，视网膜屏要 2 倍）
THUMB_MAX_FILES = 400          # 超过就删掉最旧的一半，防止缓存无限涨
THUMB_TIMEOUT = 25


def thumb_cache_dir() -> Path:
    return ws.tmp_dir / THUMB_DIR_NAME


def resolve_media(kind: str, rel: str, *, roots: dict | None = None) -> tuple[Path | None, float]:
    """条目 → ``(真实媒体文件, 取帧秒数)``。取不到返回 ``(None, 0.0)``。

    候选（``kind == candidates``）没有自己的视频文件，取它的**源录像** + 候选起点；
    图片类直接用图片本身当缩略图（不抽帧）。
    """
    rel = str(rel or "")
    if not rel:
        return None, 0.0
    if kind == "candidates":
        file_name, _, idx_s = rel.partition("::")
        root = kind_root("candidates", roots=roots)
        jf = root / file_name
        if not file_ops.is_safe_stem(file_name) or not jf.is_file():
            return None, 0.0
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            idx = int(idx_s)
        except (OSError, ValueError, TypeError):
            return None, 0.0
        cand = None
        for c in (data.get("candidates") or []):
            try:
                if int(c.get("idx")) == idx:
                    cand = c
                    break
            except (TypeError, ValueError):
                continue
        if cand is None:
            return None, 0.0
        try:
            start = float(cand.get("start") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        src = _find_source_video(str(data.get("videoName") or ""), roots=roots)
        return src, max(0.0, start)
    # v3.46：rel 可能带源前缀（跨目录收集来的）→ 先定位到**真实分类的根**
    root, clean = item_root(kind, rel, roots=roots)
    p = (root / clean).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None, 0.0
    if not p.is_file():
        return None, 0.0
    if p.suffix.lower() in IMAGE_EXT:
        return p, 0.0
    return p, 0.0


def source_like_kinds() -> list[str]:
    """**既是素材库分类、又是「源根」的那些分类**（v3.59：`live` 与 `network`）。

    判据反过来取：分类表里 `root` 指向 `paths.SOURCE_ROOT_ATTRS` 里那些属性的，
    就是源根分类。这样**新增源根时不用改这里**（K44：别再维护第二张名单）。
    """
    attrs = {attr for _name, attr in SOURCE_ROOT_ATTRS}
    return [k for k, spec in KINDS.items() if spec.get("root") in attrs]


def _find_source_video(video_name: str, *, roots: dict | None = None) -> Path | None:
    """按候选产物里的 ``videoName`` 找回源录像（可能带后缀也可能不带）。

    v3.59（）：源来自录制素材根（``live_raw``），


    ⚠️ **必须走 `kind_root(kind, roots=roots)`**，不能直接用 `ws.source_roots()`：
    冒烟用注入的 `roots` 做隔离（K36/K60），直连 `ws` 会绕过注入去扫**真实工作区**，
    表现就是「候选行也能出画面」那条断言红（本次真踩到，正是它抓回来的）。
    """
    name = (video_name or "").strip()
    if not name:
        return None
    bases: list[Path] = []
    for kind in source_like_kinds():
        try:
            bases.append(Path(kind_root(kind, roots=roots)))
        except KeyError:                               # 分类被删了也不该让缩略图 500
            continue
    for base in bases:
        for cand_name in (name, f"{name}.mp4", f"{Path(name).stem}.mp4"):
            p = base / cand_name
            if p.is_file():
                return p.resolve()
        stem = Path(name).stem
        if stem:
            for p in sorted(base.glob(f"{stem}.*")):
                if p.suffix.lower() in VIDEO_EXT:
                    return p.resolve()
    return None


def thumbnail(kind: str, rel: str, *, roots: dict | None = None) -> dict:
    """取（或生成）一条素材的缩略图，返回 ``{"ok": True, "dataUri": "data:image/jpeg;base64,…"}``。"""
    if kind not in KINDS and kind not in EXTRA_THUMB_ROOTS:
        return {"ok": False, "error": f"未知分类：{kind}"}
    roots = dict(roots or {})
    if kind in EXTRA_THUMB_ROOTS and kind not in roots:
        # 借 kind_root 的注入通道把这个额外根挂上去（见 EXTRA_THUMB_ROOTS 的说明）
        roots[kind] = getattr(ws, EXTRA_THUMB_ROOTS[kind])
    src, seek = resolve_media(kind, rel, roots=roots)
    if src is None:
        return {"ok": False, "error": "找不到对应的媒体文件（源录像可能已被删除）"}
    try:
        st = src.stat()
    except OSError as exc:
        return {"ok": False, "error": f"读文件失败：{exc}"}

    cache = thumb_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    # v3.177：签名**带上抽帧宽度** —— 不带的话改了 THUMB_WIDTH 旧缓存照样命中，
    # 界面上一点变化都看不出来（K112 同源：状态要从真实条件算，别拿"以为的"当判据）
    key = hashlib.sha1(
        f"{kind}|{rel}|{src}|{seek:.2f}|{int(st.st_mtime)}|{st.st_size}|w{THUMB_WIDTH}".encode(
            "utf-8")).hexdigest()[:20]
    out = cache / f"{key}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return {"ok": True, "dataUri": _as_data_uri(out), "cached": True}

    if src.suffix.lower() in IMAGE_EXT:
        # 图片素材：直接缩成缩略图（ffmpeg 也能干，省一条分支）
        cmd = [str(ws.ffmpeg), "-y", "-loglevel", "error", "-i", str(src),
               "-vf", f"scale={THUMB_WIDTH}:-2", "-frames:v", "1", str(out)]
    else:
        # -ss 放在 -i 前是**关键帧快速定位**，放在后是精确但慢；
        # 缩略图不需要帧级精确，取前者（大文件秒出）
        cmd = [str(ws.ffmpeg), "-y", "-loglevel", "error", "-ss", f"{seek:.2f}",
               "-i", str(src), "-frames:v", "1", "-vf", f"scale={THUMB_WIDTH}:-2",
               "-q:v", "6", str(out)]
    try:
        r = proc.run(cmd, capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=THUMB_TIMEOUT)
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "error": f"抽帧失败：{type(exc).__name__}: {exc}"}
    if not out.is_file() or out.stat().st_size == 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        # 音频素材没有画面 —— 这不是错误，前端会显示音频占位符
        return {"ok": False, "error": f"抽不出画面：{tail[0]}"}
    _trim_thumb_cache(cache)
    return {"ok": True, "dataUri": _as_data_uri(out), "cached": False}


def _as_data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _trim_thumb_cache(cache: Path) -> None:
    """缓存上限保护：超过阈值就删掉最旧的一半（**只动自己这个缓存目录**）。"""
    try:
        files = list(cache.glob("*.jpg"))
    except OSError:
        return
    if len(files) <= THUMB_MAX_FILES:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files[: len(files) // 2]:
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 端点

@router.get("/thumb")
def thumb(kind: str = "live", rel: str = "") -> dict:
    """一条素材的缩略图（data URI）。失败也回 200 + ok=False，前端显示占位符。"""
    return thumbnail(kind, rel)
