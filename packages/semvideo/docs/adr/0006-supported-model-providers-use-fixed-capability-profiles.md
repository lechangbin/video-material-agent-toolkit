---
status: accepted
---

# 受支持的模型提供商使用固定能力配置档

Semvideo 以模型提供商配置档选择经过适配的非秘密能力组合，而不是让提供商、端点、模型和上下文限制自由拼装。Agnes 配置档固定使用 OpenAI-compatible `https://apihub.agnes-ai.com/v1`、`agnes-2.5-flash`、512K 总上下文、默认 8K 输出保留和最多 2 个同时在途请求；凭据只从 `AGNES_API_KEY` 指向的进程环境读取。512K 是请求总预算，因此默认输入预算是 504K，而不是把完整 512K 同时用于输入和输出。

正式 CLI 只允许选择 Agnes 与 SiliconFlow 两个固定配置档。内部集成和自托管开发可显式使用不可由 CLI 选择的 `openai-compatible` 适配档；它拥有独立的通用请求字段与追踪头规则，不会继承 SiliconFlow 专属参数。任意其他 Provider 名称仍会在配置边界拒绝。

## Rejected alternatives

- 仅替换模型字符串：会继续发送硅基流动专属请求字段，并允许端点、上下文和模型形成未经验证的组合。
- 根据上下文长度自动提高并发：上下文上限是单请求约束，不能推导账户 RPM、TPM 或安全的同时请求数。
- 把 Agnes 设为所有工作区的新默认：会隐式改变现有工作区的提供商和凭据来源；本版本保留显式选择。

## Consequences

配置档让 CLI、Doctor、Worker 和 Agent Skill 共享同一安全边界。任务提交时会把完整非秘密配置及其哈希写入任务包；Worker 和恢复尝试只读取这份冻结配置，因此切换提供商只影响新任务。缺少冻结配置或完整性校验失败的任务不会回退到可变工作区配置，而会报告结构化错误。

每次模型 HTTP 调用前，证据请求规划器以 UTF-8 文本字节数作为文本 token 上界，为每张高细节联系图保留固定 8192 token，并加入消息开销保留；估计值超过 `context_window_tokens - max_output_tokens` 时在本地返回 `model_input_budget_exceeded`，不会发送请求。该估计是保守准入边界，不声称等于提供商的实际图片计费；提供商返回的实际总用量仍会被二次校验。Agnes 账户若具有更高额度仍不能把同时请求数提高到 2 以上，除非以后以新的验证证据修改该配置档。
