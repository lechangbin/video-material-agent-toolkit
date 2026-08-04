---
name: select-video-segments
description: Rank structured candidate-segment observations from an external video-understanding module and select a traceable global Top-K set for one short-video theme segment. Use when the collection workflow has finished downloading and cross-platform duplicate resolution, video understanding has returned timestamped segment descriptions, and an Agent must choose the most relevant and complementary segments before cutting and ingestion. Do not use for searching platforms, watching videos, downloading media, judging final material sufficiency, or selecting shots for the final edit.
---

# Select Video Segments

Select a global Top-K set from structured video-understanding results without making another multimodal request. Optimize the set for the query plan’s thematic intent and required visual facets, not for final shot order.

## Required input

Require:

- selection lineage: `selection_id`, `workflow_id`, `workflow_state_version`,
  `query_plan_id`, `segment_id`, and `round_number`;
- one theme segment;
- its query plan and ordered required visual facets;
- `max_k` from the query-plan selection policy;
- a candidate artifact containing `candidate_segment_id`, Semvideo job identity, media
  identities, time range, summaries and semantic evidence;
- completeness metadata for every video-understanding catalog contributing candidates;
- source and cross-platform work-group identity when available.

Read [references/selection-contract.md](references/selection-contract.md) for the v2 workflow
contract and output shape. Do not paste the full candidate artifact into the parent Agent prompt,
and do not invent missing observations.

Unless the query plan explicitly overrides it, require the workflow to calculate:

```text
max_k = min(12, 2 * required_visual_facet_count + 2)
```

If this cap cannot provide two independent candidates per required facet, return a planning warning that requests theme splitting or an explicit override. Do not silently weaken the coverage rule.

## Execution isolation

Run this skill in a fresh selection subagent with no inherited parent conversation whenever subagents are available. Pass only:

- the selection-request artifact path and workflow identity;
- the candidate artifact path and content hash;
- the output directory;
- the Skill path.

Keep the full candidate summaries inside the selection subagent. Write the full selection result and audit trail to session artifacts. Return to the parent Agent only the result path, selected Top-K records, warnings, and aggregate counts. Keep selection-stage coverage notes in the artifact; do not present them as final material gaps.

If the candidate artifact exceeds the subagent’s safe context budget, partition it by media unit and process independent shards. Preserve a bounded shortlist for every required facet plus an overall shortlist, then perform one global reduction over their union. Do not silently truncate later pages.

## Workflow

1. Validate workflow identity, candidate hash, catalog completeness, identities, and time ranges.
   Accept candidates only from catalogs explicitly marked `complete`. Return `invalid_input` with
   structured errors if lineage is missing, a result cannot be traced to a Semvideo job and media
   unit, or a contributing catalog is silently incomplete.
2. Remove only exact duplicate records and invalid records. Do not recreate the deleted metadata, copyright, or visual-quality prefilter.
3. Compare each candidate with the original theme intent and required visual facets. Treat titles and source descriptions as weak supporting evidence only.
4. Mark candidates `strong`, `partial`, `weak`, or `unsupported` for theme relevance and list the evidence used.
5. Build the selected set globally across the whole candidate batch:
   - prefer strong theme relevance;
   - add coverage for higher-priority missing visual facets;
   - prefer complementary visual expressions over near-identical segments;
   - penalize overlapping time ranges and semantically redundant segments;
   - target at least two independent candidates for each required visual facet when evidence permits;
   - target aggregate candidate duration of about three times the narration duration;
   - stop when coverage and duration targets are met, even if fewer than `max_k` items are selected;
   - never select a weak candidate only to fill `max_k`.
6. Apply deterministic tie-breaking in this order: required-facet priority, evidence
   completeness, lower redundancy, stable `candidate_segment_id`.
7. Write selected and reserve candidates with reasons, selection-stage facet coverage, warnings, and input lineage to the result artifacts.

For sharded input, keep the complete per-candidate audit in an artifact. Do not return the audit or unselected summaries through the parent conversation.

## Guardrails

- Use only the supplied structured understanding results. Do not call a cloud multimodal model or inspect video frames as part of this skill.
- Do not infer that two platform sources are the same work. Cross-platform grouping must already have been confirmed by the collection workflow.
- Do not discard unselected sources from `collection-result.json`; Top-K selection only controls the downstream cutting and ingestion request.
- Do not decide final material gaps or whether the query plan is sufficient. Those decisions belong to the parent Agent after it receives the successfully committed effective-segment snapshot.
- Do not return the complete candidate catalog to the parent Agent.
- Do not generate the next search query, arrange shots, or choose final edit timing.
- Treat `max_k` as a hard cost ceiling, not a quota. Return fewer items when targets are met early or evidence is insufficient.

## Completion

Finish only after the output:

- validates against the v2 workflow contract;
- contains no duplicate segment identities;
- contains at most `max_k` selected items;
- explains every selection and omission using supplied evidence;
- echoes the exact selection and workflow identity;
- records selection-stage facet coverage for audit without labeling it as the final material gap.
