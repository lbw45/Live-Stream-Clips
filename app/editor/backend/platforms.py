#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布平台注册表（**唯一出处**，K44）。

为什么单开一个模块：平台名、创作者中心地址、cookie 域、标题/标签上限、页面选择器
这五类信息在**登录、队列校验、浏览器填写、界面卡片**四个地方都要用。原先只有抖音，
所有值散落在 ``publisher`` 与 ``api.publish`` 里；加到四平台后如果各写一份，
「界面说最多 5 个标签、后端却按 10 个收」这种漂移必然发生。

**红线不变（R1）**：这里只描述「打开哪个页面、往哪些输入框填什么」，
没有任何「点发布」的定位方式；执行器永远停在填表完成，等人在浏览器里自己点发布。

选择器为什么是**一组**而不是一个：三家平台的创作后台都是 SPA，改版频繁。
写成候选列表 + 逐个探测（``publisher``），改版时最多少填一个字段并在界面提示，
不会整条链路直接报「找不到元素」。找不到时**如实报错**，绝不假装填好了。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 平台顺序 = 发布页头部小卡片的显示顺序（抖音在前，沿用既有用户的直觉）
PLATFORM_ORDER: tuple[str, ...] = ("douyin", "kuaishou", "bilibili", "xiaohongshu")
DEFAULT_PLATFORM = "douyin"


