#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一设置：**只读聚合视图** + 四平台账号配置（v4.18）。

为什么**不在这里重写一份 AI 配置**：
AI 服务配置（provider / baseUrl / apiKey / model）的**写入逻辑只有一份**，
在 slicer 的 ``POST /api/slice/llm-config``（含 DPAPI 加密、原子写盘、脱敏回显）。
本模块只做**只读读取**并原样透出，绝不复制那条链路 ——
「规则写两遍必然漏一处」（K33 同源教训）。

同理，**开机自启**的写入也走既有的 ``app.common.autostart``（注册表），
这里仅提供状态查询，开关动作由桌面桥 ``autostart_set`` 执行（桥已经过冒烟）。

本模块真正拥有的写操作：**四平台账号备注 + 平台真实昵称缓存**（v4.18）。
真实的登录态仍由发布执行器的 persistent browser profile 承载；昵称由
``account_info.read_account()`` 用浏览器自己读，不在这里另写一份解析规则（K44）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from app.common import autostart
from app.common.paths import APP_ID, APP_NAME, APP_VERSION, ws
from app.editor.backend import platforms

router = APIRouter()

ACCOUNT_KEYS = ("nickname", "note", "loginHint", "creatorUrl")

# v4.17（）：账号备注改为**按平台**存（发布页四张卡片各自一个账号）。
# 抖音那一份同时镜像到老的 `douyinAccount` 键 —— 老界面与老测试还按它读，不能断。
ACCOUNTS_KEY = "publishAccounts"

#: v4.28：发布时是否自动上传封面（全局一份）。v4.31 起开关**按平台**存（下面那个键），
#: 这个旧全局键只当**迁移兜底**：某平台还没单独设置过时，先看它的值——
#: v4.28 里关过全局开关的老用户，升级后未单独设置的平台仍是「关」，不会跳回默认开。
AUTO_COVER_KEY = "publishAutoCover"
#: v4.31：发布自动封面按平台（发布页四张卡片各一个独立开关）。
AUTO_COVER_PLATS_KEY = "publishAutoCoverPlatforms"


def auto_cover_enabled(platform: object = None, data: dict | None = None) -> bool:
    """某平台「发布自动上传封面」的开关；缺省/脏值一律按**开**（保持旧行为不变）。

    平台没单独设置过时回退旧全局键 ``publishAutoCover``；两处都没有 ⇒ 默认开。
    ``platform=None`` 只看旧全局键（等价 v4.28 的全局语义）。
    """
    d = data if isinstance(data, dict) else _read_settings()
    if platform is not None:
        key = platforms.normalize(platform)
        plats = d.get(AUTO_COVER_PLATS_KEY)
        if isinstance(plats, dict):
            raw = plats.get(key)
            if isinstance(raw, bool):
                return raw
    raw = d.get(AUTO_COVER_KEY)
    return raw if isinstance(raw, bool) else True


def set_auto_cover(enabled: bool, platform: object,
                   *, path: Path | None = None) -> bool:
    """持久化**单个平台**的开关；返回写入后的值。只动这一个平台键，其余设置不碰。

    注意：不清旧全局键 ``publishAutoCover``——它继续给「还没单独设置的平台」当兜底。
    """
    key = platforms.normalize(platform)
    data = _read_settings(path)
    plats = data.get(AUTO_COVER_PLATS_KEY)
    if not isinstance(plats, dict):
        plats = {}
    plats[key] = bool(enabled)
    data[AUTO_COVER_PLATS_KEY] = plats
    _write_settings(data, path)
    return bool(enabled)


#: v4.38：自动发布按平台（发布页四张卡片各一个独立开关）。
#: **缺省一律关**——R1 修订（用户 2026-11-05 授权）：只有显式打开的平台才会自动点发布。
AUTO_PUBLISH_PLATS_KEY = "publishAutoPublishPlatforms"


def auto_publish_enabled(platform: object = None, data: dict | None = None) -> bool:
    """某平台「自动发布」开关；**缺省一律按关**（R1 默认口径：不点发布）。"""
    d = data if isinstance(data, dict) else _read_settings()
    if platform is not None:
        key = platforms.normalize(platform)
        plats = d.get(AUTO_PUBLISH_PLATS_KEY)
        if isinstance(plats, dict):
            raw = plats.get(key)
            if isinstance(raw, bool):
                return raw
    return False


