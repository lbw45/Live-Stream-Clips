# -*- coding: utf-8 -*-
"""Build the Windows installer for Live Stream Clips.

Steps:
  1. staging  = build/staging  (app / launcher / docs)
  2. runtime_env = staging/runtime_env  (portable python + deps, from workspace)
  3. makensis build/installer.nsi  ->  build/LiveStreamClips-Setup-<ver>.exe

Usage:
  python build/make_installer.py            # full build
  python build/make_installer.py --no-rt    # reuse staging/runtime_env from last run
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGING = os.path.join(ROOT, "build", "staging")
RUNTIME_SRC = os.path.join(ROOT, "workspace", "runtime", "python")
MAKENSIS_CANDS = [
    r"C:\Program Files (x86)\NSIS\makensis.exe",
    r"C:\Program Files\NSIS\makensis.exe",
]

EXCLUDE_DIRS = {"__pycache__", ".git", "node_modules"}
EXCLUDE_EXT = {".pyc", ".pyo"}


def copy_tree(src, dst, skip_pyc=True):
    n = 0
    for dp, dn, fns in os.walk(src):
        dn[:] = [d for d in dn if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(dp, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for fn in fns:
            if skip_pyc and os.path.splitext(fn)[1].lower() in EXCLUDE_EXT:
                continue
            shutil.copy2(os.path.join(dp, fn), os.path.join(target, fn))
            n += 1
    return n


def dir_size_mb(p):
    return sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(p) for f in fs) / 1024 / 1024


BUILD_LOCK = os.path.join(ROOT, "build", ".build.lock")


def _acquire() -> bool:
    """只允许一个构建在跑：两个 makensis 同时读 staging 会产出 CRC 坏的安装包。"""
    if os.path.isfile(BUILD_LOCK):
        try:
            age = time.time() - os.path.getmtime(BUILD_LOCK)
        except OSError:
            age = 0
        if age < 3600:
            print("FATAL: 已有构建在跑（build/.build.lock，%.0f 分钟前），等它结束或删掉锁文件" % (age / 60))
            return False
    open(BUILD_LOCK, "w").write(str(os.getpid()))
    return True


def main():
    t0 = time.time()
    if not _acquire():
        return 2
    no_rt = "--no-rt" in sys.argv

    # ---- find makensis ----
    mk = next((c for c in MAKENSIS_CANDS if os.path.isfile(c)), None)
    if not mk:
        print("FATAL: makensis.exe not found (install NSIS first)")
        return 1

    # ---- 1) staging: program + docs ----
    print("[1/4] staging program files ...")
    if os.path.isdir(STAGING):
        shutil.rmtree(STAGING)
    os.makedirs(STAGING)
    copy_tree(os.path.join(ROOT, "app"), os.path.join(STAGING, "app"))
    copy_tree(os.path.join(ROOT, "launcher"), os.path.join(STAGING, "launcher"))
    for f in ["README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
              "requirements.txt", ".gitignore"]:
        shutil.copy2(os.path.join(ROOT, f), os.path.join(STAGING, f))
    print(f"      app+launcher staged ({dir_size_mb(os.path.join(STAGING, 'app')):.1f} MB app)")

    # ---- 2) runtime_env ----
    rt_dst = os.path.join(STAGING, "runtime_env")
    if no_rt and os.path.isdir(rt_dst):
        print("[2/4] runtime_env reused from last run")
    else:
        if not os.path.isdir(RUNTIME_SRC):
            print("FATAL: portable runtime not found at", RUNTIME_SRC)
            return 1
        print("[2/4] copying portable runtime (469 MB, takes a few minutes) ...")
        t = time.time()
        n = copy_tree(RUNTIME_SRC, rt_dst)
        print(f"      {n} files, {dir_size_mb(rt_dst):.0f} MB, {time.time()-t:.0f}s")

    # ---- 3) makensis ----
    print("[3/4] compiling installer (LZMA solid on ~470MB, this is the long step) ...")
    t = time.time()
    r = subprocess.run([mk, os.path.join(ROOT, "build", "installer.nsi")],
                       cwd=os.path.join(ROOT, "build"),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    log = (r.stdout or "") + (r.stderr or "")
    with open(os.path.join(ROOT, "build", "makensis.log"), "w",
              encoding="utf-8", errors="replace") as f:
        f.write(log)
    if r.returncode != 0:
        print("FATAL: makensis failed (see build/makensis.log)")
        print(log[-1200:])
        return 1
    print(f"      compiled in {time.time()-t:.0f}s")

    # ---- 4) result ----
    out = os.path.join(ROOT, "build", "LiveStreamClips-Setup-1.0.1.exe")
    if not os.path.isfile(out):
        print("FATAL: installer not produced")
        return 1
    print(f"[4/4] OK  {out}")
    print(f"      {os.path.getsize(out)/1024/1024:.1f} MB, total {time.time()-t0:.0f}s")
    try:
        os.remove(BUILD_LOCK)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
