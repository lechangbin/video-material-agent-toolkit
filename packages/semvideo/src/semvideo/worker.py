"""Internal detached Worker entry point."""

from __future__ import annotations

import argparse
import json
import traceback

from semvideo.application.task_store import StaleAttemptError, TaskStore
from semvideo.application.workspace import discover_workspace
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semvideo-worker")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    workspace = discover_workspace(explicit=args.workspace)
    store = TaskStore(workspace)
    try:
        from semvideo.application.processor import run_job

        result = run_job(
            workspace,
            store,
            args.job_id,
            args.attempt_id,
            progress_sink=lambda event: print(
                json.dumps(event, ensure_ascii=False),
                flush=True,
            ),
        )
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except StaleAttemptError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "code": "stale_worker_attempt",
                    "category": "interrupted",
                    "message": str(exc),
                    "retryable": False,
                    "recovery": "none",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise SystemExit(9)
    except SemvideoError as exc:
        exc.with_context(job_id=args.job_id, attempt_id=args.attempt_id)
        terminal_state = (
            "interrupted"
            if exc.payload.category == ErrorCategory.INTERRUPTED
            else "cancelled"
            if exc.payload.category == ErrorCategory.CANCELLED
            else "failed"
        )
        try:
            store.write_state(
                args.job_id,
                state=terminal_state,
                current_stage=exc.payload.stage,
                attempt_id=args.attempt_id,
                failure=exc.as_dict(),
                event_type=(
                    "job_cancelled"
                    if terminal_state == "cancelled"
                    else "job_interrupted"
                    if terminal_state == "interrupted"
                    else "job_failed"
                ),
                expected_attempt_id=args.attempt_id,
            )
        except StaleAttemptError:
            raise SystemExit(9)
        print(json.dumps(exc.as_dict(), ensure_ascii=False), flush=True)
        raise SystemExit(exc.exit_code)
    except BaseException as exc:
        payload = SemvideoError(
            code="unhandled_worker_exception",
            category=ErrorCategory.INTERNAL,
            message="Worker 发生未处理的程序错误。",
            retryable=False,
            recovery=RecoveryAction.REPORT_BUG,
            job_id=args.job_id,
            attempt_id=args.attempt_id,
            details={"exception_type": type(exc).__name__, "reason": str(exc)},
            exit_code=10,
        )
        try:
            store.write_state(
                args.job_id,
                state="failed",
                attempt_id=args.attempt_id,
                failure=payload.as_dict(),
                event_type="job_failed",
                expected_attempt_id=args.attempt_id,
            )
        except StaleAttemptError:
            raise SystemExit(9)
        traceback.print_exc()
        print(json.dumps(payload.as_dict(), ensure_ascii=False), flush=True)
        raise SystemExit(10)


if __name__ == "__main__":
    main()
