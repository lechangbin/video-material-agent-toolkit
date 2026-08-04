"""Launch one detached, no-console Worker per processing attempt."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from semvideo.application.locking import application_file_lock
from semvideo.application.task_store import TaskStore, new_opaque_id
from semvideo.application.workspace import WorkspacePaths
from semvideo.errors import config_error
from semvideo.infrastructure.process_identity import process_started_at


def launch_worker(
    workspace: WorkspacePaths,
    store: TaskStore,
    job_id: str,
    *,
    prepare: Callable[[], None] | None = None,
) -> dict[str, Any]:
    launch_lock = workspace.locks / f"launch-{job_id}.lock"
    with application_file_lock(launch_lock):
        current = store.reconcile_interrupted(job_id)
        if store.worker_matches(job_id) and current["state"] not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise config_error(
                "job_worker_already_running",
                f"任务已有仍在运行的 Worker：{job_id}",
                job_id=job_id,
                state=current["state"],
            )
        if prepare is not None:
            prepare()
        attempt_id = new_opaque_id("attempt")
        job_root = store.job_path(job_id)
        relative_log = Path("logs") / f"{attempt_id}.log"
        log_path = job_root / relative_log
        command = [
            sys.executable,
            "-m",
            "semvideo.worker",
            "--workspace",
            str(workspace.root),
            "--job-id",
            job_id,
            "--attempt-id",
            attempt_id,
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        state = store.write_state(
            job_id,
            state="queued",
            current_stage=None,
            attempt_id=attempt_id,
            event_type="job_queued",
        )
        log_handle = log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=str(workspace.root),
                env=os.environ.copy(),
                close_fds=True,
                creationflags=creationflags,
            )
        finally:
            log_handle.close()
        started_at = process_started_at(process.pid)
        worker = store.write_worker(
            job_id,
            attempt_id=attempt_id,
            pid=process.pid,
            process_started_at=started_at,
            log_path=relative_log.as_posix(),
        )
        return {
            "schema_version": 1,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "worker": worker,
            "state": state["state"],
        }
