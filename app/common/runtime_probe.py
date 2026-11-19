#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时依赖探测（editor 与 slicer 共用）。

对应文档 §9.3 的验证口径：Python / FFmpeg / ffprobe / CUDA / Whisper 模型。
只读、不抛异常，任何一项缺失都只体现为 ``ok=False``。
"""
from __future__ import annotations

from pathlib import Path

from app.common.paths import ws


def _item(label: str, path: Path, required: bool = True, hint: str = "") -> dict:
    return {
        "label": label,
        "path": str(path),
        "ok": path.exists(),
        "required": required,
        "hint": hint,
    }


def probe_runtime() -> list[dict]:
    """返回运行时依赖就绪清单。"""
    ws.ensure_dirs()
    return [
        _item("Python 运行时", ws.python_exe, hint="工作区自带的 3.12 运行时"),
        _item("FFmpeg", ws.ffmpeg, hint="录制与渲染依赖"),
        _item("ffprobe", ws.ffprobe, hint="流探测与素材分析"),
        _item("CUDA 运行时", ws.cuda_dir / "cublas64_12.dll",
              hint="GPU 转写加速；缺失则回退 CPU"),
        _item("Whisper small", ws.models_dir / "small" / "model.bin",
              hint="标准识别档（默认）"),
        _item("Whisper medium", ws.models_dir / "medium" / "model.bin",
              required=False, hint="高精度档，可选"),
    ]


def runtime_summary() -> dict:
    items = probe_runtime()
    missing = [i["label"] for i in items if i["required"] and not i["ok"]]
    return {
        "ready": not missing,
        "missing": missing,
        "items": items,
    }


def probe_system() -> dict:
    """系统级信息（磁盘水位等，供侧栏显示剩余空间）。"""
    import shutil

    info: dict = {"workspace": str(ws.root)}
    try:
        usage = shutil.disk_usage(str(ws.root))
        info["disk"] = {
            "totalGb": round(usage.total / 1024 ** 3, 1),
            "usedGb": round(usage.used / 1024 ** 3, 1),
            "freeGb": round(usage.free / 1024 ** 3, 1),
            "freePercent": round(usage.free / usage.total * 100, 1),
            # A6：剩余 <50GB 触发告警并停止新录制
            "low": usage.free < 50 * 1024 ** 3,
        }
    except OSError as exc:
        info["disk"] = {"error": str(exc)}
    return info


# ---------------------------------------------------------------- 服务探测

def load_ports() -> dict | None:
    """读取并校验 ``local-service-ports.json``（与启动编排使用同一份凭据）。"""
    import json

    path = ws.ports_path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slicer_port = int(data["slicerPort"])
        editor_port = int(data["editorPort"])
        token = str(data["sessionToken"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (1024 <= slicer_port <= 65535 and 1024 <= editor_port <= 65535
            and slicer_port != editor_port and len(token) >= 32):
        return None
    return {"slicerPort": slicer_port, "editorPort": editor_port,
            "sessionToken": token}


def http_json(url: str, token: str | None = None, timeout: float = 1.5) -> dict | None:
    """GET 一个 JSON 端点，任何失败都返回 None（探测场景不需要区分失败原因）。

    ⚠️ K16：**必须显式禁用代理**。
    ``urllib.request.urlopen`` 会用 ``getproxies()`` 的结果，在设了
    ``HTTP_PROXY``/``HTTPS_PROXY`` 的机器上（企业代理、抓包工具、沙箱环境）
    会把 ``http://127.0.0.1:xxxx`` 也送进代理，导致本地健康检查从 <20ms
    劣化到数秒乃至永久挂起。本地应用自己访问自己的回环服务，必须直连。
    """
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Session-Token", token)
    try:
        with _loopback_opener().open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


_OPENER = None


def _loopback_opener():
    """返回一个「不走任何代理」的全局 opener（惰性构造，线程安全由 GIL 保证）。"""
    global _OPENER
    if _OPENER is None:
        import urllib.request

        # ProxyHandler({}) 表示「已知的代理集合为空」→ 一律直连
        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def service_request(port: int, token: str, method: str, path: str,
                    body: dict | None = None, timeout: float = 30.0) -> dict:
    """向本地服务发一个 JSON 请求（桌面 UI 桥代理用）。

    与 :func:`http_json` 的区别：
    ① 支持 GET/POST/PUT/DELETE 与 JSON 请求体；
    ② **不吞错** —— HTTP 状态码原样带回（UI 需要区分 401/404/409/500）；
    ③ 连接层失败返回 ``status=0``（K26：status 0 ≈ 服务挂死/未启动，
       与 4xx/5xx 应用层错误是两回事，UI 要区别提示）。

    返回 ``{"status": int, "data": dict}``。
    """
    import json
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}{path}"
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method=method.upper())
    request.add_header("X-Session-Token", token)
    if payload is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with _loopback_opener().open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return {"status": response.status,
                    "data": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:   # 4xx/5xx：应用层响应，原样带回
        try:
            raw = exc.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        return {"status": exc.code, "data": data}
    except (urllib.error.URLError, OSError) as exc:
        return {"status": 0, "data": {"error": f"连接失败：{exc}"}}


def probe_services(with_timing: bool = False) -> dict:
    """探测本地双服务的存活与健康（桌面启动页与设置页用）。

    ``with_timing=True`` 时额外返回 ``timing`` 字段（各步毫秒数），
    用于排查「本地回环请求为何变慢」（K16：代理环境变量会拖慢 urllib）。
    """
    import time

    def _ms(since: float) -> float:
        return round((time.perf_counter() - since) * 1000, 1)

    t_all = time.perf_counter()
    timing: dict = {}

    t0 = time.perf_counter()
    ports = load_ports()
    timing["loadPorts"] = _ms(t0)

    result: dict = {
        "portsPath": str(ws.ports_path),
        "ports": ports,
        "slicer": {"name": "素材切片服务", "url": "", "alive": False, "health": None},
        "editor": {"name": "剪辑工作台", "url": "", "alive": False, "health": None},
    }
    if not ports:
        if with_timing:
            timing["total"] = _ms(t_all)
            result["timing"] = timing
        return result

    slicer_url = f"http://127.0.0.1:{ports['slicerPort']}"
    editor_url = f"http://127.0.0.1:{ports['editorPort']}"
    result["slicer"]["url"] = slicer_url
    result["editor"]["url"] = editor_url

    t0 = time.perf_counter()
    payload = http_json(f"{slicer_url}/api/health")
    timing["slicerHealth"] = _ms(t0)
    if payload:
        result["slicer"]["alive"] = True
        result["slicer"]["health"] = payload

    t0 = time.perf_counter()
    payload = http_json(f"{editor_url}/api/editor/health")
    timing["editorHealth"] = _ms(t0)
    if payload:
        result["editor"]["alive"] = True
        result["editor"]["health"] = payload

    if with_timing:
        timing["total"] = _ms(t_all)
        result["timing"] = timing
    return result

