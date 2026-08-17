from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from material_collector.application.session_runtime import (
    CancellationRequestedError,
    ExecutionLeaseConflictError,
    ExecutionLeaseLostError,
    SessionControlApplication,
)
from material_collector.application.sessions import (
    SESSION_SCHEMA_VERSION,
    CreateSessionRequest,
    SessionApplication,
)
from material_collector.core.errors import SessionVersionError
from material_collector.infrastructure.session_runtime_store import (
    SqliteSessionRuntime as SessionRuntime,
)
from material_collector.infrastructure.session_store import SqliteSessionStore


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_session(tmp_path: Path, session_id: str = "ses_runtime_001") -> tuple[Path, str]:
    workspace = tmp_path / "materials"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    _write_json(
        input_path,
        {
            "schema_version": "1.0",
            "full_script": "一段测试文案",
            "segments": [
                {
                    "segment_id": "seg_a",
                    "order": 1,
                    "text": "一段测试文案",
                }
            ],
        },
    )
    _write_json(
        plans_path,
        {
            "schema_version": "2.0",
            "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
            "plans": [
                {
                    "segment_id": "seg_a",
                    "visual_strategy": "寻找相关画面",
                    "required_visual_facets": [{"facet_id": "facet_a", "description": "测试画面"}],
                    "initial_queries": [
                        {
                            "query_id": "query_a",
                            "text": "测试",
                            "target_platforms": [
                                "bilibili",
                                "douyin",
                                "xiaohongshu",
                            ],
                            "facet_ids": ["facet_a"],
                        }
                    ],
                }
            ],
        },
    )
    created = SessionApplication(
        store=SqliteSessionStore(
            now=lambda: datetime(2026, 7, 29, 12, tzinfo=UTC),
            session_id_factory=lambda _now: session_id,
        )
    ).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
        )
    )
    return workspace, created.session_id


def test_stage_completion_is_idempotent_and_finishes_session(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock, lease_ttl_seconds=30)

    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("authenticate", "search"),
    )
    assert lease.next_stage == "authenticate"
    attempt = runtime.begin_stage(
        lease,
        stage_key="authenticate",
        operation_key="auth:round-1",
    )
    assert runtime.complete_stage(lease, attempt, result={"valid": True}) == "search"

    replay = runtime.begin_stage(
        lease,
        stage_key="authenticate",
        operation_key="auth:round-1",
    )
    assert replay.replayed is True
    assert replay.result == {"valid": True}

    search = runtime.begin_stage(
        lease,
        stage_key="search",
        operation_key="search:round-1",
    )
    assert (
        runtime.completed_operation_result(
            workspace,
            session_id,
            "search:round-1",
        )
        is None
    )
    assert runtime.complete_stage(lease, search, result={"count": 3}) is None
    assert runtime.completed_operation_result(
        workspace,
        session_id,
        "search:round-1",
    ) == {"count": 3}
    runtime.finish_execution(lease)

    snapshot = runtime.inspect(workspace, session_id)
    assert snapshot.status == "completed"
    assert snapshot.completed_stages == ("authenticate", "search")
    assert snapshot.lease_owner_id is None


def test_child_operation_failure_is_retryable_without_replaying_success(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(
        now=lambda: datetime(2026, 7, 29, 13, tzinfo=UTC),
        lease_ttl_seconds=30,
    )
    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("search",),
    )
    stage = runtime.begin_stage(
        lease,
        stage_key="search",
        operation_key="search:stage",
    )
    successful = runtime.begin_operation(
        lease,
        stage_key="search",
        operation_key="search:bilibili:q1",
    )
    runtime.complete_operation(lease, successful, result={"count": 1})
    failed = runtime.begin_operation(
        lease,
        stage_key="search",
        operation_key="search:douyin:q1",
    )
    runtime.fail_operation(
        lease,
        failed,
        error={"code": "platform_request_rejected"},
    )
    runtime.fail_stage(
        lease,
        stage,
        error={"code": "stage_has_retryable_children"},
    )
    runtime.release_execution(lease)

    resumed = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-b",
        stages=("search",),
    )
    runtime.begin_stage(
        resumed,
        stage_key="search",
        operation_key="search:stage",
    )
    replay = runtime.begin_operation(
        resumed,
        stage_key="search",
        operation_key="search:bilibili:q1",
    )
    retry = runtime.begin_operation(
        resumed,
        stage_key="search",
        operation_key="search:douyin:q1",
    )

    assert replay.replayed is True
    assert replay.result == {"count": 1}
    assert retry.replayed is False
    assert retry.attempt_id != failed.attempt_id


