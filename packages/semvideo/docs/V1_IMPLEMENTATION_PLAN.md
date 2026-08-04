# Semvideo V1 正式实施计划

## 1. V1 的可用性定义

V1 必须能够被通用 Agent 通过 CLI 实际使用：

```text
输入真实本地视频
→ 后台创建并执行任务
→ 生成候选边界、关键帧、九宫格、字幕或 ASR
→ 调用真实多模态 LLM 理解跨镜头的连续叙事事件
→ 协调并验证语义分段
→ 生成正式摘要
→ 生成检索片段记录
→ 按需导出可观看的正式子视频
→ Agent 查询任务状态和结构化结果
```

仅实现 CLI 壳、任务状态、`ffprobe`、抽帧、九宫格或 Mock LLM 不算可用 V1。LLM 是 Semvideo 形成语义片段的核心能力，必须进入首个正式端到端验收。

## 2. 首个正式纵向切片

### 2.1 共享应用 Interface

实现并测试：

```text
create_job(request) -> JobId
launch_job(job_id) -> ProcessingAttempt
run_job(job_id, attempt_id, progress_sink) -> JobResult
get_job(job_id) -> JobSnapshot
cancel_job(job_id) -> JobSnapshot
resume_job(job_id, from_stage?) -> ProcessingAttempt
retry_job(job_id, from_stage?) -> ProcessingAttempt
```

CLI、无窗口 Worker 和测试都调用这些用例，不复制流水线规则。

### 2.2 文件任务与后台 Worker

- 实现 `semvideo init <workspace-root>`，显式创建工作区标记和目录。
- 其他命令从当前目录向上发现工作区，也允许 `--workspace` 明确指定；找不到标记时失败。
- 将视频处理工作区作为持久化边界，全部任务内容保存到 `<workspace-root>/semvideo-data/`。
- 默认复制源视频到当前工作区的受管 `sources/` 目录。
- 同一工作区跨 Agent 会话共享任务，不同工作区不隐式共享任务或源文件。
- 通过内容哈希复用源视频；同一源视频允许多个处理任务。
- 创建 `job.json`、`state.json`、`worker.json`、`events.jsonl` 和阶段检查点。
- Windows 上启动不显示终端窗口的独立 Worker。
- `process` 在 Worker 成功启动后返回任务 ID；`--wait` 只观察，不承担执行。
- 状态文件使用原子替换；Worker 身份使用 PID 与进程创建时间共同确认。

### 2.3 原生媒体与证据模块

- FFmpeg/ffprobe 运行时探测。
- 媒体事实与内容指纹。
- 候选场景边界及最短、最长范围约束。
- 原生关键帧提取与近重复过滤。
- 内嵌字幕优先、faster-whisper 回退。
- 转写、关键帧、候选边界和时间戳对齐。
- 时序九宫格与格子到源时间点映射。
- 长视频使用连续、重叠的证据窗口。

正式包不依赖 `claude-real-video`；原型只作为算法和产物参考。

### 2.4 真实 LLM 语义路径

- 首个主 Profile 使用 `Qwen/Qwen3.6-35B-A3B` 非思考模式。
- 模型 Adapter 只从配置所指向的环境变量读取凭据，默认名称为 `SEMVIDEO_API_KEY`；工作区配置不保存密钥值。
- Worker 通过继承环境获得凭据，命令行、状态、事件、日志和模型运行记录不得包含密钥。
- Prompt 明确九宫格是证据而不是分割模板。
- 模型根据主题、任务、动作阶段和前后文判断跨镜头连续事件。
- 输出引用合法候选边界，不得虚构时间点。
- 原始响应、结构修复响应、模型、Prompt、Schema、用量和耗时全部留档。
- 严格验证 JSON Schema、边界引用、时间顺序、连续覆盖和置信状态。
- 429 使用跨 Worker 共享冷却和抖动指数退避；503/504 与账户限流分开处理。
- 不设置单任务 token 硬预算；证据窗口和请求规模根据视频长度、证据密度、图片数量及模型上下文上限动态生成，与主 Agent 上下文预算无关。
- 单请求输出上限只用于满足模型技术限制和约束结构化响应，实际 token 与费用继续记录但不自动中止任务。

离线测试可以使用保存的响应夹具，但 V1 真实视频验收必须调用一次真实配置模型。验收调用应由用户明确启动，记录成本，不在普通单元测试中自动产生费用。

### 2.5 协调、计划、摘要与渲染

