# Segment selection contract

Schema version 2 binds selection to the downstream workflow rather than one collector
session. A candidate set may contain sources discovered across several search rounds and
collector sessions.

## File layout

```text
<workflow-root>/rounds/<segment-id>/round-<nnn>/selection/
  selection-request.json
  selection-result.json
  selection-audit.jsonl
```

The candidate JSONL remains a cumulative workflow artifact. Pass its path and hash to the
selection subagent; do not paste it into the parent conversation.

## Selection request

```json
{
  "schema_version": "segment-selection-request/v2",
  "selection_id": "sel_...",
  "workflow_id": "vmw_...",
  "workflow_state_version": 4,
  "query_plan_id": "qp_seg_001",
  "segment_id": "seg_001",
  "round_number": 2,
  "theme_segment": {
    "id": "seg_001",
    "text": "Narration text",
    "intent": "The visual strategy",
    "narration_duration_ms": null
  },
  "required_visual_facets": [
    {
      "facet_id": "facet_...",
      "description": "A required visual aspect",
      "priority": 1
    }
  ],
  "max_k": 8,
  "max_k_source": "derived",
  "selection_targets": {
    "independent_candidates_per_required_facet": 2,
    "candidate_duration_to_narration_ratio": 3.0
  },
  "collection_session_ids": ["ses_..."],
  "understanding_catalogs": [
    {
      "item_id": "proxy_...",
      "understanding_job_id": "job_...",
      "status": "complete",
      "media_unit_ids": ["bilibili:..."],
      "collection_session_ids": ["ses_..."],
      "covered_ranges_ms": [[0, 600000]],
      "unprocessed_ranges_ms": [],
      "page_count": 3,
      "segment_count": 42
    }
  ],
  "candidate_artifact": {
    "path": "candidate-segments.jsonl",
    "format": "jsonl",
    "sha256": "hex-digest",
    "candidate_count": 120
  }
}
```

Require exact workflow lineage, a positive round number, a valid candidate hash and only
catalogs marked `complete`. `narration_duration_ms` may be null when the narration has not yet
been timed; in that case omit the duration-ratio stopping rule and add a warning. Never estimate
duration inside the selection subagent.

## Candidate record

Each JSONL line uses the Semvideo projection produced by the orchestration Skill:

```json
{
  "schema_version": "video-material-candidate-segment/v1",
  "candidate_segment_id": "job_...:segment_...",
  "semvideo_segment_id": "segment_...",
  "understanding_job_id": "job_...",
  "item_id": "proxy_...",
  "asset_sha256": "hex-digest",
  "media_unit_ids": ["bilibili:..."],
  "collection_session_ids": ["ses_..."],
  "work_group_ids": ["wg_..."],
  "start_ms": 1000,
  "end_ms": 9000,
  "duration_ms": 8000,
  "title": "Observed event",
  "short_summary": "Compact observation",
  "detailed_summary": "Detailed observation",
  "visual_summary": "Visible content",
  "topics": [],
  "participants": [],
  "locations": [],
  "organizations": [],
  "objects": [],
  "actions": [],
  "keywords": [],
  "transcript": {},
  "confidence": 0.9,
  "review_required": false,
  "review_reasons": [],
  "provider_evidence": {},
  "lineage": {
    "provider": "semvideo",
    "understanding_schema_version": 1,
    "understanding_job_id": "job_...",
    "asset_sha256": "hex-digest"
  }
}
```

`candidate_segment_id` is globally stable within the workflow. A single understood physical
asset can map to several collector media-unit IDs, but it remains one candidate timeline.

## Selection result

```json
{
  "schema_version": "segment-selection-output/v2",
  "selection_id": "sel_...",
  "workflow_id": "vmw_...",
  "workflow_state_version": 4,
  "query_plan_id": "qp_seg_001",
  "segment_id": "seg_001",
  "round_number": 2,
  "max_k": 8,
  "max_k_source": "derived",
  "status": "selected",
  "selected": [
    {
      "rank": 1,
      "candidate_segment_id": "job_...:segment_...",
      "understanding_job_id": "job_...",
      "media_unit_ids": ["bilibili:..."],
      "start_ms": 1000,
      "end_ms": 9000,
      "relevance": "strong",
      "covered_facet_ids": ["facet_..."],
      "evidence": ["Evidence copied or referenced from the input"],
      "selection_reason": "Why this segment improves the selected set"
    }
  ],
  "selection_stage_uncovered_facet_ids": [],
  "warnings": [],
  "audit_artifact": {
    "path": "selection-audit.jsonl",
    "sha256": "hex-digest"
  },
  "lineage": {
    "candidate_artifact_sha256": "hex-digest",
    "candidate_count": 120,
    "shortlisted_count": 24,
    "understanding_job_ids": ["job_..."],
    "collection_session_ids": ["ses_..."]
  }
}
```

Echo all request identity fields exactly. Use `status: "invalid_input"`, an empty `selected`
array and structured `errors` for invalid lineage or catalogs. The result deliberately contains
no `sufficient` field.

## Parent-agent return

Return only:

```json
{
  "status": "selected",
  "selection_id": "sel_...",
  "workflow_id": "vmw_...",
  "workflow_state_version": 4,
  "result_path": "selection-result.json",
  "selected": [
    {
      "candidate_segment_id": "job_...:segment_...",
      "understanding_job_id": "job_...",
      "media_unit_ids": ["bilibili:..."],
      "start_ms": 1000,
      "end_ms": 9000
    }
  ],
  "warnings": [],
  "candidate_count": 120,
  "selected_count": 8
}
```

Do not include the full candidate catalog, audit records, unselected summaries or a final
material-gap judgment.

## Sharded selection

When the artifact cannot safely fit in one context:

1. Partition by `item_id`; never split records from one understood asset across shards.
2. Keep an overall shortlist and one shortlist for every required facet from each shard.
3. Size the union so every facet can still contribute at least `max_k` candidates when available.
4. Run one global selection over the union.
5. Write every candidate’s shard decision to `selection-audit.jsonl`.

Sharding controls context only. Never omit pages, promote weak evidence to fill `max_k`, or
interpret selection-stage coverage as the final material-gap decision.
