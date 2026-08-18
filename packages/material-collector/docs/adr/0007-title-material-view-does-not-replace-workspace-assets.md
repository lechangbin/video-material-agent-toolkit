# Title material views do not replace workspace assets

Downloaded media remains a workspace-owned, SHA-256 content-addressed asset because integrity,
deduplication, and cross-session reuse require one stable physical identity. The collector also
publishes a session-scoped title material view for the primary member of each cross-platform work
group, using source titles for folders and media-unit titles for files; this view is a hard link
when supported and an atomically verified copy otherwise. Keeping the readable path separate
avoids making mutable platform titles part of asset identity, while session scope preserves each
fingerprint decision without introducing a cross-session global work registry or deleting paths
already referenced by editing projects.
