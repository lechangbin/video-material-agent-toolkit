"""Stable application use cases for creating and observing jobs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from semvideo.application.execution_config import FrozenExecutionConfig
from semvideo.application.launcher import launch_worker
from semvideo.application.locking import application_file_lock
from semvideo.application.progress import ProgressSink, stage_progress
from semvideo.application.source_store import register_source
from semvideo.application.subagent_handoff import require_approved_profile
from semvideo.application.task_store import TaskStore
from semvideo.application.workspace import WorkspacePaths
from semvideo.config import load_workspace_config
from semvideo.errors import (
    ErrorCategory,
    RecoveryAction,
    SemvideoError,
    config_error,
)

PROCESS_STAGES = (
    "probe",
    "segment",
    "evidence",
    "cinematography",
    "subagent",
    "windows",
    "analyze",
    "reconcile",
    "plan",
    "summarize",
    "retrieval",
    "render",
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _admission_snapshot(
    store: TaskStore,
    configured_limit: int,
    *,
    exempt_job_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    active_jobs: list[dict[str, Any]] = []
    for job_id in store.list_job_ids():
        snapshot = get_job_snapshot(store, job_id)
        if snapshot["state"] in TERMINAL_STATES | {"awaiting_subagent", "subagent_ready"} or job_id in exempt_job_ids:
            continue
        active_jobs.append(
            {
                "job_id": job_id,
                "state": snapshot["state"],
                "current_stage": snapshot.get("current_stage"),
                "attempt_id": snapshot.get("attempt_id"),
            }
        )
    active_jobs.sort(key=lambda row: str(row["job_id"]))
    active_count = len(active_jobs)
    return {
        "schema_version": 1,
        "configured_limit": configured_limit,
        "active_count": active_count,
        "available_submission_slots": max(
            0,
            configured_limit - active_count,
        ),
        "active_job_ids": [str(row["job_id"]) for row in active_jobs],
        "active_jobs": active_jobs,
    }


def _require_job_capacity(
    store: TaskStore,
    configured_limit: int,
    *,
    exempt_job_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    admission = _admission_snapshot(
        store,
        configured_limit,
        exempt_job_ids=exempt_job_ids,
    )
    if admission["available_submission_slots"] < 1:
        raise SemvideoError(
            code="job_admission_capacity_reached",
            category=ErrorCategory.RESOURCE_TRANSIENT,
            message="工作区当前活跃任务已达到配置的并发准入上限。",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            details={"admission": admission},
            exit_code=6,
        )
    return admission


def get_job_admission(workspace: WorkspacePaths) -> dict[str, Any]:
    """Return the application-owned, read-only job-admission snapshot."""

    config = load_workspace_config(workspace.data)
    store = TaskStore(workspace)
    with application_file_lock(workspace.locks / "submit.lock"):
        return _admission_snapshot(store, config.concurrency.media)


def get_job_snapshot(store: TaskStore, job_id: str) -> dict[str, Any]:
    """Return one cross-session job snapshot with verified Worker identity."""

    state = store.reconcile_interrupted(job_id)
    job = store.read_job(job_id)
    worker = store.read_worker(job_id)
    worker_snapshot = None
    if worker:
        worker_snapshot = {
            "status": "running" if store.worker_matches(job_id) else "stopped",
            "pid": worker.get("pid"),
            "started_at": worker.get("process_started_at"),
            "attempt_id": worker.get("attempt_id"),
        }
    return {
        **state,
        "source_video_id": job["source_video_id"],
        "profile": job["profile"],
        "created_at": job["created_at"],
        "cancel_requested": store.cancel_requested(job_id),
        "worker": worker_snapshot,
    }


def wait_for_job(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    progress_sink: ProgressSink | None = None,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Wait for a terminal snapshot while emitting state-derived progress."""

    store = TaskStore(workspace)
    last_marker: tuple[str, str | None] | None = None
    while True:
        snapshot = get_job_snapshot(store, job_id)
        state = str(snapshot["state"])
        stage = (
            str(snapshot["current_stage"]) if snapshot.get("current_stage") else state
        )
        marker = (state, snapshot.get("current_stage"))
        if progress_sink is not None and marker != last_marker:
            completed = (
                len(PROCESS_STAGES)
                if state == "completed"
                else PROCESS_STAGES.index(stage)
                if stage in PROCESS_STAGES
                else 0
            )
            progress_sink(
                stage_progress(
                    job_id=job_id,
                    stage=stage,
                    completed_units=completed,
                    total_units=len(PROCESS_STAGES),
                    message=f"任务状态：{state}",
                )
            )
        last_marker = marker
        if state in TERMINAL_STATES | {"awaiting_subagent", "subagent_ready"}:
            return snapshot
        time.sleep(poll_interval)


