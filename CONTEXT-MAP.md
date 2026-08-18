# Context map

## Bounded contexts

### Material Collector

Source: `packages/material-collector/CONTEXT.md`

Owns platform authentication, query planning contracts, material discovery,
search execution, downloads, collection sessions, and collector-side browser
and executor behavior.

### Semvideo

Source: `packages/semvideo/CONTEXT.md`

Owns local video understanding, semantic segmentation, summaries, durable
processing jobs, cinematography annotations, and segment or shot export.

## Relationships

- Material Collector produces downloaded material and normalized metadata that
  can be consumed by Semvideo.
- Semvideo does not own platform search, authentication, or download behavior.
- Cross-package orchestration connects both contexts through versioned files and
  explicit CLI contracts; it does not replace either package's authoritative state.
- Semvideo owns immutable video-understanding subagent requests and validates imported
  results. The orchestration Skill owns scheduling generic isolated subagents and
  cannot write Semvideo state directly.
- Decisions affecting only one context remain in that package's ADR directory.
- Decisions governing both contexts belong in the repository-level `docs/adr/`
  directory.
