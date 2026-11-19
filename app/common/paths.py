#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一路径与运行环境配置。

设计原则（对应文档 §2.4 / §7.5）：
- 业务代码**禁止硬编码路径**，一律从本模块取；
- 工作区根可由环境变量 ``JACK_WORKSPACE_ROOT`` 覆盖，默认仓库同级的 ``workspace`` 目录；
- 子进程环境由 :func:`subprocess_env` 统一注入，保证临时文件不落 C 盘（R2 / D16）；
- CUDA 运行时用工作区自带的 ``runtime\\cuda12``（D20），不依赖系统安装。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------- 常量

APP_NAME = "Live Stream Clips"            # 产品名（界面与标题一律用它）
APP_TITLE = "Live Stream Clips"
APP_ID = "SlicerWorkbench"         # 内部标识（用于单实例锁与环境变量前缀）
# 版本号：跟随功能迭代同步更新
# （用户 2026-09-22 要求「版本号同步当前项目进度的版本号」，不单独搞语义化版本）
APP_VERSION = "5.7"   # 与文档版本同步（升版必须两处一起动，见 §4.1）

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 仓库根
# 工作区默认放在仓库同级的 workspace 目录；可用环境变量覆盖（见 Workspace.__init__）
DEFAULT_WORKSPACE = PROJECT_ROOT.parent / "workspace"

# 业务流水线目录名
# ⚠️ D25 / K24：一律用 **ASCII 英文**。
#  ① K8：临时目录在中文路径下会让 faster-whisper 的 av 库读 WAV 失败；
#  ② K18：.bat 按 GBK 读字节，中文会乱码；
#  ③ 原版用中文名（直播素材/切片/…），我们刻意改为英文，避免上述两类静默故障。
# 语义对照：live_raw=直播素材 clips=切片 edits=成品 exports=最终成片
#          music=音乐素材 backgrounds=游戏背景素材 runtime_data=运行数据
DIR_LIVE_SOURCE = "live_raw"
DIR_SLICE = "clips"
DIR_EDITOR_OUTPUT = "edits"
DIR_FINAL_OUTPUT = "exports"
DIR_MUSIC = "music"
DIR_UPLOADS = "uploads"           # 用户自己上传的素材（素材库「我的上传」分类）
DIR_BACKGROUND = "backgrounds"
DIR_RUNTIME_DATA = "runtime_data"
DIR_RECORDS = "records"          # 录制原始分片（中间产物，已存在于工作区）
# 两个服务各自的运行目录（服务名与目录名一致）
DIR_SLICER_RUNTIME = "slicer"
DIR_EDITOR_RUNTIME = "editor"
DIR_TRANSCRIPT = "transcripts"   # 转写产物（SRT/JSON），落在 runtime_data\slicer\ 下
DIR_CANDIDATES = "candidates"    # 切片候选（元数据 JSON，不是成片），同上


# ---------------------------------------------------------------- 工作区

