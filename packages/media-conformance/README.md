# Media Conformance

`media-conformance` converts explicitly selected ranges from immutable source assets into
versioned, technically uniform editing clips. It owns codec, frame-rate, color, audio topology,
timestamp, atomic-output, and concat-compatibility validation; it makes no creative decision.

The only supported automation boundary is versioned JSON:

```powershell
media-conformance contracts
media-conformance prepare --request <editing-media-conformance-request-v1.json>
media-conformance status --request <request.json>
media-conformance cancel --request <request.json>
media-conformance resume --request <request.json>
media-conformance verify-set --request <request.json>
```

Every command writes one JSON object. `prepare` and `resume` validate the immutable source hash,
render each half-open range once, publish only complete clips, FFprobe the frozen 1080p30 SDR
H.264/AAC profile, and run a stream-copy concat smoke test. Callers must stop unless the final
Assembly Set reports both `compatible=true` and `packet_concat_verified=true`.

The module never chooses ranges, order, transitions, speed, reframing, titles, effects, or audio
mix decisions. Source assets are opened read-only and checked again after success or failure.
