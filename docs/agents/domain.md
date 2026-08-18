# Domain documentation

This repository uses a multi-context domain-documentation layout.

## Reading order

1. Read `CONTEXT-MAP.md` to identify the affected bounded contexts.
2. Read each affected package's `CONTEXT.md`.
3. Read the ADRs under that package's `docs/adr/` directory.
4. For decisions spanning multiple packages, read the system ADRs under
   the repository-level `docs/adr/` directory when that directory exists.

## Locations

- Material collection: `packages/material-collector/CONTEXT.md`
- Video understanding: `packages/semvideo/CONTEXT.md`
- Package decisions: `packages/<package>/docs/adr/`
- System decisions: `docs/adr/`

## Editing rules

- Put shared vocabulary and stable domain definitions in the owning
  package's `CONTEXT.md`.
- Put durable design decisions and their consequences in an ADR.
- Keep package-local decisions with the package.
- Put a decision in `docs/adr/` only when it governs more than one bounded context.
- Update `CONTEXT-MAP.md` when a context or relationship is added, removed,
  renamed, or materially changed.
- Do not duplicate authoritative definitions across contexts; link to the owner.
- Preserve rejected alternatives and consequences in ADRs so later Agents do not
  reopen settled decisions without new evidence.
