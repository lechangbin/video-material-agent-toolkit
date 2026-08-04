# Third-party notices

## claude-real-video

The prototype depends on
[`claude-real-video`](https://github.com/HUANGCHIHHUNGLeo/claude-real-video)
version `0.7.16`.

- Copyright: LeoAido
- License: MIT
- Use in this repository: installed as a pinned Python dependency and called
  through an Adapter; no source file from the project has been copied into this
  repository.

Its transitive tools and optional features have their own licenses. The default
Semvideo wheel does not install this optional prototype dependency.

## FFmpeg

FFmpeg and FFprobe are external system tools and are not bundled in this source
repository or its wheels. Users install a build separately and remain responsible
for that build's license, enabled codecs and redistribution terms.
