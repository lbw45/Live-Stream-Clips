#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回环访问诊断：K16（代理环境变量拖慢/挂死本地请求）的回归工具。

背景：``urllib.request.urlopen`` 默认读取 ``getproxies()``。在设了
``HTTP_PROXY`` / ``HTTPS_PROXY`` 的机器上（企业代理、抓包工具、CI 沙箱），
连 ``http://127.0.0.1:xxxx`` 也会被送进代理，健康检查从 <20ms 劣化成数秒
甚至永久挂起。本项目所有回环请求都必须走 :func:`_loopback_opener`。

本脚本输出各步骤毫秒数，用于确认：
    1. 代理环境变量是否存在（仅报告，不一定有害）；
    2. 直连（proxy bypass）与走代理的耗时差异；
    3. ``probe_services()`` 的实际耗时是否在 <200ms 的健康区间。

用法：
    python tools/diagnose_loopback.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common import runtime_probe as rp  # noqa: E402
from app.common.paths import ws  # noqa: E402

REPORT = Path(os.environ.get("YJ_DIAG_REPORT")
              or Path(__file__).resolve().parent / "reports" / "diagnose_loopback.json")


def ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 1)


def main() -> int:
    out: dict = {
        "workspace": str(ws.root),
        "proxy_env": {k: v for k, v in os.environ.items() if "proxy" in k.lower()},
        "getproxies": urllib.request.getproxies(),
        "steps": {},
    }
    steps: dict = out["steps"]

    t = time.perf_counter()
    ws.ensure_dirs()
    steps["ensureDirs"] = ms(t)

    t = time.perf_counter()
    ports = rp.load_ports()
    steps["loadPorts"] = ms(t)
    out["ports"] = {k: v for k, v in (ports or {}).items() if k != "sessionToken"}

    if not ports:
        out["verdict"] = "无可用的 local-service-ports.json —— 请先运行 orchestrator.py start"
        write(out)
        return 1

    editor, slicer = ports["editorPort"], ports["slicerPort"]

    # 裸 TCP：区分「网络栈开销」与「HTTP 库开销」
    for name, port in (("slicer", slicer), ("editor", editor)):
        t = time.perf_counter()
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.close()
            steps[f"rawConnect_{name}"] = ms(t)
        except OSError as exc:
            steps[f"rawConnect_{name}"] = f"{type(exc).__name__}: {exc}"

    # 直连（本项目采用的方式）
    for name, url in (
        ("slicer", f"http://127.0.0.1:{slicer}/api/health"),
        ("editor", f"http://127.0.0.1:{editor}/api/editor/health"),
    ):
        t = time.perf_counter()
        payload = rp.http_json(url)
        steps[f"direct_{name}"] = ms(t)
        out[f"{name}Ok"] = payload is not None

    # 走默认 opener（受代理影响）——仅作对照，业务代码不使用
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{editor}/api/editor/health", timeout=3) as resp:
            resp.read()
        steps["viaDefaultOpener_editor"] = ms(t)
    except Exception as exc:            # noqa: BLE001
        steps["viaDefaultOpener_editor"] = f"{type(exc).__name__}: {exc}"

    t = time.perf_counter()
    result = rp.probe_services(with_timing=True)
    steps["probeServices"] = ms(t)
    out["probeTiming"] = result.get("timing")
    out["bothAlive"] = bool(result["slicer"]["alive"] and result["editor"]["alive"])

    total = steps["probeServices"]
    if not out["bothAlive"]:
        out["verdict"] = "服务未就绪（API 不可达）"
    elif isinstance(total, (int, float)) and total < 200:
        out["verdict"] = f"健康：probe_services {total}ms（<200ms 区间）"
    else:
        out["verdict"] = f"异常：probe_services {total}ms 偏慢，检查代理环境变量与 K16"

    write(out)
    print(out["verdict"], flush=True)
    return 0 if out["bothAlive"] else 1


def write(out: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
