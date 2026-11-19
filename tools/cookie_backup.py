#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四平台 cookie 备份与注入（v4.54，用户：四个平台的 cookie 都保存备份用作测试时登录用）。

用法（项目 Python：hc/runtime/python/python.exe）：
  备份  tools/cookie_backup.py save        # 需要 profile 里的登录态；开一次无头浏览器导出
  查看  tools/cookie_backup.py list        # 各平台备份的 cookie 数与时间
  注入  在测试/取证脚本里：
        from tools.cookie_backup import inject
        inject(ctx, "kuaishou")            # ctx = browser.new_context(...) 后调用
备份位置：hc/runtime_data/cookie_backup/<platform>.json（另有一份 all_cookies.json 全量）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.common.paths import ws                                        # noqa: E402

BACKUP = Path(ws.runtime_data) / "cookie_backup"
GROUPS = {
    "douyin": ("douyin.com", "iesdouyin", "zjcdn"),
    "kuaishou": ("kuaishou.com", "kwaixiaodian", "gifshow"),
    "bilibili": ("bilibili.com", "bilivideo"),
    "xiaohongshu": ("xiaohongshu.com", "xhscdn"),
}


def path_of(platform: str) -> Path:
    return BACKUP / f"{platform}.json"


def inject(ctx, platform: str) -> int:
    """把备份的 cookie 注入一个 browser context（登录态恢复）。"""
    p = path_of(platform)
    if not p.is_file():
        raise FileNotFoundError(f"{platform} 没有备份（先 save）")
    cookies = json.loads(p.read_text(encoding="utf-8"))
    ctx.add_cookies(cookies)
    return len(cookies)


def save_now() -> dict:
    """开一次无头浏览器把 profile 里**现有的**登录态导出（不新登录）。"""
    from playwright.sync_api import sync_playwright
    BACKUP.mkdir(parents=True, exist_ok=True)
    summary = {}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(ws.publisher_profile), channel="msedge", headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        try:
            allc = ctx.cookies()
            for plat, domains in GROUPS.items():
                hits = [c for c in allc
                        if any(d in (c.get("domain") or "") for d in domains)]
                path_of(plat).write_text(json.dumps(hits, ensure_ascii=False), encoding="utf-8")
                summary[plat] = len(hits)
            (BACKUP / "all_cookies.json").write_text(
                json.dumps(allc, ensure_ascii=False), encoding="utf-8")
        finally:
            ctx.close()
    return summary


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "save":
        print(json.dumps(save_now(), ensure_ascii=False))
        return 0
    out = {}
    for plat in GROUPS:
        p = path_of(plat)
        if p.is_file():
            n = len(json.loads(p.read_text(encoding="utf-8")))
            out[plat] = {"cookies": n, "mtime": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))}
        else:
            out[plat] = {"cookies": 0}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
