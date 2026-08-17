# CLI execution contract

## Runner

Invoke the bundled runner with PowerShell splatting:

```powershell
$runner = "<skill-root>\scripts\invoke-collector.ps1"
$invoke = @{
    Operation = "run"
    Workspace = "<material-workspace>"
    InputPath = "<collection-input.json>"
    QueryPlansPath = "<query-plans.json>"
    RequestTimeoutSeconds = 30
    ProgressFormat = "jsonl"
    MaxRounds = 3
    MaxVideos = 18
}
& $runner @invoke
```

For resume, use `Operation="resume"` and `SessionId="<session-id>"`; omit `InputPath`,
`QueryPlansPath`, `RequestTimeoutSeconds`, `MaxRounds`, and `MaxVideos`.

`MaxRounds` and `MaxVideos` are positive integer strings passed only for `run`.
They define the per-session Collector constraints. A downstream orchestration
workflow may pass remaining budgets for one theme segment, but QueryPlan content
must never carry these values.

`QueryPlansPath` must point to QueryPlans 2.0. Its top-level `platform_scope` is the
frozen collection boundary, and every initial or supplemental expression must use
that complete scope. The runner and Collector must not authenticate or search a
platform outside it.

For monitoring and cancellation, invoke this same runner with `Operation="status"` or
`Operation="cancel"`, plus `Workspace` and `SessionId`. These operations remain synchronous and
relay the CLI JSON and exit code; do not call the CLI around the runner.

Do not add process-control statements around this call. The runner owns background execution,
terminal suppression, PID validation, log paths, and duplicate-executor rejection.

Every runner invocation writes exactly one versioned JSON object to stdout and keeps stderr
empty, including parameter and startup errors.

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
- `collector_path_invalid`: pass a non-empty executable path or omit the parameter to use the
  installed command.
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
