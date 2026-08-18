# Video Material Agent Toolkit

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Agent Skills](https://skills.sh/b/lechangbin/video-material-agent-toolkit)](https://skills.sh/lechangbin/video-material-agent-toolkit)

面向本地 AI Agent 的视频素材工作流工具集，包含：

- `material-collector`：从 Bilibili、抖音和小红书搜索视频来源，下载最高不超过
  720p 的理解代理，持久化可恢复会话，并保留来源清单；
- `semvideo`：对本地视频进行语义理解、分段、镜头语言标注、查询和片段导出；
- 四个遵循开放 [Agent Skills 规范](https://agentskills.io/) 的 Skills，用于把搜索、
  理解、Top-K 选择和有界补搜编排为可追踪工作流。

当前工具包发布为 `v0.2.0`：Material Collector `0.2.0`，Semvideo `0.1.3`。
支持 Windows x64 和 CPython `>=3.14.6,<3.15`。
Docker 部署另支持 Linux/amd64 容器，并通过本机 noVNC 页面完成交互式平台登录。

> 本项目提供技术工具，不授予任何第三方视频、音乐、肖像、平台数据或商标的使用权。
> 使用者必须遵守目标平台条款、适用法律和素材权利要求。不要使用本项目绕过访问控制、
> 大规模抓取或干扰平台服务。

## 只把仓库地址交给 Agent

可以。仓库根目录的 [`AGENTS.md`](AGENTS.md) 是 Agent 自举协议，
[`scripts/bootstrap-agent.ps1`](scripts/bootstrap-agent.ps1) 是唯一的自动配置入口。把下面
这段提示词和仓库地址交给任意具备 Windows PowerShell 与终端权限的 Agent：

```text
请配置这个仓库：https://github.com/lechangbin/video-material-agent-toolkit
克隆默认分支，完整读取仓库根目录 AGENTS.md，并严格执行其中的 Fresh-machine setup。
不要自行改写安装、PATH、进程控制或 Skills 复制命令。最后返回 bootstrap 脚本的完整
JSON 结果，以及仍需我亲自完成的交互步骤。
```

Agent 会从仓库根目录执行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1
```

该入口会检测并安装缺失的 Python 3.14、uv、Node.js、Edge/Chrome 浏览器通道和 FFmpeg，从最新 GitHub
Release 下载并校验两个 wheel，然后通过 `npx skills` 给受支持的 Agent 安装全部四个
Skills。它不会写入 API Key，也不会代替用户完成平台扫码登录。

只检查、不修改机器：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 -CheckOnly
```

如果 WorkBuddy 或其他自定义宿主未被 `npx skills` 识别，让 Agent 加上传入其 Skills
根目录：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap-agent.ps1 `
  -AdditionalSkillsDirectory 'C:\path\to\workbuddy\skills'
```

自动配置结束后仍有两个有意保留的人工边界：用户通过安全环境注入
`SEMVIDEO_API_KEY`；首次搜索需要登录时，用户在可见浏览器中完成扫码。视频工作区路径
也必须由用户明确指定，然后 Agent 才运行 `semvideo init` 和 `semvideo doctor`。

## Docker 一条命令部署

已安装并启动 Docker Desktop 后，在仓库根目录执行：

```powershell
docker compose up -d --build
```

该命令构建并启动包含 `material-collector`、`semvideo`、Google Chrome、FFmpeg 和
noVNC 桌面的 Linux/amd64 容器。默认把运行数据持久化到 Git 忽略的
`./docker-data`，并只在本机回环地址开放登录页面：

```text
http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale
```

部署成功后仍需明确初始化一个视频工作区：

```powershell
docker compose exec toolkit semvideo init /data/workspace --json
docker compose exec toolkit semvideo doctor --workspace /data/workspace --json
```

运行采集命令使用同一个长驻容器，例如：

```powershell
docker compose exec toolkit material-collector sessions list --workspace /data/workspace
```

API Key 只从启动 Compose 的进程环境传入，不写进镜像、构建参数或仓库。宿主数据目录、
首次登录、CLI 调用方式、Agent Skills 边界和停止方式见 [Docker 部署文档](docs/docker.md)。
容器以非 root 用户运行，并使用 Playwright 官方建议的 seccomp 配置保持 Chromium
sandbox 开启。

## 仓库结构

```text
packages/
  material-collector/  # 搜索下载 CLI 源码
  semvideo/            # 视频理解 CLI 源码
skills/
  collect-video-materials/
  semvideo/
  select-video-segments/
  search-understand-refine-video-materials/
scripts/
  bootstrap-agent.ps1
  build-release.ps1
  install-tools.ps1
docker/
  toolkit-entrypoint.sh
Dockerfile
compose.yaml
```

两个源码快照对应的原始 Git 提交见 [SOURCE_COMMITS.md](SOURCE_COMMITS.md)。

## 快速安装

### 1. 安装系统依赖

在 Windows PowerShell 中执行：

```powershell
winget install --id Python.Python.3.14 --exact
winget install --id astral-sh.uv --exact
winget install --id OpenJS.NodeJS.LTS --exact
winget install --id Gyan.FFmpeg --exact
```

Windows 原生模式默认使用 `--browser-channel auto`，按 Microsoft Edge、Google Chrome
的顺序选择本机浏览器；系统已有 Edge 时无需额外安装 Chrome。若要固定 Chrome，可另行
执行 `winget install --id Google.Chrome --exact` 并传入 `--browser-channel chrome`。
首次安装也可向 `bootstrap-agent.ps1` 传入 `-BrowserChannel auto|edge|chrome`；默认 `auto`
不会在 Edge 可用时要求安装 Chrome。

重新打开 PowerShell，确认：

```powershell
python --version   # 需要 CPython >=3.14.6,<3.15
uv --version
node --version
ffmpeg -version
ffprobe -version
```

FFmpeg 是独立的第三方程序，本仓库和 wheel 均不捆绑 FFmpeg 二进制。若使用自定义
FFmpeg 构建，请自行核对该构建的许可证和编码器配置。

### 2. 下载并安装两个 CLI

从 [GitHub Releases](https://github.com/lechangbin/video-material-agent-toolkit/releases)
下载：

```text
video_material_collector-0.2.0-py3-none-any.whl
semvideo-0.1.3-py3-none-any.whl
SHA256SUMS.txt
```

也可以使用 GitHub CLI：

```powershell
gh release download v0.2.0 `
  --repo lechangbin/video-material-agent-toolkit `
  --dir .\video-toolkit-release
```

验证 SHA-256 后安装：

```powershell
uv tool install --python 3.14 `
  .\video-toolkit-release\video_material_collector-0.2.0-py3-none-any.whl

python -m pip install --user `
  .\video-toolkit-release\semvideo-0.1.3-py3-none-any.whl
```

或者运行随 Release 下载的安装脚本；脚本会先验证两个 wheel 的 SHA-256：

```powershell
.\video-toolkit-release\install-tools.ps1 `
  -ReleaseDirectory .\video-toolkit-release `
  -BrowserChannel auto
```

验证：

```powershell
material-collector --help
semvideo --version --json
```

`material-collector` 通过 `uv tool` 使用独立环境，避免与 Semvideo 的 Python 依赖
发生冲突。Semvideo Skill 能解析用户级安装的 CLI 绝对路径，因此 Agent 不要求继承
Python Scripts 的 PATH。

### 3. 一条命令安装 Skills 到多个 Agent

先查看仓库中的 Skills：

```powershell
npx skills add lechangbin/video-material-agent-toolkit --list
```

安装全部 Skills 到 Codex 和 Pi：

```powershell
npx skills add lechangbin/video-material-agent-toolkit `
  --skill '*' --agent codex --agent pi --global --yes
```

安装到所有已支持的 Agent：

```powershell
npx skills add lechangbin/video-material-agent-toolkit `
  --skill '*' --agent '*' --global --yes
```

`npx skills` 支持 Codex、Claude Code、Cursor、Pi、OpenCode、Cline 等多种 Agent，
并为每个 Agent 选择正确的全局或项目级目录。查看和更新已安装 Skills：

```powershell
npx skills list --global
npx skills update --global --yes
```

如果某个宿主（例如自定义 WorkBuddy 构建）没有被 `npx skills` 自动识别，把 `skills/`
下面的四个完整目录复制到该宿主配置的 Skills 根目录。目标结构必须保持为
`<skills-root>/<skill-name>/SKILL.md`，不要只复制 `SKILL.md`。

## Semvideo 初始化与 FFmpeg 检查

API Key 只通过目标机器的环境变量或安全凭据机制提供，不要写入仓库、Skill、命令
参数、工作区配置或日志。

```powershell
$env:SEMVIDEO_API_KEY = '<在当前终端注入密钥>'

semvideo init C:\video-workspace --json
semvideo doctor --workspace C:\video-workspace --json
```

如果 `doctor` 找不到 FFmpeg，但你知道其绝对路径：

```powershell
semvideo config set-media-tools `
  --ffmpeg C:\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\ffmpeg\bin\ffprobe.exe `
  --workspace C:\video-workspace --json

semvideo doctor --workspace C:\video-workspace --json
```

`doctor` 必须确认 FFmpeg、FFprobe、`libx264`、`aac` 和 `-fps_mode` 可用后，才提交
视频理解任务。

## 最小使用流程

1. 主 Agent 根据完整文案和主题分段生成版本化 QueryPlan。
2. `material-collector` 搜索三平台并下载 720p 理解代理。
3. 到达 `integration_required` 后，Semvideo 理解新代理。
4. 隔离子 Agent 根据结构化理解结果选择 Top-K。
5. 主 Agent 判断素材是否充分；不足时生成下一轮 QueryPlan。
6. 剪辑真正采用某个来源后，再按需下载高质量媒体。

推荐让 Agent 使用：

```text
search-understand-refine-video-materials
```

它会加载另外三个组件 Skill，并通过版本化文件连接两个 CLI。

## 从源码开发和构建

搜索下载工具：

```powershell
Set-Location .\packages\material-collector
uv sync
uv run pytest
uv run ruff check .
uv run mypy .
```

Semvideo：

```powershell
Set-Location .\packages\semvideo
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
```

构建两个 wheel：

```powershell
.\scripts\build-release.ps1
```

产物写入仓库根目录的 `dist/`，同时生成 `SHA256SUMS.txt`。

## 数据与凭据边界

- 搜索会话状态和代理媒体只写入调用方显式指定的素材工作区；
- Semvideo 任务、日志、证据和理解结果只写入初始化工作区的 `semvideo-data/`；
- 平台登录 profile 和 Cookie 不进入仓库、wheel、Skill 或素材工作区；
- 不要提交 API Key、Cookie、媒体文件、任务数据库或浏览器 profile；
- 搜索工具默认禁用系统代理和环境代理，不使用 `127.0.0.1:10808`。

## 许可证

本仓库原创代码和 Skills 使用 [MIT License](LICENSE)。第三方依赖、外部 CLI、平台
内容和用户安装的 FFmpeg 构建分别适用其自身许可证与条款。详见各包的第三方说明。
