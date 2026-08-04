# Real-video semantic merge prototype

> PROTOTYPE — disposable. Do not treat this package as production code.

This prototype answers one question:

> Can deterministic candidate cuts plus real keyframe evidence produce useful,
> structured semantic decisions for merging adjacent video segments?

It accepts a real local video, uses FFmpeg for candidate boundaries, uses a
pinned `claude-real-video` Adapter for evidence frames, optionally calls an
OpenAI-compatible multimodal model, builds a deterministic merge plan, stores a
scratch SQLite catalog, and writes a self-contained inspection report.

## One-command run

After the editable install:

```powershell
semvideo-prototype process C:\path\to\video.mp4 --out data\prototype-run
```

Model configuration is read only from environment variables:

```powershell
$env:SEMVIDEO_API_KEY = '<your key>'
$env:SEMVIDEO_BASE_URL = 'https://api.siliconflow.cn/v1'
$env:SEMVIDEO_MODEL = 'Qwen/Qwen3.6-35B-A3B'
```

Use `--no-llm` to validate the real media and artifact path without a paid
model call. In that mode every candidate boundary is retained for review.

Useful commands:

```powershell
semvideo-prototype doctor
semvideo-prototype process video.mp4 --out data\prototype-run --export-segments --open-report
semvideo-prototype grid-segment video.mp4 --out data\grid-run --transcribe --overwrite --open-report
```

`grid-segment` is a separate disposable experiment. It sends the whole video's
chronological 3x3 contact sheets to one multimodal request, asks the model for a
contiguous semantic segmentation, and then renders those model-selected ranges.
Camera or angle changes are explicitly not treated as semantic cuts. With
`--transcribe`, CRV also supplies timestamped transcript spans to the same
multimodal request and inspection report.

The output directory is intentionally disposable and contains:

```text
media.json
candidate-timeline.json
evidence/
segment-meanings.json
boundary-decisions.json
merge-plan.json
final-summaries.json
exported-segments.json
segments/*.mp4
prototype.sqlite3
inspection.html
manifest.json
```