def test_second_executor_gets_structured_conflict_until_lease_expires(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock, lease_ttl_seconds=30)
    runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("search",),
    )

    with pytest.raises(ExecutionLeaseConflictError) as captured:
        runtime.begin_execution(
            workspace,
            session_id,
            owner_id="worker-b",
            stages=("search",),
        )

    assert captured.value.code == "session_execution_conflict"
    assert captured.value.details["owner_id"] == "worker-a"


def test_concurrent_executors_produce_exactly_one_writer(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(
        now=lambda: datetime(2026, 7, 29, 13, tzinfo=UTC),
        lease_ttl_seconds=30,
    )
    barrier = threading.Barrier(2)

    def acquire(owner_id: str) -> str:
        barrier.wait()
        try:
            lease = runtime.begin_execution(
                workspace,
                session_id,
                owner_id=owner_id,
                stages=("search",),
            )
        except ExecutionLeaseConflictError:
            return "conflict"
        return f"acquired:{lease.owner_id}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(
            future.result()
            for future in (
                pool.submit(acquire, "worker-a"),
                pool.submit(acquire, "worker-b"),
            )
        )

    assert outcomes.count("conflict") == 1
    assert sum(outcome.startswith("acquired:") for outcome in outcomes) == 1


def test_expired_executor_is_recovered_from_first_unfinished_stage(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock, lease_ttl_seconds=10)
    crashed_lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="crashed-worker",
        stages=("search", "download"),
    )
    crashed_attempt = runtime.begin_stage(
        crashed_lease,
        stage_key="search",
        operation_key="search:round-1",
    )

    clock.advance(seconds=11)
    resumed = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="replacement-worker",
        stages=("search", "download"),
    )
    assert resumed.generation == crashed_lease.generation + 1
    assert resumed.next_stage == "search"
    with pytest.raises(ExecutionLeaseLostError):
        runtime.complete_stage(
            crashed_lease,
            crashed_attempt,
            result={"stale": True},
        )

    retry = runtime.begin_stage(
        resumed,
        stage_key="search",
        operation_key="search:round-1",
    )
    assert retry.attempt_id != crashed_attempt.attempt_id
    runtime.complete_stage(resumed, retry, result={"recovered": True})

    database = workspace / ".material-collector" / "sessions" / session_id / "session.sqlite3"
    with sqlite3.connect(database) as connection:
        statuses = [
            row[0]
            for row in connection.execute("SELECT status FROM stage_attempt ORDER BY attempt_id")
        ]
    assert statuses == ["interrupted", "completed"]


def test_cancel_request_is_cooperative_and_durable(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock)
    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("search",),
    )

    requested = runtime.request_cancel(workspace, session_id)
    assert requested.cancel_requested is True
    assert runtime.cancellation_requested(lease) is True
    with pytest.raises(CancellationRequestedError):
        runtime.begin_stage(
            lease,
            stage_key="search",
            operation_key="search:round-1",
        )

    runtime.acknowledge_cancel(lease)
    snapshot = runtime.inspect(workspace, session_id)
    assert snapshot.status == "cancelled"
    assert snapshot.cancel_requested is True
    assert snapshot.lease_owner_id is None


def test_inspect_distinguishes_an_expired_cancelled_executor_lease(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock, lease_ttl_seconds=60)
    runtime.begin_execution(
        workspace,
        session_id,
        owner_id="dead-worker",
        stages=("authenticate",),
    )
    runtime.request_cancel(workspace, session_id)

    clock.advance(seconds=61)
    snapshot = runtime.inspect(workspace, session_id)

    assert snapshot.cancel_requested is True
    assert snapshot.lease_owner_id == "dead-worker"
    assert snapshot.lease_expired is True


def test_application_status_marks_expired_non_cancelled_lease_recoverable(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock, lease_ttl_seconds=60)
    runtime.begin_execution(
        workspace,
        session_id,
        owner_id="dead-worker",
        stages=("authenticate",),
    )
    clock.advance(seconds=61)

    payload = SessionControlApplication(
        sessions=SessionApplication(store=SqliteSessionStore()),
        runtime=runtime,
    ).get_status(workspace, session_id)

    assert payload["runtime"]["state"] == "idle"
    assert payload["runtime"]["lease_expired"] is True
    assert payload["runtime"]["lease_owner_id"] == "dead-worker"


