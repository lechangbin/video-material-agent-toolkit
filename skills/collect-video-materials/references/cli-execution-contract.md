# CLI execution contract

## Resolve once

Run the bundled resolver before authoring or executing a request:

```text
python <skill-root>/scripts/resolve_material_collector.py
```

On Windows, `py -3.14` may replace `python` when it is the configured launcher. A successful
result has `ok=true`, an absolute `command`, `cli_version=0.2.1`,
`skill_protocol_version=1`, Collection input range 1.0..1.0, and QueryPlans range 2.0..2.0.
Use that one `command` value for the rest of the task. The resolver owns the bounded candidate
list and protocol check; the Agent must not add PATH scans, source inspection, trial commands, or
alternate executables. Exit 3 is not found and exit 4 is incompatible; report the emitted recovery
object and stop.

## Cross-platform executor

The lifecycle authority is the collector-owned executor. On hosts without PowerShell, invoke it
directly:

```text
<collector> executor invoke \
  --operation run \
  --workspace <material-workspace> \
  --input <collection-input.json> \
  --query-plans <query-plans.json> \
  --request-timeout-seconds 30 \
  --browser-channel auto \
  --progress-format jsonl \
  --max-rounds 3 \
  --max-videos 18
```

Resume, inspect, and cancel with these complete commands:

```text
<collector> executor invoke \
  --operation resume \
  --workspace <material-workspace> \
  --session-id <session-id> \
  --progress-format jsonl

<collector> executor invoke \
  --operation status \
  --workspace <material-workspace> \
  --session-id <session-id>

<collector> executor invoke \
  --operation cancel \
  --workspace <material-workspace> \
  --session-id <session-id>
```

The interface writes exactly one versioned JSON object to
stdout and keeps stderr empty. Its start, duplicate-executor, monitoring, cancellation, and
interrupted-recovery semantics are identical on Windows and non-Windows hosts.
Pass `--browser-channel auto|edge|chrome` only for a new run. Add `--show-search-browsers` to a
run or resume only when the user explicitly requests visible search windows.

## Windows adapter

Invoke the bundled runner with PowerShell splatting:

```powershell
$runner = "<skill-root>\scripts\invoke-collector.ps1"
$invoke = @{
    Operation = "run"
    Workspace = "<material-workspace>"
    InputPath = "<collection-input.json>"
    QueryPlansPath = "<query-plans.json>"
    RequestTimeoutSeconds = 30
    BrowserChannel = "auto"
    ShowSearchBrowsers = $false
    ProgressFormat = "jsonl"
    MaxRounds = 3
    MaxVideos = 18
    ResolvedCollectorPath = "<collector>"
}
& $runner @invoke
```

For resume, use `Operation="resume"` and `SessionId="<session-id>"`; omit `InputPath`,
`QueryPlansPath`, `RequestTimeoutSeconds`, `MaxRounds`, and `MaxVideos`.

```powershell
& $runner -Operation resume -ResolvedCollectorPath "<collector>" `
    -Workspace "<material-workspace>" `
    -SessionId "<session-id>" -ProgressFormat jsonl
& $runner -Operation status -ResolvedCollectorPath "<collector>" `
    -Workspace "<material-workspace>" `
    -SessionId "<session-id>"
& $runner -Operation cancel -ResolvedCollectorPath "<collector>" `
    -Workspace "<material-workspace>" `
    -SessionId "<session-id>"
```

`MaxRounds` and `MaxVideos` are positive integer strings passed only for `run`.
They define the per-session Collector constraints. A downstream orchestration
workflow may pass remaining budgets for one theme segment, but QueryPlan content
must never carry these values.

`QueryPlansPath` must point to QueryPlans 2.0. Its top-level `platform_scope` is the
frozen collection boundary, and every initial or supplemental expression must use
that complete scope. The runner and Collector must not authenticate or search a
platform outside it.

For monitoring and cancellation, invoke this same adapter with `Operation="status"` or
`Operation="cancel"`, plus `Workspace` and `SessionId`. These operations remain synchronous and
relay the executor JSON and exit code; do not call the CLI around the adapter.

The adapter only translates its stable PowerShell parameters into the cross-platform executor
interface. The collector owns background execution, terminal suppression, PID validation, log
paths, and duplicate-executor rejection. Do not add process-control statements around this call.

Every runner invocation writes exactly one versioned JSON object to stdout and keeps stderr
empty, including parameter and startup errors.

## Synchronous checkpoint commands

These commands do not use the background executor. Invoke the same resolved absolute Collector
command; each writes one final JSON object to stdout.

List pending human decisions:

```text
<collector> review list --workspace <material-workspace> --session-id <session-id>
```

The result has `schema_version=1.0`, `status=ok`, and `items[]` containing `media_unit_id`,
`platform`, `title`, `canonical_url`, `duration_seconds`, and `status`. After the human explicitly
chooses one item, record exactly one decision:

```text
<collector> review approve --workspace <material-workspace> --session-id <session-id> --media-unit-id <media-unit-id>
<collector> review reject --workspace <material-workspace> --session-id <session-id> --media-unit-id <media-unit-id>
```

Then resume through the executor. Never infer approval from duration, title, or source.

Republish/read the authoritative result when the final workflow output names it or bounded
diagnostics require it:

```text
<collector> result export --workspace <material-workspace> --session-id <session-id>
```

Only after a downstream editing decision has selected a specific media unit may that downstream
stage request its high-quality source:

```text
<collector> media fetch-hq --workspace <material-workspace> --session-id <session-id> --media-unit-id <media-unit-id>
```

Direct collection and the complete search/understand/refine workflow never call `fetch-hq`.

## Read-only diagnostics

- `<collector> version` verifies the CLI version, Skill protocol, and supported input ranges. The
  bundled resolver already performs this once on the normal path.
