---
name: semvideo
description: Use the local Semvideo CLI to understand video files, create semantic segments and summaries, inspect shot-scale/camera-motion annotations, inspect durable background jobs, recover interrupted processing, export selected clips or shots, or diagnose the Semvideo runtime. Trigger for local video understanding, semantic video segmentation, cinematography such as aerial/wide/push/rise shots, timestamped summaries, Semvideo job status/recovery, and segment or shot export. Do not use it for search ranking or Top-K selection.
---

# Semvideo

Use only the public Semvideo CLI. Treat this Skill directory as read-only. Never
edit `semvideo-data/` task files, invoke internal Semvideo Python modules, or infer
recovery actions from free-form logs.

## NON-NEGOTIABLE CONTEXT GATE

Before any `process`, `job resume`, `job retry`, cancellation, or configuration
mutation, run this exact gate with any Python available to the host Agent:

```text
python <this-skill-directory>/scripts/load_context.py \
  --workspace <root> --profile <name>
```

This step is mandatory, not a recommendation. Do not substitute memory, an earlier
session, raw `config.toml` reads, or assumptions about defaults. The gate loads and
returns all required operational context through public CLI commands:

- the resolved, version-compatible absolute CLI command;
- the initialized workspace identity and paths;
- the complete non-secret effective configuration, including concurrency;
- validation of the selected processing profile;
- Doctor results for credentials, media tools, codecs, locks, and dependencies;
- current jobs and the remaining batch-submission allowance.

If the gate does not return `ok: true`, STOP. Follow its structured error; do not
submit or recover work. Use its exact `command` as `<semvideo>` below. Rerun the
gate after any job submission, terminal transition, retry, resume, cancellation,
or non-secret configuration change.

The returned `admission.available_submission_slots` is a hard upper bound for new
or resumed jobs in the current batch. Never fan out a directory into concurrent
`process` calls. Never launch more work because internal file locks exist. Submit
only enough jobs to fill the available slots, poll existing jobs to a terminal
state, rerun the gate, and then admit the next jobs. If the value is `0`, submit
nothing and continue observing the listed active jobs.

Semvideo atomically rechecks this limit inside every `process`, `job resume`, and
`job retry`; the Skill does not calculate terminal states or reserve capacity.
If a racing session consumes the last slot, accept
`job_admission_capacity_reached` as a normal structured capacity result, do not
retry immediately, observe the returned active jobs, and rerun the gate after a
terminal transition.

For read-only inspection that cannot mutate or start work, the bundled resolver may
be run directly:

```text
python <this-skill-directory>/scripts/resolve_semvideo.py
```

The resolver does not rely on the Agent's isolated `PATH`; it validates
`cli_version` `0.1.4`, workspace Schema max `1`, job Schema max `1`, and
`skill_protocol_version` `1`. If the user supplies a CLI location, pass it through
the non-secret `SEMVIDEO_CLI` environment variable.

If resolution fails, stop and report that a matching Semvideo wheel or package
must be installed. Do not use the host Agent's unrelated Python to reinstall it
and do not silently install software.

Find an initialized workspace by passing `--workspace <root>`. Initialize only when
the user explicitly chooses a new workspace:

```powershell
<semvideo> init <root> --json
<semvideo> doctor --workspace <root> --json
```

Run `doctor` before submitting or resuming work in a session.
Treat `doctor.credential.ok` as a boolean. Never read, print, log, or persist the API
key. Processing requires the configured credential environment variable in the
Worker's inherited environment.

If Doctor reports missing media tools and exact executable paths are already
known, correct them once through the public CLI, then rerun Doctor:

```powershell
<semvideo> config set-media-tools `
  --ffmpeg <absolute-ffmpeg.exe> `
  --ffprobe <absolute-ffprobe.exe> `
  --workspace <root> --json
