#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印后端接口清单（**只看不落文件**）。

为什么是"打印"而不是生成一份 `API-ROUTES.md`：接口清单属于**派生信息**，落成文件就等于
多了一份会漂移的副本（K44 单一出处）。真要看，跑一次命令即可。

用法（源码根下）：
    python tools/list_api_routes.py
    ... tools/list_api_routes.py --filter feed      # 只看含某个词的
"""
from __future__ import annotations

import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ("app/slicer/web_server.py", "slicer（素材切片服务，http.server）"),
    ("app/editor/backend/api/*.py", "editor（剪辑工作台，FastAPI；按文件分组）"),
]

ROUTE_RE = re.compile(r'@(?:app|router)\.(get|post|put|delete)\("([^"]+)"[^)]*\)')
PY_ROUTE_RE = re.compile(r'@route\("(GET|POST|PUT|DELETE)", "([^"]+)"\)')


def first_doc_line(lines: list[str], start: int) -> str:
    """取函数 docstring 的第一句（够用就行，不做完整解析）。"""
    for ln in lines[start + 1:start + 14]:
        t = ln.strip()
        if t.startswith('"""') or t.startswith("'''"):
            body = t.strip('"').strip("'").strip()
            if body:
                return body[:80]
            continue
    return ""


def scan(path: Path, title: str) -> list[tuple[str, str, str, int]]:
    if not path.is_file():
        return []
    text = io.open(path, encoding="utf-8").read()
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        m = PY_ROUTE_RE.search(ln) or ROUTE_RE.search(ln)
        if not m:
            continue
        method, url = m.group(1).upper(), m.group(2)
        out.append((method, url, first_doc_line(lines, i), i + 1))
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="", help="只打印路径/用途含这个词的")
    args = ap.parse_args()
    total = 0
    for rel, title in TARGETS:
        files = sorted(ROOT.glob(rel)) if "*" in rel else [ROOT / rel]
        print("=" * 78)
        print(title)
        print("=" * 78)
        for f in files:
            rows = scan(f, title)
            if args.filter:
                rows = [r for r in rows if args.filter in r[1] or args.filter in r[2]]
            if not rows:
                continue
            print("\n%s（%d 个）" % (f.relative_to(ROOT).as_posix(), len(rows)))
            for method, url, desc, line in rows:
                print("  %-6s %-46s %s" % (method, url, (desc or "")[:60]))
            total += len(rows)
    print("\n共 %d 个接口（提示：前端一律经桥，新增长任务接口要同步 launcher/desktop_entry.py 的超时分档）" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
