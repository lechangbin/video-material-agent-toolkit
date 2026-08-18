# ADR 0008: Foreign platforms use capability-scoped network routes

## Status

Accepted for v0.3.0.

## Context

The collector currently treats its supported domestic platforms as one network environment. Adding YouTube and TikTok creates a security and correctness boundary: foreign access requires the user's local proxy, while domestic traffic must not inherit that proxy. Platform extractors also change faster than the toolkit release cadence, so baking one yt-dlp version into the collector executable would make site fixes unavailable until the next toolkit release.

## Decision

- Platform capabilities declare either `domestic_direct` or `foreign_proxy`; callers cannot override the route per request.
- Bilibili, Douyin, and Xiaohongshu always use the default direct network. YouTube and TikTok require a validated local HTTP CONNECT or SOCKS5 proxy and fail closed if it is unavailable.
- Proxy discovery is bounded and ordered: toolkit-specific configuration or environment, Windows WinINET, then standard proxy environment variables. The collector does not scan ports and never silently retries a foreign request directly.
- QueryPlans v3 replace the single expression set with per-platform search branches so language and wording can differ without widening the frozen platform scope.
- YouTube discovery uses yt-dlp search. TikTok discovery uses the persistent Edge platform profile. yt-dlp resolves and downloads both platforms through a narrow bridge that enables only their named extractors and disables the generic extractor.
- yt-dlp is a managed side-by-side Python runtime, sourced from the official nightly PyPI channel. Before a new foreign session, the manager checks for an update at most once per 24 hours, runs bridge-contract and public-fixture smoke tests, then atomically activates a passing version. Existing sessions freeze their active version. The last known good version remains available for rollback.
- A site extractor failure may trigger one managed update and one retry. Every session and error report records the attempted versions without recording proxy secrets or browser profile paths.
- Long-lived authentication uses an isolated persistent Edge profile per foreign platform outside the repository and workspaces. Interactive login is visible; ordinary discovery and download are hidden. yt-dlp may read only the designated profile. Expiry returns `foreign_auth_required`.

## Rejected alternatives

- **One global proxy setting:** rejected because domestic traffic could be routed across an unintended boundary.
- **Foreign direct fallback:** rejected because it violates the user's explicit network constraint and can leak requests.
- **Fixed yt-dlp dependency in the collector wheel/executable:** rejected because extractor breakage cannot wait for a toolkit release.
- **Unbounded latest/master installation:** rejected because an upstream regression would immediately affect active workflows.
- **Generic yt-dlp extractor:** rejected because it would silently expand supported sites and the authorization surface.
- **Exported Netscape cookie files:** rejected because they create portable credentials that are easier to leak and harder to isolate.

## Consequences

- Foreign collection depends on a locally reachable proxy and can stop before search with a structured report.
- The managed runtime requires an updater, conformance fixtures, atomic activation, version freezing, and cleanup rules for inactive versions.
- TikTok search remains browser-sensitive even though download resolution is delegated to yt-dlp.
- QueryPlans v3 and v0.3 sessions are intentionally incompatible with previous contracts; no migration layer is provided.
- Authentication profiles remain local mutable state and are never copied into a workspace, repository, log, or error payload.

## References

- [yt-dlp installation and update channels](https://github.com/yt-dlp/yt-dlp/wiki/Installation)
- [yt-dlp nightly builds](https://github.com/yt-dlp/yt-dlp-nightly-builds)