@dataclass(frozen=True)
class PlatformSpec:
    """一个发布平台的全部静态规格。"""

    key: str
    name: str
    short: str                 # 卡片上的单字徽标（不依赖图标字体）
    color: str                 # 卡片强调色（与各平台品牌色一致）
    creator_url: str           # 打开它就能登录/发布（登录与上传共用同一个入口）
    cookie_hosts: tuple[str, ...]   # cookie host_key 的匹配片段（判登录态用）
    title_max: int
    tag_max: int
    tag_count_max: int
    creator_label: str         # 「创作者中心」在各平台的叫法，界面提示用
    # 页面选择器（按顺序探测，第一个命中的用）
    file_input: tuple[str, ...] = ('input[type="file"]',)
    title_inputs: tuple[str, ...] = ()
    desc_inputs: tuple[str, ...] = ()
    tag_inputs: tuple[str, ...] = ()
    #: 该平台网页端**没有独立标题框**，标题要写进描述第一行（快手就是这样）
    title_in_desc: bool = False
    #: 标签是否直接在描述框里以 ``#话题`` 输入（抖音/小红书/快手）
    tags_in_desc: bool = True
    #: 上传后表单页 URL 特征（用来等跳转；空＝不等，直接等输入框）
    post_url_glob: str = ""
    notes: str = ""            # 发布页卡片上的补充说明（例如 B 站要选分区）
    # ---- v4.18：读「当前登录账号昵称」用（account_info.py）----
    #: 要打开的页面（默认与 creator_url 相同；B站要开主站才会请求 nav 接口）
    account_page: str = ""
    #: 旁听哪个接口的 JSON（页面自己带签名参数，我们只读响应，不自己拼）
    account_api_match: str = ""
    nickname_paths: tuple[str, ...] = ()
    account_id_paths: tuple[str, ...] = ()
    avatar_paths: tuple[str, ...] = ()
    #: localStorage 兜底键（平台把用户信息写在前端存储时用）
    account_storage_keys: tuple[str, ...] = ()
    #: 登录窗口标题里可能出现的词（v4.20：启动后把窗口拉到前台时用来认窗）
    window_hints: tuple[str, ...] = ()
    # ---- v4.26：发布封面（3:4 / 4:3，见 app.common.publish_cover）----
    #: 优先上传哪些比例；页面只有一个图片文件框时只用第一项。
    #: 页面若提供两个图片文件框，则按这里的顺序依次上传（多出的忽略）。
    cover_ratios: tuple[str, ...] = ("3x4", "4x3")
    #: 打开封面上传弹层的按钮（找不到图片文件框时才点；绝不含「发布/投稿」文字）
    cover_trigger: tuple[str, ...] = (
        'button:has-text("设置封面")',
        'button:has-text("设置视频封面")',
        'button:has-text("选择封面")',
        'button:has-text("上传封面")',
        'button:has-text("更换封面")',
        'button:has-text("编辑封面")',
        'button:has-text("封面设置")',
        '[role="button"]:has-text("设置封面")',
        '[role="button"]:has-text("选择封面")',
        '[role="button"]:has-text("上传封面")',
        '[role="button"]:has-text("更换封面")',
        '[role="button"]:has-text("编辑封面")',
        'label:has-text("上传封面")',
    )
    #: 封面图片文件框候选（**只认 image/jpeg/png，绝不碰视频框**）
    cover_inputs: tuple[str, ...] = (
        'input[type="file"][accept*="image"]',
        'input[type="file"][accept*="jpg"]',
        'input[type="file"][accept*="jpeg"]',
        'input[type="file"][accept*="png"]',
        'input[type="file"][accept*=".jpg"]',
        'input[type="file"][accept*=".jpeg"]',
        'input[type="file"][accept*=".png"]',
    )
    #: 封面弹层容器候选（确认按钮只在含「封面」文字的弹层里找）
    #: v4.32：B站实测弹层不吃前面那串类名 ⇒ 补一组**宽松**的类名匹配
    #: （仍要求「可见 + 文字含封面」，不会误把表单容器当弹层）
    cover_dialogs: tuple[str, ...] = (
        'div[role="dialog"]', '.semi-modal', '.semi-modal-content',
        '.el-dialog', '.el-dialog__body', '.ant-modal', '.ant-modal-content',
        '.bili-dialog', '.bili-modal', '.d-modal', '.cover-modal',
        '[class*="cover-plugin"]', '[class*="cover-panel"]',
        '[class*="modal"]', '[class*="Modal"]',
        '[class*="dialog"]', '[class*="Dialog"]',
        '[class*="popup"]', '[class*="Popup"]',
    )
    #: 封面弹层的保存/确认按钮文字（**按平台各自可覆盖**）
    #: 抖音向导最后一步是「完成」，部分平台的封面弹层是「保存」；只在含「封面」的弹层里找。
    cover_confirm_labels: tuple[str, ...] = ("保存", "确定", "确认", "完成", "应用")
    #: 封面流程：generic=直接找图片框/一个弹层；dual_wizard=同一弹层里竖封面→横封面两步向导
    cover_flow: str = "generic"
    #: dual_wizard 的打开入口（抖音是「选择封面」卡片按钮，不是「设置封面」）
    cover_open_trigger: tuple[str, ...] = ()
    #: dual_wizard 中从竖封面切到横封面的按钮文字（抖音实测「设置横封面」）
    cover_next_labels: tuple[str, ...] = ()
    #: v4.32：封面入口按钮的**文字**（CSS 选择器兜不到时按文字找）。
    #: B站实测入口是「+ 添加封面」，不在上面那串 cover_trigger 里。
    cover_entry_labels: tuple[str, ...] = (
        "添加封面", "设置封面", "设置视频封面", "选择封面", "上传封面",
        "更换封面", "编辑封面", "修改封面",
    )
    #: v4.36：多封面（PK封面）开关文案；开了之后平台会出现多个封面上传位（快手/小红书）。
    #: 用户 2026-11-05：「有个叫PK封面的要打开，开了好像要填三张照片」。
    cover_pk_labels: tuple[str, ...] = ()
    #: 开了 PK 封面后最多填几张（平台通常 2~3）
    cover_pk_slots: int = 3

    #: v4.38：发布按钮的**文字**（精确匹配，不会误中「定时发布」）；自动发布用
    publish_labels: tuple[str, ...] = ("发布",)
    #: v4.38：点发布后的成功文案（URL 没跳转也认；页面出现任一即算成功）
    publish_success_texts: tuple[str, ...] = ("发布成功", "投稿成功")

    #: v4.35：封面上传槽位的**结构化**选择器（优先于文案启发式；来自 social-auto-upload 的实战结论）。
    #: 抖音实测：封面是 `.semi-upload` 且包含拖拽主文案元素；「生成参考图」槽不含 ⇒ 精准区分，
    #: 不受「生成参考图/上传封面」等文案改版影响。
    cover_slot_selectors: tuple[str, ...] = ()
    #: v4.32：封面弹层里**真正上传封面**的按钮文字。抖音弹层左侧还有「生成参考图」
    #: （AI 生成封面的参考图）也是 accept=image 的文件框，按「第一个图片框」选会传错位置。
    #: dual_wizard 流程必须点这个按钮拉起文件选择器，绝不按 DOM 顺序猜输入框。
    cover_upload_labels: tuple[str, ...] = ("上传封面",)


