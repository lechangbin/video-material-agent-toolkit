# 会话执行运行时

状态：第一版已实现

## 目标与边界

应用层 `SessionRuntime` Protocol 为单个会话定义可恢复的执行协调边界。生产
`SqliteSessionRuntime` adapter 将该协议持久化到 `session.sqlite3`。该边界只负责：

- 同一会话的单写入者租约；
- 有序阶段、执行尝试和幂等操作结果；
- 协作式取消；
- 进程中断后的阶段恢复。

它不执行搜索、下载、视频理解或落库。外部操作必须在 SQLite 事务之外完成，
并仅在开始、心跳和提交结果时调用运行时。`SessionApplication.get_session` 和
`SessionRuntime.inspect` 都是只读操作，不续租、不迁移数据库，也不改变业务状态。

## 会话存储接缝

`material_collector.application.sessions` 定义会话模型、`SessionStore` Protocol 和
`SessionApplication` 接口。应用层不解析 JSON、不创建目录、不执行 SQLite，
也不知道原子文件发布的实现细节。

生产使用 `material_collector.infrastructure.session_store.SqliteSessionStore`
adapter。它在该 seam 后集中负责：

- 同时校验和规范化采集输入、QueryPlan；
- 素材工作区及会话目录布局；
- 冻结文件的原子发布和 SHA-256；
- 基础会话 SQLite schema、读写与兼容投影；
- 会话列表扫描和损坏状态诊断。

测试可以向 `SessionApplication(store=...)` 注入内存 adapter，经同一个接口验证应用
调用而不触碰文件系统。生产 adapter 继续由原有端到端测试通过公开
`SessionApplication` 接口验证，因此该 seam 同时存在生产与测试两个真实 adapter。

执行运行时遵循相同方向：数据模型、错误和 `SessionRuntime` Protocol 位于
`material_collector.application.session_runtime`；所有 SQLite 事务、迁移、租约和
表结构实现位于
`material_collector.infrastructure.session_runtime_store.SqliteSessionRuntime`。

## 数据库兼容策略

基础会话 schema 使用 `schema_info.schema_version=2` 和 `PRAGMA user_version=2`，
其中 schema `2` 新增冻结的 `request_timeout_seconds`。schema `1` 会话仍可只读查询，
并按兼容默认值 `30` 秒展示；首次进入写执行时在一个 `BEGIN IMMEDIATE` 短事务中
增加该列并升级到 schema `2`。

运行时扩展使用独立的 `runtime_schema_info`，当前版本为 `1`。这样已有会话读侧契约
保持稳定，同时旧的、只有基础表的会话也可在第一次 `begin_execution` 或
`request_cancel` 时完成运行时表和基础 schema 的幂等迁移。

新会话初始化时直接创建：

- `execution_lease`：owner、generation、heartbeat 和 expiry；
- `runtime_control`：持久取消请求和结构化 `action_required` 检查点；
- `stage_state`：固定顺序的阶段计划；
- `stage_attempt`：每次尝试及其终态；
- `operation_record`：幂等操作键及已提交 JSON 结果。

仍使用 SQLite rollback journal (`journal_mode=DELETE`)。所有写操作使用短事务，
外部网络和文件工作不得持有数据库事务。

## 最小调用流程

```python
from material_collector.infrastructure.session_runtime_store import (
    SqliteSessionRuntime,
)

runtime = SqliteSessionRuntime(lease_ttl_seconds=30)
lease = runtime.begin_execution(
    workspace,
    session_id,
    owner_id=worker_id,
    stages=("authenticate", "search", "download"),
)

attempt = runtime.begin_stage(
    lease,
    stage_key=lease.next_stage,
    operation_key="search:segment-a:round-1",
)
if attempt.replayed:
    result = attempt.result
else:
    child = runtime.begin_operation(
        lease,
        stage_key=lease.next_stage,
        operation_key="search:bilibili:segment-a:query-1",
    )
    if not child.replayed:
        result = perform_external_work()
        runtime.complete_operation(lease, child, result=result)
    runtime.complete_stage(lease, attempt, result={"completed": True})

lease = runtime.heartbeat(lease)
```

阶段计划在首次执行时冻结。`resume` 必须传入完全相同的阶段序列，且只会从第一个
未完成阶段继续。阶段只能按持久顺序执行。

