# QueryPlan 文件契约

状态：已确认

## 文件结构

第一阶段 `material-collector run --query-plans <path>` 只接受版本化 UTF-8 JSON：

```json
{
  "schema_version": "1.0",
  "plans": [
    {
      "query_plan_id": "qp_seg_001",
      "segment_id": "seg_001",
      "visual_strategy": "该主题段需要怎样的视觉表达",
      "required_visual_facets": [
        {
          "facet_id": "facet_001",
          "description": "必须覆盖的视觉面向"
        }
      ],
      "initial_queries": [
        {
          "query_id": "query_001",
          "text": "具体检索表达",
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
  ]
}
```

## 对应关系

- 每个冻结输入中的 `segment_id` 必须恰好对应一个 QueryPlan，不得缺失、重复或引用未知主题段。
- `query_plan_id` 可省略；CLI 根据 `segment_id` 稳定生成。
- 每个 QueryPlan 至少包含一个必需视觉面向和一个首轮检索表达。
- 每条检索表达至少关联一个本 QueryPlan 内存在的 `facet_id`。
- `visual_strategy` 用于审计和辅助后续缺口判断，不能替代结构化必需视觉面向。

## 平台覆盖

- `target_platforms` 只允许 `bilibili`、`douyin` 和 `xiaohongshu`。
- 单条检索表达可以面向平台子集，以支持平台化措辞。
- 同一 QueryPlan 的全部首轮检索表达合计必须覆盖三个平台，使首轮可以按既定规则并发搜索。
- 视觉面向、检索表达和目标平台列表经过确定性标准化后不得重复。

## 核心规则边界

QueryPlan 不得设置或覆盖：

- `max_rounds`、`max_videos`；
- 20 分钟自动理解边界；
- Bilibili、抖音、小红书的同作品优先级；
- Top-K 计算上限；
- 充分性最低阈值、平台期规则或技术重试规则。

这些值由版本化核心规则和运行约束决定。CLI 负责结构、引用、平台枚举和对应关系校验；视觉面向及检索表达的语义质量由主 Agent 负责。

## 冻结

CLI 创建会话时复制规范化 QueryPlan 文件、记录内容哈希并与冻结输入绑定。`resume` 不重新读取原始 QueryPlan 文件；后续检索扩展通过版本化决策检查点追加，不修改首轮计划。
