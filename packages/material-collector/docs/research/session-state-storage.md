# 会话状态存储架构研究

> 后续设计确认：媒体物理文件属于素材工作区，而不属于单个采集会话。回收、归档或删除会话不触发资产回收；本文关于“无引用资产垃圾回收”的内容仅保留为研究时考虑过的方案，不构成当前设计决定。

## 结论

对本项目，推荐采用：

> **CLI 进程无状态 + 每个会话拥有独立工作区 + 工作区内使用会话级 SQLite 保存运行状态 + 文件系统保存媒体与对外产物。**

也就是在三个候选方案中选择 **B，并保留 C 的外部使用方式**：

- CLI 本身不持有常驻状态；
- `run` 接收一个显式素材工作区并在其中创建会话；
- `status`、`resume`、`cancel` 都通过素材工作区路径与 `session_id` 共同定位任务；
- 每个会话的内部关系状态存入自己的 `session.sqlite3`；
- `collection-result.json`、视频理解结果、Top-K 结果和日志位于会话目录；代理视频位于同一素材工作区的共享资产区；
- 应用安装目录或源码仓库根目录不产生任何运行期内容。

因此，“使用 SQLite”并不意味着必须建立一份位于应用根目录的全局数据库。二者是不同的架构决定。

用户提到的另一工具所采用的“CLI 无状态、产物全部位于会话工作区”在**会话边界和产物归属**方面优于全局 SQLite；但若其内部只依靠多个 JSON/Markdown 文件表达复杂运行状态，则不如“会话工作区内嵌 SQLite”适合本项目的并发结算、幂等恢复和跨实体查询。最佳方案是两者结合，而不是二选一。

## 1. 三种方案的精确定义

### A. 全局 SQLite

所有会话共享一个数据库，例如：

```text
<user-data>/video-material-collector/state.sqlite3
```

全局数据库**不应位于应用安装目录或 Git 仓库根目录**。如果选择 A，它也应位于操作系统的用户数据目录，并允许显式配置。但无论实际路径在哪里，其关键特征都是“所有会话共享同一个故障域和状态索引”。

### B. 每会话 SQLite

每个会话是一个自包含工作区：

```text
<session-workspace>/
  session.sqlite3
  collection-result.json
  input/
  proxies/
  understanding/
  selections/
  ingest/
  logs/
```

CLI 每次根据工作区路径打开对应数据库，不需要全局会话注册表。

### C. 纯文件会话工作区

CLI 同样通过工作区路径运行，但不使用数据库，状态由 JSON、JSONL、Markdown 或目录结构表达：

```text
<session-workspace>/
  session-state.json
  events.jsonl
  collection-result.json
  ...
```

这里的“CLI 无状态”只表示**进程和应用根目录不保存状态**。会话本身仍然必须持久化状态，否则无法实现 `status` 和断点恢复。

## 2. 官方资料给出的关键事实

