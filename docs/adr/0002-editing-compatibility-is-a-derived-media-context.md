# ADR 0002: Editing compatibility is a derived media context

## Status

Accepted for v0.3.0.

## Context

Downloaded videos vary in codec, frame rate, resolution, pixel format, time base, color transfer, and audio topology. Passing those files directly to a concat operation causes stream-parameter errors, timestamp failures, or inconsistent playback. Mutating the downloaded source would destroy provenance and reusable quality, while placing FFmpeg recipes in Skills would duplicate fragile media logic across callers.

## Decision

- Add a Media Conformance bounded context with one deep public module: given immutable source-asset references, explicit source ranges, and one frozen Editing Delivery Profile, it returns Conformed Clips plus a Conformance Report.
- Material Collector continues to preserve complete proxy and high-quality source assets. Semvideo continues to own semantic ranges. Orchestration chooses ranges and order; Media Conformance makes only technical transformations.
- The v0.3 default delivery profile is MP4, H.264 High via software `libx264`, 8-bit `yuv420p`, 1920x1080 square pixels, constant 30 fps, fixed 60-frame GOP, 90 kHz video track time scale, zero-based monotonic timestamps, and SDR BT.709 output. HDR inputs are deterministically tone-mapped to that SDR envelope.
- Audio is one AAC-LC stereo stream at 48 kHz and 192 kb/s with zero-based timestamps. The primary source audio is resampled; a source without audio receives a duration-matched silent stream so every clip has the same stream topology.
- Trimming and conformance occur in one FFmpeg render so a selected range is decoded and encoded once. A full-source range is valid, but the default workflow waits until selection and high-quality retrieval rather than transcoding every discovered candidate.
- Every output is written atomically, inspected with local FFprobe, and admitted to an Assembly Set only after exact profile checks and a packet-level concat smoke test. Partial or incompatible outputs never replace source assets.
- The profile and schemas are versioned. A later resolution, frame-rate, HDR, or codec policy requires a new profile rather than silently changing existing results.

## Rejected alternatives

- **Overwrite downloaded media:** rejected because it loses the authoritative source, breaks content identity, and forces all consumers to accept one editing policy.
- **Remux or stream-copy only:** rejected because container changes cannot reconcile codec, resolution, frame rate, pixel format, time base, or audio topology.
- **Normalize every download before selection:** rejected because most candidates are never edited and full-source transcoding wastes compute and storage.
- **Repair incompatibility only during final concat:** rejected because failures occur late, error attribution is poor, and every editing caller would need the same complex filter graph.
- **Put normalization in Material Collector or Semvideo:** rejected because collection must preserve sources and video understanding must remain independent of delivery encoding.
- **Use hardware encoders in the v0.3 contract:** rejected because device-specific output and availability would weaken reproducibility and packaged acceptance.

## Consequences

- v0.3 adds a third package and public CLI/contract surface, but concentrates codec, timestamp, audio, tone-mapping, validation, and cleanup behavior behind one interface.
- Conformance consumes time and produces derived storage; 720p sources may be upscaled and high-frame-rate sources reduced to 30 fps under the default profile.
- Source assets remain reusable, lossless with respect to the toolkit, and independently verifiable.
- Creative sequence order, transitions, speed changes, reframing, crop/pad, and final composition remain downstream decisions.
