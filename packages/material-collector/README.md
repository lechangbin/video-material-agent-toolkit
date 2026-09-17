# 查找下载工具

本仓库用于开发一套同时适合人类用户和 Agent 使用的查找下载工具。

长期计划提供两个用户入口：

- 可脚本化调用的 CLI。
- 面向人类用户的前端操作界面。

业务能力将首先实现为与界面无关的核心模块和应用服务，再分别接入 CLI 与前端。具体功能完成并验证后再进行正式的终端命令包装，但开发过程中必须持续保留清晰、可复用的调用接口。

第一阶段只交付面向 Agent 和脚本调用的 Python 3.14 CLI；Web/API、桌面 GUI 和浏览器前端暂不实现。

第一阶段 CLI 可执行文件名为 `material-collector`。第一版已经实现：

```text
material-collector run
material-collector resume
material-collector cancel
material-collector status
material-collector sessions list
material-collector auth status|login|logout
material-collector contracts normalize
material-collector contracts schema
material-collector executor invoke
material-collector review list|approve|reject
material-collector result export
material-collector media fetch-hq
material-collector version
```

`run` 会冻结输入，只检查 QueryPlans 3.0 范围内的平台登录态；Windows 原生模式默认先尝试 Edge、再尝试
Chrome，需要登录时打开所选浏览器的有头窗口，随后固定使用同一通道的无头浏览器
并发搜索范围内的 Bilibili、抖音和/或小红书，解析媒体单元并下载低码率
代理。低码率代理选择源站最高且不超过 720p 的版本，并在落地后通过本地
`ffprobe` 校验结构和实际分辨率，再由 FFmpeg 严格解码完整主时间线；中断或损坏文件
不会进入工作区资产。跨平台版本会分别下载，再由本地音频与视频指纹确认
同作品并按 `Bilibili > 抖音 > 小红书` 标记主来源；其他版本仍完整保留。全部发现
来源都会写入会话的 `collection-result.json`；代理媒体以 SHA-256 内容寻址保存在
长期素材工作区，后续恢复不会重复已提交阶段。指纹去重完成后，每个作品组只为主来源
发布按来源标题和分段标题命名的会话级可读入口；回退来源仍保留内容寻址资产，但不暴露
第二个标题入口。按需取得的高质量媒体遵循同一规则。

搜索浏览器默认隐藏。`run` 或 `resume` 可为当前一次执行传入
`--show-search-browsers`，并发显示每个平台带有 `Material Collector · <platform>`
标题的独立窗口；该开关不会写入 session，也不会改变冻结的 Edge/Chrome 通道。
关闭任一可见搜索窗口会以可恢复错误 `search_browser_closed` 中断当前执行，其他平台
已安全提交的结果继续保留；程序不会在同一次执行中静默切回隐藏模式。

超过 20 分钟或时长未知的视频只记录稳定链接并进入人工复核，不自动送入视频理解。
查找下载 CLI 在真实搜索和代理下载结束后以退出码 `20` 到达
`integration_required` 检查点。它明确表示“采集完成、等待外部集成”，不会在
CLI 内伪造理解、Top-K、落库或素材充分性结果。

完整命令契约见 `docs/design/cli-contract.md`。

`contracts normalize` 是无状态、只读的机器接口，用于让外部编排在创建会话前取得与
`run` 完全相同的规范化采集输入和 QueryPlan；它不创建会话或写入素材工作区。
`contracts schema` 从同一组 Pydantic 输入模型返回 Collection input 1.0 与
QueryPlans 3.0 的机器可读 Schema；`version` 返回 CLI 0.3.0、Skill 协议 1 和支持的
输入范围。安装后的 Skill 解析器先用 `version` 确认精确兼容性，正常请求直接读取
Skill 随附的 Schema 和最小示例，不通过运行时失败猜字段。

## 开发环境

安装 uv 后，在仓库根目录运行：

```powershell
uv sync
```

这会按 `.python-version` 使用 Python 3.14，并在 `.venv` 中安装 `pyproject.toml` 与 `uv.lock` 定义的依赖。

运行时复用本机已安装的 Microsoft Edge 或 Google Chrome，不要求另行下载 Playwright
Chromium。`auto` 只在 Windows 原生模式按 Edge、Chrome 顺序探测；非 Windows 与
Docker 环境固定使用 Chrome。显式选择 `edge` 或 `chrome` 时不会跨通道回退。
登录配置按认证 profile、浏览器通道、平台三层隔离，默认保存在当前 Windows 用户的
本地应用数据目录，不进入仓库和素材工作区。首次成功通道会写入 session，恢复执行不会
重新自动选择或切换浏览器。
国内平台浏览器固定以 `--no-proxy-server` 启动，HTTP 下载固定禁用环境代理，因此默认不会
使用 Windows 系统代理、`HTTP_PROXY`/`HTTPS_PROXY` 或本机 `127.0.0.1:10808`。
认证探针、有头登录和平台无头浏览器均显式启用 Chromium sandbox；有头登录会输出
逐平台探针、窗口导航、窗口打开和等待事件，并提示用户检查任务栏。Windows 桌面
验证会把可见顶层窗口绑定到本次认证 profile 的非 headless 浏览器进程。登录
完成前关闭全部页面会立即进入可恢复的 `auth_required`，不会静默等待完整超时。
抖音和小红书优先使用已渲染页面的登录标志判断状态，身份接口仅作为兜底，避免
平台裸接口拒绝请求时把已登录页面误判为未登录。

