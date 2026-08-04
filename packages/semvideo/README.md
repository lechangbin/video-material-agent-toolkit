# semvideo

`semvideo` 是当前项目的工作名称。项目目标是把视频转换为可追溯、可检索、可导出的语义片段，并首先向本地 Agent 和终端用户提供稳定的 CLI 与程序接口。

正式 V1 纵向切片已经可以运行：工作区、源视频内容寻址、文件任务状态、Windows 无窗口 Worker、原生 FFmpeg/ASR 证据、九宫格、模型结构修复、语义合并、复核标记、检索记录、检查报告和子视频导出均已接通。常规测试使用本地假模型服务，不产生云端费用；指定真实样例已通过硅基流动 `Qwen/Qwen3.6-35B-A3B` 和全量渲染，当前得到三个按叙事阶段组织的正式片段，等待用户观看确认边界质量。

V1 不开发前端，也不运行常驻 HTTP 服务或 SQLite 任务队列。`semvideo process` 创建自描述任务目录并启动独立无窗口 Worker；关闭终端后任务继续运行，Agent 通过 CLI 查询、取消、恢复和重试。未来只有在持续人工操作成为真实需求时，才在同一应用接口外增加有状态 HTTP 服务。

视频处理工作区是持久化边界。CLI 程序可以全局安装，但源视频副本、任务状态、日志、证据和子视频全部保存在当前工作区的 `semvideo-data/` 中。同一工作区可由不同 Agent 会话继续使用，不同工作区互相隔离。

超过 `1280×720` 的源视频会在受管源目录中自动生成一份共享 720p 分析代理，
用于场景检测、关键帧和镜头语言分析；字幕、ASR 以及所有片段/镜头导出始终使用
原分辨率源视频。小于或等于上限的视频不转码。

工作区必须显式初始化。其他命令从当前目录向上查找 `semvideo-data/workspace.json`，也可以通过 `--workspace <path>` 指定；找不到标记时不会静默创建数据目录。

## 运行时基线

- CPython `3.14.6` x64，标准 GIL 构建；仓库根目录的 `.python-version` 是开发环境的固定版本。
- FFmpeg/ffprobe `8.1.2` 是 V1 认证版本；实际可用性还必须通过 `doctor` 能力检查。
- 可在 `semvideo-data/config.toml` 的 `[media]` 中配置绝对
  `ffmpeg_path`/`ffprobe_path`；Doctor、Worker 和按需导出共用该配置。
  Agent 应使用 `semvideo config set-media-tools --ffmpeg <path>
  --ffprobe <path> --workspace <root> --json`，不得直接编辑任务包配置。
- FFmpeg 命令使用输出流级 `-fps_mode:v:0 vfr`，不再新增已弃用的 `-vsync vfr`。

## 目标流水线

```text
输入视频
→ 候选边界与证据时间线
→ 生成镜头时间线、镜头内连续帧与镜头语言标注
→ 九宫格、关键帧与带时间戳文本
→ 多模态模型在长上下文窗口中提出语义分段
→ 协调并验证连续时间线
→ 生成正式片段及摘要
→ 生成供外部搜索 Skill 使用的检索片段记录
→ 保存文件任务状态、检查点和媒体产物
```

Semvideo 只负责视频理解，不实现查询、embedding、相关性评分或 Top-K。每个任务输出 `retrieval/segments.jsonl`，包含片段时间、标题、长短摘要、视觉描述、主题、实体、动作、关键词、完整片段转写、质量状态和媒体引用，供其他目录中的搜索 Skill 消费。任务还输出 `segmentation/shot-timeline.json` 和 `semantics/cinematography-annotations.jsonl`，记录航拍视点、景别、推进/拉远、升高/降低、速度变化、镜头语言摘要和关键词。镜头超过九个证据帧时生成并上传多张联系图；全部镜头帧和联系图进入最终 manifest。分页列表默认省略完整转写，单片段查询返回完整内容。正式片段内的细粒度 `unit` 尚属后续范围，当前 CLI 不提供 `unit list/show/export`。

正式 CLI 默认不批量渲染子视频；外部 Top-K Skill 可以先筛选记录，再调用
`segment export` 向任务包外导出命中片段，该命令不会改写已完成任务包。需要
一次性生成并登记全部正式子视频时显式使用 `process --render`。