- 协调重叠窗口中的重复、缺口、重叠和冲突。
- 只允许合并源时间线上相邻的候选片段。
- 验证完整、连续、无重叠且不越界的正式时间线。
- 对每个正式片段生成标题、长短摘要、视觉描述、主题、实体、动作、关键词和质量状态。
- 将正式片段、摘要、片段内转写和产物引用投影为稳定的检索片段记录。
- `retrieval/segments.jsonl` 保存片段范围内的完整转写；分页列表默认省略完整转写，单片段查询返回。
- 按时间顺序写入 `retrieval/segments.jsonl`，供其他目录中的搜索或 Top-K Skill 消费。
- Semvideo 不实现查询、embedding、相关性评分或 Top-K。
- 默认不批量渲染；`process --render` 使用 FFmpeg 生成并登记任务包内子视频，
  `segment export` 只向任务包外输出指定片段且不改变完成任务包；两者均校验
  时长和可解码性。
- 生成自包含检查报告，显示正式片段、九宫格、字幕和模型理由。

### 2.6 首批 CLI

```text
semvideo init <workspace-root> [--json]
semvideo workspace show [--workspace <path>] [--json]
semvideo doctor [--json]
semvideo process <video> [--profile <name>] [--render] [--wait] [--idempotency-key <key>] [--json]
semvideo job list [--state <state>] [--json]
semvideo job status <job-id> [--json]
semvideo job logs <job-id> [--follow] [--quiet]
semvideo job cancel <job-id> [--json]
semvideo job resume <job-id> [--json]
semvideo job retry <job-id> [--from <stage>] [--json]
semvideo inspect <job-id> [--open]
semvideo segment list <job-id> [--review-only] [--offset <n>] [--limit <n>] [--json]
semvideo segment show <job-id> <segment-id> [--json]
semvideo segment export <job-id> <segment-id> --output <path>
```

每个命令必须提供 `--help`。机器可读输出、标准错误和退出码遵守 [`CONTRACTS.md`](./CONTRACTS.md)。

## 3. 开发任务顺序

### 任务 1：正式包骨架与纯领域规则

- 新建 `src/semvideo/`，与 `src/semvideo_prototype/` 分离。
- 建立领域类型、时间范围和合并不变量。
- 迁移并重写可复用的纯合并逻辑。
- 添加单元测试，覆盖完整覆盖、相邻合并、复核边界和非法计划。

完成标志：纯领域测试通过，正式代码不导入原型包。

### 任务 2：文件任务包与真实媒体探测

- 实现源文件复制、内容哈希、任务创建和原子 JSON。
- 实现显式工作区初始化、向上发现和 `--workspace` 覆盖。
- 实现 Worker 启动、日志、PID 身份与状态协调。
- 运行真实 ffprobe，形成首个阶段检查点。
- 接入 `doctor`、`process`、`job list/status/logs`。

完成标志：关闭调用终端后，真实视频探测继续完成，另一个 CLI 会话可查询结果。

这只是首个纵向切片的内部里程碑，不作为可用 V1 对外交付。

### 任务 3：候选边界与原生证据

- 实现 FFmpeg 候选边界、关键帧、字幕、ASR、九宫格和证据窗口。
- 使用 `-fps_mode:v:0 vfr`，不使用弃用的 `-vsync vfr`。
- 为每个阶段写入可验证检查点。
- 对照原型样例检查证据完整性和时间映射。

完成标志：不调用 LLM 时能够生成完整、可检查、可复用的证据包。

这仍是内部里程碑，不作为可用 V1 对外交付。

### 任务 4：真实 LLM 语义闭环

- 实现模型 Profile、Prompt 版本和结构化响应验证。
- 完成长证据窗口提案、窗口协调、合并计划和正式摘要。
- 实现 429、503、504、超时、无效 JSON 和有界重试。
- 保存原始模型运行记录及成本。

完成标志：真实样例得到连续正式时间线和摘要，所有正式片段都能追溯到证据与模型运行。

### 任务 5：子视频与检查报告

- 实现惰性渲染；默认理解任务不生成全部子视频。
- 实现 `process --render` 批量生成登记产物，以及不改变任务包的
  `segment export` 单片段外部按需导出。
- 校验解码、范围和覆盖误差。
- 生成 `retrieval/segments.jsonl`，包含时间、结构化语义、片段转写、质量状态和媒体引用。
- 生成自包含检查报告。
- 用用户既有样例完成人工观看验收。

完成标志：Agent 能返回检索片段记录目录和子视频列表；外部搜索 Skill 可读取记录，用户可直接观看判断分段质量。

### 任务 6：并发、取消与恢复

