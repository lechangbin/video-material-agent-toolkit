# Input contracts

Create both inputs as UTF-8 JSON. These are the complete current request shapes. Do not probe the
CLI with trial files. This release does not accept or migrate older contract versions.

## Collection input 1.0

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

`schema_version`, non-empty `full_script`, and at least one non-empty `segments[].text` are
required. `theme`, `content_suggestion`, `segment_id`, and `order` may be omitted. Omitted segment
IDs and order values are generated deterministically. Do not include cookies, API keys, download
URLs, or temporary signed media URLs.

## QueryPlans 2.0

```json
{
  "schema_version": "2.0",
  "platform_scope": [
    "bilibili"
  ],
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
            "bilibili"
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

- `platform_scope` is a non-empty, duplicate-free subset of `bilibili`, `douyin`, and
  `xiaohongshu`. Every initial or supplemental query must copy that complete scope into
  `target_platforms`; a query cannot narrow or expand it.
- Each collection-input segment must have exactly one plan. Every plan needs a non-empty
  `visual_strategy`, at least one `required_visual_facets` entry, and at least one
  `initial_queries` entry. Every query must reference at least one facet declared by its plan.
- `query_plan_id` may be omitted and is generated from `segment_id`. Runtime budgets such as
  `max_rounds` and `max_videos` do not belong in QueryPlans.
