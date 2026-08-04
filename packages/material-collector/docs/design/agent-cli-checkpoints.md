# Agent 与 CLI 决策检查点

状态：已确认

## 职责边界

- `collect-video-materials` Skill 提供确定性查找下载编排体验。
- 主 Agent 在当前上下文完成首轮查询规划。
- 下游剪辑编排层在隔离子代理中使用 `select-video-segments` Skill 完成 Top-K，
  再由主剪辑 Agent 判断素材缺口和生成下一轮 QueryPlan。
- Python CLI 不内嵌 LLM 客户端，不读取模型 API Key，不主动调用主 Agent 或创建子代理。
- CLI 负责会话状态、平台操作、下载、指纹去重和这些确定性采集阶段的恢复；它不
  调用外部视频理解、Top-K 或落库。

## 检查点协议

- 每次 CLI 调用同步执行到会话终态或下一个持久决策检查点。
- 检查点先提交到 `session.sqlite3` 并原子写出版本化请求 JSON，再通过最终输出返回。
- 请求必须包含稳定 `decision_id`、`decision_type`、`actor`、输入产物引用、期望响应 schema 和对应会话状态版本。
- `actor` 只能是 `agent` 或 `human`。
- 当前 CLI 的 Agent 检查点只有尚未提供接口的 `integration_required` 外部边界；
  Top-K、缺口判断与下一轮检索扩展属于下游剪辑编排协议。
- Human 检查点包括平台登录和人工复核。
- 同一个 `decision_id` 的结果只能接纳一次；重复提交相同内容返回已有结果，提交不同内容返回结构化冲突。

示例：

```json
{
  "status": "action_required",
  "action_required": {
    "actor": "agent",
    "type": "segment_selection_required",
    "decision_id": "dec_...",
    "request_path": "sessions/ses_.../decisions/dec_.../request.json"
  }
}
```

未来下游剪辑编排协议必须验证会话身份、状态版本和引用产物哈希，但不得在接口尚未
冻结时复用当前 CLI 的 `resume` 伪造决策提交。Top-K 分段选择继续使用
`.agents/skills/select-video-segments/references/selection-contract.md`。

## 首轮规划

- Agent Skill 在调用 `run` 前读取并验证人类输入。
- 主 Agent 一次性为输入中的全部主题段生成 QueryPlan 文件。
- `run` 同时接收 `--input` 与 `--query-plans`；只有两者都通过结构和对应关系校验后才创建会话。
- 创建会话时同时冻结规范化人类输入与 QueryPlan，并分别记录内容哈希。
- QueryPlan 无效时不创建半初始化会话，也不产生工作区媒体资产。
- CLI 创建会话后直接执行第一个主题段，不额外产生空的 `planning_required` 检查点。
- QueryPlan 字段和验证规则见 `docs/design/query-plan-contract.md`。

## 同步语义

“同步”表示一次 CLI 进程持续运行到当前可执行范围的稳定边界，不表示 CLI 可以越过尚未完成的 Agent 或 Human 决策。Agent Skill 可以连续处理检查点并再次调用 CLI，因此对最终用户仍表现为一次端到端编排。

## 命令执行边界

端到端采集 Agent Skill 必须把 CLI 执行器生命周期作为低自由度能力封装。`run` 和
`resume` 应通过 Skill 随附并经过自动化测试的确定性脚本启动；模型只填写结构化
参数，不得自行生成 `Start-Process`、PowerShell Job、计划任务或跨 shell 的等价
包装。

随附脚本必须保证单 session_id 单执行器，返回可验证的包装 PID、实际 collector
子进程 PID 与日志路径，并在启动
失败、进程消失、认证等待、取消和租约残留时执行冻结的状态检查流程。缺少日志更新
不能触发第二个执行器。同一工作区的不同 `run` 会创建不同 session，不得因已有
其他活 `run` 而被拒绝。`select-video-segments` 不承担该职责。
