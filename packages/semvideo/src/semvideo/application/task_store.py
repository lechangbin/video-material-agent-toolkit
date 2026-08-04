"""File-backed facts for processing jobs and processing attempts."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from semvideo.application.workspace import WorkspacePaths
from semvideo.infrastructure.io import append_json_line, atomic_write_json, read_json
from semvideo.infrastructure.process_identity import process_matches

TASK_SCHEMA_VERSION: Final = 1
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNNING_STATES = frozenset(
    {
        "queued",
        "probing",
        "segmenting",
        "extracting_evidence",
        "analyzing_cinematography",
        "building_windows",
        "analyzing",
        "reconciling",
        "planning",
        "summarizing",
        "rendering",
    }
)
_KNOWN_STATES = _RUNNING_STATES | frozenset(
    {"created", "completed", "failed", "cancelled", "interrupted"}
)
_TERMINAL_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
_JOB_DIRECTORIES = (
    "logs",
    "stages",
    "media",
    "segmentation",
    "evidence/frames",
    "evidence/contact-sheets",
    "windows",
    "semantics",
    "plans",
    "summaries",
    "retrieval",
    "renders",
    "reports",
    "model-runs",
)


class TaskStoreError(RuntimeError):
    """Base class for task-store errors."""


class JobNotFoundError(TaskStoreError):
    """Raised when a job ID does not name an existing task package."""


class JobAlreadyExistsError(TaskStoreError):
    """Raised when creating a duplicate job ID."""


class StaleAttemptError(TaskStoreError):
    """Raised when an old Worker tries to mutate a newer attempt."""


class UnsupportedTaskSchemaError(TaskStoreError):
    """Raised when persisted task data requires an unsupported Schema."""


def _validate_schema(value: dict[str, Any], path: Path) -> dict[str, Any]:
    version = value.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or version > TASK_SCHEMA_VERSION
    ):
        raise UnsupportedTaskSchemaError(
            f"unsupported schema_version {version!r} in {path}; "
            f"supported range is 1..{TASK_SCHEMA_VERSION}"
        )
    return value


def _read_task_json(path: Path) -> dict[str, Any]:
    return _validate_schema(read_json(path), path)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_opaque_id(prefix: str) -> str:
    _validate_component(prefix, "ID prefix")
    return f"{prefix}_{secrets.token_hex(12)}"


def _validate_component(value: str, label: str) -> str:
    if value in {".", ".."} or not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class TaskStore:
    """Read and atomically update self-describing local job packages."""

    def __init__(self, workspace: WorkspacePaths) -> None:
        self.workspace = workspace

    def job_path(self, job_id: str) -> Path:
        return self.workspace.jobs / _validate_component(job_id, "job ID")

    def _require_job(self, job_id: str) -> Path:
        path = self.job_path(job_id)
        if not (path / "job.json").is_file():
            raise JobNotFoundError(job_id)
        return path

    def create_job(
        self,
        *,
        source_video_id: str,
        profile: str = "default",
        job_id: str | None = None,
        request: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a task package in the ``created`` state."""

        _validate_component(source_video_id, "source video ID")
        if not profile:
            raise ValueError("profile must not be empty")
        job_id = job_id or new_opaque_id("job")
        path = self.job_path(job_id)
        if path.exists():
            raise JobAlreadyExistsError(job_id)

        path.mkdir(parents=True)
        for relative in _JOB_DIRECTORIES:
            (path / relative).mkdir(parents=True, exist_ok=True)

        created_at = utc_now()
        job = {
            "schema_version": TASK_SCHEMA_VERSION,
            "job_id": job_id,
            "source_video_id": source_video_id,
            "profile": profile,
            "idempotency_key": idempotency_key,
            "request": dict(request or {}),
            "created_at": created_at,
        }
        state = {
            "schema_version": TASK_SCHEMA_VERSION,
            "job_id": job_id,
            "state": "created",
            "current_stage": None,
            "attempt_id": None,
            "progress": None,
            "updated_at": created_at,
            "failure": None,
        }
        atomic_write_json(path / "job.json", job)
        atomic_write_json(path / "state.json", state)
        self.append_event(
            job_id,
            "job_created",
            {"source_video_id": source_video_id, "profile": profile},
            occurred_at=created_at,
        )
        return job

    def list_job_ids(self) -> list[str]:
        if not self.workspace.jobs.exists():
            return []
        return sorted(
            path.name
            for path in self.workspace.jobs.iterdir()
            if path.is_dir() and (path / "job.json").is_file()
        )

    def read_job(self, job_id: str) -> dict[str, Any]:
        return _read_task_json(self._require_job(job_id) / "job.json")

    def find_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Return the one job registered for a non-empty idempotency key."""

        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        matches: list[dict[str, Any]] = []
        for job_id in self.list_job_ids():
            job = self.read_job(job_id)
            if job.get("idempotency_key") == idempotency_key:
                matches.append(job)
        if len(matches) > 1:
            raise TaskStoreError(
                f"duplicate idempotency key in task store: {idempotency_key}"
            )
        return matches[0] if matches else None

    def read_state(self, job_id: str) -> dict[str, Any]:
        return _read_task_json(self._require_job(job_id) / "state.json")

    def write_state(
        self,
        job_id: str,
        *,
        state: str,
        current_stage: str | None = None,
        attempt_id: str | None = None,
        progress: Mapping[str, Any] | None = None,
        failure: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        event_type: str = "state_changed",
        expected_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically replace ``state.json`` and append its audit event."""

        path = self._require_job(job_id)
        if not state:
            raise ValueError("state must not be empty")
        if state not in _KNOWN_STATES:
            raise ValueError(f"unknown job state: {state}")
        if expected_attempt_id is not None:
            _validate_component(expected_attempt_id, "expected attempt ID")
            current = _read_task_json(path / "state.json")
            current_attempt = current.get("attempt_id")
            if current_attempt != expected_attempt_id:
                raise StaleAttemptError(
                    f"attempt {expected_attempt_id} cannot replace "
                    f"current attempt {current_attempt}"
                )
        if current_stage is not None:
            _validate_component(current_stage, "stage")
        if attempt_id is not None:
            _validate_component(attempt_id, "attempt ID")
        now = utc_now()
        snapshot: dict[str, Any] = {
            "schema_version": TASK_SCHEMA_VERSION,
            "job_id": job_id,
            "state": state,
            "current_stage": current_stage,
            "attempt_id": attempt_id,
            "progress": dict(progress) if progress is not None else None,
            "updated_at": now,
            "failure": dict(failure) if failure is not None else None,
        }
        if extra:
            reserved = set(snapshot)
            overlap = reserved.intersection(extra)
            if overlap:
                raise ValueError(f"extra state fields overlap reserved fields: {overlap}")
            snapshot.update(extra)
        atomic_write_json(path / "state.json", snapshot)
        self.append_event(
            job_id,
            event_type,
            {
                "state": state,
                "current_stage": current_stage,
                "attempt_id": attempt_id,
            },
            occurred_at=now,
        )
        return snapshot

    def append_event(
        self,
        job_id: str,
        event_type: str,
        data: Mapping[str, Any] | None = None,
        *,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        path = self._require_job(job_id)
        _validate_component(event_type, "event type")
        event = {
            "schema_version": TASK_SCHEMA_VERSION,
            "event_id": new_opaque_id("event"),
            "job_id": job_id,
            "type": event_type,
            "occurred_at": occurred_at or utc_now(),
            "data": dict(data or {}),
        }
        append_json_line(path / "events.jsonl", event)
        return event

    def iter_events(self, job_id: str) -> Iterator[dict[str, Any]]:
        events_path = self._require_job(job_id) / "events.jsonl"
        if not events_path.exists():
            return
        import json

        with events_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.endswith("\n"):
                    raise TaskStoreError(
                        f"incomplete event line {line_number} in {events_path}"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TaskStoreError(
                        f"event line {line_number} is not an object in {events_path}"
                    )
                yield _validate_schema(value, events_path)

    def write_worker(
        self,
        job_id: str,
        *,
        attempt_id: str,
        pid: int,
        process_started_at: str,
        log_path: str,
        command_version: int = 1,
    ) -> dict[str, Any]:
        path = self._require_job(job_id)
        _validate_component(attempt_id, "attempt ID")
        if pid <= 0:
            raise ValueError("pid must be positive")
        relative_log_path = Path(log_path)
        if relative_log_path.is_absolute() or ".." in relative_log_path.parts:
            raise ValueError("log_path must be relative to the job package")
        worker = {
            "schema_version": TASK_SCHEMA_VERSION,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "pid": pid,
            "process_started_at": process_started_at,
            "command_version": command_version,
            "log_path": relative_log_path.as_posix(),
        }
        atomic_write_json(path / "worker.json", worker)
        self.append_event(
            job_id,
            "worker_started",
            {
                "attempt_id": attempt_id,
                "pid": pid,
                "process_started_at": process_started_at,
            },
        )
        return worker

    def read_worker(self, job_id: str) -> dict[str, Any] | None:
        worker_path = self._require_job(job_id) / "worker.json"
        return _read_task_json(worker_path) if worker_path.is_file() else None

    def worker_matches(self, job_id: str) -> bool:
        worker = self.read_worker(job_id)
        if worker is None:
            return False
        try:
            return process_matches(
                int(worker["pid"]),
                str(worker["process_started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return False

    def reconcile_interrupted(self, job_id: str) -> dict[str, Any]:
        """Mark a stale running snapshot interrupted after PID identity check."""

        state = self.read_state(job_id)
        if state.get("state") not in _RUNNING_STATES or self.worker_matches(job_id):
            return state
        failure = {
            "code": "worker_process_missing",
            "category": "interrupted",
            "retryable": True,
            "recovery": "resume",
            "message": "worker PID is absent or its creation time no longer matches",
            "details": {},
        }
        return self.write_state(
            job_id,
            state="interrupted",
            current_stage=state.get("current_stage"),
            attempt_id=state.get("attempt_id"),
            progress=state.get("progress"),
            failure=failure,
            event_type="job_interrupted",
        )

    def write_checkpoint(
        self,
        job_id: str,
        stage: str,
        checkpoint: Mapping[str, Any],
        *,
        expected_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        path = self._require_job(job_id)
        _validate_component(stage, "stage")
        if expected_attempt_id is not None:
            _validate_component(expected_attempt_id, "expected attempt ID")
            current_attempt = _read_task_json(path / "state.json").get(
                "attempt_id"
            )
            if current_attempt != expected_attempt_id:
                raise StaleAttemptError(
                    f"attempt {expected_attempt_id} cannot checkpoint "
                    f"current attempt {current_attempt}"
                )
        value = dict(checkpoint)
        value["schema_version"] = TASK_SCHEMA_VERSION
        value["stage"] = stage
        atomic_write_json(path / "stages" / stage / "checkpoint.json", value)
        self.append_event(
            job_id,
            "checkpoint_written",
            {"stage": stage, "status": value.get("status")},
        )
        return value

    def read_checkpoint(self, job_id: str, stage: str) -> dict[str, Any] | None:
        path = self._require_job(job_id)
        _validate_component(stage, "stage")
        checkpoint_path = path / "stages" / stage / "checkpoint.json"
        return (
            _read_task_json(checkpoint_path)
            if checkpoint_path.is_file()
            else None
        )

    def invalidate_checkpoint(self, job_id: str, stage: str) -> dict[str, Any] | None:
        checkpoint = self.read_checkpoint(job_id, stage)
        if checkpoint is None:
            return None
        checkpoint["status"] = "invalidated"
        checkpoint["invalidated_at"] = utc_now()
        return self.write_checkpoint(job_id, stage, checkpoint)

    def request_cancel(
        self,
        job_id: str,
        *,
        requested_by: str = "cli",
        reason: str | None = None,
    ) -> dict[str, Any]:
        path = self._require_job(job_id)
        current_state = self.read_state(job_id).get("state")
        if current_state in _TERMINAL_STATES:
            raise ValueError(
                f"cannot request cancellation for terminal state "
                f"{current_state!r}"
            )
        request = {
            "schema_version": TASK_SCHEMA_VERSION,
            "job_id": job_id,
            "requested_at": utc_now(),
            "requested_by": requested_by,
            "reason": reason,
        }
        atomic_write_json(path / "cancel.request", request)
        self.append_event(
            job_id,
            "cancel_requested",
            {"requested_by": requested_by, "reason": reason},
        )
        return request

    def read_cancel_request(self, job_id: str) -> dict[str, Any] | None:
        request_path = self._require_job(job_id) / "cancel.request"
        return (
            _read_task_json(request_path)
            if request_path.is_file()
            else None
        )

    def cancel_requested(self, job_id: str) -> bool:
        return (self._require_job(job_id) / "cancel.request").is_file()

    def clear_cancel_request(self, job_id: str) -> None:
        (self._require_job(job_id) / "cancel.request").unlink(missing_ok=True)