def set_auto_publish(enabled: bool, platform: object,
                     *, path: Path | None = None) -> bool:
    """持久化**单个平台**的自动发布开关；返回写入后的值。只动这一个平台键。"""
    key = platforms.normalize(platform)
    data = _read_settings(path)
    plats = data.get(AUTO_PUBLISH_PLATS_KEY)
    if not isinstance(plats, dict):
        plats = {}
    plats[key] = bool(enabled)
    data[AUTO_PUBLISH_PLATS_KEY] = plats
    _write_settings(data, path)
    return bool(enabled)


def _accounts_map(data: dict) -> dict:
    raw = data.get(ACCOUNTS_KEY)
    return raw if isinstance(raw, dict) else {}


def _account_raw(data: dict, platform: str) -> dict:
    acc = _accounts_map(data).get(platform)
    if not isinstance(acc, dict):
        acc = {}
    if platform == "douyin" and not acc:
        legacy = data.get("douyinAccount")      # 迁移前只有这一份
        acc = legacy if isinstance(legacy, dict) else {}
    return acc


def _read_settings(path: Path | None = None) -> dict:
    target = Path(path) if path else ws.settings_path
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_settings(data: dict, path: Path | None = None) -> None:
    target = Path(path) if path else ws.settings_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)      # 原子替换，避免写一半被读到


def ai_view() -> dict:
    """AI 服务配置的**脱敏视图**（复用 slicer 的唯一实现，这里只调用不重写）。"""
    try:
        from app.slicer.llm import load_config
        cfg = load_config()
        view = cfg.redacted()
    except Exception as exc:                                   # noqa: BLE001
        return {"configured": False, "error": f"读取 AI 配置失败：{exc}"}
    view["configured"] = bool(cfg.configured)
    return view


def account_view(platform: str = "douyin", data: dict | None = None) -> dict:
    """某平台的账号备注 + 登录态（默认抖音，保持老调用方语义）。"""
    from app.editor.backend import platforms
    spec = platforms.get(platform)
    raw = _account_raw(data or _read_settings(), spec.key)
    # 登录目录 / 登录判定的**唯一出处**都在 publisher（K44：两处各写一个路径必然不一致）
    try:
        from app.editor.backend import publisher
        prof = publisher.profile_dir(spec.key)
        login = publisher.login_state(spec.key)
    except Exception as exc:                                  # noqa: BLE001
        prof = ws.publisher_data / "profile"
        login = {"loggedIn": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "platform": spec.key,
        "platformName": spec.name,
        "creatorLabel": spec.creator_label,
        # nickname 是**人工备注名**（可选）；autoNickname 才是平台当前登录账号的真实昵称
        "nickname": str(raw.get("nickname") or ""),
        "autoNickname": str(raw.get("autoNickname") or ""),
        "autoAccountId": raw.get("autoAccountId"),
        "autoAvatar": str(raw.get("autoAvatar") or ""),
        "nicknameUpdatedAt": str(raw.get("nicknameUpdatedAt") or ""),
        "note": str(raw.get("note") or ""),
        # 自定义地址没填就给该平台注册表里的默认上传页（界面展示「会打开哪个地址」）
        "creatorUrl": str(raw.get("creatorUrl") or spec.creator_url),
        "updatedAt": str(raw.get("updatedAt") or ""),
        # 登录态由发布执行器的持久化浏览器 profile 承载；这里只告诉用户它在哪、
        # 以及是否已经存在（存在 ≠ 已登录，但至少有 profile）
        "hasBrowserProfile": prof.is_dir(),
        "profileDir": str(prof),
        "login": login,
    }


def accounts_view(data: dict | None = None) -> dict:
    """四平台账号视图（发布页头部卡片一次取全）。"""
    from app.editor.backend import platforms
    src = data or _read_settings()
    return {k: account_view(k, src) for k in platforms.PLATFORM_ORDER}


def autostart_state() -> dict:
    """开机自启状态 + 即将写入的命令（**只读**，写操作只有 app.common.autostart 一处）。"""
    try:
        return {"enabled": autostart.is_enabled(),
                "command": autostart.command_for_display()}
    except Exception as exc:                                   # noqa: BLE001
        return {"enabled": False, "error": str(exc)}


def tuning_view() -> dict:
    try:
        from app.slicer import tuning
        return {"threshold": tuning.get_score_threshold(),
                "stats": tuning.feedback_stats()}
    except Exception as exc:                                   # noqa: BLE001
        return {"threshold": None, "error": str(exc)}


# ---------------------------------------------------------------- 端点

