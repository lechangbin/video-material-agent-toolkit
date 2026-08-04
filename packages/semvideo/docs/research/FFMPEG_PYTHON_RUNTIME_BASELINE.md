# FFmpeg 与 Python 正式运行时基线研究

> 调研日期：2026-07-28
> 范围：Windows 单机版 Semvideo V1；只采用 FFmpeg、Python、PyPI 和各依赖项目的官方资料。

## 结论摘要

正式 V1 建议采用以下基线：

| 项目 | V1 决策 |
| --- | --- |
| Python | 发布和主要开发环境使用标准 GIL 版 **CPython 3.14.6 x64**；项目声明 `>=3.14.6,<3.15`；暂不支持 free-threaded `3.14t` |
| FFmpeg | 认证版本为 **FFmpeg 8.1.2**；兼容测试范围为 **7.1.x–8.1.x**；不使用 git master/nightly 作为发布基线 |
| 帧率模式参数 | 内部命令只生成 `-fps_mode[:stream_specifier] vfr`，不再生成 `-vsync vfr` |
| FFmpeg 交付 | V1 采用“受管理的固定版本运行时 + 可显式覆盖路径”的混合方式；不只依赖 `PATH` |
| 兼容策略 | 启动时执行版本和能力探测，实际功能以能力探测结果为准，而不是只比较版本号 |
| ASR | `faster-whisper 1.2.1`、`CTranslate2 4.8.1`、`PyAV 18.0.0` 已有 Python 3.14/Windows 支持；GPU 运行时另行验证 |

本机当前已经是 FFmpeg `8.1.2-full_build-www.gyan.dev`，即截至调研日 FFmpeg 官网列出的最新稳定版本；因此 FFmpeg 不需要再“升级大版本”，需要做的是固定、探测和测试这个版本。Python 当前为 3.14.3，可升级到同一稳定分支的 3.14.6。

## 1. `-vsync vfr` 到底是弃用还是已删除

### 官方事实

