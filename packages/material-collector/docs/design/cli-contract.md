# Agent CLI 契约

状态：第一版已实现；外部理解/落库决策输入协议待接口文档

## 运行模式

- 默认提供同步运行命令；每次调用持续到会话终态或下一个已经持久化的 Agent/Human 决策检查点。
- CLI 进程无常驻状态；`--workspace` 始终指向长期存在并持有工作区资产的素材工作区。
- `run` 在素材工作区中创建新会话；后续命令使用同一个 `--workspace` 和显式 `--session-id` 定位会话。
- 每个会话在素材工作区内拥有独立状态目录和 `session.sqlite3`；不建立恢复所必需的应用级全局数据库，应用源码和安装目录不产生运行期内容。
- 长耗时流程的每个阶段都持久化状态、幂等键和产物引用，不依赖 CLI 进程内存保存唯一状态。
- 同步进程异常退出不等于任务失败；调用方可以查询会话状态并从最后一个未确认完成阶段恢复。
- 恢复不得重复已校验下载、完整视频理解目录、Top-K 选择或已确认落库副作用。

## 必需能力

第一阶段 CLI 至少需要覆盖：

- 启动一次首次成片采集；
- 查询采集会话和各主题段状态；
- 恢复中断或可恢复失败的会话；
- 请求取消尚未完成的会话；
- 处理 `auth_required` 和人工复核状态；
- 导出权威 `collection-result.json`；
- 为下游剪辑按媒体单元请求完整高质量媒体。

CLI 只是应用服务适配层，不能自行实现查询规划、预算、下载、状态转换或充分性规则。

## 命令面

第一阶段可执行文件名为 `material-collector`。

### 版本与作者契约

```text
material-collector version
material-collector contracts schema
```

两个命令均为无状态、只读接口，不创建会话、不读取或写入素材工作区。

`version` 输出：

```json
{
  "schema_version": "material-collector-version/v1",
  "cli_version": "0.2.0",
  "skill_protocol_version": 1,
  "collection_input_schema": {"min": "1.0", "max": "1.0"},
  "query_plans_schema": {"min": "2.0", "max": "2.0"}
}
```

随 Skill 发布的解析器只接受 CLI 版本、Skill 协议和两个输入范围全部相同的工具；
不兼容时返回结构化安装恢复动作，不通过读取源码或反复试命令猜测兼容性。

`contracts schema` 返回 `schema_version=material-collector-contract-schemas/v1`、
`status=available`，以及 `contracts.collection_input` 和 `contracts.query_plans` 两个由
Pydantic 草稿输入模型直接生成的 JSON Schema。它们必须与 Skill 内随版本发布的
Schema 快照逐对象相等。该命令只用于有界协议诊断；Agent 正常作者流程直接读取 Skill
快照和示例，不用它反复探测字段。

### 契约规范化

```text
material-collector contracts normalize --input <path> --query-plans <path>
```

该只读命令复用会话创建时的同一核心契约，返回规范化后的采集输入、QueryPlan 和
警告，不创建会话、不写素材工作区。下游总编排必须使用该结果计算冻结哈希，避免在
Skill 中复制文本规范化、安全 ID、引用和去重规则。

### 会话生命周期

```text
material-collector run --workspace <path> --input <path> --query-plans <path> [--max-rounds <n>] [--max-videos <n>] [--auth-profile <id>] [--browser-channel auto|edge|chrome] [--show-search-browsers] [--auth-wait-seconds <seconds>] [--request-timeout-seconds <seconds>] [--progress-format jsonl|text]
material-collector status --workspace <path> --session-id <id>
material-collector resume --workspace <path> --session-id <id> [--show-search-browsers] [--progress-format jsonl|text]
material-collector cancel --workspace <path> --session-id <id>
material-collector sessions list --workspace <path>
```

- Agent Skill 必须先为全部主题段生成 QueryPlan；`run` 同时验证 `--input` 与 `--query-plans`，通过后才在素材工作区内创建新会话并返回 `session_id`。
- `run --input` 只接受 `docs/design/collection-input.md` 定义的 UTF-8 JSON；创建会话后使用冻结副本，不再依赖原文件。
- `run --query-plans` 只接受版本化 UTF-8 JSON，并要求每个输入主题段恰好对应一个 QueryPlan。
- QueryPlan 字段、平台覆盖和核心规则边界以 `docs/design/query-plan-contract.md` 为准。
- `max_rounds` 与 `max_videos` 通过运行参数或稳定配置提供，并将生效值固化进会话。
- `--auth-profile` 选择操作系统用户持有的本机认证配置，默认值为 `default`；会话只固化该标识，不复制凭据或本机浏览器目录。
- `--browser-channel` 默认值为 `auto`。Windows 原生执行按 Edge、Chrome 顺序选择；
  显式 `edge` 或 `chrome` 不跨通道回退。首次成功通道固化进会话，`resume` 只复用该
  通道。非 Windows 与 Docker 执行把 `auto` 固定解析为 Chrome。
