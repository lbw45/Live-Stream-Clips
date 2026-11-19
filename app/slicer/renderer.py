#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染调度器（M4）—— 跑在 slicer 服务进程里，把「候选 → 成片」排队执行。

为什么必须**串行**（D24 / A4）：

- 渲染要吃 NVENC 会话与显存，4 GB 卡上并发两条极易 OOM；
- 录制、转写也在抢同一块 GPU；
- 排队的收益远大于并发：单条成片只要几秒到几十秒。

**显存守卫**（D24）：每次派发前查一次显存占用，>3200 MiB 就**暂停派发**（等待而不是硬上）。
等待有上限（默认 5 分钟），超时则照跑 —— 宁可慢，也不能让队列永久饿死。

**取消**：`should_cancel` 回调穿透到 `render()` 里，ffmpeg 会被 terminate。这一点是
故意做到引擎层的：如果在调度层才丢弃结果，ffmpeg 会白白跑完整个片子。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path

from ..common import file_ops as FO
from ..common.paths import ws
from .editplan import (STRATEGY_AGGRESSIVE, STRATEGY_CONSERVATIVE,
                       STRATEGY_MANUAL, build_edit_decision,
                       build_manual_decision, decision_fingerprint)
from .render import (ORIENT_AUTO, ORIENT_LANDSCAPE, ORIENT_PORTRAIT, RenderParams,
                     gpu_memory_used_mib, probe_media, render,
                     resolve_output_size)
from .subtitles import SubtitleStyle, build_ass, normalize_font

# 历史保留条数（超出后丢最旧的已完成任务；运行中的绝不丢）
MAX_HISTORY = 50
SOURCE_EXTS = (".mp4", ".ts", ".mkv", ".flv")
CAND_SUFFIX = ".candidates.json"


# ---------------------------------------------------------------- 成片命名（v3.39）

# 用户：「成片命名需要改为对应的小切片标题名称」+「要不渲染出来会分不清哪个视频」。
# **这里是成片文件名的唯一实现**（渲染产出 / 封面同名 / 剪辑页预测「保存并覆盖成片」的名字
# 全部调它）—— 三处各算一次必然漂移（K44），而漂移的后果是「覆盖」变成了另存一份。
#
# 三条规则（按顺序）：
#   ① 名字取该片段的**标题**（清掉 Windows 非法字符、压长度），标题空则回退旧命名；
#   ② **同一件作品（同一候选 + 同一片段号）复用已有文件名** —— 否则「保存并覆盖成片」
#      每次都会多出一个文件，"覆盖"名不副实；
#   ③ 不同作品撞了同一个标题 → 自动加 `_2`/`_3` 后缀，绝不复用别人的文件。
FILM_TITLE_MAX = 60
_FS_BAD = set('\\/:*?"<>|')


def sanitize_film_title(title: str) -> str:
    """标题 → 合法的文件名片段（非法字符换空格、压缩空白、长度截断、去掉结尾的点）。"""
    raw = str(title or "")
    raw = "".join(" " if (ch in _FS_BAD or ord(ch) < 32) else ch for ch in raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = raw[:FILM_TITLE_MAX].strip()
    return raw.strip(". ").strip()          # 结尾的点/空格在 Windows 上会被截掉，先去掉


def edit_sidecar_identities(edits_dir: Path | None = None) -> dict:
    """``{成片 stem: (candidateName, candidateIdx, strategy)}``（读伴生 `.edit.json`）。

    伴生 json 是"这个成片是哪件作品"的**权威记录**，所以改名之后仍然认得出作品归属。
    """
    root = Path(edits_dir) if edits_dir else ws.editor_output_dir
    out: dict = {}
    if not root.is_dir():
        return out
    for side in root.glob("*.edit.json"):
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        out[side.name[: -len(".edit.json")]] = (
            str(meta.get("candidateName") or ""), meta.get("candidateIdx"),
            str(meta.get("strategy") or ""))
    return out


def resolve_film_stem(ident: str, idx, strategy: str, title: str = "",
                      *, edits_dir: Path | None = None) -> str:
    """成片文件名 stem（不含扩展名）。``ident`` = **候选身份**（候选产物名 / 源视频名）。

    规则见本段开头三条。⚠️ ``ident`` 必须与伴生 json 里的 ``candidateName`` **同一口径**
    （都是候选产物名），否则「同一件作品」认不出来 → 覆盖会退化成另存一份。
    """
    root = Path(edits_dir) if edits_dir else ws.editor_output_dir
    src = Path(str(ident)).name
    try:
        idx_val = int(idx)
    except (TypeError, ValueError):
        idx_val = None
    existing = edit_sidecar_identities(root)

    # ② 同一件作品：沿用原文件名（「保存并覆盖成片」/「重新生成」都落回同一个文件）
    if idx_val is not None:
        for stem, who in existing.items():
            if who[0] == src and who[1] is not None:
                try:
                    if int(who[1]) == idx_val:
                        return stem
                except (TypeError, ValueError):
                    continue

    base = sanitize_film_title(title)
    if not base:                            # ① 标题为空 → 回退旧命名（绝不产出空名文件）
        base = f"{Path(src).stem}_c{idx_val if idx_val is not None else 0}_{strategy}"
    if not (root / f"{base}.mp4").exists() and base not in existing:
        return base
    # ③ 撞名 → 加后缀（也要跳过别人已占的 stem）
    n = 2
    while (root / f"{base}_{n}.mp4").exists() or f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


# ---------------------------------------------------------------- BGM（音乐素材）

# v3.19：BGM 的搜索根 = 「音乐素材」+「我的上传」两处。
# 起因（真实数据）：用户在**素材库**里导入了 4 首 BGM，而素材库的导入统一落到 `uploads`，
# 渲染页的 BGM 下拉却只扫 `music` 目录 → 下拉是空的、选不了。
# **解析与列表必须是同一份实现**（K44）：只改这里，别在 web_server 里再扫一遍。
BGM_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")


def bgm_roots() -> list[tuple[str, "Path"]]:
    """返回 ``[(来源标签, 目录), …]``，顺序即优先级（同名时 music 优先）。"""
    return [("音乐素材", ws.music_dir), ("我的上传", ws.uploads_dir)]


# v3.37：来源标签 → **媒体流前缀**（`cut.STREAM_ROOTS` 的键）。
# 为什么必须有：剪辑页要试听/联播 BGM，前端拿到的只有文件名，得自己拼流地址；
# 早先前端写死 `music/<名字>`，于是「我的上传」里的音乐**点了没声音**（静默失败）。
# 这里给出唯一映射，前端一律用下发的 `rel`（不变量断言：取值 ⊆ STREAM_ROOTS 的键）。
BGM_SOURCE_PREFIX = {"音乐素材": "music", "我的上传": "uploads"}


def list_bgm_files() -> list[dict]:
    """列出可用于 BGM 的音频文件（含来源、流前缀与大小），供渲染页/剪辑页使用。"""
    out: list[dict] = []
    seen: set[str] = set()
    for source, root in bgm_roots():
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir()):
            if not (p.is_file() and p.suffix.lower() in BGM_EXT):
                continue
            if p.name in seen:            # 同名只出一次（前面的目录优先）
                continue
            seen.add(p.name)
            prefix = BGM_SOURCE_PREFIX.get(source, "")
            out.append({"name": p.name, "source": source,
                        "rel": f"{prefix}/{p.name}" if prefix else "",
                        "sizeKb": round(p.stat().st_size / 1024, 1)})
    return out


