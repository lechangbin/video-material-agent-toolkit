# Title material views do not replace workspace assets

状态：accepted

## Context

Downloaded media needs a stable identity for integrity checks, deduplication and cross-session
reuse, while people and editing tools need a path that exposes the platform title. Platform titles
are mutable and may contain Windows-invalid names, so one path cannot safely satisfy both roles.

## Decision

Downloaded media remains a workspace-owned, SHA-256 content-addressed asset. The collector also
publishes a session-scoped title material view for the primary member of each work group, using the
source title for the folder and media-unit title for the file. The view is a hard link when
supported and an atomically verified copy otherwise. `relative_path` continues to identify the
authoritative asset; optional `display_relative_path` identifies the readable view. This release
does not migrate old sessions or create a cross-session primary registry.

## Rejected alternatives

- Renaming the content-addressed asset to the title was rejected because mutable titles would break
  stable identity, integrity references and cross-session reuse.
- Publishing every cross-platform duplicate was rejected because it would present fallback copies
  as separate editing choices after the collector had already selected one primary.
- A global title registry and automatic old-workspace migration were rejected because they add a
  second ownership/cleanup boundary and require guessing historical titles and primary decisions.
- Symlinks were rejected as the default because Windows creation privileges and downstream editing
  tool support are less predictable than hard links or ordinary files.

## Consequences

- Filesystems without hard-link support duplicate bytes in the readable view; the copy is verified
  before atomic publication, but consumes additional storage.
- A platform title change creates another view entry. Old entries and entries orphaned by later
  primary changes accumulate until the user explicitly cleans them; session archival does not
  silently remove them.
- The added manifest column requires a new session schema. Because this release intentionally does
  not support old-session migration, older sessions fail with the existing structured unsupported-
  version error instead of being guessed or rewritten.
- Downstream machine processing must keep using `relative_path`; consumers that choose the readable
  path accept that it is a session-scoped convenience view, not asset identity.
