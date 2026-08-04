---
name: search-understand-refine-video-materials
description: Orchestrate a complete local video-material workflow across material-collector, Semvideo, and select-video-segments. Use when an Agent must turn a versioned script and theme segments into QueryPlans, multi-platform 720p proxies, reusable video-understanding catalogs, isolated Top-K selections, and bounded supplemental searches until the parent Agent judges the material sufficient or the search budget is exhausted.
---

# Search, Understand, and Refine Video Materials

Connect the three existing tools through versioned files. This is the single
user-facing orchestration Skill; its bundled scripts are deterministic bridges,
not additional Skills.

## Load the component contracts

Before starting, read:

1. [references/workflow-contract.md](references/workflow-contract.md);
2. the sibling `collect-video-materials/SKILL.md` and its CLI execution contract;
3. the installed `semvideo/SKILL.md`;
4. the sibling `select-video-segments/SKILL.md` and its selection contract.

Use the bundled Collector runner for every Collector operation. Use only Semvideo's
public CLI and always pass its mandatory context gate before a processing or
recovery mutation. Run segment selection in the isolated subagent required by
`select-video-segments`.

## Required inputs

Require:

- versioned collection input containing the full script and theme segments;
- an existing material workspace;
- an initialized Semvideo workspace and profile;
- a caller-chosen workflow root;
- positive per-theme-segment `max_rounds` and `max_videos`.

An existing initial QueryPlan file is optional. When it is absent, the parent Agent
must read the full script and every theme segment, derive the visual strategy and
required visual facets, then write one versioned QueryPlan per segment before
initialization. This orchestration is intentionally stricter than the generic
Collector QueryPlan contract: every expression targets Bilibili, Douyin, and
Xiaohongshu. The search cap remains 20 results for that expression on each
platform. Do not reinterpret it as a shared three-platform cap.

## Plan and initialize once

If QueryPlans were not supplied, create them in the parent Agent's current
conversation. Keep the full script and current segment meaning as the semantic
anchor; content suggestions are optional. Do not delegate this initial planning
to the Top-K subagent and do not derive it from search-result titles.

Run `scripts/init_workflow.py` to freeze semantic input, paths, hashes, budgets,
normalized optional IDs, and workflow identity. Pass the absolute installed
`material-collector` executable through `--collector`; initialization calls its
read-only `contracts normalize` command so the frozen snapshot exactly matches
session creation. If the workflow root already
contains conflicting frozen input, stop instead of overwriting it.

Process theme segments serially. Multi-platform concurrency belongs inside one
Collector session; do not create competing Collector sessions that share browser
profiles. Semvideo submissions may run concurrently only up to the context gate's
reported admission allowance.

## Run one theme segment

Repeat the following bounded loop:

1. **Plan the search round.** Run `scripts/plan_round.py`. Round one uses the
   frozen initial QueryPlan. A later round requires the previous structured gap
   decision and cumulative understanding batch. Pass the returned remaining
   `MaxRounds` and `MaxVideos` to the Collector runner.
2. **Search and download proxies.** Invoke the Collector with the round's
   single-segment input and QueryPlan. Follow `collect-video-materials` for
   authentication, monitoring, cancellation, and recovery. Continue only when
   final output reaches `integration_required` and provides the authoritative
   `collection-result.json`.
3. **Build the cumulative understanding batch.** Run
   `scripts/prepare_understanding_batch.py` with every collection result for this
   theme segment. Every downloaded proxy consumes the video budget, including
   fallback sources. Only `eligible_for_understanding=true` primary assets enter
   Semvideo. Reuse identical proxy hashes.
4. **Understand new assets.** Run the Semvideo context gate. Submit only batch
   items without a bound job, carrying the prior round's job mappings into the
   current cumulative `understanding-jobs.json`. Use the batch's stable
   idempotency key, omit
   `--render`, and never exceed `admission.available_submission_slots`. Record each
   successful submission with `scripts/record_understanding_job.py`. Observe and
   recover strictly through the Semvideo Skill, rerunning the gate after every
   state-changing command or terminal transition.
5. **Build complete catalogs.** Run `scripts/collect_semvideo_catalog.py`.
   Failed or incomplete jobs remain recorded but contribute no candidates. If no
   complete catalog exists, stop this segment as `understanding_failed`; do not
   invent a gap or launch a blind supplemental search.
6. **Select global Top-K.** Run `scripts/build_selection_request.py`, then give
   the request and candidate artifact to a fresh isolated subagent using
   `select-video-segments`. Persist its v2 result and audit. Return only its compact
   result to the parent Agent.
7. **Judge sufficiency in the parent Agent.** Read the frozen full script,
   current theme segment, required visual facets, and selected Top-K. Write the
   versioned gap decision defined in the workflow contract. Selection-stage
   coverage is evidence, not the final decision.
8. **Stop or refine.** If sufficient, complete the theme segment. If insufficient
   and budget remains, create targeted next queries anchored to the original
   script and facet IDs, then start a new Collector session. If rounds or video
   slots are exhausted, finish as `stopped_with_gaps`. Persist every segment
   outcome in `workflow-state.json` with `scripts/record_segment_state.py`; use the
   selection, gap, understanding failure, or action-required artifact as its
   result reference.

## Guardrails

- Never edit Collector SQLite, execution leases, auth locks, browser profiles,
  Semvideo task files, or either tool's installed source.
- Never use titles or descriptions as a substitute for video understanding.
- Never repeat a paid understanding request for the same proxy hash and profile.
- Never delete low-resolution proxies, source manifests, understanding results, or
  unselected sources; they remain workflow/session assets.
- Never fetch high-quality media in this workflow. Defer that to the later editing
  stage after actual clip selection.
- Never let Semvideo or the selection subagent decide final sufficiency or generate
  supplemental search expressions.
- Stop and preserve artifacts for human authentication, structured `user_action`,
  `report_bug`, cancellation, or invalid lineage. Do not recover by inventing shell
  commands.

## Completion

Finish only after `workflow-state.json` records every theme segment in one terminal
state:

- `sufficient`;
- `stopped_with_gaps`;
- `understanding_failed`;
- `human_action_required`;
- `cancelled`;
- `failed`.

Return the workflow root, per-segment terminal states, collection session IDs,
Semvideo job IDs, selection-result paths, remaining gaps, and retained source
manifest paths. Do not paste full candidate catalogs into the parent conversation.