SQLite 官方明确把单个数据库文件描述为合适的应用文件格式：它是单文件、可移动、跨平台，并提供 schema、索引、查询和原子事务。这意味着“每个会话一个 SQLite 文件”符合 SQLite 的设计用途，而不只是把 SQLite 当成全局服务器数据库。[SQLite As An Application File Format](https://www.sqlite.org/appfileformat.html)

SQLite 官方也明确提到，可以通过为不同子域使用不同数据库文件进行分片，从而改善并发；官方示例是每个用户一份数据库。映射到本项目，“每个会话一份数据库”正是同一种隔离思路。[Appropriate Uses For SQLite](https://www.sqlite.org/whentouse.html)

SQLite 支持多个并发读事务，但同一数据库文件同时只能存在一个写事务。本项目已经决定单个会话只允许一个写入执行者，因此这个限制与会话级 SQLite 匹配；不同会话位于不同数据库文件，还能避免互相争用同一个写锁。[SQLite Transaction](https://www.sqlite.org/lang_transaction.html)

Terraform CLI 是可比较的第一方 CLI 设计：默认本地状态位于当前工作目录；同一工作目录中的多个 workspace 也保存为隔离的状态文件。HashiCorp 同时指出，需要更强隔离边界时，应使用分离的配置和 backend，而不是仅依靠共享 backend 中的命名 workspace。这支持“任务状态与其工作区绑定，而非隐式汇入应用全局状态”的方向。[Terraform 初始化工作目录](https://developer.hashicorp.com/terraform/cli/init)；[Terraform CLI Workspaces](https://developer.hashicorp.com/terraform/cli/workspaces)

## 3. 方案比较

| 维度 | A：全局 SQLite | B：每会话 SQLite | C：纯文件会话工作区 |
| --- | --- | --- | --- |
| 会话隔离 | 依赖所有查询都正确带 `session_id` | 文件系统边界天然隔离 | 文件系统边界天然隔离 |
| 应用根目录洁净 | 可以做到，但必须另选用户数据目录 | 天然做到 | 天然做到 |
| 搬移与归档 | 需从全局库导出该会话记录 | 整个工作区可整体归档 | 整个工作区可整体归档 |
| 单会话删除 | 需正确清理多表关系和资源引用 | 删除/归档明确的会话工作区 | 删除/归档明确的会话工作区 |
| 多会话并发 | 所有会话竞争同一 SQLite 写入器 | 各自拥有写入器，互不竞争 | 各自独立，但需自建文件锁 |
| 原子多实体更新 | 强 | 强 | 弱；需自建事务或事件重放 |
| 幂等唯一约束 | 强 | 强 | 需扫描、索引或自行维护 |
| 复杂状态查询 | 强 | 强 | 文件数量增长后明显复杂 |
| 人工可读性 | 需工具查询 | SQLite 状态需工具；对外 JSON 可读 | 最强 |
| 状态与媒体一致归属 | 容易出现跨目录引用 | 最清晰 | 最清晰 |
| 单点损坏影响 | 可能影响所有会话 | 只影响一个会话 | 通常只影响一个会话，但多文件可能部分不一致 |
| “列出所有会话” | 最方便 | 需要给出会话根目录或额外扫描 | 需要给出会话根目录或额外扫描 |

## 4. 为什么不推荐全局 SQLite 作为权威状态库

### 4.1 它不会天然混淆数据，但扩大了误用与故障范围

只要每张会话相关表都有外键和唯一约束，全局数据库在理论上不会把会话混在一起。但这要求所有读取、更新、重试和清理路径始终正确限定 `session_id`。一处漏过滤就可能读写其他会话。

更重要的是，全局库让以下风险共享：

- 数据库迁移失败影响所有会话；
- 文件损坏或错误替换影响所有会话；
- 长事务或写锁影响其他正在运行的会话；
- 搬走一个会话的媒体目录后，全局库可能留下失效路径；
- 人工复制会话产物时无法同时带走完整恢复状态；
- 清理一个会话必须同时修改全局数据库和文件系统，二者无法天然形成一个事务。

SQLite 官方说明同一数据库只有一个并发写事务，而分离数据库文件可以改善不同子域间的并发。因此本项目没有必要让互相独立的采集会话竞争同一个写入点。[SQLite Transaction](https://www.sqlite.org/lang_transaction.html)；[Appropriate Uses For SQLite](https://www.sqlite.org/whentouse.html)

### 4.2 全局查询便利性不是当前阶段的核心需求

全局库最明显的优势是快速实现：

```text
collector sessions list
collector sessions find --status waiting_for_login
```

但第一阶段的核心命令是针对已知会话执行 `run/status/resume/cancel`。Agent 可以从 `run` 的最终 JSON 中保存 `session_id` 和 `workspace_path`，后续显式传回。为了尚未确认的全局检索需求牺牲会话可搬移性和隔离性，不划算。

如果未来确实需要全局会话列表，可以增加一份**非权威、可重建的索引**，或者扫描一个可配置的会话根目录。该索引不应成为恢复单个会话的必要条件。

## 5. 为什么纯文件方案也不完全适合本项目

纯文件会话对简单、线性任务非常优秀。例如一个任务只有：

```text
created → running → completed/failed
```

一个原子替换的 `session-state.json` 就足够了。

本项目的状态明显更复杂：

- 多个主题段 QueryPlan 顺序执行；
- 每轮三个平台并发检索；
- 多个候选分别处于发现、下载、理解、选择和落库状态；
- 需要稳定幂等键防止恢复后重复副作用；
- Bilibili 一个 BV 下还可能包含多个 P/CID；
- 跨会话可能共享内容寻址媒体；
- 落库可能出现“远端已提交、调用响应丢失”的结果未知状态；
- 充分性判断依赖已提交的累计快照，而不是单个阶段标记。

如果完全使用多个 JSON 文件，需要自行实现：

- 跨文件原子提交；
- 崩溃后检测半写入状态；
- 并发写入锁；
- 唯一约束和引用完整性；
- 索引、查询和 schema 迁移；
- 事件日志压缩与快照重建。

SQLite 官方指出，与自定义文件集合相比，SQLite 原生提供原子事务、schema、索引和查询；自行补齐并发逻辑容易出错。[SQLite As An Application File Format](https://www.sqlite.org/appfileformat.html)

所以，如果“C”指的是外部行为——CLI 无全局状态、每个会话完全位于自己的工作区——它非常适合本项目；如果“C”还要求内部禁止 SQLite、所有协调都由散落文件完成，则只有在显著缩减状态复杂度后才更优。

## 6. 推荐的具体落地方式

### 6.1 会话定位

启动时显式指定或创建工作区：

```text
collector run --workspace D:\material-collection --input input.json
```

最终 JSON 返回：

```json
{
  "schema_version": "1.0",
  "session_id": "ses_...",
  "workspace_path": "D:\\material-collection",
  "status": "completed_with_issues",
  "result_path": "D:\\material-sessions\\session-20260729-001\\collection-result.json"
}
```

后续命令通过工作区定位，不依赖当前目录和全局注册：

```text
collector status --workspace <path> --session-id <id>
collector resume --workspace <path> --session-id <id>
collector cancel --workspace <path> --session-id <id>
```

也可以允许把工作区作为位置参数，但协议中应始终返回规范化绝对路径。

### 6.2 会话内权威边界

- `session.sqlite3`：内部运行权威状态，包括阶段、尝试、幂等键、锁租约、检查点和产物引用。
- `collection-result.json`：对外权威来源清单，供 Agent、人工剪辑和下游程序消费。
- 其他 JSON/JSONL：外部模块交换结果、进度事件和可审计报告。
- 媒体文件：文件系统存储，数据库只记录相对路径、摘要、大小和生命周期状态。

SQLite 不存放大媒体二进制，避免数据库膨胀，也使媒体工具能够直接读取文件。

### 6.3 路径规则

数据库内的会话产物和工作区资产路径应保存为**相对素材工作区的路径**或稳定资产身份。这样整个素材工作区搬移后仍可恢复。

需要引用会话外的共享内容寻址仓库时，应保存：

- 稳定资产 ID / 内容摘要；
- 可重新解析的 store 标识；
- 当前解析路径仅作为缓存，不作为唯一身份。

否则将一个会话归档到另一块磁盘后，绝对路径会全部失效。

### 6.4 并发规则

- 一个会话同一时间只能有一个命令获得写租约；
- `status` 可以只读打开数据库；
- 不同会话可并行执行，因为它们写不同的数据库文件；
- 平台并发任务不应各自长期持有写事务，结果应通过短事务汇总；
- 捕获取消信号时先保存检查点，再释放会话租约。

SQLite 的“多个读取者、一个写入者”模型与这些规则一致。[SQLite Transaction](https://www.sqlite.org/lang_transaction.html)

## 7. SQLite 文件复制与归档风险

会话级 SQLite 易于搬移，但不能在运行中只复制主数据库文件并假设得到一致快照。

SQLite 官方指出，事务期间可能存在 `-journal` 或 `-wal` 辅助文件；复制活动数据库时漏掉对应日志可能得到损坏或不一致的副本。官方提供 Online Backup API 和 `VACUUM INTO` 等方式生成活动数据库的一致副本。[How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html)；[SQLite Backup API](https://www.sqlite.org/backup.html)

因此：

- 归档命令应先验证会话没有活动写入者；
- 静止会话可以在关闭所有连接、完成检查点后整体复制目录；
- 活动会话若要导出，应使用 SQLite Backup API 生成一致快照；
- 不允许只复制一个正在使用的 `session.sqlite3`；
- 若使用 WAL，必须认识到同目录还会出现 `-wal` 与 `-shm` 文件。

SQLite 官方还说明 WAL 的共享内存机制不支持跨机器网络文件系统上的多客户端使用。因此会话数据库应放在本机文件系统；会话完成后再归档到 NAS。不要让两台机器同时通过网络共享目录运行同一个会话。[SQLite Database File Format — WAL Index](https://www.sqlite.org/fileformat.html#the_wal_index_file_format)

## 8. 日志模式建议

第一阶段不需要为了“更先进”默认启用 WAL：

- 本项目规定单会话只有一个写入执行者；
- 状态写入应是短事务；
- `status` 读取即使短暂等待也可以接受；
- 默认 rollback journal 的单文件静止态更方便归档。

如果实测发现长时间运行时 `status` 经常被写事务阻塞，再考虑启用 WAL。WAL 可以让读取和写入并发，但仍然只有一个写入者，并且引入 `-wal`、`-shm`、checkpoint 和网络文件系统限制。[SQLite Write-Ahead Logging](https://www.sqlite.org/wal.html)

无论选择何种 journal 模式，都应：

- 配置有限的 busy timeout；
- 保持事务短小；
- 在终态或暂停点关闭连接；
- 提供 `integrity_check` 与安全导出/归档命令；
- 为 schema 保存显式版本并测试迁移。

## 9. 主要风险及缓解

### 会话路径丢失

无全局注册表意味着 Agent 必须保存 `workspace_path`。缓解方式是让所有终态和 `action_required` JSON 都返回路径，并允许使用 `session.json` 中的稳定 `session_id` 验证工作区身份。

### 同一工作区被重复恢复

两个 `resume` 可能同时启动。应使用会话内写租约和操作系统文件锁；数据库唯一约束是第二道防线。检测到活动执行者时返回结构化冲突，而不是继续运行。

### 工作区被部分复制

人工只复制媒体或只复制 SQLite 会产生断链。提供正式的 `archive` / `export-session` 命令，并在恢复前校验 manifest、数据库和关键产物摘要。

### 数据库 schema 升级

旧会话可能由新版本 CLI 打开。需要 schema 版本、向前迁移、迁移前备份及“不支持降级”的结构化错误。迁移失败只影响被打开的会话，不影响其他会话，这正是 B 相比 A 的优势。

### 全局资产复用

当前设计允许内容寻址媒体跨会话复用，这与“每会话状态隔离”并不冲突。共享资产仓库是素材工作区持有的资产层，会话 SQLite 保存引用；会话的业务状态仍然独立。后续设计进一步确认：回收、归档或删除单个会话不会触发物理资产回收，资产只随素材工作区级显式变更而改变。

## 10. 最终决策建议

建议把设计决策定为：

1. 应用源码目录和安装目录保持只读、无运行产物。
2. 调用方显式指定一个长期素材工作区，每次采集任务在其中创建独立会话目录。
3. CLI 进程无状态，后续命令通过 `--workspace` 与 `--session-id` 恢复会话。
4. 每个会话目录包含独立 `session.sqlite3`，不建立权威全局 SQLite。
5. 媒体与交换产物保存在工作区文件系统，数据库保存关系状态和相对引用。
6. `collection-result.json` 继续作为对外权威来源清单。
7. 若未来需要全局会话列表，只增加可重建索引，不让它成为运行和恢复依赖。
8. 静止会话才允许直接整体搬移；活动会话通过正式快照/归档流程复制。

这既保留了用户现有工具“无全局状态、会话可整体归档”的优点，也利用 SQLite 解决本项目比普通 CLI 更复杂的事务、幂等和恢复问题。