PLATFORMS: dict[str, PlatformSpec] = {
    "douyin": PlatformSpec(
        key="douyin",
        name="抖音",
        short="抖",
        color="#fe2c55",
        creator_url="https://creator.douyin.com/creator-micro/content/upload",
        cookie_hosts=("douyin",),
        title_max=30,
        tag_max=20,
        tag_count_max=5,
        creator_label="抖音创作者中心",
        title_inputs=('input[placeholder*="作品标题"]', 'input[placeholder*="标题"]'),
        desc_inputs=('div[contenteditable="true"]', 'textarea[placeholder*="简介"]'),
        tag_inputs=(),
        tags_in_desc=True,
        post_url_glob="**/content/post/video**",
        # 抖音实测：一个弹层里的两步向导，竖封面选图 →「设置横封面」→ 横封面选图 →「完成」
        cover_flow="dual_wizard",
        cover_open_trigger=(
            'button:has-text("选择封面")',
            '[role="button"]:has-text("选择封面")',
            'text="选择封面"',
        ),
        cover_next_labels=("设置横封面", "下一步"),
        cover_confirm_labels=("完成", "保存", "确定"),
        # 抖音实测：弹层左侧「生成参考图」也是 accept=image 文件框（AI 生成封面用），
        # 真正的封面上传是裁切区下方的「上传封面」按钮 —— 必须点它，不能按 DOM 顺序选输入框。
                cover_slot_selectors=(
            '.semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input',
        ),
cover_upload_labels=("上传封面",),
        notes="标签以 #话题 写进简介",
        account_page="https://creator.douyin.com/creator-micro/content/upload",
        account_api_match="/aweme/v1/creator/user/info/",
        nickname_paths=("douyin_user_verify_info.nick_name", "user.nickname"),
        account_id_paths=("douyin_user_verify_info.douyin_unique_id", "user.uid"),
        avatar_paths=("douyin_user_verify_info.avatar_url",),
        window_hints=("抖音", "创作者中心"),
    ),
    "kuaishou": PlatformSpec(
        key="kuaishou",
        name="快手",
        short="快",
        color="#ff5000",
        creator_url="https://cp.kuaishou.com/article/publish/video",
        cookie_hosts=("kuaishou",),
        title_max=28,
        tag_max=20,
        tag_count_max=4,
        creator_label="快手创作者服务平台",
        title_inputs=('input[placeholder*="标题"]',),
        desc_inputs=('div[contenteditable="true"]', 'textarea[placeholder*="描述"]',
                     'textarea[placeholder*="介绍"]'),
        tag_inputs=(),
        # 快手网页端没有独立标题框，标题就是描述的第一行（2026-11 实测的页面结构）
        title_in_desc=True,
        tags_in_desc=True,
        # 快手实测（用户截图）：封面只有一张，「封面截取」弹层里点「上传图片」再「确定」
        cover_ratios=("3x4",),
        cover_flow="dialog",
        cover_upload_labels=("上传图片", "上传封面"),
        # 快手 PK 封面需要「开 PK 后弹层重置 + 逐张填」的专项流程（2026-11-05 真机未过），先停用
        # v4.37：PK 流程已实现（逐缩略图），但 PK2 弹层结构真机仍未过，先停用待 dump
        # cover_pk_labels=("PK封面",),  # v4.37：PK=表单多缩略图，逐张点开弹层上传
        # cover_pk_labels=("PK封面",),
        cover_confirm_labels=("确认", "确定", "保存"),
        notes="网页端无独立标题框：标题写在描述第一行，话题最多 4 个",
        account_page="https://cp.kuaishou.com/article/publish/video",
        account_api_match="/rest/v2/creator/pc/authority/account/current",
        nickname_paths=("data.userName",),
        account_id_paths=("data.userId",),
        avatar_paths=("data.userAvatar",),
        window_hints=("快手",),
    ),
    "bilibili": PlatformSpec(
        key="bilibili",
        name="B站",
        short="B",
        color="#00aeec",
        creator_url="https://member.bilibili.com/platform/upload/video/frame",
        cookie_hosts=("bilibili",),
        title_max=80,
        tag_max=20,
        tag_count_max=10,
        creator_label="B站创作中心",
        title_inputs=('input[placeholder*="标题"]',),
        desc_inputs=('.ql-editor[contenteditable="true"]',
                     'div[contenteditable="true"]',
                     'textarea[placeholder*="简介"]'),
        tag_inputs=('input[placeholder*="标签"]', 'input[placeholder*="回车"]'),
        tags_in_desc=False,
        # B站视频封面以横版为主；只有一个图片框时优先 4:3，页面有两个框时两份都传
        # B站实测：弹层里是「首页推荐封面 4:3」+「个人空间封面 16:9」；我们只传 4:3，16:9 交给平台默认，不拿 3:4 硬塞 16:9 槽
        # B站实测（用户截图）：弹层底部「上传封面」（拖拽区）→ 右下「完成」
        cover_ratios=("4x3",),
        cover_flow="dialog",
        cover_confirm_labels=("确定", "完成", "保存"),
        notes="需在浏览器里选「分区」并勾选创作公约，再自己点投稿",
        account_page="https://www.bilibili.com/",
        account_api_match="/x/web-interface/nav",
        nickname_paths=("data.uname",),
        account_id_paths=("data.mid",),
        avatar_paths=("data.face",),
        window_hints=("哔哩哔哩", "B站", "创作中心"),
    ),
    "xiaohongshu": PlatformSpec(
        key="xiaohongshu",
        name="小红书",
        short="红",
        color="#ff2442",
        creator_url="https://creator.xiaohongshu.com/publish/publish"
                    "?source=official&from=tab_switch&target=video",
        cookie_hosts=("xiaohongshu",),
        title_max=20,
        tag_max=20,
        tag_count_max=10,
        creator_label="小红书创作服务平台",
        title_inputs=('input[placeholder*="标题"]',),
        desc_inputs=('div[contenteditable="true"]', 'p[contenteditable="true"]'),
        tag_inputs=(),
        tags_in_desc=True,
        # 小红书实测（用户截图）：缩略图悬停有「编辑封面」→ 弹层底部「上传」→ 右下「完成」
        cover_ratios=("3x4",),
        cover_flow="dialog",
        cover_upload_labels=("上传",),
        # 小红书 PK 是 1/3 逐张编辑流程，暂不启用（见 publisher._upload_cover_xhs 注释）
        # cover_pk_labels=("PK封面",),
        cover_confirm_labels=("完成", "保存", "确定"),
        notes="标题最多 20 字，话题写在正文里",
        account_page=("https://creator.xiaohongshu.com/publish/publish"
                      "?source=official&from=tab_switch&target=video"),
        account_api_match="/api/galaxy/user/info",
        nickname_paths=("data.userName",),
        account_id_paths=("data.redId", "data.userId"),
        avatar_paths=("data.userAvatar",),
        account_storage_keys=("USER_INFO_FOR_BIZ",),
        window_hints=("小红书",),
    ),
}


