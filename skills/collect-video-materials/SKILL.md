---
name: collect-video-materials
description: Safely orchestrate the local material-collector CLI for video-material discovery, authentication, resumable search, download, status monitoring, cancellation, and Agent decision checkpoints. Use when an Agent needs to start or resume a collection session from versioned input and QueryPlan JSON without inventing shell process-control commands.
---

# Collect Video Materials

Use the deterministic CLI while preserving one executor per session. Never author a replacement
PowerShell launcher.

## Invoke

1. Read [references/cli-execution-contract.md](references/cli-execution-contract.md).
2. Validate that the workspace and versioned JSON inputs are explicit.
3. Invoke `scripts/invoke-collector.ps1` for every `run`, `resume`, `status`, or `cancel`.
4. Pass values only through the script parameters. Do not edit, copy, inline, or reimplement the
   script.
5. For a successfully started `run` or `resume`, persist the returned `control_path`,
   `process_id`, `collector_process_id`, `session_id`, `stdout_path`, and `stderr_path` in the
   current Agent task.
   `status` and `cancel` instead relay the CLI result and do not create an executor record.

Do not use `Start-Process`, PowerShell Jobs, scheduled tasks, Bash continuations, or a second
terminal command to replace the bundled runner.

## Monitor

- Treat `status=started` as process creation, not collection completion.
- While the recorded wrapper `process_id` is alive, read only its recorded stderr and invoke
  `Operation=status` through the runner. Treat `collector_process_id` as the actual CLI child
  identity, not as the wrapper-lifetime monitor. Do not launch another `run` or `resume`.
- Allow another `run` in the same material workspace; it creates a distinct session. Never start
  a second `resume` for one session.
- Treat `authentication_login_waiting` as a human action. Tell the user which platform needs
  login and ask them to check Chrome in the taskbar. Keep the executor alive.
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
- `action_required.actor=human`: report the requested human action and wait.
- `action_required.actor=agent`: read the referenced request artifact, create the versioned
  decision artifact, and resume through the bundled runner.
- `status=integration_required`: stop at the declared external integration boundary.
- `status=cancelled|completed`: stop; do not resume a terminal session.

Use `select-video-segments` only for the isolated Top-K decision requested after external video
understanding. Do not move collection execution into that Skill.
