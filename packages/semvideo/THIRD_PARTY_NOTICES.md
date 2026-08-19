# Third-party notices

## claude-real-video

The formal v0.3 workflow installs
[`claude-real-video`](https://github.com/HUANGCHIHHUNGLeo/claude-real-video)
from its official stable PyPI releases into a managed side-by-side runtime. Active
jobs freeze the exact tested version; the historical prototype remains pinned to
`0.7.16`.

- Copyright: LeoAido
- License: MIT
- Use in this repository: invoked through a versioned Bridge; no source file from
  the project has been copied into this repository.

Its transitive tools and optional features have their own licenses. The Semvideo
wheel does not bake CRV in; the managed runtime installs and validates it locally.

## FFmpeg

FFmpeg and FFprobe are external system tools and are not bundled in this source
repository or its wheels. Users install a build separately and remain responsible
for that build's license, enabled codecs and redistribution terms.
