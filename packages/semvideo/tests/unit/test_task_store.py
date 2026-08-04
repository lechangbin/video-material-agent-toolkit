from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from semvideo.application.task_store import (
    JobAlreadyExistsError,
    JobNotFoundError,
    StaleAttemptError,
    TaskStore,
    UnsupportedTaskSchemaError,
)
from semvideo.application.workspace import initialize_workspace
from semvideo.infrastructure.process_identity import current_process_identity


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(initialize_workspace(tmp_path))


def test_create_job_builds_self_describing_package(store: TaskStore) -> None:
    job = store.create_job(
        job_id="job_001",
        source_video_id="video_001",
        profile="default",
        request={"render_final_segments": False},
    )

    job_root = store.job_path("job_001")
    assert job["job_id"] == "job_001"
    assert store.read_state("job_001")["state"] == "created"
    assert (job_root / "logs").is_dir()
    assert (job_root / "stages").is_dir()
    assert (job_root / "retrieval").is_dir()
    events = list(store.iter_events("job_001"))
    assert [event["type"] for event in events] == ["job_created"]
    assert all(line.endswith("\n") for line in (job_root / "events.jsonl").read_text().splitlines(keepends=True))


def test_duplicate_job_and_unsafe_ids_are_rejected(store: TaskStore) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")

    with pytest.raises(JobAlreadyExistsError):
        store.create_job(job_id="job_001", source_video_id="video_001")
    with pytest.raises(ValueError):
        store.create_job(job_id="../outside", source_video_id="video_001")
    with pytest.raises(JobNotFoundError):
        store.read_job("job_missing")


def test_state_replace_event_checkpoint_and_cancel_request(store: TaskStore) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")

    state = store.write_state(
        "job_001",
        state="probing",
        current_stage="probe",
        attempt_id="attempt_001",
        progress={"completed_units": 0, "total_units": 1, "unit": "source"},
    )
    checkpoint = store.write_checkpoint(
        "job_001",
        "probe",
        {
            "status": "succeeded",
            "implementation_version": "1",
            "config_hash": "sha256:config",
            "input_hashes": ["sha256:input"],
            "outputs": [],
            "completed_at": "2026-07-28T00:00:00Z",
        },
    )
    cancel = store.request_cancel("job_001", reason="test cancellation")

    assert state["state"] == "probing"
    assert checkpoint["schema_version"] == 1
    assert store.read_checkpoint("job_001", "probe") == checkpoint
    assert store.cancel_requested("job_001")
    assert store.read_cancel_request("job_001") == cancel
    store.clear_cancel_request("job_001")
    assert not store.cancel_requested("job_001")
    assert [event["type"] for event in store.iter_events("job_001")] == [
        "job_created",
        "state_changed",
        "checkpoint_written",
        "cancel_requested",
    ]
    assert not list(store.job_path("job_001").glob(".state.json.*.tmp"))


def test_worker_identity_uses_pid_and_creation_time(store: TaskStore) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    pid, started_at = current_process_identity()
    store.write_worker(
        "job_001",
        attempt_id="attempt_001",
        pid=pid,
        process_started_at=started_at,
        log_path="logs/attempt_001.log",
    )

    assert store.worker_matches("job_001")

    worker_path = store.job_path("job_001") / "worker.json"
    worker = json.loads(worker_path.read_text(encoding="utf-8"))
    worker["process_started_at"] = "2000-01-01T00:00:00.000000Z"
    worker_path.write_text(json.dumps(worker), encoding="utf-8")
    assert not store.worker_matches("job_001")


def test_stale_running_worker_is_reconciled_to_interrupted(
    store: TaskStore,
) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state(
        "job_001",
        state="analyzing",
        current_stage="analyze",
        attempt_id="attempt_001",
    )
    store.write_worker(
        "job_001",
        attempt_id="attempt_001",
        pid=os.getpid(),
        process_started_at="2000-01-01T00:00:00.000000Z",
        log_path="logs/attempt_001.log",
    )

    state = store.reconcile_interrupted("job_001")

    assert state["state"] == "interrupted"
    assert state["failure"]["category"] == "interrupted"
    assert state["failure"]["retryable"] is True
    assert state["failure"]["recovery"] == "resume"


def test_invalidate_checkpoint_preserves_auditable_record(store: TaskStore) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_checkpoint("job_001", "probe", {"status": "succeeded"})

    invalidated = store.invalidate_checkpoint("job_001", "probe")

    assert invalidated is not None
    assert invalidated["status"] == "invalidated"
    assert "invalidated_at" in invalidated


def test_find_job_by_idempotency_key(store: TaskStore) -> None:
    created = store.create_job(
        job_id="job_001",
        source_video_id="video_001",
        idempotency_key="request-001",
    )

    assert store.find_by_idempotency_key("request-001") == created
    assert store.find_by_idempotency_key("request-missing") is None


def test_stale_attempt_cannot_replace_newer_state(store: TaskStore) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    current = store.write_state(
        "job_001",
        state="queued",
        attempt_id="attempt_new",
    )

    with pytest.raises(StaleAttemptError):
        store.write_state(
            "job_001",
            state="failed",
            attempt_id="attempt_old",
            expected_attempt_id="attempt_old",
        )

    assert store.read_state("job_001") == current


def test_task_store_rejects_future_schema_on_every_read_interface(
    store: TaskStore,
) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    pid, started_at = current_process_identity()
    store.write_worker(
        "job_001",
        attempt_id="attempt_001",
        pid=pid,
        process_started_at=started_at,
        log_path="logs/attempt_001.log",
    )
    store.write_checkpoint("job_001", "probe", {"status": "succeeded"})
    store.request_cancel("job_001")
    job_root = store.job_path("job_001")

    readers = [
        (job_root / "job.json", lambda: store.read_job("job_001")),
        (job_root / "state.json", lambda: store.read_state("job_001")),
        (job_root / "worker.json", lambda: store.read_worker("job_001")),
        (
            job_root / "stages" / "probe" / "checkpoint.json",
            lambda: store.read_checkpoint("job_001", "probe"),
        ),
        (
            job_root / "cancel.request",
            lambda: store.read_cancel_request("job_001"),
        ),
    ]
    for path, reader in readers:
        original = path.read_bytes()
        value = json.loads(original)
        value["schema_version"] = 999
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(UnsupportedTaskSchemaError):
            reader()
        path.write_bytes(original)

    events_path = job_root / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    events[0]["schema_version"] = 999
    events_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    with pytest.raises(UnsupportedTaskSchemaError):
        list(store.iter_events("job_001"))


def test_task_store_rejects_cancel_request_for_terminal_job(
    store: TaskStore,
) -> None:
    store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state("job_001", state="completed")

    with pytest.raises(ValueError, match="terminal"):
        store.request_cancel("job_001")

    assert not store.cancel_requested("job_001")
