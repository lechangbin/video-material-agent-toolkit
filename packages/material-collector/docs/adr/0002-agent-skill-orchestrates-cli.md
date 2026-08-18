# Agent Skill 编排确定性 CLI

状态：accepted

主 Agent 推理和隔离子代理选择由仓库 Skill 在 Agent 运行时中编排，Python CLI 不内嵌 LLM 客户端、不保存模型密钥，也不尝试访问当前 Agent 会话。CLI 只执行确定性阶段并同步运行到会话终态或下一个持久决策检查点，由 Skill 读取请求产物、完成相应 Agent 决策后通过 `resume --decision` 继续，以避免重复模型调用和上下文复制。Collector Skill 独立持有直接采集所需的完整 Agent 调用契约和 QueryPlan 作者指南；总编排 Skill 复用该契约，只增加理解、选择、缺口判断和补搜编排。
