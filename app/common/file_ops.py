#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""目录内文件删除的**共享实现**（防路径穿越 + 逐条失败明细 + 释放空间统计）。

为什么要抽出来：录制成片、转写产物、切片候选**三处都要删**，各写一遍必然出现
「一处修了越界防护、另两处忘了」这种情况 —— 而这类漏洞是**静默**的（表现为"能删"，
不会报错）。所以规则只有一份：

1. **路径必须落在指定根目录内**（``resolve()`` + ``relative_to()``，越界即拒）；
2. **只删文件，不删目录**（目录一律拒绝，避免"删了 `unmerged/` 连带丢掉合片源"）；
3. **不存在的文件如实报 failed**（不静默吞掉，否则界面会显示"删了 N 个"而实际没删）；
4. **单个失败不影响其余**（逐条处理，返回 `deleted` / `failed` 两份明细）；
5. **额外保护名单**（如 ``index.json`` 是任务索引，不是业务产物，误删会丢任务历史）。

响应结构与既有 `POST /api/recordings/delete` **完全一致**，因此那条链路的
13 项冒烟断言可以直接当本模块的回归测试用。
"""
from __future__ import annotations

import errno
import time
from pathlib import Path

# 受保护的文件名：即使落在目录内、即使存在，也拒绝删除
PROTECTED_NAMES: frozenset[str] = frozenset({"index.json"})


def _locked(exc: OSError) -> bool:
    """是不是「文件被别的进程/句柄占用」。

    Windows 的典型表现是 ``WinError 32``（另一个程序正在使用此文件）；
    POSIX 上对应的 errno 是 EACCES / EPERM。
    """
    return getattr(exc, "winerror", None) == 32 or exc.errno in (errno.EACCES, errno.EPERM)


def delete_files_in(root: Path, rels, *, protected: frozenset[str] = PROTECTED_NAMES,
                    log=None) -> tuple[int, dict]:
    """删除 ``root`` 内的若干文件。

    ``rels`` 是**相对 root 的路径**（也可传绝对路径，只要最终落在 root 内）。
    返回 ``(http_status, payload)``，payload 形状同录制库删除接口。
    """
    root = Path(root).resolve()
    deleted: list[str] = []
    failed: list[dict] = []
    freed = 0

    for rel in (rels or []):
        rel = str(rel)
        target = (root / rel).resolve()
        try:
            target.relative_to(root)              # 越界即抛 ValueError
        except ValueError:
            failed.append({"rel": rel, "error": "路径越界，已拒绝"})
            continue
        if target.name in protected:
            failed.append({"rel": rel, "error": "受保护文件，已拒绝"})
            continue
        if target == root or target.is_dir():
            failed.append({"rel": rel, "error": "不是文件，已拒绝"})
            continue
        try:
            size = target.stat().st_size
        except OSError:
            failed.append({"rel": rel, "error": "文件不存在"})
            continue
        # v3.158（用户 2026-10-28「预览多了就删不掉」）：删之前先**掐断正在读它的媒体流**。
        # 真正的长命句柄来自 `/api/media/stream`（播放器暂停 ⇒ send 阻塞 ⇒ open 不释放），
        # 这里放在**删除的唯一入口**上兜底：不管前端有没有记得关预览，都会先松开再删。
        try:
            from app.slicer import media_streams
            media_streams.detach([target], timeout=2.6)   # 等流自己收尾（写超时 2s）
        except Exception:                                    # noqa: BLE001
            pass
        # WinError 32 的退避重试：0.15/0.3/0.45/0.7/1.0 秒（合计约 2.6s，够杀毒/抽帧松手）
        last_exc: OSError | None = None
        delays = (0.15, 0.3, 0.45, 0.7, 1.0)
        for attempt in range(len(delays) + 1):
            try:
                target.unlink()
                last_exc = None
                break
            except OSError as exc:
                last_exc = exc
                if _locked(exc) and attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                break
        if last_exc is not None:
            hint = "（文件仍被占用：先关掉预览/播放，稍等一下再删）" if _locked(last_exc) else ""
            failed.append({"rel": rel, "error": f"删除失败{hint}：{last_exc}"})
            continue
        deleted.append(rel)
        freed += size

    if log:
        log(f"删除文件 {len(deleted)} 个（失败 {len(failed)}），"
            f"释放 {freed / 1024 ** 2:.1f} MB")
    return 200, {
        "ok": not failed,
        "deleted": deleted,
        "deletedCount": len(deleted),
        "failed": failed,
        "freedBytes": freed,
        "freedMb": round(freed / 1024 ** 2, 2),
    }


def is_safe_stem(stem: str) -> bool:
    """``stem`` 必须是**纯文件名**（不含路径分隔符、不含 ``..``），防越界的第一道闸。

    用它而不是只用 :func:`delete_files_in` 的 ``relative_to`` 检查，
    是因为有些接口是「按 stem 拼出多个伴随文件」（如转写是 `.json` + `.srt`），
    必须在下手拼路径**之前**就挡住。
    """
    raw = str(stem or "").strip()
    if not raw:
        return False
    if raw in (".", "..") or ".." in raw:
        return False
    return Path(raw).name == raw and not any(sep in raw for sep in ("/", "\\"))