- `--auth-wait-seconds` 配置有头登录等待时间，默认值为 `600`；生效值固化进会话，`resume` 不临时改变它。
- `--request-timeout-seconds` 配置单个平台网络请求的等待上限，默认值为 `30`；
  生效值固化进会话，`resume` 始终继续使用冻结值，不能临时覆盖。
  会话数据库 schema `2` 新增该字段；schema `3` 新增请求和已选浏览器通道。
  本次破坏性版本不迁移较早会话；旧 schema 返回明确的不兼容结果。
- `run` 和 `resume` 的 `--progress-format` 只控制当前 CLI 进程写入 `stderr`
  的进度表现形式，不属于业务约束，也不固化进会话；默认值为 `jsonl`，
  `text` 用于人类直接观察。
- 搜索默认隐藏；`--show-search-browsers` 只显示当前执行中每个在范围平台的可识别
  窗口，不固化进会话，也不改变已冻结浏览器通道。多平台仍并发执行。
- 关闭可见搜索窗口返回该平台的 `search_browser_closed`，等待其他并发平台到达安全
  提交点后中断当前执行；已提交结果保留，未完成操作由后续显式 `resume` 重试。同一
  执行绝不因窗口关闭而切换为 headless。
- 外部视频理解和落库决策文件的 schema 尚未提供，第一版 `resume` 只恢复内部阶段；
  `--decision` 保留到接口冻结后实现，当前不会静默接收未知决策文件。
- `sessions list` 只扫描指定素材工作区，不依赖或创建应用级全局索引。
- 生产执行租约 TTL 为 60 秒。活执行器在认证、搜索、解析和下载等长外部操作期间
  每 5 秒续租并检查取消；执行器死亡后，一个租约窗口内即可由 `resume` 接管。
- `status` 与 `cancel` 返回 `runtime` 对象，至少包含 `state`、
  `cancel_requested`、`lease_owner_id`、`lease_expires_at`、
  `lease_expired`、`next_stage` 和 `completed_stages`。`cancel` 另返回
  `suggested_action`，区分等待活执行器与恢复过期取消。终态会话的 runtime state
  为 `completed|cancelled`；再次取消终态会话不修改状态且建议动作固定为 `none`。

### 登录态

```text
material-collector auth status --auth-profile <id> --platform <platform> [--browser-channel edge|chrome]
material-collector auth login --auth-profile <id> --platform <platform> [--browser-channel auto|edge|chrome] [--auth-wait-seconds <seconds>]
material-collector auth logout --auth-profile <id> --platform <platform> --confirm <platform> [--browser-channel edge|chrome]
```

- `auth status` 和 `auth login` 不要求 `--workspace`，因为本机认证配置属于操作系统用户并可跨素材工作区复用。
- `--auth-profile` 默认值为 `default`。
- `auth status` 使用平台只读认证探针，并以结构化字段返回 `valid`、`invalid`、`challenge_required` 或 `probe_failed`；不得仅凭 Cookie 是否存在判断。
- `run` 和 `resume` 在每轮多平台并发搜索前预检全部目标平台；任一登录态不可用时，当前命令自动打开所选通道的有头浏览器等待人工登录，通过验证后直接继续，无需用户另行调用认证命令。
- 多个平台需要登录时固定按 `Bilibili → 抖音 → 小红书` 串行处理，一次只打开一个有头浏览器。
- 认证进度至少包含 `authentication_probe_started`、
  `authentication_probe_completed`、`authentication_login_window_opened`、
  `authentication_login_waiting` 和 `authentication_platform_completed`。
  页面导航超过 10 秒时额外周期输出 `authentication_login_window_opening`，避免
  浏览器已启动但导航尚未完成时日志静默。
  登录等待事件携带平台、认证配置、`actor=human`、截止时间、等待秒数和任务栏提示。
- 用户关闭全部登录页面时输出 `authentication_login_window_closed`，随后本次执行
  返回可恢复的 `auth_required`，不得跳过当前未认证平台继续搜索。
