#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-A 待发布队列：扫描成片入队 + 队列管理端点。

对应文档 §5.2「待发布队列」、§9.2 M5 验收前半（队列不丢件）。
**这里没有任何「点发布」逻辑** —— R1 红线：软件永远不得在无人确认时点击「发布」。
「自动上传 + 自动填表 + 停住等人工」是 M5-B 的发布执行器，本模块只负责队列。

解耦（§4.1.1 D29）：本模块**只读** slicer 的产物文件 ——
``edits\\`` 成片 + 伴生 ``.edit.json``，``candidates\\`` 候选 JSON ——
不直连 slicer 的 SQLite，也不 import slicer 的服务模块。

v3.77：
  · **自动入队**：``jobs()`` 里按目录签名判断成片目录变没变，变了就自动扫一遍 ——
    原先只有点「扫描成片」才入队，用户看不到刚出好的片子。
  · **来源单一**：``source_kind`` = ``edit``（剪辑成片）。媒体流与缩略图的路径由
    **后端算好**下发（``streamRel`` / ``thumbKind``+``thumbRel``），前端不拼路径（K44）。
  · **按幂等键真删**：``POST /publish/forget``。

纯逻辑函数都带 ``db`` / ``edits_dir`` / ``candidates_dir`` 可注入参数：
端点层用默认（真实 db 与目录），冒烟测试传临时 db + 临时目录，完全隔离、
不碰真实数据（K36）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from app.common import proc
from app.common import publish_cover as pubcover
from app.common.paths import ws
from app.editor.backend import database, platforms
from app.editor.backend import publisher as pub

router = APIRouter()

# 队列状态（§6.2 状态机）：queued → running → waiting_user → completed/failed
# 「丢弃」是队列业务动作，用独立状态 discarded 标记（软删，保留审计、不破坏幂等键）。
STATUS_QUEUED = "queued"
STATUS_DISCARDED = "discarded"

CAND_SUFFIX = ".candidates.json"

# ---------------------------------------------------------------- v3.77 来源与自动扫描
#: 队列条目的来源。目前只有「剪辑渲染出来的成片」一种，
#: 媒体流前缀固定为 ``edits/``（见 `cut.STREAM_ROOTS`）。
SOURCE_EDIT = "edit"
#: 自动扫描的签名缓存（进程内，**按平台各一份**）。缺键 = 该平台还没扫过，第一次一定扫。
#: v4.17：不能四平台共用一个签名 —— 抖音扫过之后签名就固定了，切到快手会一直不扫。
_auto_scan_sig: dict[str, tuple] = {}

# ---------------------------------------------------------------- 发布元信息限制（唯一出处）
# v3.49：标题 / 标签的长度规则**只写在这里**。前端从 ``/publish/jobs`` 的 ``limits``
# 字段读，不在 index.html 里再抄一份（K44：两处写必然漂移，漂移=用户填了才被拒）。
# 输入校验的实测值：标题 30 字、单个标签 20 字、最多 5 个。
# ⚠️ 别和 ``renderer.FILM_TITLE_MAX``（60，管的是**成片文件名**）搞混 —— 两回事。
# v4.17（）：数值本身搬到 `platforms.PLATFORMS`（四平台各一套），
# 这三个常量保留为**抖音**的别名 —— 老代码/老断言按它们取值，不留第二份数字。
PUBLISH_TITLE_MAX = platforms.get("douyin").title_max
PUBLISH_TAG_MAX = platforms.get("douyin").tag_max
PUBLISH_TAG_COUNT_MAX = platforms.get("douyin").tag_count_max


def publish_limits(platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    """下发某平台给界面用的限制值（界面据此显示 0/30、0/5 与禁用态）。"""
    return platforms.limits(platform)


def _has_control(s: str) -> bool:
    """是否含换行 / 制表符 / 其它控制字符（发布页填进去会坏事，参考实现同样拒绝）。"""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in s)


def clean_tags(tags) -> list[str]:
    """去空白与空项，**保留顺序**（去重是校验的事，见 validate_tags）。

    为什么不在这里顺手去重：参考实现对重复是**明确报错**而不是静默吞掉；
    静默去重会让用户以为"加进去了"，实际被吃掉一个（K69 同族：别替用户猜）。
    """
    out: list[str] = []
    for t in (tags or []):
        s = str(t).strip()
        if s:
            out.append(s)
    return out


def validate_title(title: str, platform: str = platforms.DEFAULT_PLATFORM) -> str | None:
    """标题校验；合法返回 None，不合法返回**给人看的中文原因**（上限按平台取）。"""
    lim = publish_limits(platform)
    tmax = lim["titleMax"]
    if not title or not title.strip():
        return "标题不能为空"
    if _has_control(title):
        return "标题不能包含换行或控制字符"
    if len(title.strip()) > tmax:
        return f"标题最多 {tmax} 个字（当前 {len(title.strip())} 个）"
    return None