def submit_job(
    workspace: WorkspacePaths,
    input_video: Path,
    *,
    profile: str = "default",
    render: bool | None = None,
    idempotency_key: str | None = None,
    subagent_context_tokens: int | None = None,
    subagent_slots: int | None = None,
) -> dict[str, Any]:
    """Register a source, create or reuse a job, and launch its Worker."""

    config = load_workspace_config(workspace.data)
    frozen_config = FrozenExecutionConfig.freeze(config)
    if profile != config.profile:
        raise config_error(
            "profile_not_found",
            f"处理策略不存在：{profile}",
            profile=profile,
        )
    subagent_request: dict[str, Any] | None = None
    if subagent_context_tokens is not None:
        if subagent_slots is not None and subagent_slots < 1:
            raise config_error(
                "subagent_capability_unavailable",
                "Agent host reported no generic subagent capacity.",
            )
        tier, evidence_profile, profile_hash = require_approved_profile(
            workspace, subagent_context_tokens
        )
        subagent_request = {
            "backend": "crv_generic_subagent",
            "effective_context_tokens": subagent_context_tokens,
            "context_tier": tier.value,
            "subagent_slots": subagent_slots or 1,
            "evidence_profile": evidence_profile,
            "evidence_profile_hash": profile_hash,
        }
    source = register_source(workspace, input_video)
    store = TaskStore(workspace)
    request = {
        "source": {
            "kind": "local_file",
            "original_filename": source["original_filename"],
        },
        "overrides": ({"render_final_segments": render} if render is not None else {}),
    }
    if subagent_request is not None:
        request["subagent"] = subagent_request
    with application_file_lock(workspace.locks / "submit.lock"):
        existing = (
            store.find_by_idempotency_key(idempotency_key) if idempotency_key else None
        )
        reused = existing is not None
        if existing:
            if (
                existing["source_video_id"] != source["source_video_id"]
                or existing["profile"] != profile
                or existing["request"] != request
                or existing.get("execution_config_hash") != frozen_config.config_hash
            ):
                raise config_error(
                    "idempotency_key_conflict",
                    "同一 idempotency key 已用于不同的处理请求。",
                    idempotency_key=idempotency_key,
                    existing_job_id=existing["job_id"],
                )
            job = existing
        else:
            _require_job_capacity(
                store,
                config.concurrency.media,
            )
            job = store.create_job(
                source_video_id=str(source["source_video_id"]),
                profile=profile,
                request=request,
                idempotency_key=idempotency_key,
                **frozen_config.as_job_fields(),
            )
        if existing:
            state = store.reconcile_interrupted(str(job["job_id"]))
            if state["state"] == "created":
                _require_job_capacity(
                    store,
                    config.concurrency.media,
                    exempt_job_ids=frozenset({str(job["job_id"])}),
                )
                launched = launch_worker(workspace, store, str(job["job_id"]))
            else:
                launched = {
                    "state": state["state"],
                    "attempt_id": state.get("attempt_id"),
                }
        else:
            launched = launch_worker(workspace, store, str(job["job_id"]))
    return {
        "schema_version": 1,
        "job_id": job["job_id"],
        "source_video_id": job["source_video_id"],
        "state": launched["state"],
        "attempt_id": launched["attempt_id"],
        "workspace_root": str(workspace.root),
        "job_path": str(store.job_path(str(job["job_id"]))),
        "idempotent_reuse": reused,
    }


def cancel_job(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    store = TaskStore(workspace)
    snapshot = get_job_snapshot(store, job_id)
    if snapshot["state"] in {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }:
        raise config_error(
            "job_not_cancellable",
            f"任务当前状态不能取消：{snapshot['state']}",
            job_id=job_id,
            state=snapshot["state"],
        )
    if snapshot["state"] in {"awaiting_subagent", "subagent_ready"}:
        store.write_state(
            job_id,
            state="cancelled",
            current_stage="subagent",
            attempt_id=(
                str(snapshot["attempt_id"]) if snapshot.get("attempt_id") else None
            ),
            progress={"reason": reason},
            event_type="subagent_job_cancelled",
        )
        return get_job_snapshot(store, job_id)
    store.request_cancel(job_id, reason=reason)
    return get_job_snapshot(store, job_id)


def resume_job(workspace: WorkspacePaths, job_id: str) -> dict[str, Any]:
    config = load_workspace_config(workspace.data)
    store = TaskStore(workspace)
    with application_file_lock(workspace.locks / "submit.lock"):
        snapshot = store.reconcile_interrupted(job_id)
        if snapshot["state"] not in {"interrupted", "failed", "created", "subagent_ready"}:
            raise config_error(
                "job_not_resumable",
                f"任务当前状态不能恢复：{snapshot['state']}",
                job_id=job_id,
                state=snapshot["state"],
            )
        _require_job_capacity(
            store,
            config.concurrency.media,
            exempt_job_ids=frozenset({job_id}),
        )
        return launch_worker(
            workspace,
            store,
            job_id,
            prepare=lambda: store.clear_cancel_request(job_id),
        )


def retry_job(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    from_stage: str | None = None,
) -> dict[str, Any]:
    config = load_workspace_config(workspace.data)
    store = TaskStore(workspace)
    with application_file_lock(workspace.locks / "submit.lock"):
        snapshot = store.reconcile_interrupted(job_id)
        if store.worker_matches(job_id) and snapshot["state"] not in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise config_error(
                "job_worker_already_running",
                f"运行中的任务不能重试：{job_id}",
                job_id=job_id,
                state=snapshot["state"],
            )
        if from_stage is not None and from_stage not in PROCESS_STAGES:
            raise config_error(
                "retry_stage_invalid",
                f"未知阶段：{from_stage}",
                stage=from_stage,
                allowed=list(PROCESS_STAGES),
            )
        _require_job_capacity(
            store,
            config.concurrency.media,
            exempt_job_ids=frozenset({job_id}),
        )

        def prepare() -> None:
            if from_stage is not None:
                start = PROCESS_STAGES.index(from_stage)
                for stage in PROCESS_STAGES[start:]:
                    store.invalidate_checkpoint(job_id, stage)
            store.clear_cancel_request(job_id)

        return launch_worker(
            workspace,
            store,
            job_id,
            prepare=prepare,
        )
