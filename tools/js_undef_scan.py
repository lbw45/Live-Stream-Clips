# -*- coding: utf-8 -*-
"""扫描 HTML 内联 JS 里「**调用了但没定义**」的函数名。

为什么有这个工具（v4.80 教训）：
  封面设计子窗点击 AI 候选时我调了 `paintEmojis()` —— 真名是 `paintEmoji`。
  当时 **node 语法检查绿、check_ui 绿、smoke_ui_wiring 971 条绿**，因为那段代码只在
  「点了 AI 候选」这条分支上跑；真点一下才炸 `ReferenceError: paintEmojis is not defined`。
  ⇒ 大单文件前端（designer.html 这类自包含页面）值得做一次「名字级」静态比对。

判定方式（保守，宁漏不误报）：
  · 只扫 `<script>` 内联块（带 `src=` 的外链跳过）；
  · 用**状态机**剥掉注释与字符串/模板串（正则剥会把注释里的英文撇号当引号，把大段代码吃掉 ——
    第一版就踩了：`boot`/`loadDesign` 被误报成未定义）；
  · 定义 = `function X(` / `const|let|var X =` / `window.X =`；
  · 参数 = `function (a, b)`、`(a, b) =>`、`catch (e)` 里的名字（回调形参常被当函数调）；
  · 调用 = 前面不是 `.`/字母/数字/$ 的 `X(`；
  · 白名单 = JS 内建 + DOM 全局（WHITELIST）。

用法：
    python tools/js_undef_scan.py app/common/assets/cover/designer.html
"""
import io
import re
import sys

WHITELIST = {
    # 关键字 / 运算符
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "delete",
    "void", "in", "of", "do", "else", "throw", "await", "async", "yield", "super",
    # 全局对象与内建
    "window", "document", "Math", "JSON", "String", "Number", "Boolean", "Object",
    "Array", "Promise", "Set", "Map", "WeakMap", "Date", "Event", "MouseEvent",
    "KeyboardEvent", "CustomEvent", "Image", "RegExp", "Error", "TypeError",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "alert", "confirm", "prompt", "fetch", "localStorage", "sessionStorage",
    "Intl", "Uint8Array", "Blob", "URL", "FileReader", "Audio", "Function",
    # 常见浏览器全局方法（不是本项目定义的）
    "function", "getComputedStyle", "matchMedia", "getSelection", "URLSearchParams",
    "AbortController", "structuredClone", "queueMicrotask", "atob", "btoa",
    "encodeURI", "decodeURI", "escape", "unescape", "navigator", "screen",
    "performance", "history", "location", "open", "close", "postMessage",
}


_REGEX_PREV = set("(=:[!&|?{};,+-*%~^<>")


def strip_js(js: str) -> str:
    """剥掉 JS 注释、字符串/模板串与**正则字面量**，只留代码骨架（保留换行以便定位）。

    ⚠️ 正则字面量必须单独处理：`replace(/[&<>"']/g, …)` 里那个 `"` 会把状态机
    带进「字符串」状态，之后整段代码被当成字符串吃掉（第一版就踩了，见 K175 旁注）。
    """
    out: list[str] = []
    i, n = 0, len(js)
    state = "code"          # code | line | block | sq | dq | tpl | regex
    last_sig = ""           # 上一个有意义的字符（判 `/` 是除号还是正则）
    while i < n:
        ch = js[i]
        nxt = js[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if ch == "/" and (last_sig == "" or last_sig in _REGEX_PREV):
                state = "regex"          # 操作数位置上的 `/` ⇒ 正则字面量
                out.append(" ")
                i += 1
                continue
            if ch in "'\"":
                state = "sq" if ch == "'" else "dq"
                out.append(" ")
                i += 1
                continue
            if ch == "`":
                state = "tpl"
                out.append(" ")
                i += 1
                continue
            out.append(ch)
            if not ch.isspace():
                last_sig = ch
            i += 1
            continue
        if state == "line":
            if ch == "\n":
                state = "code"
                out.append("\n")
            i += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                out.append(" ")
                continue
            if ch == "\n":
                out.append("\n")
            i += 1
            continue
        if state == "regex":
            # 正则字面量：吃到未被转义的 `/`（`[...]` 里的 `/` 不算），再跳过 flags
            if ch == "\\":
                i += 2
                continue
            if ch == "[":
                i += 1
                while i < n and js[i] != "]":
                    if js[i] == "\\":
                        i += 1
                    i += 1
                i += 1
                continue
            if ch == "/":
                state = "code"
                out.append(" ")
                last_sig = "/"        # 正则后面可能紧跟 `.test` 之类
                i += 1
                while i < n and js[i].isalpha():
                    i += 1
                continue
            if ch == "\n":
                out.append("\n")
            i += 1
            continue
        # 字符串 / 模板：吃掉直到同种引号（跳过转义）
        if ch == "\\":
            i += 2
            continue
        if (state == "sq" and ch == "'") or (state == "dq" and ch == '"') or (state == "tpl" and ch == "`"):
            state = "code"
            out.append(" ")
        elif ch == "\n":
            out.append("\n")
        i += 1
    return "".join(out)


_DEF_FN = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(")
_DEF_VAR = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
_DEF_WIN = re.compile(r"window\.([A-Za-z_$][\w$]*)\s*=")
_PARAMS_FN = re.compile(r"function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)")
_PARAMS_ARROW = re.compile(r"\(([^()]*)\)\s*=>")
_PARAMS_CATCH = re.compile(r"catch\s*\(([^)]*)\)")
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def undefined_functions(html: str) -> list[str]:
    """返回「调用了但没定义、也不在白名单」的名字（已排序）。"""
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html)
    js = strip_js("\n".join(blocks))
    defined = set(_DEF_FN.findall(js)) | set(_DEF_VAR.findall(js)) | set(_DEF_WIN.findall(js))
    params: set[str] = set()
    for rx in (_PARAMS_FN, _PARAMS_ARROW, _PARAMS_CATCH):
        for m in rx.finditer(js):
            params |= {p.strip().split("=")[0].strip() for p in m.group(1).split(",")}
    params.discard("")
    calls = set(_CALL.findall(js))
    return sorted(calls - defined - params - WHITELIST)


def main(argv: list[str]) -> int:
    if not argv:
        print("用法: python tools/js_undef_scan.py <file.html> [...]")
        return 2
    bad = 0
    for path in argv:
        html = io.open(path, encoding="utf-8").read()
        undef = undefined_functions(html)
        print(f"{path}: " + ("OK" if not undef else "未定义 → " + ", ".join(undef)))
        bad += 1 if undef else 0
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