项目借鉴 [`claude-real-video`](https://github.com/HUANGCHIHHUNGLeo/claude-real-video) 的本地视频预处理思路，包括场景感知抽帧、帧去重、字幕优先、Whisper 回退、时间戳保留和本地检查页面。第三方项目只负责或启发“证据提取”阶段；语义边界、合并计划、正式摘要、任务恢复和持久化由本项目自行实现。

## 设计文档

- [领域词汇](./CONTEXT.md)
- [系统架构](./docs/ARCHITECTURE.md)
- [接口、CLI、产物与数据契约](./docs/CONTRACTS.md)
- [正式开发注意事项](./docs/FORMAL_DEVELOPMENT_NOTES.md)
- [V1 正式实施计划](./docs/V1_IMPLEMENTATION_PLAN.md)
- [V1 当前开发状态](./docs/V1_STATUS.md)
- [并发上限人工调优指南](./docs/CONCURRENCY_TUNING_GUIDE.md)
- [本机媒体与渲染并发验证](./docs/research/CONCURRENCY_MEDIA_RENDER_LOCAL.md)
- [本机 ASR 并发验证](./docs/research/CONCURRENCY_ASR_LOCAL.md)
- [云端 LLM 官方限流研究](./docs/research/CONCURRENCY_LLM_OFFICIAL_LIMITS.md)
- [首个原型计划](./docs/PROTOTYPE.md)
- [真实视频原型结果](./docs/PROTOTYPE_RESULT.md)
- [架构决策](./docs/adr/)

## 安装与正式 CLI

从源码安装到当前 Python 的全局用户环境：

```powershell
python --version  # 必须为 3.14.6
python -m pip install .
semvideo --version --json
```

也可以安装已经构建的 wheel：

```powershell
python -m pip install .\dist\semvideo-0.1.3-py3-none-any.whl
```

开发环境：

```powershell
python --version  # 必须为 3.14.6
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\semvideo.exe init .
$env:SEMVIDEO_API_KEY = '<your key>'
.\.venv\Scripts\semvideo.exe doctor --workspace . --json
```

提交后台任务；命令返回后可以关闭终端：

```powershell
.\.venv\Scripts\semvideo.exe process C:\path\to\video.mp4 `
  --workspace . `
  --idempotency-key my-request-001 `
  --json
```

Agent 批量提交前必须运行已安装 Semvideo Skill 的
`scripts/load_context.py --workspace <root> --profile <name>`。它通过公开 CLI
加载有效配置、Doctor、当前任务和应用层可用提交槽；不得在未经过门禁时展开整个
视频目录。`process/resume/retry` 还会在工作区提交锁内原子复核容量，因此多个
Agent 会话不能靠同时读取同一快照突破上限。

查询、恢复与读取结果：

```powershell
.\.venv\Scripts\semvideo.exe job status <job-id> --workspace . --json
.\.venv\Scripts\semvideo.exe job logs <job-id> --workspace . --json
.\.venv\Scripts\semvideo.exe job resume <job-id> --workspace . --json
.\.venv\Scripts\semvideo.exe segment list <job-id> --workspace . --json
.\.venv\Scripts\semvideo.exe segment show <job-id> <segment-id> --workspace . --json
.\.venv\Scripts\semvideo.exe shot list <job-id> --workspace . `
  --viewpoint aerial --scale extreme_wide --motion push_in --motion rise `
  --speed slow --keyword 缓慢推进 --keyword 逐渐拉高 --json
.\.venv\Scripts\semvideo.exe shot show <job-id> <shot-id> --workspace . --json
.\.venv\Scripts\semvideo.exe shot export <job-id> <shot-id> `
  --workspace . --output C:\path\to\shot.mp4 --json
.\.venv\Scripts\semvideo.exe segment export <job-id> <segment-id> `
  --workspace . --output C:\path\to\clip.mp4 --json
```

需要等待终态并批量导出全部正式片段：

```powershell
.\.venv\Scripts\semvideo.exe process C:\path\to\video.mp4 `
  --workspace . --render --wait --json
```

`process --wait --json` 成功后直接返回 `segment_records.path`、`cinematography_records.path`、正式片段数、镜头数、镜头语言标注数、复核数、报告和渲染引用。分页 `segment list` 不返回全文转写；`segment show` 返回单条完整记录。`shot list` 支持按视点、景别、一个或多个运镜、速度和中文关键词做确定性过滤；重复的同类条件按 AND 处理。所有任务事实位于 `<workspace-root>/semvideo-data/`，不依赖创建任务的 Agent 会话。

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

构建 wheel：

```powershell
python -m pip wheel . --no-deps --wheel-dir dist
```

Agent Skill 源码位于 `skills/semvideo/`。发布时把整个 `semvideo` 目录复制到目标
Agent 的技能根目录；例如 Codex：

```powershell
Copy-Item -Recurse .\skills\semvideo `
  "$env:USERPROFILE\.codex\skills\semvideo"
```

CLI 与 Skill 必须成对使用：当前均为 `0.1.3`，Skill 协议版本为 `1`。
Skill 会先运行 `scripts/resolve_semvideo.py`，发现并验证 CLI 的绝对路径，因此
目标 Agent 不需要继承用户级 Python Scripts 的 PATH。安装副本不得直接修改；
所有变更先落到仓库 `skills/semvideo/`，再重新复制或打包。

## 原型保留策略

可抛弃原型仍保存在 `prototypes/real_video_semantics/`，只用于对照旧实验和 CRV 思路。正式包 `src/semvideo/` 不依赖 `claude-real-video`。在指定真实样例通过正式云端模型、全量渲染和人工观看验收后，删除原型运行代码及其可选依赖。

正式 Agent Skill 已加入本仓库 `skills/semvideo/` 并与 CLI 同版本维护。Skill
只调用公开 CLI，不直接操作任务文件；真实样例的人工边界确认仍决定何时删除原型，
不再阻塞 Skill 的通用 Agent 测试。

## 安全

视频画面、字幕、OCR 和模型输出均视为不可信数据。V1 模型 API Key 只从环境变量读取；工作区配置可以记录环境变量名，但不得记录密钥值。任何真实密钥都不得写入仓库、命令行参数、日志、任务产物或示例配置。