class Workspace:
    """工作区路径集合。解析顺序：显式参数 > 环境变量 > D7 默认值。"""

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        candidate = (
            root
            or os.environ.get("JACK_WORKSPACE_ROOT")
            or os.environ.get(f"{APP_ID.upper()}_WORKSPACE_ROOT")
            or DEFAULT_WORKSPACE
        )
        self.root = Path(candidate).expanduser().resolve()

    # --- 运行时组件 ---
    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def python_dir(self) -> Path:
        return self.runtime / "python"

    @property
    def python_exe(self) -> Path:
        return self.python_dir / "python.exe"

    @property
    def cuda_dir(self) -> Path:
        return self.runtime / "cuda12"

    @property
    def models_dir(self) -> Path:
        return self.runtime / "models"

    @property
    def chromium_dir(self) -> Path:
        return self.runtime / "chromium"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def ffmpeg(self) -> Path:
        return self.bin_dir / "ffmpeg.exe"

    @property
    def ffprobe(self) -> Path:
        return self.bin_dir / "ffprobe.exe"

    # --- 业务流水线 ---
    @property
    def live_source_dir(self) -> Path:
        return self.root / DIR_LIVE_SOURCE

    @property
    def slice_dir(self) -> Path:
        return self.root / DIR_SLICE

    @property
    def editor_output_dir(self) -> Path:
        return self.root / DIR_EDITOR_OUTPUT

    @property
    def final_output_dir(self) -> Path:
        return self.root / DIR_FINAL_OUTPUT

    @property
    def music_dir(self) -> Path:
        return self.root / DIR_MUSIC

    @property
    def uploads_dir(self) -> Path:
        """**我自己上传的素材**（素材库「我的上传」分类，唯一可由用户直接写入的分类）。"""
        return self.root / DIR_UPLOADS

    @property
    def background_dir(self) -> Path:
        return self.root / DIR_BACKGROUND

    @property
    def records_dir(self) -> Path:
        """录制的**原始分片**落点（合并后的小时文件才进 ``live_source_dir``）。

        分片是中间产物，与 `live_raw`（给用户看的成片）分开，避免污染素材库。
        """
        return self.root / DIR_RECORDS

    # --- 运行数据（runtime_data：状态/DB/日志/临时；与 runtime 运行时组件区分）---
    @property
    def runtime_data(self) -> Path:
        return self.root / DIR_RUNTIME_DATA

    @property
    def slicer_runtime(self) -> Path:
        return self.runtime_data / DIR_SLICER_RUNTIME

    @property
    def editor_runtime(self) -> Path:
        return self.runtime_data / DIR_EDITOR_RUNTIME

    @property
    def transcripts_dir(self) -> Path:
        """转写产物目录（``<视频名>.srt`` / ``.json`` + ``index.json`` 任务索引）。"""
        return self.slicer_runtime / DIR_TRANSCRIPT

    @property
    def candidates_dir(self) -> Path:
        """切片**候选元数据**目录（``<视频名>.candidates.json``）。

        注意与 :attr:`slice_dir`（`clips\\`，真正的切片视频文件）区分：
        这里只放「AI 选出来的候选区间 + 评分 + 文案」，成片在 M4 渲染时才会落 `clips\\`。
        """
        return self.slicer_runtime / DIR_CANDIDATES

    @property
    def publisher_data(self) -> Path:
        """发布执行器的浏览器数据目录（**登录态就存在这里的 profile\\**）。

        ⚠️ K44 实测：这里早先指向 ``editor_runtime/publisher_data``，而发布执行器
        （``PlaywrightDriver``）用的是 ``root/publisher_data`` —— **两个目录不是同一个**，
        于是设置页永远显示「还没登录过」，而用户其实早就扫码登录了。
        目录常量只能有一个出处，各处一律走这里。
        """
        return self.root / "publisher_data"

    @property
    def publisher_profile(self) -> Path:
        """**浏览器用户数据目录**（= 抖音登录态所在的那份 profile，v3.69）。

        为什么连这个子目录也要单独收在 paths：
        发布执行器（`PlaywrightDriver`）用它开着系统 Edge 保登录态；
        v3.69 起**发布以外的模块也复用同一份 profile 取抖音 cookie** ——
        两边各拼一次 `publisher_data / "profile"` 就会漂移成两份登录态
        （K44 的老伤：目录常量只能有一个出处）。
        浏览器开着时该目录是锁住的，取 cookie 的实现必须能如实报"被占用"。
        """
        return self.publisher_data / "profile"

    @property
    def webview_data(self) -> Path:
        """WebView2 用户数据目录。

        ⚠️ K12：pywebview 默认写到 ``%LOCALAPPDATA%\\Temp``，会违反 R2（C 盘禁令）。
        必须通过 ``webview.start(storage_path=...)`` 显式指向工作区。
        """
        return self.editor_runtime / "webview_data"

    @property
    def db_path(self) -> Path:
        return self.editor_runtime / "editor.db"

    @property
    def cover_assets_dir(self) -> Path:
        """发布封面设计素材（v4.76）：fonts/（OFL 字体）+ emoji/（Noto Emoji 128px PNG，Apache 2.0）。"""
        return self.root / "assets" / "cover"

    def cover_json_for(self, video) -> Path:
        """一份成片的**封面设计配置**（v4.76）。

        落在成片同目录：`edits/<stem>_cover.json`。
        与 note.json / record.json 同模式（跟素材走）。
        """
        video = Path(str(video))
        return video.with_name(video.stem + "_cover.json")

    def resolve_film(self, name: str):
        """把「成片名 / 相对路径」解析成磁盘文件；失败返回 ``None``。

        接受的形式（正斜杠或反斜杠都行）：
          · ``xxx.mp4``                          → ``edits/xxx.mp4``
          · ``edits/xxx.mp4``                    → 同上（媒体流前缀）
          · ``edits/sub/xxx.mp4``                → ``edits/sub/xxx.mp4``
        安全：只收 .mp4、禁止 ``..``、结果必须落在成片根之内且是文件。
        """
        raw = str(name or "").strip().replace("\\", "/")
        if not raw.lower().endswith(".mp4"):
            return None
        parts = [x for x in raw.split("/") if x and x != "."]
        if not parts or any(x == ".." for x in parts):
            return None
        if parts[0] == "edits":
            parts = parts[1:]
        base = self.editor_output_dir
        if not parts:
            return None
        try:
            root = base.resolve()
            cand = root.joinpath(*parts).resolve()
            if root != cand and root not in cand.parents:
                return None                          # 越界（软链接/..）一律拒
            return cand if cand.is_file() else None
        except OSError:
            return None

    @property
    def settings_path(self) -> Path:
        return self.runtime_data / "settings.json"

    @property
    def ports_path(self) -> Path:
        return self.runtime_data / "local-service-ports.json"

    @property
    def live_recorders_path(self) -> Path:
        return self.runtime_data / "live_recorders.json"

    @property
    def live_rooms_history_path(self) -> Path:
        """历史直播间（v3.25）：**单独一个文件**，不并进 live_recorders.json。

        那一份是「当前在录的录制器」，服务启动时会被整体读进内存并自动恢复监控；
        历史是被移除的房间、绝不能自动恢复，混在一起迟早会误把历史当在录（K44）。
        """
        return self.runtime_data / "live_rooms_history.json"

    @property
    def auto_monitor_path(self) -> Path:
        return self.runtime_data / "auto_monitor_state.json"

    # --- 临时与日志（R2：一律不落 C 盘）---
    @property
    def tmp_dir(self) -> Path:
        return self.runtime_data / "tmp"

    @property
    def render_tmp_dir(self) -> Path:
        return self.runtime_data / "render_tmp"

    @property
    def cut_drafts_dir(self) -> Path:
        """手动剪辑草稿（预览剪辑页的「保存草稿」），属于 slicer 运行数据。"""
        return self.slicer_runtime / "cut_drafts"

    @property
    def log_dir(self) -> Path:
        return self.runtime_data / "logs"

    # --- 行为 ---
    def ensure_dirs(self) -> None:
        """建立全部工作目录（幂等）。"""
        for path in (
            self.live_source_dir,
            self.slice_dir,
            self.editor_output_dir,
            self.final_output_dir,
            self.music_dir,
            self.background_dir,
            self.records_dir,
            self.slicer_runtime,
            self.transcripts_dir,
            self.candidates_dir,
            self.cut_drafts_dir,
            self.editor_runtime,
            self.publisher_data,
            self.webview_data,
            self.tmp_dir,
            self.render_tmp_dir,
            self.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def has_portable_runtime(self) -> bool:
        """便携运行时是否就绪（用 portable.marker 标记）。"""
        return (self.runtime / "portable.marker").is_file() and self.python_exe.is_file()

    def source_roots(self) -> list[tuple[str, Path]]:
        """**可转写 / 可切片的源根**：``[(逻辑名, 目录)]``（v3.59，）。

        唯一出处是下面（模块级）的 `SOURCE_ROOT_ATTRS`；这里是实例方法形态，
        供 ``ws.source_roots()`` 调用（各模块已按这个写法用）。
        新增素材来源时**只改 `SOURCE_ROOT_ATTRS`**，然后确认它的消费者都跟着走：
        转写校验 / `renderer.resolve_source_video` / `cut.py` 预览前缀 /
        `library._find_source_video`（详见 SSOT K103）。
        """
        return [(name, getattr(self, attr)) for name, attr in SOURCE_ROOT_ATTRS]

    def resolve(self, value: str | os.PathLike | None, fallback: Path) -> Path:
        """解析配置里的目录：空值→兜底；绝对路径→原样；相对路径→相对工作区。"""
        raw = str(value or "").strip()
        if not raw:
            return fallback
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        return (self.root / candidate).resolve()

    def subprocess_env(self, extra: dict | None = None) -> dict:
        """子进程环境：把 TEMP/TMP 等重定向到工作区，并注入运行时根。"""
        env = os.environ.copy()
        env["TEMP"] = str(self.tmp_dir)
        env["TMP"] = str(self.tmp_dir)
        env["TMPDIR"] = str(self.tmp_dir)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["JACK_WORKSPACE_ROOT"] = str(self.root)
        env["JACK_RUNTIME_ROOT"] = str(self.runtime)
        env["JACK_MODELS_ROOT"] = str(self.models_dir)
        env["JACK_CUDA_RUNTIME_DIR"] = str(self.cuda_dir)
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        if extra:
            env.update({k: str(v) for k, v in extra.items()})
        return env

    def register_cuda(self) -> bool:
        """注册工作区自带的 CUDA 运行时（D20）。缺 cublas 时返回 False，由调用方回退 CPU。"""
        if not (self.cuda_dir / "cublas64_12.dll").is_file():
            return False
        os.environ["PATH"] = str(self.cuda_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("JACK_CUDA_RUNTIME_DIR", str(self.cuda_dir))
        try:
            os.add_dll_directory(str(self.cuda_dir))   # Python 3.8+ 必须显式注册 DLL 目录
        except (AttributeError, OSError):
            pass
        return True


# 全局单例：模块级直接 `from app.common.paths import ws` 使用
ws = Workspace()


# ---------------------------------------------------------------- 辅助

def setup_import_path(extra: Path | None = None) -> None:
    """把 app 目录加入 sys.path，使 `import app.xxx` / 子系统内部导入可用。"""
    for path in (PROJECT_ROOT, PROJECT_ROOT / "app", extra):
        if path and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中（§7.2 打包时用）。"""
    return bool(getattr(sys, "frozen", False))


# ---------------------------------------------------------------- 源媒体根（唯一出处）

# 哪些目录里的视频算「可转写 / 可切片的源」，以及它们在媒体流里的**逻辑名**。
#
# 原来全项目只有 `live_raw` 一个源根，转写校验、渲染找源视频、剪辑页拼预览地址
# 三处**各自**写死了 `ws.live_source_dir`；后来「我的上传」也要进同一条链路
# （转写 → AI 切片 → 剪辑 → 渲染），就把这张表抽出来
# —— 否则又是「同一件事写三份、迟早漂移」（K44）。
#
# 逻辑名同时是：① 媒体流前缀（``/api/media/stream?kind=<name>&rel=``）
#              ② 素材库分类名（两处都注册了别名）
SOURCE_ROOT_ATTRS: tuple[tuple[str, str], ...] = (
    ("live", "live_source_dir"),
    # v3.120（用户 2026-10-21）：「转写的位置新增一个选择视频文件的功能」——
    # 本机下载的直播录屏 / 切片视频先经素材库「导入素材」进 uploads（复用既有通道），
    # 再走既有转写提交。uploads 成为源根后，K103 点名的四个消费者跟着走：
    # 转写校验（本功能）/ renderer.resolve_source_video / cut.py 预览前缀（早已有 uploads 条目）/
    # library._find_source_video。source_roots() 的消费者只有转写与 renderer（已盘点）。
    ("uploads", "uploads_dir"),
)


# ⚠️ 两个入口都要提供（**这不是重复实现，是兼容**）：
# ``Workspace.source_roots()`` —— 实例方法，供 ``ws.source_roots()`` 调用。
#     各模块（transcriber / renderer / library / cut）**已经这么写了**；
# 模块级 ``source_roots()`` —— 供 `source_kind_of()` 内部与不方便拿 ws 的场合用。
# v3.59 教训：第一版只写了模块级函数，而调用处写的是 `ws.source_roots()`
#   → **AttributeError**（renderer / transcriber 直接炸，library 被 try/except 吞掉退化成
#   "只扫 live_raw"）。**回归冒烟当场抓到**（smoke_cut 16/17、smoke_render 98/114、
#   smoke_transcribe 9/24）—— 又一次「只跑自己新写的测试不够，被改动路径的既有冒烟必须全跑」。
def source_roots() -> list[tuple[str, Path]]:
    """``[(逻辑名, 绝对目录)]`` —— 转写/渲染/预览判断源文件归属的唯一依据。"""
    return ws.source_roots()


def source_kind_of(path: str | os.PathLike) -> str:
    """某个媒体文件属于哪个源根（返回逻辑名）；都不属于时返回空串。

    ⚠️ 比较前一律 ``resolve()``：工作区可能经 junction / 短路径访问（本机 ``hc`` 就在 G 盘），
    字面路径不同不代表文件不同。
    """
    target = Path(path).resolve()
    for name, root in source_roots():
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue
        return name
    return ""


def asset_root() -> Path:
    """静态资源根目录：打包后在 _MEIPASS，开发时在项目根。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return PROJECT_ROOT
