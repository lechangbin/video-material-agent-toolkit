# Search-understand-refine workflow contract

This contract joins three independently recoverable tools through versioned files.
It does not create a fourth application state database and does not replace either
tool's authoritative state.

## Ownership boundaries

| Owner | Responsibility | Authoritative state |
| --- | --- | --- |
| `material-collector` | Search the frozen platform scope, resolve works, download 720p proxies | session SQLite and `collection-result.json` |
| Semvideo | Understand one physical proxy and expose complete semantic segments | Semvideo public job and segment CLI |
| `select-video-segments` | Rank one cumulative candidate catalog into a bounded Top-K | selection v2 result and audit |
| Parent Agent | Judge sufficiency against the original script and create gap queries | `gap-decision.json` |
| This Skill | Freeze lineage, bridge schemas, enforce budgets and stop states | files under `<workflow-root>` |

## Layout

```text
<workflow-root>/
  workflow.json
  workflow-state.json
  input/
    collection-input.json
    initial-query-plans.json
  rounds/<segment-id>/round-<nnn>/
    round-plan.json
    collection-input.json
    query-plans.json
    collector-execution.json
    collection-result.json
    understanding-batch.json
    understanding-jobs.json
    understanding-catalogs.json
    candidate-segments.jsonl
    selection/
      selection-request.json
      selection-result.json
      selection-audit.jsonl
    gap-decision.json
```

Use cumulative collection results from all completed rounds when rebuilding the
understanding batch and candidate catalog. Files may be regenerated only when their
lineage and content agree; never silently overwrite a conflicting artifact.

## Deterministic bridge commands

Paths below are illustrative. Resolve each script relative to this Skill.

Initialize:

```powershell
python scripts/init_workflow.py `
  --input <collection-input.json> `
  --query-plans <initial-query-plans.json> `
  --collector <absolute-material-collector.exe> `
  --workflow-root <workflow-root> `
  --material-workspace <material-workspace> `
  --semvideo-workspace <semvideo-workspace> `
  --semvideo-profile <profile> `
  --max-rounds 3 `
  --max-videos 18
```

Plan round one:

```powershell
python scripts/plan_round.py `
  --workflow <workflow-root>\workflow.json `
  --segment-id <segment-id> `
  --output-dir <round-dir>
```

Plan a supplemental round:

```powershell
python scripts/plan_round.py `
  --workflow <workflow-root>\workflow.json `
  --segment-id <segment-id> `
  --decision <previous-round>\gap-decision.json `
  --batch <previous-round>\understanding-batch.json `
  --previous-query-plans <round-001>\query-plans.json `
  --previous-query-plans <round-002>\query-plans.json `
  --output-dir <next-round-dir>
```

Invoke the Collector runner with `Operation=run`, the generated input and
QueryPlans, and `MaxRounds`/`MaxVideos` from `round-plan.json`. Save the runner
startup result as `collector-execution.json`; after termination, copy or reference
the authoritative collection result at the recorded path.

Build a cumulative batch:

```powershell
python scripts/prepare_understanding_batch.py `
  --workflow <workflow-root>\workflow.json `
  --segment-id <segment-id> `
  --collection-result <round-001-result.json> `
  --collection-result <round-002-result.json> `
  --profile <profile> `
  --output <round-dir>\understanding-batch.json
```

The generated `video-material-understanding-batch/v2` copies the workflow's frozen
`platform_scope`. Every collection result must declare that exact scope, and every
candidate platform must belong to it; later round planning rechecks the same field.
Collector assets may also expose a human-readable `display_relative_path` for the work-group
primary. The bridge must continue resolving and hashing `relative_path`, which is the authoritative
content-addressed proxy; `display_relative_path` is an optional editing view and is null for
fallback sources.

For every batch item without a job mapping:

1. copy forward the prior round's valid job mappings into the current cumulative
   `understanding-jobs.json`; never discard a mapping merely because a new
   collection result was added:

   ```powershell
   python scripts/record_understanding_job.py `
     --batch <round-dir>\understanding-batch.json `
     --jobs <round-dir>\understanding-jobs.json `
     --previous-jobs <previous-round>\understanding-jobs.json
   ```

   Omit `--previous-jobs` in round one to initialize an empty mapping.
2. run Semvideo's mandatory `load_context.py` gate;
3. when a submission slot is available, run:

   ```powershell
   <semvideo> process <absolute-proxy-path> `
     --workspace <semvideo-workspace> `
     --profile <profile> `
     --idempotency-key <batch-item-idempotency-key> `
     --json
   ```

4. bind the returned job:

   ```powershell
   python scripts/record_understanding_job.py `
     --batch <round-dir>\understanding-batch.json `
     --jobs <round-dir>\understanding-jobs.json `
     --item-id <item-id> `
     --process-response <semvideo-process-response.json>
   ```