@router.get("/summary")
def summary() -> dict:
    """设置页一次性取全：AI 服务 / 抖音账号 / 开机自启 / 调优 / 环境。"""
    return {
        "app": {"name": APP_NAME, "id": APP_ID, "version": APP_VERSION},
        "ai": ai_view(),
        "account": account_view("douyin"),          # 老消费者读它（抖音）
        "accounts": accounts_view(),                 # v4.17：四平台
        "autostart": autostart_state(),
        "tuning": tuning_view(),
        "workspace": {
            "root": str(ws.root),
            "settingsPath": str(ws.settings_path),
            "uploadsDir": str(ws.uploads_dir),
            "musicDir": str(ws.music_dir),
            "liveSourceDir": str(ws.live_source_dir),
            "editorOutputDir": str(ws.editor_output_dir),
        },
        # v3.81：结构化路径清单（带 isDir / openable），设置页据此逐行给「打开」按钮
        "paths": dirs_view(),
    }


@router.post("/account")
def save_account(payload: dict) -> dict:
    """保存**某平台**的账号备注（真实登录态仍靠浏览器 profile，这里存的是给人看的信息）。

    payload：``{"platform": "douyin|kuaishou|bilibili|xiaohongshu", "nickname": "...", "note": "..."}``
    不传 platform 时按抖音处理（老调用方兼容）。
    """
    from app.editor.backend import platforms
    plat = platforms.normalize((payload or {}).get("platform"))
    data = _read_settings()
    store = _accounts_map(data)
    acc = _account_raw(data, plat)
    for k in ACCOUNT_KEYS:
        if k in payload:
            acc[k] = str(payload.get(k) or "").strip()
    acc["updatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store[plat] = acc
    data[ACCOUNTS_KEY] = store
    if plat == platforms.DEFAULT_PLATFORM:
        data["douyinAccount"] = acc          # 老键镜像，别让老界面/老数据看空
    _write_settings(data)
    return {"ok": True, "account": account_view(plat, data)}


def save_auto_account(platform: str, info: dict) -> dict:
    """把「平台当前登录账号」的昵称/ID/头像写进设置（v4.18）。

    与人工备注 `nickname` **分开存**：备注是给人看的别名，这里是平台事实；
    界面优先显示 `autoNickname`，读不到才退回备注（K69：不拿备注冒充真实昵称）。
    """
    from app.editor.backend import platforms
    plat = platforms.normalize(platform)
    data = _read_settings()
    store = _accounts_map(data)
    acc = _account_raw(data, plat)
    acc["autoNickname"] = str(info.get("nickname") or "").strip()
    acc["autoAccountId"] = info.get("accountId")
    acc["autoAvatar"] = str(info.get("avatarUrl") or "")
    acc["nicknameUpdatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store[plat] = acc
    data[ACCOUNTS_KEY] = store
    _write_settings(data)
    return account_view(plat, data)


@router.post("/read-account")
def read_account(payload: dict) -> dict:
    """读取某平台**当前登录账号**的昵称（v4.18）。

    body：``{"platform": "...", "force": false}``。
    默认有缓存就直接回（页面一进来四张卡片不该各起一次 Edge）；``force=true`` 用于
    「换了号 / 昵称改过」时点昵称重读。
    """
    from app.editor.backend import account_info, platforms
    plat = platforms.normalize((payload or {}).get("platform"))
    force = bool((payload or {}).get("force"))
    if not force:
        cached = account_view(plat)
        if cached.get("autoNickname"):
            return {"ok": True, "cached": True, "platform": plat,
                    "nickname": cached["autoNickname"],
                    "accountId": cached.get("autoAccountId"),
                    "account": cached}
    info = account_info.read_account(plat)
    if not info.get("ok"):
        return {"ok": False, "platform": plat, "error": info.get("error") or "读取失败",
                "locked": bool(info.get("locked")),
                "account": account_view(plat)}
    account = save_auto_account(plat, info)
    return {"ok": True, "cached": False, "platform": plat,
            "nickname": info.get("nickname"), "accountId": info.get("accountId"),
            "source": info.get("source"), "account": account}


# ------------------------------------------------------------- 常用标签（发布弹窗「复用标签」）
# 用户 2026-09-25：「加个复用标签的功能，这个复用标签内容也是可以自定义的」。
# 存 settings.json 的 tagPresets（与账号备注同一个文件，原子写盘复用 _write_settings）。
# 校验**不另写一套**：单条长度上限直接复用 publish.py 的 PUBLISH_TAG_MAX（K44）；
# 池子条数是另一回事（发布标签 ≤5 条管的是"一条作品"，这里是"常用池"），单独给上限。
TAG_PRESETS_MAX = 30


def _norm_presets(raw: object) -> tuple[list[str], str | None]:
    """把任意输入归一成「去空白、去重（忽略大小写）、限长」的标签池；返回 (列表, 错误)。"""
    try:
        from app.editor.backend.api.publish import PUBLISH_TAG_MAX
    except Exception:                                          # noqa: BLE001
        PUBLISH_TAG_MAX = 20                                   # pragma: no cover（兜底）
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "常用标签必须是数组"
    out: list[str] = []
    for item in raw:
        t = str(item or "").strip()
        if not t:
            continue
        if len(t) > PUBLISH_TAG_MAX:
            return [], f"单个常用标签最多 {PUBLISH_TAG_MAX} 个字：「{t}」有 {len(t)} 个"
        if any(ch in t for ch in "\r\n\t"):
            return [], "常用标签不能包含换行或制表符"
        if not any(t.lower() == x.lower() for x in out):
            # 上限判在**加之前**（v3.55 冒烟抓到的差一错误：先加后判会把"恰好 30 个"也拒掉）
            if len(out) >= TAG_PRESETS_MAX:
                return [], f"常用标签最多存 {TAG_PRESETS_MAX} 个"
            out.append(t)
    return out, None


def _presets_view() -> dict:
    tags, _err = _norm_presets(_read_settings().get("tagPresets"))
    return {"tags": tags, "max": TAG_PRESETS_MAX}


@router.get("/tag-presets")
def tag_presets() -> dict:
    """常用标签池（发布弹窗「复用标签」），所有任务共用。"""
    return _presets_view()


@router.post("/tag-presets")
def save_tag_presets(payload: dict) -> dict:
    """整体保存常用标签池（前端发整份列表；以这里归一化后的结果为准）。"""
    tags, err = _norm_presets((payload or {}).get("tags"))
    if err:
        return {"ok": False, "error": err}
    data = _read_settings()
    data["tagPresets"] = tags
    _write_settings(data)
    return {"ok": True, "tags": tags, "max": TAG_PRESETS_MAX}


@router.get("/publish-auto-cover")
def get_publish_auto_cover() -> dict:
    """四张平台卡片各一个开关：一次返回**全部平台**的真值（前端不必逐平台拉）。

    平台键来自注册表（K44），未单独设置的平台也给出回退后的真值。
    """
    data = _read_settings()
    return {"ok": True,
            "platforms": {key: auto_cover_enabled(key, data)
                           for key in platforms.PLATFORM_ORDER}}


@router.post("/publish-auto-cover")
def save_publish_auto_cover(payload: dict) -> dict:
    """保存**单个平台**的开关；platform 必须是已知平台，enabled 只认 JSON 布尔。

    注意必须拿**原始键**对注册表校验，不能先 normalize——
    normalize 会把未知平台兜底成抖音（DEFAULT_PLATFORM），假平台就溜进去了。
    """
    plat = (payload or {}).get("platform")
    raw = (payload or {}).get("enabled")
    if not isinstance(raw, bool):
        return {"ok": False, "error": "enabled 必须是布尔值"}
    if not isinstance(plat, str) or plat not in platforms.PLATFORMS:
        return {"ok": False, "error": "platform 必须是已知平台"}
    set_auto_cover(raw, plat)
    return {"ok": True, "platform": plat, "enabled": auto_cover_enabled(plat)}


@router.get("/publish-auto-publish")
def get_publish_auto_publish() -> dict:
    """四张平台卡片各一个开关：一次返回**全部平台**的真值。缺省全关。"""
    data = _read_settings()
    return {"ok": True,
            "platforms": {key: auto_publish_enabled(key, data)
                          for key in platforms.PLATFORM_ORDER}}


@router.post("/publish-auto-publish")
def save_publish_auto_publish(payload: dict) -> dict:
    """保存**单个平台**的自动发布开关；platform 必须是已知平台，enabled 只认 JSON 布尔。

    与 auto-cover 同款校验：拿**原始键**对注册表校验（不能先 normalize，
    否则未知平台会兜底成抖音溜进去）。
    """
    plat = (payload or {}).get("platform")
    raw = (payload or {}).get("enabled")
    if not isinstance(raw, bool):
        return {"ok": False, "error": "enabled 必须是布尔值"}
    if not isinstance(plat, str) or plat not in platforms.PLATFORMS:
        return {"ok": False, "error": "platform 必须是已知平台"}
    set_auto_publish(raw, plat)
    return {"ok": True, "platform": plat, "enabled": auto_publish_enabled(plat)}


@router.get("/login-status")
def login_status(platform: str = "douyin") -> dict:
    """某平台创作者登录状态（**不启动浏览器**，读该平台 profile 里的 cookie 判定）。"""
    try:
        from app.editor.backend import publisher
        return publisher.login_state(platform)
    except Exception as exc:                                   # noqa: BLE001
        return {"platform": platform, "loggedIn": False,
                "error": f"{type(exc).__name__}: {exc}"}


@router.get("/accounts")
def accounts() -> dict:
    """四平台账号视图（发布页头部卡片；一次取全，不用打四次）。"""
    return {"ok": True, "accounts": accounts_view()}


@router.post("/open-creator")
def open_creator(payload: dict | None = None) -> dict:
    """打开某平台创作者中心让人扫码登录（**只开页面，绝不发布**，R1）。

    注意这里**只做登录**：不跳上传页填表、不点任何发布动作。
    """
    body = payload or {}
    platform = body.get("platform") or "douyin"
    url = body.get("url")
    try:
        from app.editor.backend import publisher
        return publisher.open_creator(url, platform=platform)
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "platform": platform,
                "error": f"{type(exc).__name__}: {exc}"}


# v3.81：工作区路径的**结构化清单**（以前 summary 里那个 workspace 是扁平的
# {"key": "路径"}，前端没法知道「这是文件还是目录」「存不存在」—— 于是要么不敢
# 给「打开」按钮（现在能打开了），要么对着一个文件路径点开把整个目录树建出来。
# 这里给出三项元信息，让**该给按钮的才给**（K69 同源：不给点了没反应的入口）。
# 键保留 + 新的 dirs 并存：老的 `workspace` 映射不动（既有消费者不受影响）。
# ⚠️ 第一列必须是 :class:`Workspace` 的**属性名**（snake_case），本函数直接 getattr。
# 故意**不套 try/except**：写错一个属性名会让整张表静默少一行（我第一版就是这样，
# 表里只剩"工作区根目录"一行，而界面看着完全正常）—— 配置错就该立刻炸。
PATH_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("root", "工作区根目录", "所有数据的家；要整个备份就拷它"),
    ("live_source_dir", "录制原片", "直播间录下来的原始 mp4 住这里"),
    ("editor_output_dir", "成片", "渲染出来的成品"),
    ("uploads_dir", "我的上传", "手动导入的素材"),
    ("music_dir", "背景音乐", "渲染时可选的那批 BGM"),
    ("background_dir", "背景素材", "合成时的底图素材"),
    ("transcripts_dir", "转写产物", "Whisper 出的 .json + .srt"),
    ("candidates_dir", "切片候选", "AI 切出来的候选片段元数据"),
        ("models_dir", "Whisper 模型", "small / medium 识别模型"),
    ("cuda_dir", "CUDA 运行时", "GPU 加速用的 DLL（便携形式，不是装的 CUDA Toolkit）"),
    ("runtime_data", "运行时数据", "端口文件、日志、队列状态"),
    ("log_dir", "日志", "排查问题时看这里"),
    ("settings_path", "设置文件", "settings.json（文件，不是目录）"),
)