def resolve_bgm_file(bgm: str | None):
    """按文件名在「音乐素材 / 我的上传」里找 BGM。**唯一实现**（渲染与列表同源）。

    返回 Path；``bgm`` 为空返回 None；不合法或找不到返回 ``(http_status, payload)``。
    """
    if not bgm:
        return None
    rel = str(bgm).strip()
    if not FO.is_safe_stem(rel):
        return 400, {"error": "BGM 文件名不合法（不能带路径分隔符或 ..）"}
    for _source, root in bgm_roots():
        root = root.resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_file():
            return target
    return 404, {"error": f"BGM 不存在：{rel}（音乐素材 / 我的上传里都没有）"}


def resolve_source_video(source: str | None, data: dict, base_stem: str):
    """定位源视频（**唯一实现**：渲染与预览剪辑都走这里，K44 同源教训）。

    返回 Path；找不到/不合法时返回 ``(http_status, payload)``。
    ``data`` 是候选产物 JSON（可用其 ``videoName`` 提示）；``base_stem`` 兜底 stem。

    源根就是录制素材 ``live_raw``，
    一律按 ``paths.source_roots()`` 依次找（那份表是唯一出处）。显式 ``source`` 允许写成
    ``network/xxx.mp4`` 这种「逻辑名/相对路径」两段式；只给文件名时按表顺序找，先命中先用。
    """
    roots = [(name, Path(root)) for name, root in ws.source_roots()]
    live_root = ws.live_source_dir.resolve()
    hint = str(data.get("videoName") or "").strip()
    base_stem = Path(hint).stem if hint else base_stem

    if source:
        rel = str(source).strip()
        if not FO.is_safe_stem(rel):
            return 400, {"error": "源视频名不合法（不能带路径分隔符或 ..）"}
        head, _, rest = rel.replace("\\", "/").partition("/")
        picked = next(((n, r) for n, r in roots if n == head and rest), None)
        if picked:
            root, rel = picked[1].resolve(), rest
        else:
            root = live_root
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return 400, {"error": "只能渲染源素材目录内的视频（路径越界已拒绝）"}
        if not target.is_file():
            return 404, {"error": f"源视频不存在：{rel}"}
        return target

    # 未显式指定：按源根表依次找（同 stem 命中即用）
    for _name, root in roots:
        root = root.resolve()
        for ext in SOURCE_EXTS:
            p = (root / f"{base_stem}{ext}").resolve()
            if p.is_file():
                return p
        # 兜底：同 stem 任意后缀
        for p in sorted(root.glob(f"{base_stem}.*")):
            if p.is_file() and p.suffix.lower() in SOURCE_EXTS:
                return p.resolve()
    return 404, {"error": f"在源素材目录里找不到素材视频：{base_stem}"
                          f"（可在表单里手动指定 source）"}