YouTube/TikTok 登录改用本机普通 Edge 或 Chrome 窗口，不通过 Playwright 控制，也不附加
WebDriver 或远程调试参数；用户登录后关闭窗口，冻结的 yt-dlp 运行时再从该平台的隔离
profile 读取登录态。外国平台才使用已验证代理，搜索与下载默认隐藏。Bilibili 的登录、
探针和下载路径没有变化。带用户名或密码的代理 URL 会在可见浏览器启动前被拒绝。

## 当前 CLI 示例

仓库提供可直接启动采集的示例文件：

- `examples/collection-input.json`
- `examples/query-plans.json`

```powershell
uv run material-collector run `
  --workspace D:\video-materials `
  --input .\examples\collection-input.json `
  --query-plans .\examples\query-plans.json `
  --browser-channel auto `
  --show-search-browsers `
  --request-timeout-seconds 30 `
  --progress-format jsonl
```

命令的标准输出是一个最终 JSON 对象，进度和诊断只写入标准错误。首次运行若需要
登录会自动打开浏览器；登录完成后无需再次执行命令。保存输出中的 `session_id`
后可查询或恢复：

```powershell
uv run material-collector status `
  --workspace D:\video-materials `
  --session-id <session-id>

uv run material-collector sessions list `
  --workspace D:\video-materials

uv run material-collector resume `
  --workspace D:\video-materials `
  --session-id <session-id> `
  --show-search-browsers `
  --progress-format text

uv run material-collector result export `
  --workspace D:\video-materials `
  --session-id <session-id>
```

`--request-timeout-seconds` 是每个平台请求的显式超时，默认 30 秒，并在创建会话
时冻结；恢复会话继续使用同一个值。`--progress-format` 只改变当前命令写入
`stderr` 的进度格式：默认 `jsonl` 适合 Agent，`text` 适合人类观察，不会污染
`stdout` 的单个最终 JSON。
`--show-search-browsers` 同样只影响当前命令；省略时搜索始终恢复为默认隐藏行为。

`status` 和 `cancel` 额外返回 `runtime`，包含取消状态、租约 owner、到期时间、
是否已经过期和下一阶段。执行租约默认 60 秒；活执行器在认证、搜索、解析和下载等
长操作期间每 5 秒续租并检查取消，认证锁轮询本身也可立即取消；死亡执行器最多等待
一个租约窗口即可恢复。对已完成或已取消会话再次执行 `cancel` 是幂等操作。

仓库级 Skill `.agents/skills/collect-video-materials/` 为 Agent 提供确定性的
`run/resume/status/cancel` 调用边界。Agent 必须调用 Skill 随附脚本，不能自行拼装
`Start-Process`、PowerShell Job 或重复执行器。Runner 会分别记录包装进程与实际
collector 子进程身份；同一 session 的 `resume` 保持单执行器，不同 `run` 可在
同一素材工作区并存。

仓库级总编排 Skill
`.agents/skills/search-understand-refine-video-materials/` 在 CLI 边界之外连接
`material-collector`、已安装的 Semvideo 和 `select-video-segments`：按主题段执行
搜索与 720p 代理下载，复用完整视频理解结果，在隔离子代理中选择 Top-K，并由主
Agent 对照原始文案判断是否需要有界补搜。它只通过版本化文件和各工具公开接口联动，
不修改 Collector SQLite 或 Semvideo 任务文件。

会话运行数据位于调用方指定的素材工作区，而不是源码仓库或应用安装目录：

```text
<workspace>/
  .material-collector/
    workspace.json
    sessions/
      <session-id>/
        session.sqlite3
        collection-result.json
        downloads/
        input/
          collection-input.json
          query-plans.json
  assets/
    sha256/
  materials/
    by-session/
      <session-id>/
        <source-title>__<platform>__<source-id>/
          low-proxy/
            <media-unit-title>__<media-unit-id>.<container>
          high-quality/
            <media-unit-title>__<media-unit-id>.<container>
```

`assets/sha256/` 是权威工作区资产，`materials/by-session/` 只是方便人工浏览和剪辑工具
选择文件的标题素材视图。标题视图优先使用硬链接，无法创建硬链接时使用经过 SHA-256
复核的原子复制；它不会改变或替代内容寻址资产。`collection-result.json` 中每个已下载
资产的 `relative_path` 指向权威资产，主来源额外通过 `display_relative_path` 指向标题
入口，回退来源该字段为 `null`。标题更新会发布新入口并保留旧文件；清理由用户显式完成。

## 当前状态

第一版 CLI、会话恢复、三平台适配器、内容寻址资产、来源清单和本地媒体指纹均已
完成离线自动化验证。真实平台页面可能随时发生结构变化；首次使用建议以少量查询
做受控烟雾测试。Semvideo 联动、隔离子代理 Top-K、素材缺口判断和递归补搜已由
下游总编排 Skill 定义；切分落库和高码率按需下载仍等待后续剪辑工具接口。查找下载
CLI 继续作为每轮确定性采集执行器，不在自身内部承担剪辑推理。

## 开发约定

完整的架构、CLI、前端、安全和完成标准见 [AGENTS.md](AGENTS.md)。
第一版实现边界和验证记录见
[docs/implementation/v1-usable.md](docs/implementation/v1-usable.md)。