- 实现 `media=2`、`asr=1`、`llm=2`、`render=1` 分类锁，以及 `ffmpeg_cpu=2` 共享预算。
- 所有 CPU FFmpeg 路径按“共享槽 → 分类槽”的固定顺序获取锁。
- 实现共享冷却、资源等待事件和超时。
- 实现取消、显式恢复和指定阶段重试。
- 验证多个视频同时提交、关闭终端、异常杀死 Worker 后恢复。
- 根据 [`CONCURRENCY_TUNING_GUIDE.md`](./CONCURRENCY_TUNING_GUIDE.md) 验证配置调整与回退。

完成标志：多个任务不会绕过并发控制；中断后不重复已经验证的昂贵阶段。

### 任务 7：通用 Agent Skill

- 在本仓库 `skills/semvideo/` 维护 Skill 源码，并与 CLI 使用同一版本号。
- 安装时注册到通用 Agent，开发时不直接修改用户技能目录。
- 启动时检查 `semvideo --version --json` 的 CLI、Schema 和 Skill 协议兼容性。
- 指导 Agent 执行 `doctor --json`。
- Skill 只根据诊断布尔值判断凭据是否存在，不读取或显示凭据。
- 指导提交任务并保存任务 ID。
- 指导轮询状态、读取日志和识别终态。
- 根据结构化 `category/retryable/recovery` 指导修正调用、恢复 `interrupted`、重试可恢复失败和报告程序缺陷。
- 对确定的命令、路径或非敏感配置修正一次；不通过自然语言日志猜测，不循环尝试。
- `internal_error`、不变量破坏和版本不兼容停止自动操作并报告。
- 指导发现未验证并发配置时退回安全默认值。
- 禁止 Skill 直接编辑任务文件或自行实现状态判断。

完成标志：新的通用 Agent 会话只依赖 Skill 和 CLI 即可完成一次真实视频处理。

正式 CLI 尚未具备端到端能力前不创建仅有占位说明的 Skill，避免通用 Agent 误以为工具已经可用。

## 4. V1 验收产物

每个成功任务至少包含：

```text
job.json
state.json
events.jsonl
media/facts.json
segmentation/candidate-timeline.json
evidence/evidence-timeline.json
evidence/contact-sheets/
evidence/transcript.json
windows/evidence-windows.jsonl
semantics/segmentation-proposals.jsonl
semantics/boundary-decisions.jsonl
plans/merge-plan.json
summaries/final-summaries.jsonl
retrieval/segments.jsonl
model-runs/
renders/*.mp4                    仅已请求并成功渲染时存在
reports/inspection.html
manifest.json
```

Agent 的机器可读结果至少给出：

- `job_id`
- `state`
- `source_video_id`
- `final_segment_count`
- `review_required_count`
- `manifest` 相对路径
- `report` 相对路径
- 正式片段和导出视频引用
- `retrieval/segments.jsonl` 的相对路径、记录数和 Schema 版本
- 失败时的稳定错误代码、失败阶段和可重试性

## 5. 真实样例验收

首个验收继续使用：

```text
C:\path\to\sample-video.mp4
```

通过条件：

1. 源视频被复制或通过内容哈希复用。
2. CLI 返回任务 ID，关闭调用终端后 Worker 继续运行。
3. 真实九宫格、字幕/ASR 与时间戳进入模型输入。
4. 真实多模态 LLM 输出通过结构和时间线验证。
5. 镜头变化不能机械切断同一次直升机运动。
6. 使用 `process --render` 生成正式摘要和全部正式子视频；不带该参数的任务只生成理解记录。
7. 每个正式片段生成标题、摘要、视觉语义、实体、动作、关键词、完整片段转写和产物引用。
8. `retrieval/segments.jsonl` 能被外部 Skill 流式读取，且不包含查询相关性或 Top-K 字段。
9. 用户能够直接观看子视频并给出质量判断。
10. 新 CLI 会话能够查询任务、日志和结果。
11. 中断后能够复用已完成阶段并恢复。

## 6. 测试边界

- 纯领域、计划验证、文件状态机和 CLI 契约使用自动化测试。
- FFmpeg、ffprobe、字幕和 ASR 使用本地集成测试。
- LLM Adapter 使用保存的合法、非法和限流响应夹具测试，不在常规测试中产生费用。
- 发布候选验收单独运行一次受控的真实 LLM 端到端测试。
- 原型在 V1 验收通过前保留为参考；通过后删除原型运行代码和依赖。

## 7. 暂不包含

- 前端。
- 常驻 HTTP 服务。
- SQLite 任务队列。
- 开机自动恢复。
- 多机器调度。
- 非相邻片段主题拼接。
- 自动接受所有低置信边界。

这些内容不得阻塞通用 Agent 可使用的 CLI V1。