- 只有全部目标平台通过认证预检后才启动本轮无头并发搜索。
- 预检通过后若平台返回 `authentication_lost`，当前命令等待其他平台到达安全提交点，自动重新认证一次并只补跑该平台未完成请求；同一平台同一轮再次失效时才持久化为 `auth_required`。
- `auth login` 仅用于在采集任务之外主动更新本机认证配置，不是正常搜索流程的必需步骤。
- 登录默认等待 `600` 秒。无桌面、浏览器被关闭或达到等待上限时，本次有头浏览器关闭且会话持久化为 `auth_required`；调用方随后只需执行 `resume`，由恢复流程再次自动预检和打开浏览器。
- 任何需要打开浏览器配置的命令都要取得“认证配置标识 + 浏览器通道 + 平台”跨进程锁；第二个进程最多等待 `60` 秒。
- 等待锁超时返回错误码 `auth_profile_busy` 和退出码 `30`。会话状态保持在原业务阶段，不增加轮次、不占用视频名额，也不允许通过删除磁盘锁文件抢占。
- 会话状态和输出只记录认证配置标识、平台、规范化探针状态及验证时间；账号、昵称、Cookie 和探针原始响应不得进入日志或产物。
- 同一认证配置切换到另一个有效账号不形成会话阻塞，第一阶段不冻结或比较平台账号身份。
- `auth logout` 在取得相应跨进程锁后删除单个平台的完整浏览器配置；`--confirm` 必须与平台名完全一致。它不修改采集会话、素材工作区或媒体资产，已有会话下次恢复时会自动重新登录。
- 第一阶段不提供一次删除认证配置内全部平台的命令。
- 本机认证配置的默认位置、隔离规则和生命周期见 `docs/design/platform-authentication.md`。

### 人工复核

```text
material-collector review list --workspace <path> --session-id <id>
material-collector review approve --workspace <path> --session-id <id> --media-unit-id <id>
material-collector review reject --workspace <path> --session-id <id> --media-unit-id <id>
```

人工复核处理超过 20 分钟或时长未知的媒体单元，并遵循既定补偿批次规则。

### 结果与高质量媒体

```text
material-collector result export --workspace <path> --session-id <id>
material-collector media fetch-hq --workspace <path> --session-id <id> --media-unit-id <id>
```

- `result export` 从已提交会话状态重新发布或导出 `collection-result.json`。
- `media fetch-hq` 按媒体单元幂等获取完整高质量媒体。

## 输出协议

- `stdout` 只输出一个最终 JSON 对象，不混入进度、日志或面向人类的提示。
- 最终对象至少包含 `schema_version`、`session_id`、规范化素材工作区
  `workspace_path`、`status`、`result_path`、`segments` 各主题段结果摘要
  和 `action_required`。
- `stderr` 承载进度与诊断信息；默认使用便于 Agent 增量消费的 JSONL，并允许调用方显式切换为人类可读文本。
- 基础流程不得依赖终端交互式提问。需要外部动作时，通过 `action_required`
  返回 `actor`、动作类型、`target`、原因、`requested_artifacts` 和后续命令；
  `actor` 明确区分 `agent` 与 `human`。新增的 `target` 与
  `requested_artifacts` 是 `1.0` 输出协议的向后兼容可选扩展：无明确目标或请求
  产物时分别为 `null` 和空数组。
- 输出结构使用显式 `schema_version`；不兼容字段变更必须提升版本并记录迁移说明。

## 退出码

查询类命令的退出码只表示查询命令本身是否成功，不镜像被查询对象的业务状态。`status`、`sessions list`、`auth status` 和 `review list` 成功读取并输出有效 JSON 时返回 `0`，即使会话处于失败、缺口或等待操作状态。

执行类命令使用以下稳定退出码：

| 退出码 | 含义 |
| ---: | --- |
| `0` | 请求的命令成功完成 |
| `10` | 流程完成，但存在素材缺口或局部问题 |
| `20` | 到达需要 Agent 或 Human 决策的检查点 |
| `21` | 运行被正常取消 |
| `30` | 可恢复技术失败，可以再次执行 `resume` |
| `40` | 参数、输入或 schema 错误 |
| `50` | 状态损坏或不可恢复内部错误 |
| `130` | 当前 CLI 进程被 Ctrl+C 或中断信号终止 |

- `cancel` 成功提交并确认取消请求时返回 `0`；正在运行的 `run` 或 `resume` 观察到该请求并正常停止时返回 `21`。
- `auth_profile_busy` 属于退出码 `30` 的可恢复技术错误；会话调用方可以稍后执行 `resume`，独立认证命令可以直接重试。
- `browser_channel_failed` 和 `browser_channel_exhausted` 属于退出码 `30`；错误尝试只含
  通道、`unavailable|launch|navigation|desktop|window_verification` 阶段、规范化原因和
  所需动作，不输出浏览器路径或启动命令。
- `search_browser_closed` 属于退出码 `30`，调用方可重新执行 `resume`；是否再次显示
  搜索窗口由新的执行参数决定，而不是会话状态。
- 具体业务状态和错误细节始终以最终 JSON 的 `status`、`action_required` 和结构化 `error` 为准。
- 新增退出码不得改变已冻结数字的含义。
