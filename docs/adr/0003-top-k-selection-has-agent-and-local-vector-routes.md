# ADR 0003: Top-K selection has agent and local vector routes

## Status

Accepted for v0.4.0.

## Context

The cumulative segment catalog can be complete even when the Agent host cannot
start a text selection subagent. The existing selection contract is owned by
cross-package orchestration; moving ranking into Semvideo or Collector would
make one package responsible for the other's query-plan or video semantics.

## Decision

The orchestration workflow freezes `auto`, `subagent_only`, or `embedding_only`
selection policy. `auto` prefers an isolated text subagent and uses a fixed local
multilingual embedding model when that capability or a free slot is unavailable
before selection starts. A started selection does not switch algorithms after
an invalid result or execution failure. Both routes consume the same complete
cumulative catalog and produce a common versioned result and audit. The vector
route is local and offline after checksum-verified setup; it uses theme and
facet similarity as ranking evidence while preserving candidate lineage,
diversity, K as a ceiling, and the parent Agent's final sufficiency decision.

## Consequences

The selected route and model fingerprint become part of selection lineage, and
derived vectors live under the workflow rather than Semvideo or Collector
authoritative state. A host without any multimodal subagent still cannot finish
formal automatic video understanding in v0.4.0; that capability boundary is
scheduled for v0.5.0.

## Rejected alternatives

- Cloud embeddings would add another credential and network dependency to the
  fallback route.
- A cross-workflow vector material library would change asset ownership and
  lifecycle without a current product need.
- Falling back after an invalid in-progress selection would hide which method
  made the decision and complicate recovery.