FFmpeg 在 2022 年加入了输出流级别的 `-fps_mode`，同时明确将全局 `-vsync` 标为弃用，并说明未来会移除；`-fps_mode` 可以针对单个输出流设置并覆盖 `-vsync`。[FFmpeg 官方提交记录](https://ffmpeg.org/pipermail/ffmpeg-cvslog/2022-June/132700.html)

2023 年，FFmpeg 又把 `-vsync` 放入可在未来关闭的编译条件中，进一步表明该选项不应再被新代码依赖。[FFmpeg 官方提交记录](https://ffmpeg.org/pipermail/ffmpeg-cvslog/2023-December/140217.html)

截至调研日，当前官方文档只列出输出、逐流选项 `-fps_mode[:stream_specifier]`；其中 `vfr` 的含义是保留帧时间戳，并在必要时丢帧以避免两个帧具有相同时间戳。[FFmpeg 当前 CLI 文档](https://ffmpeg.org/ffmpeg.html#Advanced-Video-options)

但是，**“弃用”不等于“官方 8.1.2 已拒绝”**。官方 8.1.2 标签中的 `fftools/ffmpeg.h` 仍将 `FFMPEG_OPT_VSYNC` 定义为 `1`，而 `ffmpeg_opt.c` 仍在该条件下注册 `-vsync`。[FFmpeg 8.1.2 `ffmpeg.h`](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/fftools/ffmpeg.h#L60-L61)、[FFmpeg 8.1.2 `ffmpeg_opt.c`](https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/fftools/ffmpeg_opt.c#L2193-L2197)

### 本机复核

本机 `FFmpeg 8.1.2-full_build-www.gyan.dev` 的最小 lavfi 测试结果为：

- `-vsync vfr`：退出码 `0`，标准错误输出 `-vsync is deprecated. Use -fps_mode`。
- `-fps_mode vfr`：退出码 `0`，无该弃用警告。

所以，之前遇到的“拒绝参数”不能仅凭现有证据归因于“所有新版 FFmpeg 都移除了 `-vsync`”。还可能涉及不同的下游构建、快照版本、参数位置、包装器把标准错误误判为失败，或同一命令中的其他选项。若要确定原故障原因，需要保留当时的完整命令、`ffmpeg -version`、标准错误和退出码。

### 对正式代码的决定

尽管当前 8.1.2 仍兼容旧参数，正式代码仍应立即迁移：

```text
旧：-vsync vfr
新：-fps_mode:v:0 vfr
```

`-fps_mode` 是**输出、逐流选项**，必须放在对应输出文件之前；只有一个视频输出流时也可使用 `-fps_mode vfr`。不要通过“先尝试旧参数、失败再回退”的方式保留技术债。

## 2. FFmpeg 稳定版本选择

FFmpeg 官网截至 2026-07-28 提供的稳定分支包括：

- 8.1.2，发布于 2026-06-17，是 8.1 分支的最新稳定版。
- 8.0.3，发布于 2026-06-18。
- 7.1.5，发布于 2026-06-20。
- 6.1.6，发布于 2026-06-20。

FFmpeg 约每六个月产生一个主要版本，稳定分支的点版本以重要修复为主；官网当前默认下载也是 8.1.2。[FFmpeg 官方下载页](https://ffmpeg.org/download.html)

### 推荐范围

- **认证版本：8.1.2**。开发机、CI 主路径和 Windows 发布包均以该版本验证。
- **兼容测试下限：7.1.5**。V1 不主动支持更旧版本，以控制媒体组合和回归矩阵。
- **允许范围：7.1.x、8.0.x、8.1.x 的已验证点版本**。未来升级先进入兼容测试，再修改认证版本。
- **不使用 nightly/git master 交付**。开发分支变化快，不适合作为可复现的桌面工具运行时。

这个下限是项目的测试与支持政策，并不表示 `-fps_mode` 到 7.1 才出现；该选项在 2022 年已经加入。设置较新的下限是为了减少旧版 CLI、过滤器、编解码器和安全修复差异。

### Windows 构建来源

FFmpeg 项目本身只发布源代码，官网把 Windows 可执行文件链接到 Gyan 和 BtbN 等外部构建方。[FFmpeg 官方下载页](https://ffmpeg.org/download.html#build-windows) 因此必须同时固定：

- FFmpeg 上游版本；
- Windows 构建来源和构建变体；
- 下载地址；
- SHA-256；
- `ffmpeg -version` 的完整配置串；
- `ffprobe` 的配套版本。

当前本机 Gyan `full_build` 启用了 `--enable-gpl --enable-version3` 和 `libx264` 等 GPL 组件。若随产品再分发该二进制，必须先完成许可证方案和第三方声明；FFmpeg 官方说明，启用 GPL 部件时 GPL 会适用于整个 FFmpeg 构建。[FFmpeg 官方法律说明](https://ffmpeg.org/legal.html)

## 3. Python 与主要依赖的兼容性

### Python 版本

截至调研日，Python 3.14 处于 bugfix 阶段，最新正式维护版为 **3.14.6**；3.15 仍是预发布分支，3.13 也仍处于 bugfix 阶段。[Python 版本状态](https://devguide.python.org/versions/)、[Python 3.14 发布计划](https://peps.python.org/pep-0745/)

因此推荐：

- Windows 发布运行时固定标准版 CPython 3.14.6 x64。
- `pyproject.toml` 使用 `requires-python = ">=3.14.6,<3.15"`，仓库 `.python-version` 固定为 `3.14.6`。
- CI 和发布环境固定 3.14.6；升级维护版本时显式修改固定文件并重新运行兼容矩阵。
- V1 不采用 free-threaded Python。即使部分依赖已有 `3.14t` wheel，Worker 并发将通过多进程和阶段调度实现，不值得同时引入 free-threaded 原生扩展兼容变量。
- Python 3.15 正式发布并且所有原生依赖 wheel、端到端测试通过后，再单独升级。

### 媒体与 ASR 依赖

- `faster-whisper 1.2.1` 要求 Python 3.9+；其音频解码使用 PyAV 自带的 FFmpeg 库，因此 **faster-whisper 不直接依赖系统 `ffmpeg.exe`**。[faster-whisper 官方仓库](https://github.com/SYSTRAN/faster-whisper#requirements)、[faster-whisper PyPI](https://pypi.org/project/faster-whisper/)
- `CTranslate2 4.8.1` 要求 Python 3.9+，官方同时发布了 Windows x64 的 CPython 3.14 wheel；Windows GPU wheel 使用 CUDA 12.x，语音卷积模型还涉及 cuDNN 版本要求。[CTranslate2 安装文档](https://opennmt.net/CTranslate2/installation.html)、[CTranslate2 PyPI](https://pypi.org/project/ctranslate2/)
- `PyAV 18.0.0` 要求 Python 3.11+，提供 Windows x64 的 CPython 3.11+ ABI wheel；其 wheel 自带 FFmpeg 库。[PyAV PyPI](https://pypi.org/project/av/)
- `Pillow 12.3.0` 正式支持 Python 3.10–3.14，并在 Windows Server 2025 上持续测试 3.14。[Pillow Python 支持矩阵](https://pillow.readthedocs.io/en/stable/installation/python-support.html)、[Pillow 平台支持](https://pillow.readthedocs.io/en/stable/installation/platform-support.html)

系统 `ffmpeg.exe` 与 PyAV wheel 内部的 FFmpeg 是两个独立运行时：前者负责抽帧、字幕处理和最终渲染；后者主要供 faster-whisper 解码音频。升级其中一个不会自动升级另一个，诊断信息必须分别记录。

### 曾评估的本地 API 与数据库候选栈

以下内容是运行时调研时的兼容性记录，不是当前 V1 依赖决策。架构现已确定 V1 不引入 FastAPI、Uvicorn、SQLAlchemy 或 SQLite 任务队列；Pydantic 是否用于文件 Schema 校验仍由正式实现切片决定。若未来出现持续人工操作需求并增加有状态 HTTP 服务，可重新验证这些版本。

若未来采用 FastAPI/Pydantic/SQLAlchemy，调研时的稳定版本均已有 Python 3.14 支持：

- FastAPI 当前元数据声明 Python 3.10+，列出 Python 3.14。[FastAPI PyPI](https://pypi.org/project/fastapi/)
- Pydantic 从 2.12 开始支持 Python 3.14；Pydantic V1 不支持 Python 3.14+。[Pydantic PyPI](https://pypi.org/project/pydantic/)
- SQLAlchemy 2.0.51 提供 CPython 3.14 Windows wheel。[SQLAlchemy PyPI](https://pypi.org/project/SQLAlchemy/)

这些结论证明 3.14 不会阻塞未来服务化，但当前具体依赖只在正式包骨架和首个 Worker 切片需要时锁定，不能因为候选库兼容就提前引入。

## 4. FFmpeg 打包与能力探测

### 推荐交付模式

V1 采用“受管理运行时优先、允许显式覆盖、最后才查找系统 PATH”的解析顺序：

1. 配置文件 `[media]` 中的 `ffmpeg_path` / `ffprobe_path`；
2. Semvideo 管理目录中带版本和校验和的固定运行时；
3. 系统 `PATH`；
4. 均不可用时给出可操作的安装错误。

发布包不应只依赖用户机器恰好安装了某个 FFmpeg；否则任务恢复后可能因为 PATH、版本或构建功能变化而产生不可复现结果。同时，不建议把大型 FFmpeg 二进制直接塞进 Python wheel；更适合由 Windows 安装包或独立的受管理运行时安装步骤提供，并保存来源、许可证和校验信息。

### `doctor` 必须检查的内容

`semvideo doctor` 和 Worker 启动前探测至少包括：

- `ffmpeg`、`ffprobe` 是否来自同一受信目录，版本是否匹配；
- `ffmpeg -version`、构建配置和 SHA-256；
- 是否存在 `-fps_mode`，以及 `-fps_mode vfr` 最小 lavfi 冒烟测试是否成功；
- 所需过滤器：`select`、`scale`、`tile` 或项目实际使用的等价过滤器；
- 所需 demuxer/muxer、字幕能力和目标编码器；
- 对一个项目内极小测试媒体执行 probe、抽帧、音频抽取和 MP4 渲染；
- 报告“缺少可选硬件编码器”与“缺少 V1 必需能力”的区别。

版本号只用于快速拒绝明显不支持的版本；真正可用性以能力探测为准。不同 Windows 构建即使版本相同，启用的编解码器、过滤器和许可证选项也可能不同。

### 任务可复现性

每个 Job 的运行清单应记录：

- Python 完整版本和标准/free-threaded 构建类型；
- Semvideo 版本；
- `ffmpeg`、`ffprobe` 的版本、路径、SHA-256 和配置串；
- PyAV、faster-whisper、CTranslate2、Pillow 版本；
- GPU、CUDA、cuDNN 和实际 ASR compute type；
- 生成每项媒体产物时使用的规范化参数模板版本。

## 5. 后续正式开发任务

以下内容应进入 V1 开发任务，而不是只保留在兼容性备注中：

1. 将原型 `src/semvideo_prototype/pipeline.py` 和参考 CRV 0.7.16 中的 `-vsync vfr` 全部替换为内部命令构建器产生的 `-fps_mode`；禁止业务模块手拼 FFmpeg 参数。
2. 实现 `FfmpegRuntime` / `FfmpegCapabilities` Adapter，统一解析路径、版本、构建配置、能力和失败原因。
3. 增加 `semvideo doctor --json`，稳定输出 Python、FFmpeg、FFprobe、ASR/GPU 能力以及必需/可选检查结果。
4. 建立 Windows FFmpeg 运行时清单：固定 8.1.2 构建来源、下载地址、SHA-256 和构建配置。
5. 在 CI 或专用兼容机上运行 FFmpeg 7.1.5 与 8.1.2 冒烟矩阵；至少覆盖 CFR/VFR 输入、无音频、内嵌字幕、中文路径和最终 MP4。
6. 把 FFmpeg 的标准错误、退出码和规范化参数保存到阶段日志；发生兼容故障时不得只记录 Python 异常摘要。
7. 将 Python 发布基线升级到 3.14.6，生成锁文件并验证 Windows 全新虚拟环境安装。
8. 为 CPU ASR 和 NVIDIA GPU ASR 分别锁定、探测和测试依赖；GPU 任务明确校验 CUDA/cuDNN，不因 `ctranslate2` 可导入就判定 GPU 可用。
9. 添加运行时升级流程：新 FFmpeg/Python 先通过兼容矩阵，运行中的 Job 继续记录并使用其原运行时身份，不能静默改变恢复结果。
10. 公开发行物不捆绑 FFmpeg 二进制，因此 FFmpeg 二进制再分发审查不属于当前 wheel 发布范围；若以后改为捆绑分发，再创建许可证与 `THIRD_PARTY_NOTICES.md` 专项任务。

## 风险与待确认项

- 之前“`-vsync vfr` 被拒绝”的原始 stderr 和退出码尚未留存，故障根因仍未证实；迁移参数可以消除该风险，但不能替代故障记录。
- FFmpeg 8.1.2 是上游版本，不等于所有 Windows 8.1.2 构建能力相同；必须固定具体构建。
- Python 3.14 的通用生态已满足本项目，但 GPU ASR 仍受 NVIDIA 驱动、CUDA 和 cuDNN 组合影响，需要在目标机器单独认证。
- 当前公开发行物只提供安装指导，不再分发 GPL-enabled FFmpeg full build；若以后捆绑该构建，其分发方式必须重新评估。