```

Never edit `config.toml` directly. If the paths are unknown, stop and ask the user
to install or identify the certified runtime. Do not mutate the Agent's `PATH`.
Doctor must confirm `-fps_mode`, `libx264`, and `aac` before processing or
exporting.

When the user selects Agnes, apply its complete supported Provider Profile through
the public CLI, then rerun the context gate:

```powershell
<semvideo> config set-llm-provider agnes --workspace <root> --json
```

This selects `agnes-2.5-flash`, the official OpenAI-compatible endpoint,
`AGNES_API_KEY`, a 512K total context with an 8K output reservation, and an LLM
concurrency ceiling of 2. Accept the command's structured result as authoritative;
keep the credential in the process environment and pass only its presence through
Doctor. To return to the repository baseline, run the same command with
`siliconflow`; do not reconstruct either profile by editing TOML.

## Process a video

For Agent automation, submit asynchronously and retain the returned `job_id`:

```powershell
<semvideo> process <video> --workspace <root> --json
```

The source video is copied into the workspace automatically. Submission success
means the background Worker started; it does not mean processing completed.

Use `--render` only when all final clips are required. Otherwise keep lazy rendering:

```powershell
<semvideo> process <video> --workspace <root> --render --json
```

Use `--idempotency-key <stable-key>` when the same request might be submitted again.
Do not create an idempotency key from mutable timestamps.

For a foreground wait, keep stdout machine-readable and suppress progress:

```powershell
<semvideo> process <video> --workspace <root> --wait --quiet --json
```

## Observe and recover

Poll with:

```powershell
<semvideo> job status <job-id> --workspace <root> --json
```

Terminal states are `completed`, `failed`, `cancelled`, and `interrupted`. For
diagnosis, retrieve structured logs without following:

```powershell
<semvideo> job logs <job-id> --workspace <root> --json
```

Follow the structured `failure.recovery` value:

- `resume`: run `<semvideo> job resume <job-id> --workspace <root> --json`.
- `retry_same`: run `<semvideo> job retry <job-id> --workspace <root> --json`.
- `correct_and_retry`: correct only the invocation or non-secret configuration
  identified by the error, then resubmit or retry.
- `user_action`: stop and report the exact structured error and requested action.
- `report_bug`: stop; do not modify task files or repeatedly retry.
- `none`: do not retry.

Apply the same deterministic correction fingerprint at most once. Create at most
one new processing attempt for the same retryable terminal failure; report a
recurring failure instead of looping.

Use `job resume` only for an interrupted task. Do not cancel, resume, or retry a
terminal task unless the returned contract explicitly permits it. To request
cancellation:

```powershell
<semvideo> job cancel <job-id> --workspace <root> --json
```

## Read understanding results

After `completed`, list compact semantic segments:

```powershell
<semvideo> segment list <job-id> --workspace <root> --json
```

Use `--offset`, `--limit`, and `--review-only` for pagination and review filtering.
Read a complete segment, including transcript and provenance, with:

```powershell
<semvideo> segment show <job-id> <segment-id> --workspace <root> --json
```

When the request includes cinematography such as aerial wide shots, slow push-ins,
or a camera gradually rising, query structured shot annotations:

```powershell
<semvideo> shot list <job-id> --workspace <root> `
  --viewpoint aerial --scale extreme_wide `
  --motion push_in --motion rise --speed slow `
  --keyword 缓慢推进 --keyword 逐渐拉高 --json
<semvideo> shot show <job-id> <shot-id> --workspace <root> --json
```

Treat these switches as exact structured filters, not relevance ranking. Repeated
filters of the same kind use AND semantics. Inspect
the returned `shot_scale`, `camera_motions`, summary, keywords, confidence, and
intersecting `final_segment_ids`.

Return segment IDs, half-open timestamp ranges, titles, summaries, confidence,
review status, transcript information, and artifact references to the calling
Agent. Semvideo performs video understanding only; leave query relevance, ranking,
embeddings, and Top-K selection to another tool or skill.

Export only selected segments to a path outside the task package:

```powershell
<semvideo> segment export <job-id> <segment-id> `
  --workspace <root> --output <clip.mp4> --json
<semvideo> shot export <job-id> <shot-id> `
  --workspace <root> --output <shot.mp4> --json
```

Do not treat exported files as modifications to the completed task package.

## Error discipline

Use exit codes and structured JSON together. Normal results are on stdout;
diagnostics and progress are on stderr. When an invocation fails before the command
runs, correct the arguments once. Never:

- expose credentials in commands or messages;
- edit state, checkpoint, manifest, retrieval, or cancel files directly;
- retry internal errors or invariant violations;
- assume a clip boundary from a keyframe or contact-sheet cell;
- claim completion before the job reaches `completed`.
