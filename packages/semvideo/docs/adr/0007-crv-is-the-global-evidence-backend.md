# ADR 0007: CRV is the global evidence backend

## Status

Accepted for v0.3.0.

## Context

Semvideo currently builds its own full-video evidence and sends one bounded semantic model request. The v0.3.0 workflow requires scene-aware, deduplicated global evidence that can be consumed by isolated multimodal subagents. The free CRV package provides keyframes, timestamps, and transcription, but its public output is not a stable versioned contract and it does not provide the full shot-scale, viewpoint, and camera-motion annotations already owned by Semvideo.

## Decision

- CRV is a hard runtime dependency and the only global understanding-evidence backend in the formal v0.3.0 workflow. There is no native-evidence or direct semantic-model fallback.
- A deep CRV Bridge invokes CRV only on the local analysis proxy, disables memory features, and normalizes its output into a versioned Global Understanding Evidence Package. CRV never receives a platform URL, cookie, browser profile, script, query plan, or material gap.
- CRV is installed from the official stable PyPI channel into managed side-by-side runtimes. A candidate version must pass bridge conformance and golden-video tests before atomic activation. Each workflow freezes its CRV version, evidence profile, source hash, and normalized evidence hash; a last known good runtime is retained for rollback.
- Semvideo remains the owner of the deterministic shot timeline and Cinematography Annotations. It first reuses CRV frames that fall inside each shot. If those frames are insufficient, it may extract deterministic `cinematography_supplemental` frames inside that shot solely for cinematography analysis.
- The existing configured cinematography model remains responsible for shot language. The orchestration Skill does not switch that model.

## Rejected alternatives

- **Depend directly on CRV output files:** rejected because their schema and evidence selection are not a versioned Semvideo contract.
- **Allow CRV to download source URLs:** rejected because it would bypass collector authentication, proxy routing, provenance, and rendition controls.
- **Retain the current native global-evidence path as an implicit fallback:** rejected because two evidence backends make results and recovery nondeterministic.
- **Replace Semvideo cinematography with free CRV:** rejected because the free package does not provide the required complete shot-language contract.
- **Track CRV master continuously:** rejected because untested upstream changes could alter frame selection, dependencies, or side effects during a workflow.

## Consequences

- A CRV failure is a hard understanding failure and produces a structured report rather than a silent fallback.
- CRV updates can change evidence quantity and content, so cache identity, audit records, fixtures, and workflow recovery all include the exact version.
- Semvideo must maintain a bridge and golden fixtures even if CRV's direct Python call remains simple.
- Cinematography may use more frames than the global evidence package, but those additions cannot influence global semantic evidence or become a second backend.

## References

- [claude-real-video repository](https://github.com/HUANGCHIHHUNGLeo/claude-real-video)
- [claude-real-video on PyPI](https://pypi.org/project/claude-real-video/)
