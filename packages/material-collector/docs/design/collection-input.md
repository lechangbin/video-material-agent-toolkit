# 首次成片采集输入契约

状态：已确认

## 文件格式

第一阶段 `material-collector run --input <path>` 只接受 UTF-8 JSON。自由格式 Markdown 不作为权威输入；后续如需支持，必须由独立适配器先转换并验证为本契约。

```json
{
  "schema_version": "1.0",
  "full_script": "完整视频文案",
  "theme": "可选的整体主题",
  "segments": [
    {
      "segment_id": "seg_001",
      "order": 1,
      "text": "主题段文案",
      "content_suggestion": "可选内容建议"
    }
  ]
}
```

## 字段规则

- `schema_version`、非空 `full_script` 和至少一个非空 `segments[].text` 必填。
- `theme` 与 `content_suggestion` 可省略；缺失时由主 Agent 从文案推导。
- `segment_id` 可省略；CLI 按数组顺序稳定生成 `seg_001`、`seg_002` 等标识。
- `order` 可省略并默认采用数组顺序；会话输入快照必须将最终顺序显式固化。
- 输入不得夹带平台 Cookie、API Key、下载地址或临时签名媒体地址。

## 一致性与冻结

- CLI 对完整文案和分段文案做确定性文本标准化后比较。
- 分段文案拼接与完整文案不一致时，输出结构化警告但不拒绝创建会话，因为标题、省略标点或格式整理可能产生合理差异。
- 创建会话时将规范化输入复制到会话目录、计算内容哈希并冻结。
- `resume` 只读取会话内冻结快照，不重新读取调用方的原始输入文件。
- 第一版运行期间不允许替换完整文案、调整主题段或修改内容建议。

## 运行约束

`max_rounds` 与 `max_videos` 是运行约束，不属于语义输入。它们由 CLI 参数或稳定配置提供，并把最终生效值记录进会话快照，以支持审计和恢复。