def candidate_source_exists(candidate: str, *, candidates_dir: Path | None = None) -> bool:
    """这份候选**还能不能进预览 / 剪辑** —— 唯一判据是「源素材是否仍在磁盘上」。

    v3.60（）为什么必须由**服务端**回答这个问题：
    真窗口取证发现「点预览/剪辑没反应」的真实原因是**源录像已经从源根里消失**
    （多半是用户在「素材库 → 直播录制」里删掉了，候选 JSON 还留着）。
    而列表页当时对此一无所知 —— 于是按钮照给、点了才在角落报一句错，
    用户看到的就是「功能坏了」。

    ⚠️ 判据必须与 `/api/cut/plan` **同源**：这里调的就是它用的那个
    `resolve_source_video`（K44：两处各写一份必然漂移，漂移 = 静默给死按钮）。
    候选 JSON 读不出来时按「有源」处理（保守：宁可让用户点一下看到真错误，
    也不要在源还在的情况下把按钮摘掉）。
    """
    stem = Path(str(candidate or "").strip()).name
    if not stem:
        return False
    root = Path(candidates_dir) if candidates_dir else ws.candidates_dir
    data: dict = {}
    side = root / f"{stem}.candidates.json"
    if side.is_file():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    got = resolve_source_video(None, data if isinstance(data, dict) else {}, stem)
    return isinstance(got, Path)


def select_candidates_for_batch(data: dict, min_score: float) -> tuple[list[dict], float]:
    """**按分数批量渲染**的挑片逻辑（纯函数：便于冒烟直接测，不必排真实任务）。

    返回 ``(挑中的片段, 这份产物里的最高分)``；挑中的按综合评分**从高到低**排。
    评分口径与渲染页一致 = ``scores.overall``（服务端重算过的那一份）。
    """
    cands = (data or {}).get("candidates") or []
    try:
        threshold = float(min_score)
    except (TypeError, ValueError):
        threshold = 0.0
    best = 0.0
    scored: list[tuple[float, dict]] = []
    for c in cands:
        try:
            overall = float((c.get("scores") or {}).get("overall") or 0)
        except (TypeError, ValueError):
            overall = 0.0                  # 脏数据（"坏值"、None、缺字段）一律当 0 分
        best = max(best, overall)
        scored.append((overall, c))
    # ⚠️ 排序必须用**已经解析过**的分数：在 sort key 里再 float 一次的话，
    #    一条脏数据就会让整批渲染 500（冒烟抓到过：`overall="坏值"`）。
    scored.sort(key=lambda t: t[0], reverse=True)
    picked = [c for overall, c in scored if overall >= threshold]
    return picked, best