def normalize(key: object) -> str:
    """把任意输入归一成合法平台键；不认识的**回退抖音**（不抛，界面不该因参数脏而崩）。"""
    k = str(key or "").strip().lower()
    return k if k in PLATFORMS else DEFAULT_PLATFORM


def get(key: object) -> PlatformSpec:
    return PLATFORMS[normalize(key)]


def limits(key: object) -> dict:
    """下发给界面的限制值（与 ``api.publish.publish_limits`` 同一口径）。"""
    s = get(key)
    return {"titleMax": s.title_max, "tagMax": s.tag_max, "tagCountMax": s.tag_count_max}


def public_list() -> list[dict]:
    """发布页头部小卡片用：只给界面需要的最小字段（选择器不下发）。"""
    out: list[dict] = []
    for k in PLATFORM_ORDER:
        s = PLATFORMS[k]
        out.append({
            "key": s.key, "name": s.name, "short": s.short, "color": s.color,
            "creatorUrl": s.creator_url, "creatorLabel": s.creator_label,
            "notes": s.notes, "limits": limits(k),
        })
    return out


def queue_key(platform: object, base: str) -> str:
    """队列幂等键：抖音沿用**裸键**（历史数据与其它服务都按它找），其余加平台前缀。

    ⚠️ 为什么必须分平台：同一条成片要能分别投到四个平台，各自有独立的标题/标签/状态。
    ``client_request_id`` 是 UNIQUE，共用裸键会让第 2~4 个平台**静默入不了队**。
    """
    p = normalize(platform)
    return str(base) if p == DEFAULT_PLATFORM else f"{p}:{base}"
