# Input contracts

Create both inputs as UTF-8 JSON. These are the complete current request shapes. Do not probe the
CLI with trial files. This release does not accept or migrate older contract versions.

Release artifacts:

- [Collection input 1.0 JSON Schema](schemas/collection-input-1.0.schema.json) and
  [minimal example](examples/collection-input-1.0.min.json)
- [QueryPlans 3.0 JSON Schema](schemas/query-plans-3.0.schema.json) and
  [minimal example](examples/query-plans-3.0.min.json)

The snapshots are generated from the same Pydantic draft models used by session creation. For a
read-only machine-readable copy from the installed CLI, run exactly:

```text
<collector> contracts schema
```

This command is allowed for version diagnostics; it does not replace the bundled authoring guide
and must not be used for trial-and-error field discovery. Author the final two artifacts once, then
perform at most one final normalization before `run`:

```text
<collector> contracts normalize --input <collection-input.json> --query-plans <query-plans.json>
```

`<collector>` is the absolute `command` returned by the bundled resolver, not a PATH lookup.

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

## QueryPlans 3.0

```json
{
  "schema_version": "3.0",
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
      "platform_branches": [
        {
          "platform": "bilibili",
          "language": "zh-CN",
          "queries": [
            {
              "query_id": "query_001_bilibili",
              "text": "具体检索表达",
              "facet_ids": ["facet_001"],
              "budget": 20
            }
          ]
        }
      ]
    }
  ]
}
```

- `platform_scope` is a non-empty, duplicate-free subset of `bilibili`, `douyin`,
  `xiaohongshu`, `youtube`, and `tiktok`. Every plan contains exactly one branch for every
  in-scope platform. Branches can use different languages and search expressions.
- Each collection-input segment must have exactly one plan. Every plan needs a non-empty
  `visual_strategy`, at least one `required_visual_facets` entry, and at least one
  query in every branch. Every query must reference at least one facet declared by its plan and
  carries a `budget` from 1 through 100 (default 20).
- `query_plan_id` may be omitted and is generated from `segment_id`. Runtime budgets such as
  `max_rounds` and `max_videos` do not belong in QueryPlans.
