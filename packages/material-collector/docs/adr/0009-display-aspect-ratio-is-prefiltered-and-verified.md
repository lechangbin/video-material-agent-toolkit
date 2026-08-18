# ADR 0009: Display aspect ratio is prefiltered and verified

## Status

Accepted for v0.3.0.

## Context

Platform metadata can avoid obviously unsuitable downloads, but it may omit rotation or sample-aspect-ratio information and can describe a different rendition from the bytes ultimately downloaded. FFprobe can inspect some remote URLs, but doing so still transfers data, may fail for authenticated or expiring URLs, and cannot prove the geometry of a later local rendition.

## Decision

- The collection target is display 16:9 with a tolerance of plus or minus one percent after normalizing encoded dimensions, rotation, sample aspect ratio, and display aspect ratio.
- A versioned media-geometry assessment records the observed values, evidence source, stage, and `accepted`, `rejected`, or `unknown` disposition.
- Platform and yt-dlp metadata form the first pre-download filter. A remote FFprobe check is optional only for bounded, public, unauthenticated direct-media URLs and is never authoritative.
- Unknown metadata may proceed to a staging download. Every low-rate proxy and every later high-quality rendition must pass local FFprobe validation on the staged bytes before publication as a workspace asset.
- A rejected staging file is deleted, while source identity, geometry, phase, and structured rejection reason remain in the manifest. Supplemental rounds do not download the same rejected source identity again.
- A high-quality rendition that disagrees with the accepted proxy is ineligible for final use and re-enters the normal material-gap decision flow.

## Rejected alternatives

- **Remote FFprobe only:** rejected because remote inspection is partial download, often incompatible with authentication, and may observe a different rendition.
- **Metadata only:** rejected because rotation, SAR, and rendition drift can produce false acceptance.
- **Download first with no prefilter:** rejected because known portrait media would consume bandwidth unnecessarily.
- **Automatic crop or pad:** rejected because changing composition is an editing decision outside the collector boundary.
- **Validate only the 720p proxy:** rejected because the later high-quality rendition can have different geometry.

## Consequences

- Some candidates with unknown metadata consume staging bandwidth before rejection.
- Staging storage must remain separate from durable workspace assets and be recoverably cleaned.
- Geometry failures become a normal source disposition rather than a transport failure.
- The source manifest and collection results must preserve enough structured evidence to explain exclusions and prevent repeated downloads.