def validate_tags(tags: list[str], platform: str = platforms.DEFAULT_PLATFORM) -> str | None:
    """标签校验（顺序照参考实现：空 → 长度 → 控制字符 → 重复 → 个数；上限按平台取）。"""
    lim = publish_limits(platform)
    tag_max, count_max = lim["tagMax"], lim["tagCountMax"]
    if not tags:
        return "标签不能为空"
    for t in tags:
        if len(t) > tag_max:
            return f"单个标签最多 {tag_max} 个字：「{t}」有 {len(t)} 个"
        if _has_control(t):
            return f"标签「{t}」不能包含换行或控制字符"
    seen: set[str] = set()
    for t in tags:
        low = t.lower()
        if low in seen:
            return f"标签不能重复：「{t}」已添加过"
        seen.add(low)
    if len(tags) > count_max:
        return f"最多添加 {count_max} 个标签（当前 {len(tags)} 个）"
    return None


# ---------------------------------------------------------------- 纯逻辑（可单测）

def _read_edit_meta(mp4: Path) -> dict:
    side = mp4.with_suffix(".edit.json")
    if side.is_file():
        try:
            return json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def candidate_meta(candidate_name: str, idx, *,
                   candidates_dir: Path | None = None) -> dict:
    """从候选产物取发布元数据（title / douyinTags / overall / category）。

    ``candidate_name`` 可能带或不带 ``.candidates.json`` 后缀，都兼容。
    找不到对应片段时返回空 dict。
    """
    name = candidate_name or ""
    if name.endswith(CAND_SUFFIX):
        name = name[: -len(CAND_SUFFIX)]
    if not name:
        return {}
    root = candidates_dir or ws.candidates_dir
    cand_file = root / f"{name}{CAND_SUFFIX}"
    if not cand_file.is_file():
        return {}
    try:
        data = json.loads(cand_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    try:
        want = int(idx)
    except (TypeError, ValueError):
        want = -1
    for c in (data.get("candidates") or []):
        try:
            if int(c.get("idx") or -1) != want:
                continue
        except (TypeError, ValueError):
            continue
        scores = c.get("scores") or {}
        return {
            "title": str(c.get("title") or "").strip(),
            "tags": [str(t).strip() for t in (c.get("douyinTags") or []) if str(t).strip()],
            "score": float(scores.get("overall") or 0.0),
            "category": str(c.get("category") or ""),
            "priority": int(c.get("publishPriority") or 0),
        }
    return {}


def _edit_files(edits_dir: Path) -> list[tuple[Path, dict]]:
    """某成片目录里的成片 + 伴生元数据（mtime 降序）。"""
    out: list[tuple[Path, dict]] = []
    if edits_dir.is_dir():
        for mp4 in sorted(edits_dir.glob("*.mp4")):
            try:
                stat = mp4.stat()
            except OSError:
                continue
            meta = _read_edit_meta(mp4)
            meta["_size"] = stat.st_size
            meta["_mtime"] = stat.st_mtime
            out.append((mp4, meta))
    out.sort(key=lambda t: t[1].get("_mtime", 0.0), reverse=True)
    return out


def _probe_duration(path: Path) -> float:
    """读一个媒体文件的容器时长（秒）。失败给 0.0，绝不抛。

    ⚠️ 这里是**编辑器进程内**的轻量实现：切片那边有一份更全的
    （``app.slicer.merge.probe_duration``），但那是**另一个服务**的模块 —— 本模块开头
    写着「不 import slicer 的服务模块」，跨服务 import 会把两个服务的边界搅在一起。
    两边都只是让 ffprobe 读同一条流的容器时长，口径不会分叉。
    """
    try:
        r = proc.run([str(ws.ffprobe), "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", str(path)],
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
        return max(0.0, float((r.stdout or "").strip()))
    except (OSError, ValueError, TypeError):
        return 0.0


def _insert_job(conn, *, key: str, title: str, tags: list[str], score: float,
                path: Path, size: int, duration: float, source: str,
                platform: str = platforms.DEFAULT_PLATFORM) -> None:
    """插一条队列记录（两种来源 × 四个平台共用一份 SQL —— 别写两遍，K44）。"""
    now = database.utcnow()
    plat = platforms.normalize(platform)
    conn.execute(
        """
        INSERT INTO publish_jobs (
            client_request_id, platform, status, phase, publish_mode,
            title, tags_json, file_name, file_path, file_size_bytes,
            publish_score, duration_sec, created_at, updated_at,
            original_title, original_tags_json, source_kind
        ) VALUES (?, ?, ?, 'preflight', 'manual',
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (platforms.queue_key(plat, key), plat, STATUS_QUEUED, title,
         json.dumps(tags, ensure_ascii=False),
         path.name if source == SOURCE_EDIT else f"{path.parent.name}/{path.name}",
         str(path), size, score, duration, now, now,
         title, json.dumps(tags, ensure_ascii=False), source),
    )


def scan_edits_to_queue(*, edits_dir: Path | None = None,
                        candidates_dir: Path | None = None,
                        lyric_dir: Path | None = None,
                        db: Path | None = None,
                        platform: str = platforms.DEFAULT_PLATFORM) -> tuple[int, int]:
    """把成片目录里还没入队的成片扫进队列（幂等）。

    幂等键 = 成片 stem（``client_request_id`` UNIQUE）。已入队的（含被丢弃过的）
    一律跳过 —— 一个成片对应且只对应一条队列记录；想重新入队由 UI 把状态改回 queued。
    返回 ``(本次新入队数, 队列内非丢弃总数)``。
    """
    edir = Path(edits_dir) if edits_dir else ws.editor_output_dir
    cdir = Path(candidates_dir) if candidates_dir else ws.candidates_dir
    plat = platforms.normalize(platform)
    added = 0
    with database.session(db) as conn:
        for mp4, meta in _edit_files(edir):
            stem = mp4.stem
            if conn.execute(
                "SELECT id FROM publish_jobs WHERE client_request_id = ?",
                (platforms.queue_key(plat, stem),),
            ).fetchone() is not None:
                continue
            cm = candidate_meta(meta.get("candidateName", ""),
                                meta.get("candidateIdx"), candidates_dir=cdir)
            # 标题兜底链：候选 title → 渲染决策 coverTitle → 成片 stem
            title = (cm.get("title") or meta.get("coverTitle") or "").strip() or stem
            _insert_job(conn, key=stem, title=title, tags=cm.get("tags") or [],
                        score=cm.get("score") or 0.0, path=mp4,
                        size=meta.get("_size", 0),
                        duration=float(meta.get("durationSec") or 0),
                        source=SOURCE_EDIT, platform=plat)
            added += 1

        total = conn.execute(
            "SELECT COUNT(*) AS n FROM publish_jobs WHERE status != ? AND platform = ?",
            (STATUS_DISCARDED, plat),
        ).fetchone()["n"]
    return added, int(total)


def platform_counts(*, db: Path | None = None) -> dict:
    """每个平台各有多少条未丢弃的队列条目（发布页头部卡片上的角标）。

    一次 GROUP BY 取全，别在界面里为四张卡片打四次接口（K82 同族：轮询别干重复的重活）。
    """
    out = {k: 0 for k in platforms.PLATFORM_ORDER}
    with database.session(db) as conn:
        # 只算**还没发出去**的（completed 是历史记录，不该算进「待发条数」——卡片写的是待发）
        rows = conn.execute(
            "SELECT platform, COUNT(*) AS n FROM publish_jobs "
            "WHERE status NOT IN (?, ?) GROUP BY platform",
            (STATUS_DISCARDED, pub.STATUS_COMPLETED)).fetchall()
    for r in rows:
        out[platforms.normalize(r["platform"])] = int(r["n"] or 0)
    return out


def _file_mtime(path: str | None) -> float:
    """成片文件的修改时间（unix 秒）；文件不在就给 0，**绝不抛**（K72：排序键不会抛）。

    v3.61（）：用户「发布页面的素材缺少时间展示」—— 队列行的「时间」取
    **成片文件自己的 mtime**，与「历史作品」页同一口径（同一个成片两边显示同一个时间；
    素材库「修改时间」、下载页「下载时间」也都是文件时间，K44：别再引入第二种口径）。
    为什么不取 `created_at`：它是**批量扫描**写进去的，同一批十几条挤在同一秒，
    显示出来没有信息量（实测 6 条同为 2026-09-24 16:34）。
    """
    if not path:
        return 0.0
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _row_to_dict(row) -> dict:
    try:
        tags = json.loads(row["tags_json"] or "[]")
    except (ValueError, TypeError):
        tags = []
    if not isinstance(tags, list):
        tags = []
    keys = row.keys()
    mt = _file_mtime(row["file_path"])
    raw_orig_title = row["original_title"] if "original_title" in keys else row["title"]
    raw_orig_tags = row["original_tags_json"] if "original_tags_json" in keys else "[]"
    try:
        orig_tags = json.loads(raw_orig_tags or "[]")
    except (ValueError, TypeError):
        orig_tags = []
    if not isinstance(orig_tags, list):
        orig_tags = []
    return {
        "id": row["id"],
        "clientRequestId": row["client_request_id"],
        "status": row["status"],
        "phase": row["phase"],
        "title": row["title"],
        "tags": tags,
        "originalTitle": raw_orig_title,
        "originalTags": orig_tags,
        "fileName": row["file_name"],
        "filePath": row["file_path"],
        "fileSizeBytes": row["file_size_bytes"],
        # v3.61：成片文件的时间（发布页「时间」列 + 「按时间排序」共用这一个字段 ——
        # 显示与排序必须同源，否则「按时间排完看列对不上」）
        "mtime": mt,
        # v4.22：文件已不在（预览/发布必然失败）—— 界面据此标红并提供「清除失效条目」。
        # 判据与 mtime 同源（同一次 stat，不重复扫盘；文件在但读不到也算失效）。
        "fileMissing": mt <= 0,
        "score": row["publish_score"],
        "durationSec": row["duration_sec"],
        "platform": platforms.normalize(row["platform"]),
        "platformName": platforms.get(row["platform"]).name,
        # v3.77：来源 + **后端算好的可播路径**（前端不拼前缀，K44）
        "sourceKind": (row["source_kind"] if "source_kind" in keys else SOURCE_EDIT),
        "streamRel": f"edits/{row['file_name']}",
        "thumbKind": "edits",
        "thumbRel": row["file_name"],
        "attemptCount": row["attempt_count"],
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def list_jobs(*, status: str | None = None, include_discarded: bool = False,
              db: Path | None = None, platform: str | None = None) -> list[dict]:
    """队列列表。默认排除已丢弃；``status`` 给定时只看该状态（配合 include_discarded）。

    排序：评分降序 → 入队时间降序（A8「待发布队列堆积要能按评分排序」）。
    ``platform`` 给定时只看该平台（发布页按顶部卡片切换）；不给＝全部（老调用方语义）。
    """
    where: list[str] = []
    params: list = []
    if not include_discarded and not status:
        where.append("status != ?")
        params.append(STATUS_DISCARDED)
    elif status:
        where.append("status = ?")
        params.append(status)
    if platform:
        where.append("platform = ?")
        params.append(platforms.normalize(platform))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (f"SELECT * FROM publish_jobs {clause} "
           "ORDER BY publish_score DESC, created_at DESC")
    with database.session(db) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_job_meta(job_id: int, *, title: str | None, tags: list[str] | None,
                    db: Path | None = None, feedback: bool = True) -> bool:
    """队列内改标题 / 标签（§5.2「队列内改标题标签」）。返回是否找到该条。

    ``feedback=False``：重置/清除这类**还原动作**不算"人改了内容"，不记评分反馈。
    """
    with database.session(db) as conn:
        if conn.execute("SELECT id FROM publish_jobs WHERE id = ?", (job_id,)).fetchone() is None:
            return False
        sets: list[str] = []
        params: list = []
        if title is not None:
            sets.append("title = ?")
            params.append(title.strip())
        if tags is not None:
            sets.append("tags_json = ?")
            params.append(json.dumps(clean_tags(tags), ensure_ascii=False))
        if sets:
            sets.append("updated_at = ?")
            params.append(database.utcnow())
            params.append(job_id)
            conn.execute(f"UPDATE publish_jobs SET {', '.join(sets)} WHERE id = ?", params)
    if sets and feedback:
        # M7：记录人工干预（评分闭环数据；写 slicer 的 feedback 文件，属文件级交互，不违反 D29）
        try:
            from app.slicer import tuning as _tuning
            _tuning.record_feedback("title_or_tags_edited", f"job#{job_id}")
        except Exception:
            pass
    return True


def discard_jobs(job_ids: list[int], *, db: Path | None = None) -> tuple[int, list[int]]:
    """批量丢弃（软删，status → discarded）。返回 (成功数, 未找到的 id)。"""
    ok = 0
    missing: list[int] = []
    with database.session(db) as conn:
        for jid in job_ids:
            cur = conn.execute(
                "UPDATE publish_jobs SET status = ?, updated_at = ? "
                "WHERE id = ? AND status != ?",
                (STATUS_DISCARDED, database.utcnow(), jid, STATUS_DISCARDED),
            )
            if cur.rowcount:
                ok += 1
            else:
                missing.append(jid)
    return ok, missing


def missing_job_ids(*, db: Path | None = None,
                    platform: str | None = None) -> list[int]:
    """文件已不在磁盘的队列条目 id（v4.22）。

    只读判断，**不动文件**：这些成片本来就被外部删了，队列里留着只会让预览/发布必然失败。
    ``platform`` 给定时只看该平台（发布页当前平台），否则全部。
    """
    where = ["file_path IS NOT NULL", "file_path != ''"]
    params: list = []
    if platform:
        where.append("platform = ?")
        params.append(platforms.normalize(platform))
    with database.session(db) as conn:
        rows = conn.execute(
            f"SELECT id, file_path FROM publish_jobs WHERE {' AND '.join(where)}",
            params).fetchall()
    return [int(r["id"]) for r in rows
            if not Path(str(r["file_path"])).is_file()]


def purge_missing(*, db: Path | None = None,
                  platform: str | None = None) -> int:
    """**删除**文件已不在的队列记录（不是标记丢弃，也不是删文件）。

    为什么真删：这些条目对应的成片已经不存在，留着没有任何可用状态；
    标记 discarded 只会让库越来越大、扫描时还一遍遍 stat。
    """
    ids = missing_job_ids(db=db, platform=platform)
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with database.session(db) as conn:
        cur = conn.execute(f"DELETE FROM publish_jobs WHERE id IN ({marks})", ids)
        n = int(cur.rowcount or 0)
    print(f"[publish] 清除失效条目 {n} 条（文件已不在）", flush=True)
    return n


def _scan_signature(edits_dir: Path | None = None,
                    lyric_dir: Path | None = None) -> tuple:
    """成片目录的**轻量签名**：``(文件数, 最新 mtime, 总字节)``。

    为什么不每次全扫：``jobs()`` 是**每 12 秒轮询一次**的接口，全扫要给每个 mp4
    读伴生 json —— 全是白干的磁盘活。签名没变就直接跳过（轮询别干重复的重活）。

    ``lyric_dir`` 参数保留但不参与计算（接口兼容，调用方无需改动）。
    """
    edir = Path(edits_dir) if edits_dir else ws.editor_output_dir
    items: list[tuple[int, float, int]] = []
    for root, pattern in ((edir, "*.mp4"),):
        n, newest, total = 0, 0.0, 0
        try:
            for p in root.glob(pattern):
                try:
                    st = p.stat()
                except OSError:
                    continue
                n += 1
                newest = max(newest, st.st_mtime)
                total += st.st_size
        except OSError:
            pass
        items.append((n, round(newest, 3), total))
    return tuple(items)


def reset_auto_scan_cache() -> None:
    """清掉自动扫描的签名缓存 —— **只给冒烟用**（否则「第一次一定会扫」没法反复验）。"""
    _auto_scan_sig.clear()


def maybe_auto_scan(*, edits_dir: Path | None = None, candidates_dir: Path | None = None,
                    lyric_dir: Path | None = None, db: Path | None = None,
                    platform: str = platforms.DEFAULT_PLATFORM) -> int:
    """成片目录变过就自动扫一遍入队，返回本次新入队条数（v3.77）。

    用户原话：「自动剪好的不会在发布页面自动呈现的，要点一次扫描成片按钮才会出现」
    —— 现在发布页每 12 秒轮询 ``jobs()``，顺带就把新出的成片收进来了；
    「扫描成片」按钮保留（显式入口 + 立即反馈，用户点它就是要看到"确实动了"）。

    v4.17：**按当前平台扫**（签名与缓存也按平台分开）。切到快手时这一轮就把它自己的
    队列补齐，而不是等用户在四个平台之间来回切才逐个出现。
    """
    plat = platforms.normalize(platform)
    sig = _scan_signature(edits_dir, lyric_dir)
    if sig == _auto_scan_sig.get(plat):
        return 0
    added, _total = scan_edits_to_queue(edits_dir=edits_dir, candidates_dir=candidates_dir,
                                       lyric_dir=lyric_dir, db=db, platform=plat)
    _auto_scan_sig[plat] = sig
    if added:
        print(f"[publish] 自动扫描（{plat}）：新入队 {added} 条", flush=True)
    return added


def forget_jobs(keys: list[str], db: Path | None = None) -> int:
    """按幂等键把一个条目从队列里**真删**（不是标记丢弃）。返回删除条数。

    v3.77：素材被删掉时，发布页里那条也必须消失 ——
    否则点开就是"播一个不存在的文件"。**只删这一条**：成片文件本身由调用方决定去留，
    这里不碰磁盘。
    """
    wanted = [str(k).strip() for k in (keys or []) if str(k).strip()]
    if not wanted:
        return 0
    # v4.17：一条成片现在最多有 4 条队列记录（每平台一条，键带平台前缀）。
    # 调用方传的是**裸键**，所以各种键形态都要一起删 ——
    # 只删裸键会留下「快手/小红书里点开是 404」的僵尸条目。
    expanded: list[str] = []
    for k in wanted:
        for spec in platforms.PLATFORMS.values():
            kk = platforms.queue_key(spec.key, k)
            if kk not in expanded:
                expanded.append(kk)
    marks = ",".join("?" for _ in expanded)
    with database.session(db) as conn:
        cur = conn.execute(
            f"DELETE FROM publish_jobs WHERE client_request_id IN ({marks})", expanded)
        return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 端点

@router.post("/scan")
def scan(platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    """扫描成片目录，把未入队的补进**当前平台**队列（幂等）。"""
    plat = platforms.normalize(platform)
    added, total = scan_edits_to_queue(platform=plat)
    # 手动扫过了 ⇒ 签名同步，别下一轮又扫一遍（按平台记）
    _auto_scan_sig[plat] = _scan_signature()
    return {"added": added, "queuedTotal": total, "platform": plat,
            "platformName": platforms.get(plat).name}


@router.post("/forget")
def forget(payload: dict) -> dict:
    """按幂等键把条目从队列里删掉。"""
    keys = payload.get("clientRequestIds")
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list) or not keys:
        return {"ok": False, "error": "clientRequestIds 不能为空"}
    deleted = forget_jobs([str(k) for k in keys])
    return {"ok": True, "deleted": deleted}


@router.get("/jobs")
def jobs(status: str | None = None, include_discarded: bool = False,
         platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    # v3.77：**先自动扫描**再列队列 —— 刚出好的成片这一轮就出现在界面上，
    # 不用用户去点「扫描成片」（那条按钮还在，但要它才有反馈的场合已经没有了）。
    plat = platforms.normalize(platform)
    auto_added = maybe_auto_scan(platform=plat)
    items = list_jobs(status=status or None,
                      include_discarded=bool(include_discarded), platform=plat)
    # v3.49/v4.17：把**该平台**的标题/标签限制随列表一起下发 —— 界面不写第二份
    # （K44 / K79 同源：「规则由服务端定义」的就别让前端猜，否则填了才被拒）。
    # `platforms` 让发布页头部小卡片**完全由后端驱动**（平台增删只改注册表一处）。
    return {"jobs": items, "count": len(items), "limits": publish_limits(plat),
            "autoAdded": auto_added, "platform": plat,
            "platformName": platforms.get(plat).name,
            "platforms": platforms.public_list(),
            "platformCounts": platform_counts()}


def job_platform(job_id: int, *, db: Path | None = None) -> str:
    """某条队列记录自己的平台（校验上限要用它，不能用「界面上当前选中的平台」——
    用户在完成编辑前切了卡片，标题就会按错的平台去判长短）。找不到回退抖音。"""
    with database.session(db) as conn:
        row = conn.execute("SELECT platform FROM publish_jobs WHERE id = ?",
                           (job_id,)).fetchone()
    return platforms.normalize(row["platform"]) if row is not None else platforms.DEFAULT_PLATFORM


def cover_assets_for_job(job_id: int, *, db: Path | None = None,
                         generate: bool = True) -> dict:
    """按队列条目解析 3:4 / 4:3 封面（已有封面优先，否则成片首帧）。

    封面是**文件级派生物**：editor 只读成片路径，不在数据库里维护第二套路径。
    ``generate=False`` 只探测；预览/发布都传 True（默认），文件已在就直接复用。
    """
    with database.session(db) as conn:
        row = conn.execute(
            "SELECT id, file_path, source_kind FROM publish_jobs WHERE id = ?",
            (job_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    path = str(row["file_path"] or "")
    if not path:
        return {"ok": False, "error": "这条队列记录没有成片路径"}
    kind = row["source_kind"] if "source_kind" in row.keys() else SOURCE_EDIT
    try:
        assets = pubcover.assets_for_file(path, str(kind or SOURCE_EDIT),
                                          generate=generate)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "fileMissing": True}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"生成封面失败：{type(exc).__name__}: {exc}"}
    assets["jobId"] = int(row["id"])
    return assets


@router.get("/covers")
def covers(job_id: int) -> dict:
    """发布页「封面」预览：返回 3:4 / 4:3 两份的实际文件与尺寸。

    有已有封面就用已有封面派生；没有就从**成片首帧**生成。接口会按需落盘，
    但幂等（文件已是最新时只读不重建）。
    """
    return cover_assets_for_job(job_id, generate=True)


@router.post("/update")
def update(payload: dict) -> dict:
    """队列内改标题 / 标签（§5.2）。body：{"id": int, "title": str?, "tags": [..]?}

    v3.49：**加校验**（原先直接存库，什么都能塞进去）。规则与前端同源 ——
    前端读 ``/publish/jobs`` 的 ``limits`` 做即时提示，这里是最后一道闸。
    失败时带 ``field``（title / tags），界面据此把红字标在对应输入框上。
    """
    try:
        job_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少合法的 id"}
    title = payload.get("title")
    tags = payload.get("tags")
    if title is None and tags is None:
        return {"ok": False, "error": "没有要改的字段（title / tags）"}
    plat = job_platform(job_id)
    if title is not None:
        err = validate_title(str(title), plat)
        if err:
            return {"ok": False, "error": err, "field": "title"}
        title = str(title).strip()
    if tags is not None:
        if not isinstance(tags, list):
            return {"ok": False, "error": "tags 必须是数组", "field": "tags"}
        tags = clean_tags(tags)
        err = validate_tags(tags, plat)
        if err:
            return {"ok": False, "error": err, "field": "tags"}
    if not update_job_meta(job_id, title=title, tags=tags):
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    return {"ok": True}


@router.post("/purge-missing")
def purge_missing_endpoint(platform: str = platforms.DEFAULT_PLATFORM) -> dict:
    """清除**当前平台**里文件已不在的队列条目（v4.22）。

    只删队列记录，不碰磁盘（文件本来就不在了）。返回删了几条，界面据此提示。
    """
    plat = platforms.normalize(platform)
    removed = purge_missing(platform=plat)
    return {"ok": True, "removed": removed, "platform": plat,
            "platformName": platforms.get(plat).name}


@router.post("/discard")
def discard(payload: dict) -> dict:
    ids = payload.get("ids") or []
    try:
        job_ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return {"ok": False, "error": "ids 必须是整数数组"}
    if not job_ids:
        return {"ok": False, "error": "ids 不能为空"}
    done, missing = discard_jobs(job_ids)
    return {"ok": not missing, "discarded": done, "missing": missing}


# ---------------------------------------------------------------- 重置 / 清除标题标签（v3.56）
# 用户 2026-09-26：「重置按钮 = 恢复为原版本的标题标签；再加个清除按钮 = 清空标题标签」。
# 原始值在入队时存 original_title / original_tags_json（migration v4），
# 这俩动作是「还原」，不是「人改了内容」，所以 update_job_meta 记 feedback=False。


def _parse_job_id(payload: dict) -> int | None:
    try:
        return int(payload.get("id"))
    except (TypeError, ValueError):
        return None


def _read_original(job_id: int, db: Path | None = None) -> tuple[str, list[str]] | None:
    """读原始标题/标签；找不到该条返回 None。"""
    with database.session(db) as conn:
        row = conn.execute(
            "SELECT original_title, original_tags_json FROM publish_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        tags = json.loads(row["original_tags_json"] or "[]")
    except (ValueError, TypeError):
        tags = []
    return (row["original_title"] or ""), (tags if isinstance(tags, list) else [])


def reset_meta(job_id: int, *, db: Path | None = None) -> dict:
    """纯逻辑：把标题 / 标签恢复成入队时自动生成的**原本内容**。"""
    orig = _read_original(job_id, db=db)
    if orig is None:
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    title, tags = orig
    if not update_job_meta(job_id, title=title, tags=tags, db=db, feedback=False):
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    return {"ok": True, "title": title, "tags": tags}


def clear_meta(job_id: int, *, db: Path | None = None) -> dict:
    """纯逻辑：清空标题 / 标签（清空后需重新填写才能发布）。"""
    if not update_job_meta(job_id, title="", tags=[], db=db, feedback=False):
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    return {"ok": True, "title": "", "tags": []}


@router.post("/reset-meta")
def h_reset_meta(payload: dict) -> dict:
    """端点：把标题/标签恢复成原本内容（用户 2026-09-26「重置按钮」）。"""
    job_id = _parse_job_id(payload)
    if job_id is None:
        return {"ok": False, "error": "缺少合法的 id"}
    return reset_meta(job_id)


@router.post("/clear-meta")
def h_clear_meta(payload: dict) -> dict:
    """端点：清空标题/标签（用户 2026-09-26「清除按钮」）。"""
    job_id = _parse_job_id(payload)
    if job_id is None:
        return {"ok": False, "error": "缺少合法的 id"}
    return clear_meta(job_id)


# ---------------------------------------------------------------- M5-B 发布执行器（人工触发，逐条）

# 服务内运行态（逐条串行：一次只跑一条发布）
_RUN_LOCK = threading.Lock()
_RUN: dict = {"jobId": None, "running": False, "status": None, "phase": None,
              "error": None, "startedAt": None, "_thread": None}

# 僵尸判定阈值：执行线程超过这个时长还没落定，视为「人已打断 / 卡死」，释放占用。
# 正常链路最长的是 uploading（大文件上传），15 分钟足够；超时只释放锁定性标识，
# 不去杀线程（线程是 daemon，随进程退出即可），避免误伤正在跑的真实任务。
STALE_AFTER_SEC = 900.0


def _thread_alive() -> bool:
    t = _RUN.get("_thread")
    return t is not None and t.is_alive()


def _sweep_stale_locked() -> None:
    """**僵尸锁清扫**（必须在持锁状态下调用）。

    K39：人随时可能打断执行（关浏览器、断网、直接杀进程），此时线程可能已经没了、
    但 ``running`` 还留着 True —— 那条记录就**永远不能再点发布**了。
    所以每次判定空闲之前先扫一遍：线程已死 → 释放；线程还活着但超时 → 也释放。
    """
    if not _RUN.get("running"):
        return
    if _thread_alive() and (time.time() - (_RUN.get("startedAt") or 0.0)) <= STALE_AFTER_SEC:
        return
    _RUN.update({"running": False, "status": pub.STATUS_FAILED,
                 "phase": _RUN.get("phase") or "preflight",
                 "error": _RUN.get("error") or "执行被中断或超时"})


def _run_async(job_id: int) -> None:
    global _RUN
    try:
        result = pub.run_job(job_id, log=lambda m: print(f"[editor] {m}", flush=True))
        with _RUN_LOCK:
            _RUN.update({"running": False, "status": result.get("status"),
                         "phase": result.get("phase"), "error": result.get("error")})
    except Exception as exc:                                 # noqa: BLE001
        with _RUN_LOCK:
            _RUN.update({"running": False, "status": pub.STATUS_FAILED,
                         "phase": "preflight", "error": f"{type(exc).__name__}: {exc}"})


@router.post("/start")
def start(payload: dict) -> dict:
    """逐条人工触发发布：自动上传 + 填表，**停在 waiting_user 等人工点发布**（R1）。

    **失败 / 打断后可以再次触发**（K39）：启动前先把任务状态重置回 ``queued``，
    再走一遍完整链路。重跑不会自动重试成功过的阶段 —— 因为浏览器上下文已被打断，
    不存在"继续上次"。上一轮若已进到 uploading 及之后，返回 ``notice`` 提醒人
    核对有没有重复投稿（防重复投稿是硬约束，但"人决定再来一次"应当被允许）。
    """
    try:
        job_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少合法的 id"}

    job = pub.get_job(job_id)
    if job is None:
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}

    with _RUN_LOCK:
        _sweep_stale_locked()
        if _RUN["running"] and int(_RUN["jobId"]) != job_id:
            return {"ok": False,
                    "error": f"已有发布任务在跑（job {_RUN['jobId']}）。"
                             f"请等它停住，或点该条的「重新排队」把它放回队列。"}
        if _RUN["running"] and int(_RUN["jobId"]) == job_id and _thread_alive():
            return {"ok": False, "error": "这条正在执行中，请勿重复点击（如需中止请关闭浏览器后点「重新排队」）"}

        # 每次都从干净状态起步：把上一次的 running/failed/waiting_user 清回 queued
        pub.reset_job(job_id)
        thread = threading.Thread(target=_run_async, args=(job_id,), daemon=True)
        now = time.time()
        _RUN.update({"jobId": job_id, "running": True, "status": pub.STATUS_RUNNING,
                     "phase": "preflight", "error": None,
                     "startedAt": now, "_thread": thread})
        thread.start()

    notice = None
    if pub.is_retry_notice(job.get("phase")):
        notice = ("上一轮已进行到「上传」阶段。本次会重新上传同一文件，"
                  "请在浏览器里确认没有重复投稿。")
    return {"ok": True, "jobId": job_id, "retried": int(job.get("attempt_count") or 0) > 0,
            "platform": platforms.normalize(job.get("platform")),
            "platformName": platforms.get(job.get("platform")).name,
            "notice": notice}


@router.post("/reset")
def reset(payload: dict) -> dict:
    """把一条任务**放回待发布**（ status → queued，phase → preflight ）。

    用于：发布失败/被打断后状态卡死、或想换个浏览器状态重来。
    """
    try:
        job_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少合法的 id"}
    with _RUN_LOCK:
        _sweep_stale_locked()
        if _RUN["running"] and int(_RUN["jobId"]) == job_id and _thread_alive():
            return {"ok": False, "error": "这条正在执行中，请先关闭弹出的浏览器"}
        if not pub.reset_job(job_id):
            return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
        if int(_RUN.get("jobId") or -1) == job_id:
            _RUN.update({"running": False, "status": pub.STATUS_QUEUED,
                         "phase": "preflight", "error": None, "_thread": None})
    return {"ok": True, "jobId": job_id}


@router.get("/run-status")
def run_status() -> dict:
    with _RUN_LOCK:
        _sweep_stale_locked()
        snapshot = {k: v for k, v in _RUN.items() if k != "_thread"}
        snapshot["elapsedSec"] = (round(time.time() - _RUN["startedAt"], 1)
                                  if _RUN.get("startedAt") else 0.0)
        return snapshot


@router.post("/mark-done")
def mark_done(payload: dict) -> dict:
    """人工在浏览器点完发布后，回软件把该条标记为「已发布」。"""
    try:
        job_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少合法的 id"}
    if not pub.mark_done(job_id):
        return {"ok": False, "error": f"队列里没有 id={job_id} 的条目"}
    return {"ok": True}