- `<collector> contracts schema` emits the same authoring schemas as the bundled snapshots. Use it
  only for a reported version/protocol mismatch or when a bundled snapshot cannot be read.
- `<collector> sessions list --workspace <material-workspace>` and runner-managed `status` are
  bounded state diagnostics. Prefer the known session ID over listing all sessions.
- `pwsh -NoProfile -ExecutionPolicy Bypass -File <repository>/scripts/bootstrap-agent.ps1
  -CheckOnly` is the repository-level prerequisite doctor. Use it only when installation or browser
  availability is the suspected failure.
- `--help` is text for an explicit user request, not a schema-discovery step.

Normal execution calls the resolver once, authors the two final JSON artifacts once, performs at
most one final `contracts normalize`, then starts one executor. Do not use any diagnostic above to
iterate on missing request fields.

## Exit codes and deterministic branching

- `0`: command succeeded or the workflow reached `completed`.
- `10`: workflow advanced but retained non-empty structured `issues`; inspect the final status and
  checkpoint instead of blindly resuming.
- `20`: durable `integration_required`, `auth_required`, or `decision_required`; follow
  `action_required` and do not classify it as a crash.
- `21`: terminal cooperative cancellation.
- `30`: retryable operational failure; branch on `error.code`, `details.retryable`, and the recovery
  guidance below.
- `40`: invalid input, version, workspace, missing session, or CLI usage; correct the declared input
  once, without trial-field probing.
- `50`: invalid durable state, corruption, or internal failure; preserve artifacts and report an
  error rather than retrying.
- `130`: the foreground CLI was interrupted. For executor-managed work, inspect its control record
  and runner-managed status before any recovery.

All foreground commands emit one final JSON object to stdout. Progress/diagnostics, when present,
are JSONL on stderr. The Windows runner and executor wrapper instead keep their own stderr empty and
return their one wrapper result on stdout; collection progress is read from the recorded child
stderr path.

## Successful startup

The runner returns one JSON object:

```json
{
  "schema_version": "1.0",
  "status": "started",
  "operation": "run",
  "process_id": 1234,
  "collector_process_id": 5678,
  "session_id": "ses_...",
  "stdout_path": "...",
  "stderr_path": "...",
  "control_path": "..."
}
```

Save all returned identifiers. `started` does not mean authenticated, downloaded, or completed.
The runner confirms the actual collector child process and a startup stability window before
returning `started`.

Search progress is also non-terminal. `search_plan_committed` means all requests belonging to
that QueryPlan committed. `search_plan_settled` with `status=completed_with_issues` means the
plan retained structured platform failures but has at least one committed batch available for
downstream resolution. Preserve its `requested_requests`, `completed_requests`,
`failed_requests`, and `not_attempted_requests` counts. Neither event authorizes a second
executor or replaces final stdout and runner-managed `status`.

## Runner errors

- `operation_invalid`: the caller tried to inject an operation other than `run`, `resume`,
  `status`, or `cancel`.
- `arguments_invalid`: remove unsupported runner parameters; do not pass them through to the CLI.
- `request_timeout_invalid`: pass an integer from 1 through 3600.
- `progress_format_invalid`: pass `jsonl` or `text`.
- `max_rounds_invalid`: pass a positive integer for a new `run`.
- `max_videos_invalid`: pass a positive integer for a new `run`.
- `browser_channel_invalid`: pass `auto`, `edge`, or `chrome` for a new `run`.
- `collector_path_invalid`: pass a non-empty executable path or omit the parameter to use the
  installed command.
- `resolved_collector_path_invalid`: rerun the bundled resolver and pass its successful absolute
  `command` through `ResolvedCollectorPath`; do not fall back to PATH.
- `collector_not_found`: install the CLI with uv or pass an explicit tested `CollectorPath`;
  do not synthesize another launcher.
- `execution_already_running`: monitor the returned `existing` control record; do not retry.
  The runner treats either a live wrapper or its recorded actual collector child as a live
  executor for the same session.
- `launch_lock_timeout`: another launcher is preparing an executor; wait and inspect existing
  control records before retrying.
- `session_status_unavailable`: the runner could not freeze the CLI runtime state before resume;
  report it and do not launch an executor.
- `collector_start_failed`: report the returned artifact paths; do not synthesize another
  launcher.
- `collector_start_not_confirmed`: the wrapper did not publish a valid, live collector child
  handshake; retain the returned artifacts and do not create a replacement launcher.
- `collector_exited_during_start`: read stdout/stderr before deciding whether input was invalid.
  Its `execution` object retains the wrapper `process_id`, actual `collector_process_id`, control
  path, stdout path, and stderr path.
- `session_id_not_observed`: retain and monitor the returned `execution` control record, read
  stderr and CLI session listings, and do not create a second run.

Parameter and schema errors return `40`. Recoverable launch contention, a live executor, and
startup failures return `30`. Exit `50` is reserved for unrecoverable state corruption or
internal errors.

## Runtime states

- `executing`: a non-expired lease owner exists; monitor it.
- `cancelling`: cancellation is requested and the live lease has not expired.
- `cancel_pending_recovery`: cancellation is requested and no live lease remains; use exactly one
  runner-managed resume to acknowledge it.
- `action_required`: follow the structured actor and requested artifacts.
- `idle`: no live executor owns the session; an expired owner's metadata may remain for audit.
- `cancelled`: terminal.
- `completed`: terminal.

## Prohibited recovery

Never:

- write a new PowerShell/Bash process launcher;
- use `Start-Process`, `Start-Job`, task scheduling, or delayed retry;
- run two executors for one session;
- interpret empty logs as a failed process;
- edit SQLite, remove lease rows, or delete lock files;
- route traffic through `127.0.0.1:10808`.