Do not pass `--render`. Observe/recover jobs by the Semvideo Skill and rerun its
context gate after every mutation or terminal transition.

Build candidates:

```powershell
python scripts/collect_semvideo_catalog.py `
  --batch <round-dir>\understanding-batch.json `
  --jobs <round-dir>\understanding-jobs.json `
  --semvideo <resolved-semvideo-command> `
  --workspace <semvideo-workspace> `
  --output <round-dir>\candidate-segments.jsonl `
  --catalogs-output <round-dir>\understanding-catalogs.json
```

Build the isolated-selection request:

```powershell
python scripts/build_selection_request.py `
  --workflow <workflow-root>\workflow.json `
  --segment-id <segment-id> `
  --round-number <n> `
  --catalogs <round-dir>\understanding-catalogs.json `
  --candidates <round-dir>\candidate-segments.jsonl `
  --output <round-dir>\selection\selection-request.json
```

## Budget semantics

Budgets are per theme segment:

```text
remaining_rounds = max_rounds - completed_rounds
remaining_video_slots = max_videos - occupied_media_units
round_admission_limit = ceil(remaining_video_slots / remaining_rounds)
```

The Collector receives remaining rounds and remaining video slots, not the original
totals, so its per-round admission rule remains valid. `occupied_media_units` counts
all stable downloaded proxies across prior rounds, including cross-platform
fallback copies. Understanding deduplication does not refund search/download budget.

Each query expression targets the complete frozen `platform_scope`. Each selected platform may
return at most 20 results for that expression.

## Gap decision

The parent Agent writes:

```json
{
  "schema_version": "video-material-gap-decision/v1",
  "workflow_id": "vmw_...",
  "workflow_state_version": 1,
  "segment_id": "seg_001",
  "query_plan_id": "qp_seg_001",
  "round_number": 1,
  "status": "insufficient",
  "previous_query_texts": ["bilibili:initial query text"],
  "evidence": {
    "selection_id": "sel_...",
    "selection_result_path": "selection/selection-result.json",
    "selection_result_sha256": "hex-digest",
    "selected_candidate_segment_ids": ["job_...:segment_..."]
  },
  "facet_assessment": [
    {
      "facet_id": "facet_...",
      "status": "missing",
      "reason": "The selected set does not show the required action."
    }
  ],
  "gaps": [
    {
      "gap_id": "gap_001",
      "facet_ids": ["facet_..."],
      "description": "Missing visual evidence anchored to the original narration."
    }
  ],
  "next_queries": [
    {
      "query_id": "q_seg_001_round_002_01",
      "text": "targeted search expression",
      "platform": "bilibili",
      "language": "zh-CN",
      "facet_ids": ["facet_..."],
      "budget": 20
    }
  ]
}
```

For `status: "sufficient"`, `gaps` and `next_queries` must be empty. For
`status: "insufficient"`, both must be non-empty, query text must not duplicate a
previous round. `previous_query_texts` must contain the normalized cumulative query
history through the current round as normalized `platform:text` entries and must match every prior `query-plans.json`
passed to `plan_round.py`. Pass exactly one distinct QueryPlan artifact for every
prior round; missing or repeated artifacts are rejected. Every facet ID must belong
to the frozen QueryPlan, and every supplemental expression must target the complete
frozen `platform_scope` (the example above shows a Bilibili-only workflow). The decision must reference the current
selection result; selection coverage alone is not the judgment.

If the decision is insufficient but no budget remains, retain the decision and
record the segment terminal state as `stopped_with_gaps`; do not create another
round.

Persist the segment outcome:

```powershell
python scripts/record_segment_state.py `
  --workflow <workflow-root>\workflow.json `
  --state <workflow-root>\workflow-state.json `
  --segment-id <segment-id> `
  --status sufficient `
  --round-number <n> `
  --result-artifact <selection-or-gap-or-failure-artifact> `
  --collection-session-id <session-id> `
  --semvideo-job-id <job-id>
```

Repeat the identity flags when several sessions or jobs contributed. The state
file is the recoverable per-segment summary; `workflow.json` remains the immutable
definition. A terminal segment cannot be replaced with a conflicting result.
`human_action_required` may later transition after the requested action.

## Recovery and stop rules

- Collector recovery is exclusively the sibling Collector runner contract.
- Semvideo recovery is exclusively the installed Semvideo Skill and public CLI.
- Rebuild bridge artifacts from authoritative upstream results; never patch an
  upstream database or task file.
- A batch with zero complete understanding catalogs stops as
  `understanding_failed`.
- Authentication or Semvideo `user_action` stops as `human_action_required`.
- `report_bug`, invalid lineage, corrupt hashes, or a repeated deterministic
  failure stops as `failed`.
- Cancellation stops as `cancelled`.
- Do not start supplemental search until the current round has at least one
  complete catalog, a valid selection v2 result, and a parent-Agent gap decision.
