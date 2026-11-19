# 第三方组件与素材声明

本项目在实现中使用了以下第三方组件与素材。各组件的版权归其原作者所有，
使用时请遵守其各自的许可条款。

---

## 运行时依赖（通过 pip 安装，不随仓库分发）

| 组件 | 许可 | 用途 |
|---|---|---|
| pyJianYingDraft | Apache-2.0 | 剪映草稿文件生成 |
| pywebview | BSD-3-Clause | 桌面窗口 |
| FastAPI | MIT | 本地 HTTP 服务 |
| Uvicorn | BSD-3-Clause | ASGI 服务器 |
| Playwright | Apache-2.0 | 浏览器自动化 |
| faster-whisper | MIT | 语音转写 |
| CTranslate2 | MIT | 推理后端（faster-whisper 依赖） |
| Pillow | MIT-CMU | 图像处理 |
| NumPy | BSD-3-Clause | 数值计算 |
| RapidOCR | Apache-2.0 | 字幕文字识别 |

---

## 外部可执行程序（需自行安装，不随仓库分发）

### FFmpeg

- 许可：取决于构建方式（LGPL-2.1+ 或 GPL-2.0+）
- 官网：https://ffmpeg.org/
- 说明：本项目仅通过命令行调用，不分发其二进制。若你分发的产物中包含 FFmpeg，
  需自行遵守其相应条款（GPL 构建需一并提供源码）。

---

## 内置素材

### 字体

两个字体均为 **SIL Open Font License 1.1** 授权，许可全文随仓库分发：
`app/common/assets/fonts/OFL.txt`。

| 文件 | 字体 | 许可 | 版权与来源 |
|---|---|---|---|
| `app/common/assets/fonts/NotoSansSC-Bold.otf` | Noto Sans CJK SC Bold | OFL 1.1 | © 2014-2021 Adobe，与思源黑体同源设计，Google 以 Noto 名义分发 |
| `app/common/assets/fonts/ZCOOLKuaiLe-Regular.ttf` | ZCOOL KuaiLe（站酷快乐体） | OFL 1.1 | © 2018 The ZCOOL KuaiLe Project Authors，官方仓库 googlefonts/zcool-kuaile |

**OFL 1.1 义务要点**（详见仓库内 OFL.txt 全文）：

- 允许自由使用、复制、合并、修改、分发（含商用）
- 分发时必须随附许可全文与版权声明（本仓库已随附）
- 不得单独出售字体本身
- 修改后的版本不得使用保留字体名

### Emoji 图标

`app/common/assets/emoji/*.png` 共 40 个 PNG 图形，按 Unicode 码位命名
（如 `1f602.png`），取自 **Twemoji** 开源表情图集。

- 版权：Copyright 2019 Twitter, Inc and other contributors
- 许可：图形 **CC-BY 4.0**（代码 MIT），允许商用与再分发
- 上游：https://github.com/jdecked/twemoji （原 twitter/twemoji，现由社区维护）
- 署名：本项目在 README 与本文件中注明来源与许可，满足 CC-BY 4.0 的署名要求

---

## 图标

`launcher/ui/assets/` 下的应用图标为本项目自有素材。