class RenderManager:
    """渲染任务的排队与执行（服务内单例）。"""

    # 显存守卫阈值（MiB）。4 GB 卡实测：转写约 1.5 GB，渲染约 0.6–1.2 GB，
    # 留出余量取 3200 —— 超过就说明别的重活正在跑，先等它腾地方。
    VRAM_LIMIT_MIB = 3200
    VRAM_CHECK_INTERVAL_SEC = 5.0
    VRAM_MAX_WAIT_SEC = 300.0

    def __init__(self, log=print) -> None:
        self.log = log or (lambda *a, **k: None)
        self._lock = threading.RLock()      # K26：持锁时会调 _save()，必须可重入
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []         # 任务 id 顺序（新的在前）
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None
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
            target=self._worker, name="renderer", daemon=True)
        self._thread.start()
        self.log(f"[renderer] 已启动，历史 {len(self._jobs)} 条")

    def stop(self) -> None:
        self._running = False
        self._queue.put("__stop__")
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------ 持久化索引

    @property
    def _index_path(self) -> Path:
        return ws.editor_output_dir / "index.json"

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
                # 上次服务退出时没跑完 → 标中断（不自动重跑，避免重启风暴）
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
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------ 提交与查询

    def submit(self, *, candidate: str, idx: int | None = None,
               source: str | None = None, strategy: str = STRATEGY_CONSERVATIVE,
               burn_subtitles: bool = True, bgm: str | None = None,
               params: dict | None = None,
               manual_ranges: list | None = None) -> tuple[int, dict]:
        """提交一个渲染任务。

        ``candidate`` 是候选产物的 stem（在 `runtime_data/slicer/candidates/` 下）；
        ``idx`` 省略时挑**综合评分最高**的那条；``source`` 省略时按 stem 在
        ``live_raw`` 里找同名视频。
        ``manual_ranges`` 给出时走**手动剪辑**通路（预览剪辑页：用户自己分割/删除
        分段），strategy 强制为 manual，忽略 strategy 参数。
        """
        raw = str(candidate or "").strip()
        if not raw:
            return 400, {"error": "请指定要渲染的候选产物（candidate）"}
        if not FO.is_safe_stem(raw):
            return 400, {"error": "候选产物名不合法（不能带路径分隔符或 ..）"}
        if manual_ranges is None and strategy not in (STRATEGY_CONSERVATIVE,
                                                      STRATEGY_AGGRESSIVE):
            return 400, {"error": f"strategy 只能是 {STRATEGY_CONSERVATIVE} / "
                                  f"{STRATEGY_AGGRESSIVE} / manual(带 manual_ranges)"}

        name = raw if raw.endswith(CAND_SUFFIX) else f"{raw}{CAND_SUFFIX}"
        # ⚠️ 不能直接用 ``Path(name).stem`` —— 那只剥掉 `.json`，会把
        #    `x.candidates.json` 剥成 `x.candidates`，后面再拼后缀就找错文件了。
        base = name[: -len(CAND_SUFFIX)]
        cand_path = ws.candidates_dir / name
        if not cand_path.is_file():
            return 404, {"error": f"候选产物不存在：{name}（先到「AI 切片」页切一次）"}
        try:
            data = json.loads(cand_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return 400, {"error": f"候选产物读取失败：{exc}"}

        cands = data.get("candidates") or []
        if not cands:
            return 400, {"error": "这份候选产物里没有可渲染的片段"}
        picked = None
        if idx is not None:
            picked = next((c for c in cands if int(c.get("idx", -1)) == int(idx)), None)
            if picked is None:
                return 404, {"error": f"候选产物里没有第 {idx} 条片段"}
        else:
            picked = max(cands, key=lambda c: float(
                (c.get("scores") or {}).get("overall") or 0))

        # --- 手动剪辑：keep 段在这里先校验一遍（渲染层还会再 normalize，双保险），
        #     让用户在提交瞬间就拿到可读的错误，而不是排队后才发现段非法
        eff_strategy = STRATEGY_MANUAL if manual_ranges is not None else strategy
        if manual_ranges is not None:
            from .editplan import normalize_keep_ranges
            try:
                start_ms = float(picked["start"]) * 1000.0
                end_ms = float(picked["end"]) * 1000.0
                manual_ranges = normalize_keep_ranges(
                    manual_ranges, start_ms, end_ms)
            except (KeyError, TypeError, ValueError) as exc:
                return 400, {"error": f"手动剪辑 keep 段不合法：{exc}"}

        # --- 源视频：允许显式指定，否则按 videoName / stem 在 live_raw 里找
        src_path = self._resolve_source(source, data, base)
        if isinstance(src_path, tuple):
            return src_path
        # --- BGM：只允许 music 目录内
        bgm_path = self._resolve_bgm(bgm)
        if isinstance(bgm_path, tuple):
            return bgm_path

        with self._lock:
            self._seq += 1
            jid = f"r{time.strftime('%Y%m%d-%H%M%S')}-{self._seq:03d}"
            job = {
                "id": jid,
                "candidate": base,
                "candidateIdx": picked.get("idx"),
                "candidateTitle": str(picked.get("title") or "")[:60],
                "source": src_path.name,
                "sourcePath": str(src_path),
                "strategy": eff_strategy,
                "manualRanges": manual_ranges,
                "burnSubtitles": bool(burn_subtitles),
                "bgm": Path(bgm_path).name if bgm_path else "",
                "params": params or {},
                "status": "queued",
                "stage": "queued",
                "percent": 0.0,
                "message": "排队中",
                "queuedAt": round(time.time(), 1),
                "startedAt": 0.0,
                "endedAt": 0.0,
                "elapsedSec": 0.0,
                "output": "",
                "outputPath": "",
                "outputName": "",
                "durationSec": 0.0,
                "encoder": "",
                "vramUsedMib": None,
                "waitedSec": 0.0,
                "error": "",
                "recentLog": [],
            }
            self._jobs[jid] = job
            self._order.insert(0, jid)
            self._trim()
            self._save()
        self._queue.put(jid)
        self.log(f"[renderer] 入队 {jid} ← {base}#{picked.get('idx')} "
                 f"（strategy={eff_strategy}）")
        return 200, {"ok": True, "job": dict(job)}

    def submit_batch(self, *, candidate: str, min_score: float, idx=None, source=None,
                     strategy: str = STRATEGY_CONSERVATIVE, burn_subtitles: bool = True,
                     bgm: str | None = None, params: dict | None = None) -> tuple[int, dict]:
        """**按分数批量**：这份候选产物里综合评分 ≥ ``min_score`` 的小片段全部排进队列。

        用户 2026-09-24：「渲染任务改为把指定分数的切片小片段都进行操作」——
        以前一次只能渲染一条片段，一天几十条候选得点几十次。

        实现上**逐条调用 ``submit()``**（而不是自己拷一份校验/入库逻辑）：
        命名、BGM 解析、手动段校验、显存守卫全部沿用同一条路（K44：别写第二份）。
        ``submit()`` 里有一次 ``_resolve_bgm`` 与源视频解析，重复调用不影响正确性。
        """
        raw = str(candidate or "").strip()
        if not raw:
            return 400, {"error": "请指定要批量渲染的候选产物（candidate）"}
        name = raw if raw.endswith(CAND_SUFFIX) else f"{raw}{CAND_SUFFIX}"
        cand_path = ws.candidates_dir / name
        if not cand_path.is_file():
            return 404, {"error": f"候选产物不存在：{name}（先到「AI 切片」页切一次）"}
        try:
            data = json.loads(cand_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return 400, {"error": f"候选产物读取失败：{exc}"}
        picked, best = select_candidates_for_batch(data, min_score)
        if not picked:
            return 400, {"error": f"这份候选产物里没有 ≥ {float(min_score):g} 分的片段"
                                  f"（最高 {best:g} 分）—— 把阈值调低或重新切一次"}
        jobs: list[dict] = []
        for cand in picked:
            code, payload = self.submit(
                candidate=raw, idx=cand.get("idx"), source=source, strategy=strategy,
                burn_subtitles=burn_subtitles, bgm=bgm, params=params)
            if code != 200:
                return code, payload                 # 单条失败就整体报错（不要静默少渲几条）
            if payload.get("job"):
                jobs.append(payload["job"])
        self.log(f"[renderer] 按分数批量入队 {len(jobs)} 条 ← {raw}"
                 f"（≥ {float(min_score):g} 分）")
        return 200, {"ok": True, "submitted": len(jobs), "minScore": float(min_score),
                     "jobs": jobs, "job": jobs[0] if jobs else None}

    def _resolve_source(self, source: str | None, data: dict, base_stem: str):
        """定位源视频。返回 Path，或 ``(http_status, payload)`` 表示出错。"""
        return resolve_source_video(source, data, base_stem)

    def _resolve_bgm(self, bgm: str | None):
        return resolve_bgm_file(bgm)

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
            "vramUsedMib": gpu_memory_used_mib(),
            "now": round(time.time(), 1),
        }

    def cancel(self, job_id: str | None = None) -> tuple[int, dict]:
        """取消：排队中的作废；运行中的由 worker 通过回调 terminate 掉 ffmpeg。"""
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
        self.log(f"[renderer] 请求取消 {target}（原状态 {status}）")
        return 200, {"ok": True, "job": dict(job), "message": "已请求取消"}

    def edits(self) -> tuple[int, dict]:
        """已产出的成片列表（扫描目录 + 读伴生的 `.edit.json`，不依赖内存状态）。"""
        root = ws.editor_output_dir
        files: list[dict] = []
        if root.is_dir():
            for mp4 in sorted(root.glob("*.mp4")):
                try:
                    stat = mp4.stat()
                except OSError:
                    continue
                side = mp4.with_suffix(".edit.json")
                meta: dict = {}
                if side.is_file():
                    try:
                        meta = json.loads(side.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        meta = {}
                files.append({
                    "name": mp4.name,
                    "stem": mp4.stem,
                    "path": str(mp4),
                    "sizeKb": round(stat.st_size / 1024, 1),
                    "mtime": round(stat.st_mtime, 1),
                    "durationSec": meta.get("durationSec", 0),
                    "width": meta.get("width", 0),
                    "height": meta.get("height", 0),
                    "encoder": meta.get("encoder", ""),
                    "hasAudio": meta.get("hasAudio", False),
                    # v3.39：标题优先取小切片标题（新伴生 json 有），老成片回退 coverTitle
                    "title": meta.get("title") or meta.get("coverTitle", ""),
                    "score": round(float(meta.get("score") or 0), 1),
                    "strategy": meta.get("strategy", ""),
                    "candidateName": meta.get("candidateName", ""),
                    "candidateIdx": meta.get("candidateIdx"),
                    # v3.60（）：这份成片**能不能回到剪辑页**，取决于它的
                    # 源素材还在不在（成片自己还在，所以「预览」照旧可用 —— 用户看到的
                    # 「预览能播、进不了剪辑」正是这个差异）。前端据此决定给不给按钮，
                    # 而不是给一个点了才报错的死按钮。
                    "sourceExists": bool(meta.get("candidateName"))
                    and candidate_source_exists(str(meta.get("candidateName") or "")),
                })
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return 200, {"files": files, "count": len(files), "dir": str(root)}

    def delete_edits(self, names: list[str]) -> tuple[int, dict]:
        """删除成片，**连带删掉伴生的 `.edit.json`**（删除规则一律走 file_ops）。"""
        rels: list[str] = []
        root = ws.editor_output_dir
        for raw in (names or []):
            rel = str(raw or "").strip()
            if not rel:
                continue
            rels.append(rel)
            p = root / rel
            side = p.with_suffix(".edit.json")
            if side.is_file():
                rels.append(side.name)
        if not rels:
            return 400, {"error": "没有指定要删除的成片"}
        # index.json 在 PROTECTED_NAMES 里 → 不可能被勾 ERR 掉
        code, payload = FO.delete_files_in(root, rels, log=self.log)
        if isinstance(payload, dict):
            payload["protected"] = sorted(FO.PROTECTED_NAMES)
        return code, payload

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
                return

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
                self._guard_vram(job)
                self._execute(job)
            except Exception as exc:      # 兜底：worker 绝不允许死掉
                self.log(f"[renderer] {jid} 异常：{type(exc).__name__}: {exc}")
                self._finish(job, "error", error=f"{type(exc).__name__}: {exc}")

    def _guard_vram(self, job: dict) -> None:
        """D24：显存 >3200 MiB 时暂停派发，等到降下来再跑。"""
        waited = 0.0
        while waited < self.VRAM_MAX_WAIT_SEC:
            used = gpu_memory_used_mib()
            if used is None or used <= self.VRAM_LIMIT_MIB:
                return
            with self._lock:
                job["stage"] = "waiting"
                job["message"] = (f"显存 {used} MiB 超过 {self.VRAM_LIMIT_MIB}"
                                  f" MiB，等待释放（已等 {waited:.0f}s）")
                job["waitedSec"] = round(waited, 1)
            self.log(f"[renderer] {job['id']} {job['message']}")
            time.sleep(self.VRAM_CHECK_INTERVAL_SEC)
            waited += self.VRAM_CHECK_INTERVAL_SEC
        self.log(f"[renderer] {job['id']} 显存等待超时，强行开跑")

    def _execute(self, job: dict) -> None:
        cand_path = ws.candidates_dir / f"{job['candidate']}.candidates.json"
        transcript_path = ws.transcripts_dir / f"{job['candidate']}.json"
        data = json.loads(cand_path.read_text(encoding="utf-8"))
        picked = next(c for c in (data.get("candidates") or [])
                      if int(c.get("idx", -1)) == int(job["candidateIdx"]))
        transcript: dict = {}
        if transcript_path.is_file():
            try:
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                transcript = {}
        if not transcript.get("segments"):
            # 没有转写照样能出片（只是没字幕）——如实记录，不假装成功
            self._append_log(job, f"缺少转写产物 {transcript_path.name}：本次无字幕")
            self.log(f"[renderer] {job['id']} 缺转写 {transcript_path.name}，出无字幕版本")

        # 「重新识别字幕」的结果优先：它是**针对这一条片段**重新跑的，比整场直播的
        # 整份转写更准。recues 记的是**源视频时间轴**，正好等于 transcript 段格式。
        recues = (job.get("params") or {}).get("recues") or []
        if isinstance(recues, list) and recues:
            segs = []
            for c in recues:
                try:
                    segs.append({"start": round(float(c["startMs"]) / 1000.0, 3),
                                 "end": round(float(c["endMs"]) / 1000.0, 3),
                                 "text": str(c.get("text") or "")})
                except (KeyError, TypeError, ValueError):
                    continue
            if segs:
                transcript = {"segments": segs, "fromRecheck": True}
                self._append_log(job, f"使用重新识别的字幕 {len(segs)} 段")

        rparams = RenderParams()
        for key, value in (job.get("params") or {}).items():
            if hasattr(rparams, key):
                setattr(rparams, key, value)

        # v3.37：字幕条的人工挪动（剪辑页「字幕调整」→ 解锁轨道 → 拖动）。**必须在这里生效**，
        # 否则就是用户报的那类"界面上改了、成片里没变"。规则实现在 `subtitles.shift_cues()`，
        # 与前端显示用的是同一套（键 = 该条字幕的原始起点毫秒，不是数组下标）。
        cue_shifts = (job.get("params") or {}).get("cueShifts") or {}
        if cue_shifts and transcript.get("segments"):
            try:
                from app.slicer import subtitles as _subs
                lo_ms = float(picked["start"]) * 1000.0
                hi_ms = float(picked["end"]) * 1000.0
                before = list(transcript.get("segments") or [])
                transcript = dict(transcript)
                transcript["segments"] = _subs.shift_segments(
                    before, cue_shifts, lo_ms=lo_ms, hi_ms=hi_ms)
                moved = sum(1 for a, b in zip(before, transcript["segments"])
                            if a.get("start") != b.get("start"))
                self._append_log(job, f"应用字幕挪动 {moved} 条")
            except Exception as exc:                              # noqa: BLE001
                self._append_log(job, f"字幕挪动未生效（按原时间出片）：{exc}")

        # 成片画幅（D30）：**必须在写 ASS 之前算出来** —— ASS 的 PlayRes 要跟它一致，
        # 否则字幕会被非等比拉伸。与 render() 内部用的是同一个 resolve_output_size()，
        # 保证「字幕基准」与「实际出片尺寸」不会各自算各自的。
        src_path_for_probe = Path(job["source"])
        try:
            src_info = probe_media(src_path_for_probe)
        except (RuntimeError, ValueError, OSError) as exc:
            src_info = {"width": 0, "height": 0}
            self._append_log(job, f"素材探测失败（按默认画幅出片）：{exc}")
        out_w, out_h, _crop_expr = resolve_output_size(src_info, rparams)
        _want_orient = str(rparams.orientation or ORIENT_AUTO).strip().lower()
        with self._lock:
            # v3.137：auto 就如实写「跟随录制」，别把 4:3 成片谎报成「横屏 16:9」
            job["orientation"] = (_want_orient if _want_orient in
                                  (ORIENT_PORTRAIT, ORIENT_LANDSCAPE) else ORIENT_AUTO)
            job["width"], job["height"] = out_w, out_h
            job["cropped"] = _crop_expr is not None

        style = SubtitleStyle(font=normalize_font(rparams.subtitle_font),
                              font_size=rparams.subtitle_font_size)
        # v3.41：字幕样式（剪辑页「字幕调整」的大小 / XY 滑杆）。必须在 build_ass 之前
        # 套上 —— build_ass 里还会按成片尺寸等比缩放字号，这里的缩放是"用户意图"那一级。
        layout = (job.get("params") or {}).get("subtitleLayout") or {}
        try:
            if layout.get("fontScale"):
                style.font_size = max(12, round(style.font_size * float(layout["fontScale"])))
            if layout.get("posX") is not None or layout.get("posY") is not None:
                style.pos_x = float(layout.get("posX", 50))
                style.pos_y = float(layout.get("posY", 20))
            if layout:
                self._append_log(job, "应用字幕样式：大小 %s / 位置 %s,%s" % (
                    layout.get("fontScale") or "-", layout.get("posX") or "-",
                    layout.get("posY") or "-"))
        except (TypeError, ValueError):
            pass                                          # 坏参数按默认样式出片

        with self._lock:
            job["status"] = "running"
            job["stage"] = "planning"
            job["startedAt"] = round(time.time(), 1)
            job["message"] = "生成剪辑决策"
        self.log(f"[renderer] 开始 {job['id']}（{job['outputName'] or job['candidate']}"
                 f"#{job['candidateIdx']}）")

        if job.get("manualRanges"):
            decision = build_manual_decision(
                picked, transcript, job["manualRanges"], style=style,
                with_subtitles=bool(job["burnSubtitles"]) and rparams.burn_subtitles,
                hide_fillers=bool(rparams.hide_fillers))
        else:
            # v3.168（用户 2026-10-28）：「是否保留开头钩子」开关 ——
            # 关＝不把高能钩子复制到开头（钩子内容仍在正片原位置），
            # 此时没有「钩子→正文」这一刀，转场自然不生效（只剩一段，xfade 判 <2 段=0）。
            include_hook = bool((job.get("params") or {}).get("include_hook", True))
            decision = build_edit_decision(
                picked, transcript, strategy=job["strategy"],
                planner=str(data.get("planner") or "local_fallback"),
                style=style,
                with_subtitles=bool(job["burnSubtitles"]) and rparams.burn_subtitles,
                hide_fillers=bool(rparams.hide_fillers),
                include_hook=include_hook)
        decision["_renderParams"] = rparams.fingerprint_material()

        # ASS 落**渲染临时目录**（temp repr / ASCII 路径链，K24）
        ass_path = None
        if decision.get("subtitleCues") and rparams.burn_subtitles:
            # v3.159：ASS 放 **ass/ 子目录**。之前和成片临时文件同目录，清 ass 时
            # `parent.rmdir()` 把整个任务目录（连同刚渲好的 mp4）一起送走了 —— 真机复现：
            # "after-render exists=True" → 清完 ass 后 dir 直接没了 ⇒ 搬成片时 WinError 3。
            ass_dir = ws.render_tmp_dir / str(job["id"]) / "ass"
            ass_dir.mkdir(parents=True, exist_ok=True)
            ass_path = ass_dir / "subtitle.ass"
            ass_path.write_text(
                build_ass(decision["subtitleCues"], style, out_w, out_h,
                          keywords=decision.get("keywords")),
                encoding="utf-8")
            with self._lock:
                job["stage"] = "ass"
                job["message"] = f"字幕 {len(decision['subtitleCues'])} 条"

        out_dir = ws.editor_output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        # v3.39：成片名 = 该片段的**标题**（唯一实现见 renderer.resolve_film_stem）。
        # 身份用 `job["candidate"]`（候选产物名）—— 与伴生 json 的 candidateName 同口径。
        out_stem = resolve_film_stem(job["candidate"], job["candidateIdx"],
                                     job["strategy"], str(picked.get("title") or ""))
        out_name = f"{out_stem}.mp4"
        out_path = out_dir / out_name
        # v3.159（用户 2026-10-28「停止时半成品不要，但别删上一个流程的素材」）：
        # 先写**临时名** `.xxx.partial.mp4`，成功后才原子换到正式名。
        # 这样点「停止」只丢这一份临时文件：同名老成片（上一次渲染的）原样还在，
        # 也不会出现半个 mp4 被发布队列扫进去。
        # 临时文件放 render_tmp/<jobId>/（**不放在 edits/ 里**：发布队列与成片列表都按
        # `*.mp4` 扫目录，半个文件放进去会被当成一条成片扫出来）。
        tmp_dir = ws.render_tmp_dir / str(job["id"])
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_out = tmp_dir / out_path.name
        existed_before = out_path.is_file()

        with self._lock:
            job["stage"] = "rendering"
            job["message"] = "渲染中（NVENC，失败自动回退软编）"
            job["outputName"] = out_name

        # v4.2：钩子段「缓推镜头」—— 只有当 keep_ranges[0] 确实是钩子段时才生效
        # （include_hook=False / 无钩子时，第一段就是正文，推近会推错地方）。
        _hook = decision.get("hook") or {}
        _kr = decision.get("keepRanges") or []
        hook_first = bool(_hook and _kr
                          and abs(float(_kr[0].get("startMs") or 0)
                                  - float(_hook.get("startMs") or -1)) < 1.0)

        result = render(
            Path(job["sourcePath"]), decision["keepRanges"], tmp_out,
            ass_path=ass_path, params=rparams,
            bgm=(ws.music_dir / job["bgm"]) if job.get("bgm") else None,
            on_progress=lambda pct: self._apply_progress(job, pct),
            should_cancel=lambda: job["id"] in self._cancel_ids,
            hook_zoom=hook_first,
        )

        cancelled = job["id"] in self._cancel_ids
        self._cancel_ids.discard(job["id"])
        with self._lock:
            job["vramUsedMib"] = result.vram_used_mib
        if ass_path is not None and ass_path.parent.is_dir():
            # 只收 ass/ 这一层（成片临时文件在它上一级，绝不在这里动）
            try:
                ass_path.unlink(missing_ok=True)
                ass_path.parent.rmdir()
            except OSError:
                pass

        if cancelled or not result.ok:
            # 半成品只删**本次的临时文件**；同名老成片（existed_before）一个字节都不动
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
        if cancelled:
            msg = "已停止（未完成的文件已丢弃"
            msg += "；原有成片未动）" if existed_before else "）"
            self._finish(job, "cancelled", error=msg)
            return
        if not result.ok:
            self._append_log(job, " ｜ ".join(result.errors[:2]))
            self._finish(job, "error",
                         error=" ｜ ".join(result.errors[:2]) or "渲染失败")
            return
        # 成功才换到正式名（老成片在这一刻被替换，之前一直是完整的旧文件）
        try:
            import shutil as _sh
            _sh.move(str(tmp_out), str(out_path))     # 跨盘也能用（同盘实际是 rename）
        except OSError as exc:
            try:
                tmp_out.unlink(missing_ok=True)
            except OSError:
                pass
            self._finish(job, "error", error=f"成片落盘失败：{exc}")
            return
        finally:
            try:
                tmp_dir.rmdir()                       # 空了就收掉，别堆目录
            except OSError:
                pass

        side = out_path.with_suffix(".edit.json")
        side.write_text(json.dumps({
            "candidateName": job["candidate"],
            "candidateIdx": job["candidateIdx"],
            "coverTitle": decision.get("coverTitle", ""),
            # v3.39：小切片标题 + 综合评分 —— 列表里要靠它分清"哪个视频是哪条"（用户要求）
            "title": str(picked.get("title") or ""),
            "score": round(float((picked.get("scores") or {}).get("overall") or 0), 1),
            "strategy": job["strategy"],
            "keepRanges": decision["keepRanges"],
            "durationSec": result.duration_sec,
            "width": result.width,
            "height": result.height,
            "encoder": result.encoder,
            "fallbackUsed": result.fallback_used,
            "hasAudio": result.has_audio,
            "bgm": job.get("bgm", ""),
            # v3.157：把这次用的转场记进伴生方案 —— 剪映导出据此在草稿里放**可编辑的转场**
            # v3.168：这次渲染有没有保留开头钩子（剪映导出与二次渲染的口径记录）
            "includeHook": bool((job.get("params") or {}).get("include_hook", True)),
            "hookZoom": hook_first,               # v4.2：这次渲染钩子段有没有缓推镜头
            "transition": str((job.get("params") or {}).get("boundary_transition") or "fade"),
            "transitionSec": float((job.get("params") or {}).get("boundary_transition_sec")
                                   or 0.4),
            "fingerprint": decision_fingerprint(decision),
            "cueCount": len(decision.get("subtitleCues") or []),
            "renderedAt": round(time.time(), 1),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        with self._lock:
            job["percent"] = 100.0
            job["stage"] = "done"
            job["output"] = str(out_path)
            job["outputPath"] = str(out_path)
            job["durationSec"] = result.duration_sec
            job["encoder"] = result.encoder
            job["width"] = result.width
            job["height"] = result.height
        self._finish(job, "done")
        self.log(f"[renderer] 完成 {job['id']}：{out_name} "
                 f"{result.duration_sec}s / {result.width}x{result.height} "
                 f"/ {result.encoder}")
        if job.get("manualRanges"):
            # 手动剪辑成片已落 edits：草稿**不再删**（v3.24，见 K55）——
            # 草稿里除了 keep 段，还存着六个工具的决策 / 封面位置 / 重新识别的字幕。
            # 「渲染完就删草稿」在只存 keep 段时没问题，现在会把下一次
            # 「保存并覆盖成片」所需的全部调整一起抹掉。这里改成打"已出片"标记。
            try:
                from . import cut as cut_engine
                cut_engine.mark_applied(job["candidate"],
                                        int(job["candidateIdx"] or 0), out_name)
            except (ValueError, TypeError, OSError) as exc:
                self.log(f"[renderer] {job['id']} 标记草稿已出片失败：{exc}")

    def _apply_progress(self, job: dict, pct: float) -> None:
        with self._lock:
            job["percent"] = round(min(max(float(pct), 0.0), 99.0), 1)
            job["stage"] = "rendering"

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
                job["message"] = "渲染完成"
            elif status != "cancelled":
                job["message"] = error or "任务结束"
        self._save()


# ---------------------------------------------------------------- 自检入口

def _selftest() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="渲染调度器自检")
    parser.add_argument("--candidate", required=True, help="候选产物 stem")
    parser.add_argument("--idx", type=int, default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument("--strategy", default=STRATEGY_CONSERVATIVE)
    args = parser.parse_args()

    mgr = RenderManager(log=lambda m: print(m, flush=True))
    mgr.start()
    code, payload = mgr.submit(candidate=args.candidate, idx=args.idx,
                               source=args.source, strategy=args.strategy)
    if code != 200:
        print(f"提交失败 {code}: {payload}")
        return 2
    jid = payload["job"]["id"]
    while True:
        _, st = mgr.status(jid)
        job = st["job"]
        print(f"  {job['percent']:6.2f}% {job['stage']:12s} {job['message']}")
        if job["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(1.0)
    mgr.stop()
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0 if job["status"] == "done" else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