首版加入本地 `fingerprint` 阶段时允许唯一一次受控计划迁移：旧计划
`download → understand_and_ingest` 可插入为
`download → fingerprint → understand_and_ingest`，前提是理解阶段尚未完成。
已经停在 `integration_required` 的旧会话恢复后会先补做指纹，再重新发布集成动作；
其他任意阶段增删或重排仍被拒绝。

调用方应使用稳定、具有业务含义的 `operation_key`。某个操作结果一旦提交，再次
以同一键开始会直接返回 `replayed=True` 和原 JSON 结果，不产生新尝试。

一个阶段包含多个平台请求或下载时，阶段本身使用 `begin_stage` 协调顺序，子项使用
`begin_operation`、`complete_operation` 和 `fail_operation`。子项提交不会提前
完成整个阶段；只要仍有可重试子项，调用方就把外层阶段记为失败。恢复时，已完成
子项从 `operation_record` 回放，失败子项分配新 attempt，因此只补跑未完成工作。
来源清单或资产提交必须先于 `complete_operation`；若两者之间崩溃，重复写入由清单
和内容寻址层幂等吸收。

工作流以 `workflow_retryable` 返回这类阶段失败，详情固定包含
`retryable=true` 与结构化 `issues`。CLI 将其映射为可重试执行错误，而不是会话
损坏；调用方随后执行 `resume` 即可补跑失败子项。

## 租约和崩溃恢复

- 活租约由 `owner_id + generation` 标识。
- 第二个 owner 在租约到期前尝试执行会收到
  `session_execution_conflict`，错误详情包含当前 owner 和过期时间。
- 心跳只延长与调用方 owner、generation 均匹配的活租约。
- 租约过期后，新 owner 可以接管；旧的 `running` 尝试和操作记为
  `interrupted`，对应阶段回到 `pending`，已完成阶段和结果不回滚。
- generation 每次过期接管递增，因此旧进程即使随后恢复，也无法提交过时结果。
- 正常暂停使用 `release_execution`；全部阶段完成后使用
  `finish_execution`。

基础会话 schema v1 在首次取得写租约时会于同一事务中补充
`request_timeout_seconds=30` 并升级到 schema v2。只读状态查询仍可读取 v1；
迁移只发生在实际恢复执行时。

## 取消

`request_cancel` 是不要求持有执行租约的短控制事务，因此 `cancel` 命令可以在工作
进程运行时提交请求。执行者应在外部工作的安全点调用
`cancellation_requested`，或在开始下一阶段时处理
`session_cancel_requested`。

工作流在每个平台的串行搜索、解析和下载子项之间检查取消；认证恢复后再次请求前
也会检查。已经返回的外部调用可以先完成原子清单/资产提交，随后停止，避免留下
无法辨识的半提交状态。

观察到取消后，执行者调用 `acknowledge_cancel`：

1. 当前运行尝试记为 `interrupted`；
2. 未完成阶段恢复为 `pending`，保留诊断历史；
3. 会话状态提交为 `cancelled`；
4. 删除执行租约。

取消请求本身具有幂等性，重复提交不会反复增加状态版本。

## 外部动作检查点

执行流程需要等待外部系统、人工登录或 Agent 决策时，调用
`pause_for_action`。第一版允许的状态为：

- `integration_required`
- `auth_required`
- `decision_required`

该调用在同一短事务中保存 JSON 对象 `action_required`、中断尚未提交的尝试、
更新会话状态并释放执行租约。`inspect` 会原样返回检查点，供 CLI 或未来前端
展示。下一次 `begin_execution` 成功取得租约后，旧检查点才会在同一事务中清除；
租约冲突或恢复失败不会提前丢失操作要求。

## 测试覆盖

`tests/test_session_runtime.py` 覆盖：

- 阶段顺序、结果提交和幂等回放；
- 子项成功回放、失败重试和不提前完成外层阶段；
- 两个执行者争抢同一会话；
- 租约过期后的崩溃接管与尝试历史；
- 协作式取消；
- 动作检查点的持久化、释放租约和恢复清除；
- 旧基础数据库的首次运行迁移。
- 基础会话 schema v1 到 v2 的请求超时字段迁移。
