# -*- coding: utf-8 -*-
"""v3.158：媒体流的「可撤销」注册表 —— 删文件前先掐断正在读它的流。

用户 2026-10-28：「预览多了，再去删除某段素材就 WinError 32 被占用，这个问题必须优化掉」。

真因链（K124 同族）：
  `<video>` 播放走 `/api/media/stream` 的 **HTTP Range 长连接**；播放器一暂停，浏览器不再读，
  TCP 发送缓冲填满 ⇒ 服务端 `send` 卡住 ⇒ 那个 `open("rb")` 的读句柄一直不释放。
  Windows 上 Python 的 `open` 不允许 delete 共享 ⇒ `unlink()` 报 WinError 32。
  v3.138 只加了「10 秒写超时」和前端关掉**当前那一条**预览，属于治标：
  用户预览过多条、或关预览后连接还没断，就照样占用。

这里的做法是**可撤销**：
  · `_serve_media` 每打开一次流就在这登记一个 :class:`Stream`；
  · 读文件改成「每 1MB 开一次、读完立刻关」——两次读之间**不存在**长命句柄；
  · 删除前调 :func:`detach`：给这些流打取消标记，并等它们真正收尾（默认最多 2 秒）。
    `file_ops.delete_files_in` 是**所有删除的必经之路**，所以任何入口（成片/素材库/候选）
    都会自动先掐流，不依赖前端记得清理。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_ACTIVE: dict[str, set["Stream"]] = {}


class Stream:
    """一次媒体流请求的把手：``cancel`` 由删除方触发，``closed`` 由读流方在收尾时置位。"""

    __slots__ = ("key", "cancel", "closed", "conn")

    def __init__(self, key: str) -> None:
        self.key = key
        self.cancel = threading.Event()
        self.closed = threading.Event()
        # 连接句柄（``_serve_media`` 里挂上来）：detach 时先 shutdown，才能把卡在 send 里的
        # 那一轮立刻踢醒 —— 只置标记的话，要等 10 秒写超时才会退出。
        self.conn = None

    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def kill(self) -> None:
        """打断这条流：置取消标记 + 关掉底层连接（让阻塞的 send 立刻抛错）。"""
        self.cancel.set()
        conn = self.conn
        if conn is None:
            return
        try:
            import socket as _socket
            conn.shutdown(_socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def _key(path: str | Path) -> str:
    # Windows 路径大小写不敏感 ⇒ 统一小写，避免 "E:\…\A.mp4" 与 "e:\…\a.mp4" 对不上
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def register(path: str | Path) -> Stream:
    st = Stream(_key(path))
    with _LOCK:
        _ACTIVE.setdefault(st.key, set()).add(st)
    return st


def unregister(st: Stream) -> None:
    with _LOCK:
        bucket = _ACTIVE.get(st.key)
        if bucket is not None:
            bucket.discard(st)
            if not bucket:
                _ACTIVE.pop(st.key, None)


def active_count(path: str | Path | None = None) -> int:
    with _LOCK:
        if path is None:
            return sum(len(v) for v in _ACTIVE.values())
        return len(_ACTIVE.get(_key(path), ()))


def detach(paths, timeout: float = 2.6) -> int:
    """给这些文件的在途流打取消标记，并等它们收尾；返回被要求中断的流个数。

    ``timeout`` 到点就返回（**不抛**）—— 调用方随后还会按退避重试 unlink，
    这里的职责只是「别让句柄继续挂着」，不是「保证已经释放」。
    """
    targets = [p for p in (paths or []) if str(p or "").strip()]
    if not targets:
        return 0
    keys = {_key(p) for p in targets}
    with _LOCK:
        streams = [st for k in keys for st in _ACTIVE.get(k, ())]
    for st in streams:
        st.kill()
    deadline = time.time() + max(float(timeout or 0), 0.0)
    for st in streams:
        left = deadline - time.time()
        if left <= 0:
            break
        st.closed.wait(left)
    return len(streams)