def test_cancelling_completed_session_is_idempotent_and_suggests_nothing(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime()
    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker",
        stages=("authenticate",),
    )
    stage = runtime.begin_stage(
        lease,
        stage_key="authenticate",
        operation_key="authenticate:v1",
    )
    runtime.complete_stage(lease, stage, result={"ok": True})
    runtime.finish_execution(lease)
    application = SessionControlApplication(
        sessions=SessionApplication(store=SqliteSessionStore()),
        runtime=runtime,
    )

    payload = application.request_cancel(workspace, session_id)

    assert payload["status"] == "completed"
    assert payload["cancel_requested"] is False
    assert payload["runtime"]["state"] == "completed"
    assert payload["suggested_action"] == "none"


def test_action_checkpoint_is_persisted_until_execution_resumes(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    clock = MutableClock(datetime(2026, 7, 29, 13, tzinfo=UTC))
    runtime = SessionRuntime(now=clock)
    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("search",),
    )

    checkpoint = runtime.pause_for_action(
        lease,
        status="decision_required",
        action_required={
            "actor": "agent",
            "type": "sufficiency_decision",
            "decision_id": "decision-1",
        },
    )
    assert checkpoint.status == "decision_required"
    assert checkpoint.lease_owner_id is None
    assert checkpoint.action_required == {
        "actor": "agent",
        "type": "sufficiency_decision",
        "decision_id": "decision-1",
    }

    resumed = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-b",
        stages=("search",),
    )
    assert resumed.next_stage == "search"
    active = runtime.inspect(workspace, session_id)
    assert active.status == "running"
    assert active.action_required is None
    assert active.lease_owner_id == "worker-b"


def test_legacy_initialized_database_is_migrated_on_first_execution(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    database = workspace / ".material-collector" / "sessions" / session_id / "session.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE operation_record;
            DROP TABLE stage_attempt;
            DROP TABLE stage_state;
            DROP TABLE runtime_control;
            DROP TABLE execution_lease;
            DROP TABLE runtime_schema_info;
            """
        )
        connection.commit()

    runtime = SessionRuntime(now=lambda: datetime(2026, 7, 29, 13, tzinfo=UTC))
    before = runtime.inspect(workspace, session_id)
    assert before.status == "initialized"
    assert before.completed_stages == ()

    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="worker-a",
        stages=("search",),
    )
    assert lease.next_stage == "search"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT schema_version FROM runtime_schema_info WHERE singleton = 1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SESSION_SCHEMA_VERSION


def test_v1_base_session_is_rejected_before_runtime_execution(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path)
    database = workspace / ".material-collector" / "sessions" / session_id / "session.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE session_state DROP COLUMN request_timeout_seconds")
        connection.execute("UPDATE schema_info SET schema_version = 1 WHERE singleton = 1")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    with pytest.raises(SessionVersionError) as captured:
        SessionRuntime().begin_execution(
            workspace,
            session_id,
            owner_id="worker-a",
            stages=("search",),
        )

    assert captured.value.details == {
        "session_id": session_id,
        "received_version": 1,
        "supported_versions": [SESSION_SCHEMA_VERSION],
    }
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_state)")}
        assert "request_timeout_seconds" not in columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_legacy_workflow_plan_inserts_fingerprint_before_understanding(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime()
    legacy_stages = (
        "authenticate",
        "search",
        "resolve",
        "download",
        "understand_and_ingest",
    )
    lease = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="legacy-worker",
        stages=legacy_stages,
    )
    for stage in legacy_stages[:-1]:
        attempt = runtime.begin_stage(
            lease,
            stage_key=stage,
            operation_key=f"legacy:{stage}",
        )
        next_stage = runtime.complete_stage(lease, attempt, result={"ok": True})
        lease = lease.__class__(
            workspace=lease.workspace,
            session_id=lease.session_id,
            owner_id=lease.owner_id,
            generation=lease.generation,
            expires_at=lease.expires_at,
            next_stage=next_stage,
        )
    runtime.pause_for_action(
        lease,
        status="integration_required",
        action_required={"actor": "human", "type": "integration_required"},
    )

    resumed = runtime.begin_execution(
        workspace,
        session_id,
        owner_id="new-worker",
        stages=(
            "authenticate",
            "search",
            "resolve",
            "download",
            "fingerprint",
            "understand_and_ingest",
        ),
    )

    assert resumed.next_stage == "fingerprint"
    snapshot = runtime.inspect(workspace, session_id)
    assert snapshot.completed_stages == (
        "authenticate",
        "search",
        "resolve",
        "download",
    )
