---
name: collect-video-materials
description: Safely orchestrate the local material-collector CLI for video-material discovery, authentication, resumable search, download, status monitoring, cancellation, and Agent decision checkpoints. Use when an Agent needs to start or resume a collection session from versioned input and QueryPlan JSON without inventing shell process-control commands.
---

# Collect Video Materials

Use the deterministic CLI while preserving one executor per session. Never author a replacement
PowerShell launcher.

## Invoke

1. Resolve the compatible CLI exactly once with
   `python <skill-root>/scripts/resolve_material_collector.py` (use `py -3.14` instead of
   `python` on Windows when that is the configured Python launcher). Save the successful JSON
   `command` value as the absolute Collector command. Do not scan PATH, source, executables, or
   installation directories yourself. Stop on `material_collector_cli_not_found` or
   `material_collector_cli_incompatible` and report its structured recovery object.
2. Read [references/input-contracts.md](references/input-contracts.md), then read
   [references/cli-execution-contract.md](references/cli-execution-contract.md). These references
   expose the complete request shapes and commands; do not discover either contract with `--help`,
   `contracts normalize`, trial JSON, source inspection, or PATH probing.
3. Validate that the workspace and versioned JSON inputs are explicit. QueryPlans must use
   schema 2.0, declare one non-empty `platform_scope`, and copy that complete scope into every
   expression's `target_platforms`.
4. On Windows, invoke `scripts/invoke-collector.ps1` for every `run`, `resume`, `status`, or
   `cancel` and pass the resolved command through `CollectorPath`. On non-Windows hosts, invoke the
   resolved absolute command with `executor invoke` and the matching documented options;
   PowerShell is not required.
5. Pass values only through the documented adapter or executor parameters. Do not edit, copy,
   inline, or reimplement either interface.
6. For a successfully started `run` or `resume`, persist the returned `control_path`,
   `process_id`, `collector_process_id`, `session_id`, `stdout_path`, and `stderr_path` in the
   current Agent task.
   `status` and `cancel` instead relay the CLI result and do not create an executor record.

The collector-owned executor, not the Skill script, owns locks, process creation, handshakes,
logs, and recovery checks. Do not use `Start-Process`, PowerShell Jobs, scheduled tasks, Bash
continuations, or a second terminal command to replace it.

## Monitor

- Treat `status=started` as process creation, not collection completion.
- While the recorded wrapper `process_id` is alive, read only its recorded stderr and invoke
  `Operation=status` through the runner. Treat `collector_process_id` as the actual CLI child
  identity, not as the wrapper-lifetime monitor. Do not launch another `run` or `resume`.
- Allow another `run` in the same material workspace; it creates a distinct session. Never start
  a second `resume` for one session.
- Treat `authentication_login_waiting` as a human action. Tell the user which platform needs
  login and which frozen browser channel was selected, then ask them to check that Edge or Chrome
  window in the taskbar. Keep the executor alive.
- Treat an unchanged log as normal during human login. Never use log silence as proof of death.
- Treat `search_plan_started`, `search_plan_committed`, and `search_plan_settled` as progress
  only, never as the final session result. `search_plan_committed` means every request in that
  QueryPlan was committed. `search_plan_settled` with `status=completed_with_issues` means at
  least one request failed while other committed sources may continue through resolve and
  low-proxy download. Preserve `requested_requests`, `completed_requests`, `failed_requests`,
  and `not_attempted_requests`, then keep monitoring the same executor.
- When the wrapper `process_id` disappears, read final stdout and stderr, then invoke
  `Operation=status` through the runner before deciding the next action.

## Recover

- If `runtime.state=executing` or `cancelling`, wait for that executor or its explicit lease
  deadline.
- If `runtime.state=cancel_pending_recovery`, invoke one `resume` through the bundled runner so
  the workflow can acknowledge cancellation.
- Invoke `Operation=cancel` through the runner only once. Wait for `cancelled`; do not
  immediately issue `resume`.
- Never edit `session.sqlite3`, delete an execution lease, delete an auth lock, or remove a browser
  profile to recover.

## Continue checkpoints

Use the final JSON as the authority:

- A non-empty `issues` array can accompany successful collection progress. Do not issue
  `resume` merely because one platform has a retryable search issue when the final status has
  already advanced to a checkpoint.
- `workflow_retryable` plus `runtime.state=idle` and `runtime.next_stage=search` means no search
  batch was available to advance; one runner-managed `resume` may retry the still-open stage.
- `action_required.actor=human` with `type=manual_review_required`: report the requested review,
  wait for the human, then use only the documented `review list`, `review approve`, or
  `review reject` commands before resuming. Never approve or reject on the human's behalf.
- Any other `action_required.actor=human`: report the exact action and wait; do not invent a
  decision file or alternate login command.
- `action_required.actor=agent`: follow only the named versioned artifact contract. If no such
  contract is linked in the result or this Skill, stop with a contract error instead of guessing.
- `status=integration_required`: stop at the declared external integration boundary.
- `status=cancelled|completed`: stop; do not resume a terminal session.

When presenting downloaded material, use each primary asset's `display_relative_path` as the
human-readable title entry when it is non-null. Keep `relative_path` as the authoritative
content-addressed asset for integrity checks and downstream machine processing. A fallback source
intentionally has `display_relative_path: null`; do not invent or rename another title entry.
`named_view_publish_failed` is retryable: resume the same session through the bundled runner and do
not start a replacement download.

Use `select-video-segments` only for the isolated Top-K decision requested after external video
understanding. Do not move collection execution into that Skill.
