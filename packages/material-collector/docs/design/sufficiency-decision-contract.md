# 充分性决策契约

状态：已被 `docs/adr/0004-editing-orchestrates-understanding-feedback.md` 取代；
保留为历史草案，接口待下游剪辑编排层重新冻结

## 响应结构

以下结构是旧的采集内闭环草案，当前 CLI 不创建或接纳该决策。未来下游主剪辑
Agent 将根据 Top-K 选择结果判断素材缺口，需要补搜时生成下一轮 QueryPlan；最终
字段应在视频理解/剪辑编排接口确定后另行版本化，不能把本草案视为已发布协议。

```json
{
  "schema_version": "1.0",
  "decision_id": "dec_001",
  "decision_type": "sufficiency_assessment",
  "session_id": "ses_001",
  "query_plan_id": "qp_seg_001",
  "state_version": 12,
  "decision": "search_more",
  "facet_assessments": [
    {
      "facet_id": "facet_001",
      "status": "missing",
      "accepted_segment_ids": [],
      "reason": "现有有效片段没有表达该视觉面向"
    }
  ],
  "next_queries": [
    {
      "query_id": "query_round_2_001",
      "text": "针对缺口生成的新检索表达",
      "target_platforms": [
        "bilibili",
        "douyin",
        "xiaohongshu"
      ],
      "facet_ids": [
        "facet_001"
      ]
    }
  ]
}
```

## 视觉面向判断

- `decision` 只允许 `sufficient` 或 `search_more`。
- 每个必需视觉面向必须恰好出现一次，`status` 只允许 `covered` 或 `missing`。
- `accepted_segment_ids` 只能引用检查点所绑定有效片段快照中已经实际提交的片段。
- `reason` 必须说明当前快照为何覆盖或缺失该视觉面向，不能以搜索结果数量、下载成功或尚未落库的候选代替证据。
- 技术失败、质量失败、版权限制或重复消解结果不是素材缺口，不得被标记成新的语义视觉面向。

## 决策规则

- `sufficient` 要求所有视觉面向为 `covered`，并同时通过核心充分性护栏；Agent 不能降低确定性阈值。
- `search_more` 要求至少一个视觉面向为 `missing`，并提供针对缺失面向的 `next_queries`。
- 下一轮检索表达只能引用本次 `missing` 的 `facet_id`。
- 下一轮检索表达合计仍须覆盖 Bilibili、抖音和小红书，并遵循 QueryPlan 的标准化和去重规则。
- Agent 判断需要继续但 `max_rounds`、`max_videos` 或平台期规则已经触发时，核心保存本次缺口并以 `stopped_with_gaps` 结束，不执行查询。

## 并发与幂等

- `decision_id`、`session_id`、`query_plan_id` 和 `state_version` 必须与待处理检查点完全匹配。
- `state_version` 过期时拒绝写入并保留原检查点，要求基于最新快照重新判断。
- 同一 `decision_id` 重复提交相同规范化内容时返回已接纳结果；不同内容返回结构化冲突。
- schema、引用或状态转换无效时返回退出码 `40`，但不把会话标记为技术失败。
