# Third-party notices

## yt-dlp

The foreign-platform Bridge installs official `yt-dlp` nightly PyPI releases in
a managed side-by-side runtime and enables only the named YouTube and TikTok
extractors.

- Upstream: <https://github.com/yt-dlp/yt-dlp>
- License: The Unlicense
- Use: invoked as an external managed Python module; no source file is copied into
  this repository or its wheel.

The runtime's optional dependencies retain their own licenses. Active collection
sessions freeze the exact conformance-tested version and package hash.