def dirs_view() -> list[dict]:
    """结构化路径清单，每项带 ``isDir`` / ``exists`` / ``sizeMb``。"""
    out: list[dict] = []
    for attr, label, desc in PATH_ENTRIES:
        path: Path = getattr(ws, attr)
        is_file = path.is_file()
        is_dir = path.is_dir()
        size_mb: float | None = None
        if is_file:
            try:
                size_mb = round(path.stat().st_size / 1024 / 1024, 2)
            except OSError:
                size_mb = None
        out.append({
            "key": attr, "label": label, "desc": desc, "path": str(path),
            "isDir": is_dir, "isFile": is_file, "exists": is_dir or is_file,
            "sizeMb": size_mb,
            # 只有**目录**才给「打开」：对文件路径调 open_workspace 会 mkdir 出一个
            # 同名目录来（Python 的 startfile 对不存在路径会炸，而被我们 mkdir 之后
            # 就变成凭空多出一个奇怪目录）—— 所以这里把判据明确下来。
            "openable": is_dir,
        })
    return out


@router.get("/dirs")
def dirs() -> dict:
    """同 :func:`dirs_view`（保留给外部调用，唯一实现在上面）。"""
    return {"dirs": {e["key"]: {"path": e["path"], "exists": e["exists"]}
                     for e in dirs_view()}}
