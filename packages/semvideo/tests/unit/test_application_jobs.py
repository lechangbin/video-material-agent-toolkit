from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from semvideo.application import jobs
from semvideo.application.jobs import (
    cancel_job,
    get_job_admission,
    resume_job,
    retry_job,
    submit_job,
)
from semvideo.application.source_store import register_source
from semvideo.application.task_store import TaskStore
from semvideo.application.workspace import initialize_workspace
from semvideo.errors import SemvideoError
from semvideo.infrastructure.process_identity import current_process_identity


def test_concurrent_idempotent_submit_creates_and_launches_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"same-source")
    launch_count = 0

    def fake_launch(workspace, store, job_id, **kwargs):
        nonlocal launch_count
        launch_count += 1
        store.write_state(
            job_id,
            state="queued",
            attempt_id="attempt_test",
        )
        return {
            "state": "queued",
            "attempt_id": "attempt_test",
        }

    monkeypatch.setattr(jobs, "launch_worker", fake_launch)

    def submit():
        return submit_job(
            workspace,
            source,
            idempotency_key="request-1",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    assert results[0]["job_id"] == results[1]["job_id"]
    assert {row["idempotent_reuse"] for row in results} == {False, True}
    assert launch_count == 1
    assert len(TaskStore(workspace).list_job_ids()) == 1


def test_idempotent_created_job_cannot_bypass_full_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    workspace.config.write_text(
        workspace.config.read_text(encoding="utf-8").replace(
            "media = 2",
            "media = 1",
        ),
        encoding="utf-8",
    )
    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"source")
    source = register_source(workspace, source_path)
    store = TaskStore(workspace)
    store.create_job(
        job_id="job_existing",
        source_video_id=str(source["source_video_id"]),
        request={
            "source": {
                "kind": "local_file",
                "original_filename": source_path.name,
            },
            "overrides": {},
        },
        idempotency_key="request-existing",
    )
    store.create_job(
        job_id="job_other",
        source_video_id="video_other",
    )
    monkeypatch.setattr(
        jobs,
        "launch_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full admission must prevent launch")
        ),
    )

    with pytest.raises(SemvideoError) as raised:
        submit_job(
            workspace,
            source_path,
            idempotency_key="request-existing",
        )

    assert raised.value.payload.code == "job_admission_capacity_reached"
    assert raised.value.payload.details["admission"]["active_job_ids"] == [
        "job_other"
    ]


def test_concurrent_unique_submissions_atomically_respect_capacity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    launch_count = 0

    def fake_launch(workspace, store, job_id, **kwargs):
        nonlocal launch_count
        launch_count += 1
        pid, started_at = current_process_identity()
        store.write_state(
            job_id,
            state="queued",
            attempt_id=f"attempt_{launch_count}",
        )
        store.write_worker(
            job_id,
            attempt_id=f"attempt_{launch_count}",
            pid=pid,
            process_started_at=started_at,
            log_path=f"logs/attempt_{launch_count}.log",
        )
        return {
            "state": "queued",
            "attempt_id": f"attempt_{launch_count}",
        }

    monkeypatch.setattr(jobs, "launch_worker", fake_launch)
    sources = []
    for index in range(4):
        source = tmp_path / f"source-{index}.mp4"
        source.write_bytes(f"source-{index}".encode())
        sources.append(source)

    def submit(source: Path):
        try:
            return submit_job(workspace, source)
        except SemvideoError as error:
            return error

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(submit, sources))

    accepted = [row for row in results if isinstance(row, dict)]
    rejected = [row for row in results if isinstance(row, SemvideoError)]
    assert len(accepted) == 2
    assert len(rejected) == 2
    assert {
        error.payload.code for error in rejected
    } == {"job_admission_capacity_reached"}
    assert launch_count == 2
    assert len(TaskStore(workspace).list_job_ids()) == 2
    admission = get_job_admission(workspace)
    assert admission["configured_limit"] == 2
    assert admission["active_count"] == 2
    assert admission["available_submission_slots"] == 0
    assert admission["active_job_ids"] == [
        row["job_id"] for row in admission["active_jobs"]
    ]


@pytest.mark.parametrize("operation", ["resume", "retry"])
def test_recovery_atomically_rejects_when_capacity_is_full(
    tmp_path: Path,
    monkeypatch,
    operation: str,
) -> None:
    workspace = initialize_workspace(tmp_path / operation)
    workspace.config.write_text(
        workspace.config.read_text(encoding="utf-8").replace(
            "media = 2",
            "media = 1",
        ),
        encoding="utf-8",
    )
    store = TaskStore(workspace)
    store.create_job(job_id="job_active", source_video_id="video_active")
    target = store.create_job(
        job_id="job_target",
        source_video_id="video_target",
    )
    store.write_state(target["job_id"], state="failed")
    monkeypatch.setattr(
        jobs,
        "launch_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("capacity check must happen before launch")
        ),
    )

    with pytest.raises(SemvideoError) as raised:
        if operation == "resume":
            resume_job(workspace, target["job_id"])
        else:
            retry_job(workspace, target["job_id"])

    assert raised.value.payload.code == "job_admission_capacity_reached"


def test_retry_running_job_has_no_checkpoint_or_cancel_side_effect(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    pid, started_at = current_process_identity()
    store.write_state(
        job["job_id"],
        state="analyzing",
        current_stage="analyze",
        attempt_id="attempt_001",
    )
    store.write_worker(
        job["job_id"],
        attempt_id="attempt_001",
        pid=pid,
        process_started_at=started_at,
        log_path="logs/attempt_001.log",
    )
    checkpoint = store.write_checkpoint(
        job["job_id"],
        "analyze",
        {"status": "succeeded"},
    )
    cancel = store.request_cancel(job["job_id"], reason="keep-me")

    with pytest.raises(SemvideoError) as raised:
        retry_job(workspace, job["job_id"], from_stage="analyze")

    assert raised.value.payload.code == "job_worker_already_running"
    assert store.read_checkpoint(job["job_id"], "analyze") == checkpoint
    assert store.read_cancel_request(job["job_id"]) == cancel


def test_cancel_job_returns_snapshot_with_pending_request(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state(
        job["job_id"],
        state="analyzing",
        current_stage="analyze",
        attempt_id="attempt_001",
    )
    pid, started_at = current_process_identity()
    store.write_worker(
        job["job_id"],
        attempt_id="attempt_001",
        pid=pid,
        process_started_at=started_at,
        log_path="logs/attempt_001.log",
    )

    snapshot = cancel_job(workspace, job["job_id"], reason="stop")

    assert snapshot["job_id"] == job["job_id"]
    assert snapshot["state"] == "analyzing"
    assert snapshot["cancel_requested"] is True
    assert store.read_cancel_request(job["job_id"])["reason"] == "stop"


@pytest.mark.parametrize("terminal_state", ["completed", "failed"])
def test_cancel_job_rejects_terminal_task(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    workspace = initialize_workspace(tmp_path / terminal_state)
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state(job["job_id"], state=terminal_state)

    with pytest.raises(SemvideoError) as raised:
        cancel_job(workspace, job["job_id"])

    assert raised.value.payload.code == "job_not_cancellable"
    assert not store.cancel_requested(job["job_id"])
