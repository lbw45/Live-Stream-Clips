# Live Stream Clips · 直播切片工作台

把一场几小时的直播录像，自动变成一批可以直接发布的短视频。

录制 → 语音转写 → AI 选段 → 渲染成片，整条链路在本机跑完，素材和模型都不出本地。

---

## 它是做什么的

直播回放的痛点是「长」：三小时的录像里有价值的内容可能只有十几分钟，而人工逐段听、逐段剪，一条片子要花掉半小时以上。

这个工具把这段流程自动化：

1. **接入素材** —— 本机已有的录像文件，或按直播间地址自动监控录制
2. **转写** —— 本地语音识别，产出带时间戳的逐句文本
3. **选段** —— 交给大模型按内容价值、情绪强度、可独立成立性打分，挑出候选片段
4. **成片** —— 按保留区间裁剪拼接，可选烧字幕、配 BGM、做首帧钩子

全流程离线除了一处：调用你自己配置的大模型接口。API Key 用 Windows DPAPI 加密存放在本机，不进代码、不进日志。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| 直播监控录制 | 按直播间地址轮询开播状态，开播即录；分段落盘，断线可续 |
| 本地转写 | faster-whisper + CTranslate2，GPU 加速，可选 small / medium 模型 |
| AI 选段 | 按内容类型（口播 / 带货 / 游戏 / 访谈…）套用不同的评分权重与钩子策略 |
| 切片渲染 | FFmpeg 精确裁剪、拼接、烧字幕、BGM 混音、画幅转换 |
| 封面设计 | 内置模板与参数化取景，按平台比例出多尺寸封面 |
| 素材库 | 按来源分类管理录制、成片、上传与音乐素材 |

> 界面为中文，桌面端单窗口（WebView 承载），后端是本地 HTTP 服务。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11 x64 |
| Python | **3.12**（3.13 暂无 CTranslate2 预编译包，会装不上 faster-whisper） |
| FFmpeg | 需自备 `ffmpeg.exe` / `ffprobe.exe`，放在工作区的 `bin\` 目录下（见下） |
| WebView2 | Win10 需安装 Microsoft Edge WebView2 Runtime |
| GPU | 可选。NVIDIA 显卡可显著加速语音转写，无显卡时自动回退 CPU |

### 关于工作区目录

程序把运行时组件与业务素材放在一个**工作区根目录**里，结构固定：

```
<工作区根>/
├── bin/              # ffmpeg.exe / ffprobe.exe（需自备）
├── runtime/          # python / cuda12 / models（可选，便携运行时）
├── live_raw/         # 直播录制素材
├── clips/ edits/     # 切片与成片
├── runtime_data/     # 配置、日志、索引、转写结果
└── ...
```

定位顺序：显式参数 > 环境变量 `JACK_WORKSPACE_ROOT` > 环境变量 `SLICERWORKBENCH_WORKSPACE_ROOT` > 源码内默认值（首次使用请用环境变量指定到你自己的路径）。

```bash
set JACK_WORKSPACE_ROOT=D:\my-workspace
```

### 关于 FFmpeg

本项目**不随仓库分发 FFmpeg 二进制**。请从 [ffmpeg.org](https://ffmpeg.org/download.html) 或其推荐的 Windows 构建获取，放入 `<工作区根>\bin\`。

FFmpeg 的许可取决于构建方式（LGPL 或 GPL），若你分发的产物中包含 FFmpeg，需自行遵守其相应条款。

### 关于语音模型

模型权重不随仓库分发，首次运行转写时会从 Hugging Face 下载并缓存在本地。

### 关于浏览器

项目通过 Playwright 驱动**系统已安装的 Edge**，不需要额外下载浏览器内核。

---

## 快速开始

**方式一：一键向导（推荐）**

```bash
git clone https://github.com/lbw45/Live-Stream-Clips.git
cd Live-Stream-Clips
setup.bat
```

`setup.bat` 会自动完成：找到 Python（3.9–3.12，64 位）→ 安装依赖 → 检查
FFmpeg / Edge / OCR → 引导首次启动。之后每次启动双击
`launcher\.runtime\start.bat` 即可。

**方式二：手动**

```bash
git clone https://github.com/lbw45/Live-Stream-Clips.git
cd Live-Stream-Clips

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

