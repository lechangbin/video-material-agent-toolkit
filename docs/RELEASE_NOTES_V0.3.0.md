# Toolkit v0.3.0 release notes

v0.3.0 is a breaking workflow release. It adds authorized YouTube and TikTok collection,
verified 16:9 eligibility, managed CRV evidence with generic multimodal-subagent checkpoints,
and a third public CLI for concat-safe editing clips.

## Breaking contracts

- QueryPlans is now v3. Every frozen platform in `platform_scope` has its own language-labelled
  query branch and budget. v2 plans and sessions are rejected; no migration is supplied.
- YouTube and TikTok use a `foreign_proxy` capability. Domestic HTTP, browser, and subprocess
  transports remain direct. Foreign access fails closed if no validated local proxy exists.
- Collection manifests retain route, authorization, actual managed yt-dlp version, and display
  geometry evidence. A local FFprobe check on staged proxy and high-quality bytes is authoritative.
- Formal Semvideo understanding uses managed stable CRV evidence and a generic isolated-subagent
  request/result checkpoint. The Agent host supplies effective context and subagent capacity;
  model names are not used to infer either. The 128K, 256K, 512K, and 1M evidence profiles must be
  benchmarked and explicitly approved before automatic processing.
- `media-conformance` consumes `editing-media-conformance-request/v1` and produces a frozen
  1080p30 SDR H.264 High/yuv420p/AAC Assembly Set. The workflow stops before concat unless exact
  stream equality and a stream-copy packet smoke test both pass.

## Managed dependencies and authentication

- yt-dlp follows official nightly PyPI releases. Candidate runtimes are installed side by side,
  package-hashed, conformance-tested, activated at most once per 24 hours, and frozen per collector
  session. A failed candidate retains the last-known-good version.
- CRV follows official stable PyPI releases under the same candidate/active/frozen/LKG model.
- Foreign login uses isolated persistent Microsoft Edge or Google Chrome profiles outside the
  repository and user workspaces. It opens the ordinary installed browser without Playwright,
  WebDriver, or remote-debugging flags; the human closes the window after login and yt-dlp reads
  only that designated profile. Bilibili authentication remains unchanged. Cookies are never
  exported or passed on command lines. Credentialed proxy URLs are rejected before browser launch.
  Login is always visible; routine search/download is hidden unless explicitly requested.

## Structured failures

New machine-readable failures cover missing/invalid foreign proxy, unsupported foreign extractor,
foreign authentication, browser closure, display-geometry rejection, managed-runtime update,
CRV evidence corruption, missing subagent context/capability, invalid subagent result and exhausted
repairs, immutable-source drift, conformance/profile mismatch, cancellation, and concat failure.

## Explicitly out of scope

- Generic yt-dlp URL extraction, DRM/access-control bypass, and platforms other than the five named
  capabilities.
- pi extension registration, Agent-runtime implementation, provider/model switching, or a CRV
  Skill. The handoff contract is Agent-runtime-neutral.
- Creative edit order, transitions, speed, reframing, effects, titles, mixing, or composition.
- SQLite consolidation of user-hidden artifacts and XLSX export.

## Human release gates

Publication still requires approved measurements for all four context tiers and interactive
Windows evidence for Edge login on YouTube and TikTok. SSH-only browser tests do not satisfy the
visible-login requirement.
