# ADR 0001: Semvideo checkpoints generic subagent understanding

## Status

Accepted for v0.3.0.

## Context

Semantic understanding must move from one direct cloud-model request to coordinated multimodal subagents without binding this repository to pi, a particular Agent host, or a named model. Semvideo owns validated video semantics, while the cross-package orchestration Skill already owns Agent coordination. External Agent runtimes do not necessarily provide durable task identifiers, recovery, cancellation, or the same model context capacity.

## Decision

- Semvideo deterministically completes media probing, shot segmentation, CRV evidence normalization, and cinematography annotation, then writes an immutable `video-understanding-agent-request/v1` artifact and enters `awaiting_subagent`.
- Public Semvideo CLI commands export or inspect the request, atomically import `video-understanding-agent-result/v1`, expose structured validation errors, and resume the job after a valid import. Agents never edit Semvideo's private state.
- The orchestration Skill schedules fresh, isolated generic multimodal observer subagents and a coordinator. The contract describes capabilities and artifacts, not pi registration, model switching, tool installation, or provider names.
- Subagents receive only the immutable request, bounded evidence references, cinematography annotations, and optional read-only local analysis-proxy access when the host supports it. They never receive the script, QueryPlan, theme, gaps, platform credentials, or whole workspace.
- The result is a cited, continuous, non-overlapping semantic timeline. Semvideo validates request/context hashes, anchors, ranges, continuity, overlap, and evidence references before producing its existing segment catalog.
- Automatic mode requires an explicit effective context length before execution. It maps to one frozen tier: 128K, 256K, 512K, or 1M; values below 128K are unsupported and values above 1M use the 1M tier. Model names are never used to infer capacity.
- Each tier's image, transcript, cinematography, overlap, and observer budgets are release artifacts derived from benchmark experiments, not guessed constants. Window count is computed from the selected evidence profile. Host-reported subagent slots control concurrency independently; absent capacity means serial execution.
- The parent may coordinate an initial attempt and at most two fresh repair attempts. A repair receives the immutable request, the prior result, and structured validator errors, but not the prior conversation. The parent cannot edit the result or bypass validation.
- Missing subagent capability, missing context length, evidence corruption, cancellation, or upstream CRV/cinematography failure stops without repair. Exhausted retriable validation failures produce a structured error report.
- Recovery imports a valid existing result when present or starts a new local `agent_attempt_id`; it does not promise resumption of the same external Agent process.

## Rejected alternatives

- **Bind the workflow to pi or one Agent host:** rejected because extension registration is a separate project and would make the repository non-portable.
- **Let Semvideo spawn Agent processes:** rejected because Agent scheduling belongs to the Skill and host, not video-domain state.
- **Parent-Agent semantic fallback:** rejected because it bypasses isolation, evidence budgeting, and validation.
- **Guess context from model names or default to 128K:** rejected because aliases and provider limits drift and can cause over-budget requests.
- **Fixed two-window topology:** rejected because the correct number depends on actual evidence and experimentally verified context profiles.
- **Repair by editing JSON or continuing the same conversation:** rejected because it destroys provenance and can hide deterministic validation failures.

## Consequences

- Hosts without generic subagents cannot run the formal v0.3.0 understanding workflow and receive `subagent_capability_unavailable`.
- Automatic runs stop with `subagent_context_required` until capacity is supplied.
- The four context-tier benchmark mappings are release blockers and require representative fixtures plus quality and budget acceptance evidence.
- Waiting for subagents is a durable action-required state and must not occupy Semvideo media or LLM admission slots.
- Top-K selection, material sufficiency, and supplemental-query decisions remain outside Semvideo and outside the understanding subagents.