set JACK_WORKSPACE_ROOT=D:\my-workspace
python launcher/orchestrator.py start
```

启动后桌面窗口自动打开。后端服务与窗口分离，关窗不退服务；改过后端代码用 `orchestrator.py reload-services` 重载。

补充说明：

- **FFmpeg**：把 `ffmpeg.exe` / `ffprobe.exe` 放进工作区的 `bin\` 目录
  （下载 [release essentials](https://www.gyan.dev/ffmpeg/builds/)），或加入系统 PATH。
- **Whisper 模型**：首次转写时自动下载。网络慢可先 `set HF_ENDPOINT=https://hf-mirror.com`。
- **OCR（可选）**：`pip install rapidocr-onnxruntime`，用于切片边界的字幕复核；不装则自动跳过该检查。
- `setup.bat check` 只做环境体检、不安装不启动。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 桌面壳 | pywebview（WebView2） |
| 本地服务 | FastAPI + Uvicorn，端口动态分配 |
| 前端 | 原生 HTML/CSS/JS 单页，无构建步骤 |
| 语音识别 | faster-whisper（CTranslate2 后端） |
| 音视频处理 | FFmpeg / FFprobe（子进程调用） |
| 浏览器自动化 | Playwright 驱动系统 Edge |
| 剪映草稿 | pyJianYingDraft |
| 大模型 | OpenAI 兼容协议，支持多家服务商 |

---

## 项目结构

```
app/
├── common/          # 路径、进程、浏览器通道、封面、媒体类型等共用件
├── slicer/          # 录制、转写、切片规划、渲染、LLM 接入
└── editor/          # 本地服务与 HTTP API
launcher/            # 桌面壳、启动编排、前端页面
tools/               # 运维小工具（接口清单 / 环境诊断 / cookie 备份）
build/               # PyInstaller spec 与 NSIS 安装脚本
```

便携运行时（Python、CUDA、模型、FFmpeg 二进制）放在仓库之外的独立目录，按 `app/common/paths.py` 的约定定位，不纳入版本控制。

---

## 第三方组件

本项目的实现依赖以下开源组件，各自遵循其原始许可：

| 组件 | 许可 | 用途 |
|---|---|---|
| [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft) | Apache-2.0 | 剪映草稿文件生成 |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | 桌面窗口 |
| [FastAPI](https://github.com/tiangolo/fastapi) | MIT | 本地 HTTP 服务 |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [Playwright](https://github.com/microsoft/playwright-python) | Apache-2.0 | 浏览器自动化 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | 语音转写 |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU | 图像处理 |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | 数值计算 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | Apache-2.0 | 字幕文字识别 |
| [FFmpeg](https://ffmpeg.org/) | LGPL / GPL（视构建） | 音视频编解码（外部依赖） |

各组件版权归其原作者所有。分发本项目时请一并保留上述声明。

内置素材的许可：两个内置字体均为 SIL OFL 1.1（全文见
`app/common/assets/fonts/OFL.txt`）；emoji 图形取自
[Twemoji](https://github.com/jdecked/twemoji)，版权
Copyright 2019 Twitter, Inc and other contributors，许可
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)。
完整声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

## 免责声明

- 本项目仅供**个人学习、研究与技术交流**使用。
- 使用本工具录制、转写、剪辑任何内容前，请确认你**拥有该内容的合法授权**，或该使用方式符合法律规定的合理使用情形。
- 所处理素材及生成成片的著作权归原权利人所有，本工具不授予你任何内容权利。
- 使用本工具即表示你自行承担遵守所在地区法律法规、以及各内容平台服务条款的全部责任。
- 作者不对使用者的任何使用行为及其后果承担责任。

---

## License

[PolyForm Noncommercial License 1.0.0](LICENSE)

简单说：

- ✅ 个人学习、研究、试验、爱好项目、私人娱乐 —— 随便用、随便改
- ✅ 慈善机构、教育机构、公共研究机构、政府机构 —— 可用
- ❌ **任何商业用途** —— 需另行获得授权
- 📄 分发时须随附本许可全文与 `Required Notice` 行

需要商业授权请联系仓库所有者。
