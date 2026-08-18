# Agent Skill 编排确定性 CLI

状态：accepted

主 Agent 推理和隔离子代理选择由仓库 Skill 在 Agent 运行时中编排，Python CLI 不内嵌 LLM 客户端、不保存模型密钥，也不尝试访问当前 Agent 会话。CLI 只执行确定性阶段并同步运行到会话终态或下一个持久决策检查点，由 Skill 读取请求产物、完成相应 Agent 决策后通过 `resume --decision` 继续，以避免重复模型调用和上下文复制。Collector Skill 独立持有直接采集所需的完整 Agent 调用契约和 QueryPlan 作者指南；总编排 Skill 复用该契约，只增加理解、选择、缺口判断和补搜编排。

Collector Skill 通过自身的有界解析器定位 CLI，并用公共只读 `version` 输出精确校验
CLI 版本、Skill 协议及输入 schema 范围。Pydantic 草稿模型继续是作者输入的单一事实
来源；Skill 内 JSON Schema 快照和最小示例作为发布产物，由漂移测试与公共
`contracts schema` 接口共同约束。这样做的代价是每次不兼容 CLI 发布都必须同步更新
Skill 解析器和快照；不匹配时正常流程停止并给出安装恢复动作，而不回退到源码扫描、
路径猜测或试错 JSON。
