"""SQLite adapter for durable, resumable collection-session execution state.

The runtime deliberately keeps transactions short. External search, download and
understanding work happens outside SQLite; callers only open a transaction to
claim work, heartbeat, or commit an outcome.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from material_collector.application.session_runtime import (
    CancellationRequestedError,
    ExecutionLease,
    ExecutionLeaseConflictError,
    ExecutionLeaseLostError,
    RuntimeSnapshot,
    StageAttempt,
)
from material_collector.application.sessions import (
    CONTROL_DIRECTORY,
    SESSION_DATABASE,
    SESSION_SCHEMA_VERSION,
    SESSIONS_DIRECTORY,
)
from material_collector.core.errors import (
    CollectorError,
    ContractError,
    SessionNotFoundError,
    SessionStateError,
    SessionVersionError,
    WorkspaceError,
)

RUNTIME_SCHEMA_VERSION = 1

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class SqliteSessionRuntime:
    """Coordinate a single durable execution without holding long transactions."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        lease_ttl_seconds: int = 30,
    ) -> None:
        if lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds must be positive")
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds)

    def begin_execution(
        self,
        workspace: Path,
        session_id: str,
        *,
        owner_id: str,
        stages: Sequence[str],
    ) -> ExecutionLease:
        """Acquire the writer lease and return the first unfinished stage."""

        _validate_identifier(owner_id, "owner_id")
        normalized_stages = _validate_stages(stages)
        database = _database_path(workspace, session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        expires_text = _iso(now + self._lease_ttl)

        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _ensure_runtime_schema(connection, session_id)
                state = _session_state(connection, session_id)
                if str(state["status"]) in {"completed", "cancelled"}:
                    raise SessionStateError(
                        "A terminal collection session cannot begin execution.",
                        details={
                            "session_id": session_id,
                            "status": str(state["status"]),
                        },
                    )

                _ensure_stage_plan(connection, normalized_stages, now_text)
                lease_row = connection.execute(
                    """
                    SELECT owner_id, generation, expires_at
                    FROM execution_lease
                    WHERE singleton = 1
                    """
                ).fetchone()
                generation = 1
                if lease_row is not None:
                    generation = int(lease_row["generation"])
                    lease_expired = _parse_utc(str(lease_row["expires_at"])) <= now
                    if not lease_expired and str(lease_row["owner_id"]) != owner_id:
                        raise ExecutionLeaseConflictError(
                            session_id=session_id,
                            owner_id=str(lease_row["owner_id"]),
                            expires_at=str(lease_row["expires_at"]),
                        )
                    if lease_expired:
                        _recover_interrupted_work(connection, now_text)
                        generation += 1

                connection.execute(
                    """
                    INSERT INTO execution_lease (
                        singleton, owner_id, generation, acquired_at,
                        heartbeat_at, expires_at
                    ) VALUES (1, ?, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        generation = excluded.generation,
                        acquired_at = excluded.acquired_at,
                        heartbeat_at = excluded.heartbeat_at,
                        expires_at = excluded.expires_at
                    """,
                    (owner_id, generation, now_text, now_text, expires_text),
                )
                if str(state["status"]) != "running":
                    _set_session_status(connection, session_id, "running", now_text)
                _clear_action_required(connection)
                next_stage = _next_stage(connection)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise SessionStateError(
                    "The session execution lease could not be acquired.",
                    details={"session_id": session_id, "reason": str(error)},
                ) from error

        return ExecutionLease(
            workspace=database.parents[3],
            session_id=session_id,
            owner_id=owner_id,
            generation=generation,
            expires_at=expires_text,
            next_stage=next_stage,
        )

    def heartbeat(self, lease: ExecutionLease) -> ExecutionLease:
        """Renew a live lease after validating ownership and generation."""

        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        expires_text = _iso(now + self._lease_ttl)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                connection.execute(
                    """
                    UPDATE execution_lease
                    SET heartbeat_at = ?, expires_at = ?
                    WHERE singleton = 1
                    """,
                    (_iso(now), expires_text),
                )
                next_stage = _next_stage(connection)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error
        return ExecutionLease(
            workspace=lease.workspace,
            session_id=lease.session_id,
            owner_id=lease.owner_id,
            generation=lease.generation,
            expires_at=expires_text,
            next_stage=next_stage,
        )

    def begin_stage(
        self,
        lease: ExecutionLease,
        *,
        stage_key: str,
        operation_key: str,
    ) -> StageAttempt:
        """Claim one stage operation, replaying an already committed result."""

        _validate_identifier(stage_key, "stage_key")
        _validate_identifier(operation_key, "operation_key")
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)

        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                if _cancel_requested(connection):
                    raise CancellationRequestedError(lease.session_id)
                operation = connection.execute(
                    """
                    SELECT
                        stage_key, status, latest_attempt_id, result_json
                    FROM operation_record
                    WHERE operation_key = ?
                    """,
                    (operation_key,),
                ).fetchone()
                if operation is not None:
                    if str(operation["stage_key"]) != stage_key:
                        raise SessionStateError(
                            "An idempotency key cannot be reused for another stage.",
                            details={
                                "session_id": lease.session_id,
                                "operation_key": operation_key,
                                "existing_stage": str(operation["stage_key"]),
                                "requested_stage": stage_key,
                            },
                        )
                    if str(operation["status"]) == "completed":
                        connection.commit()
                        return StageAttempt(
                            attempt_id=int(operation["latest_attempt_id"]),
                            stage_key=stage_key,
                            operation_key=operation_key,
                            replayed=True,
                            result=_decode_json(operation["result_json"]),
                        )
                    running_attempt = connection.execute(
                        """
                        SELECT attempt_id, owner_id, lease_generation, status
                        FROM stage_attempt
                        WHERE attempt_id = ?
                        """,
                        (int(operation["latest_attempt_id"]),),
                    ).fetchone()
                    if (
                        running_attempt is not None
                        and str(running_attempt["status"]) == "running"
                        and str(running_attempt["owner_id"]) == lease.owner_id
                        and int(running_attempt["lease_generation"]) == lease.generation
                    ):
                        connection.commit()
                        return StageAttempt(
                            attempt_id=int(running_attempt["attempt_id"]),
                            stage_key=stage_key,
                            operation_key=operation_key,
                            replayed=False,
                        )

                next_stage = _next_stage(connection)
                if next_stage != stage_key:
                    raise SessionStateError(
                        "Stages must execute in their persisted order.",
                        details={
                            "session_id": lease.session_id,
                            "expected_stage": next_stage,
                            "requested_stage": stage_key,
                        },
                    )
                connection.execute(
                    """
                    UPDATE stage_state
                    SET status = 'running',
                        attempt_count = attempt_count + 1,
                        updated_at = ?
                    WHERE stage_key = ?
                    """,
                    (now_text, stage_key),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO stage_attempt (
                        stage_key, operation_key, owner_id, lease_generation,
                        status, started_at
                    ) VALUES (?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        stage_key,
                        operation_key,
                        lease.owner_id,
                        lease.generation,
                        now_text,
                    ),
                )
                if cursor.lastrowid is None:
                    raise SessionStateError(
                        "The stage attempt identifier was not allocated.",
                        details={"session_id": lease.session_id},
                    )
                attempt_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO operation_record (
                        operation_key, stage_key, status, latest_attempt_id,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, 'running', ?, NULL, ?, ?)
                    ON CONFLICT(operation_key) DO UPDATE SET
                        status = 'running',
                        latest_attempt_id = excluded.latest_attempt_id,
                        result_json = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation_key,
                        stage_key,
                        attempt_id,
                        now_text,
                        now_text,
                    ),
                )
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error

        return StageAttempt(
            attempt_id=attempt_id,
            stage_key=stage_key,
            operation_key=operation_key,
            replayed=False,
        )

    def complete_stage(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        result: Any | None = None,
    ) -> str | None:
        """Atomically commit an operation result and complete its stage."""

        if attempt.replayed:
            return self.inspect(lease.workspace, lease.session_id).next_stage
        result_json = _encode_json(result)
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                row = _running_attempt(connection, lease, attempt)
                connection.execute(
                    """
                    UPDATE stage_attempt
                    SET status = 'completed', finished_at = ?, result_json = ?
                    WHERE attempt_id = ?
                    """,
                    (now_text, result_json, int(row["attempt_id"])),
                )
                connection.execute(
                    """
                    UPDATE operation_record
                    SET status = 'completed', result_json = ?, updated_at = ?
                    WHERE operation_key = ? AND latest_attempt_id = ?
                    """,
                    (
                        result_json,
                        now_text,
                        attempt.operation_key,
                        attempt.attempt_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE stage_state
                    SET status = 'completed', updated_at = ?
                    WHERE stage_key = ?
                    """,
                    (now_text, attempt.stage_key),
                )
                _touch_session(connection, lease.session_id, now_text)
                next_stage = _next_stage(connection)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error
        return next_stage

    def completed_operation_result(
        self,
        workspace: Path,
        session_id: str,
        operation_key: str,
    ) -> Any | None:
        """Read one durable idempotent operation result."""

        _validate_identifier(operation_key, "operation_key")
        database = _database_path(workspace, session_id)
        uri = database.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                row = connection.execute(
                    """
                    SELECT status, result_json
                    FROM operation_record
                    WHERE operation_key = ?
                    """,
                    (operation_key,),
                ).fetchone()
        except sqlite3.Error as error:
            raise SessionStateError(
                "The session stage result could not be read.",
                details={
                    "session_id": session_id,
                    "operation_key": operation_key,
                    "reason": str(error),
                },
            ) from error
        if row is None or str(row["status"]) != "completed":
            return None
        return _decode_json(row["result_json"])

    def begin_operation(
        self,
        lease: ExecutionLease,
        *,
        stage_key: str,
        operation_key: str,
    ) -> StageAttempt:
        """Claim one child operation without completing its enclosing stage.

        Child operations provide durable per-request idempotency for stages that
        fan out to several external calls. A failed child can be retried after
        resume while completed siblings are replayed from ``operation_record``.
        """

        _validate_identifier(stage_key, "stage_key")
        _validate_identifier(operation_key, "operation_key")
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                if _cancel_requested(connection):
                    raise CancellationRequestedError(lease.session_id)
                operation = connection.execute(
                    """
                    SELECT stage_key, status, latest_attempt_id, result_json
                    FROM operation_record
                    WHERE operation_key = ?
                    """,
                    (operation_key,),
                ).fetchone()
                if operation is not None:
                    if str(operation["stage_key"]) != stage_key:
                        raise SessionStateError(
                            "An idempotency key cannot be reused for another stage.",
                            details={
                                "session_id": lease.session_id,
                                "operation_key": operation_key,
                                "existing_stage": str(operation["stage_key"]),
                                "requested_stage": stage_key,
                            },
                        )
                    if str(operation["status"]) == "completed":
                        connection.commit()
                        return StageAttempt(
                            attempt_id=int(operation["latest_attempt_id"]),
                            stage_key=stage_key,
                            operation_key=operation_key,
                            replayed=True,
                            result=_decode_json(operation["result_json"]),
                        )
                    running_attempt = connection.execute(
                        """
                        SELECT attempt_id, owner_id, lease_generation, status
                        FROM stage_attempt
                        WHERE attempt_id = ?
                        """,
                        (int(operation["latest_attempt_id"]),),
                    ).fetchone()
                    if (
                        running_attempt is not None
                        and str(running_attempt["status"]) == "running"
                        and str(running_attempt["owner_id"]) == lease.owner_id
                        and int(running_attempt["lease_generation"]) == lease.generation
                    ):
                        connection.commit()
                        return StageAttempt(
                            attempt_id=int(running_attempt["attempt_id"]),
                            stage_key=stage_key,
                            operation_key=operation_key,
                            replayed=False,
                        )
                stage = connection.execute(
                    "SELECT status FROM stage_state WHERE stage_key = ?",
                    (stage_key,),
                ).fetchone()
                if stage is None or str(stage["status"]) != "running":
                    raise SessionStateError(
                        "A child operation requires its enclosing stage to be running.",
                        details={
                            "session_id": lease.session_id,
                            "stage_key": stage_key,
                        },
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO stage_attempt (
                        stage_key, operation_key, owner_id, lease_generation,
                        status, started_at
                    ) VALUES (?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        stage_key,
                        operation_key,
                        lease.owner_id,
                        lease.generation,
                        now_text,
                    ),
                )
                if cursor.lastrowid is None:
                    raise SessionStateError(
                        "The operation attempt identifier was not allocated.",
                        details={"session_id": lease.session_id},
                    )
                attempt_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO operation_record (
                        operation_key, stage_key, status, latest_attempt_id,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, 'running', ?, NULL, ?, ?)
                    ON CONFLICT(operation_key) DO UPDATE SET
                        status = 'running',
                        latest_attempt_id = excluded.latest_attempt_id,
                        result_json = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        operation_key,
                        stage_key,
                        attempt_id,
                        now_text,
                        now_text,
                    ),
                )
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error
        return StageAttempt(
            attempt_id=attempt_id,
            stage_key=stage_key,
            operation_key=operation_key,
            replayed=False,
        )

    def complete_operation(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        result: Any | None = None,
    ) -> None:
        """Commit a child result while leaving the enclosing stage running."""

        if attempt.replayed:
            return
        result_json = _encode_json(result)
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                row = _running_attempt(connection, lease, attempt)
                connection.execute(
                    """
                    UPDATE stage_attempt
                    SET status = 'completed', finished_at = ?, result_json = ?
                    WHERE attempt_id = ?
                    """,
                    (now_text, result_json, int(row["attempt_id"])),
                )
                connection.execute(
                    """
                    UPDATE operation_record
                    SET status = 'completed', result_json = ?, updated_at = ?
                    WHERE operation_key = ? AND latest_attempt_id = ?
                    """,
                    (
                        result_json,
                        now_text,
                        attempt.operation_key,
                        attempt.attempt_id,
                    ),
                )
                _touch_session(connection, lease.session_id, now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error

    def fail_operation(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        error: dict[str, Any],
    ) -> None:
        """Record a failed child while leaving its enclosing stage running."""

        error_json = _encode_json(error)
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                _running_attempt(connection, lease, attempt)
                connection.execute(
                    """
                    UPDATE stage_attempt
                    SET status = 'failed', finished_at = ?, error_json = ?
                    WHERE attempt_id = ?
                    """,
                    (now_text, error_json, attempt.attempt_id),
                )
                connection.execute(
                    """
                    UPDATE operation_record
                    SET status = 'failed', updated_at = ?
                    WHERE operation_key = ? AND latest_attempt_id = ?
                    """,
                    (now_text, attempt.operation_key, attempt.attempt_id),
                )
                _touch_session(connection, lease.session_id, now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as sqlite_error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, sqlite_error) from sqlite_error

    def fail_stage(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        error: dict[str, Any],
    ) -> None:
        """Record a failed attempt while leaving the stage resumable."""

        error_json = _encode_json(error)
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                _running_attempt(connection, lease, attempt)
                connection.execute(
                    """
                    UPDATE stage_attempt
                    SET status = 'failed', finished_at = ?, error_json = ?
                    WHERE attempt_id = ?
                    """,
                    (now_text, error_json, attempt.attempt_id),
                )
                connection.execute(
                    """
                    UPDATE operation_record
                    SET status = 'failed', updated_at = ?
                    WHERE operation_key = ? AND latest_attempt_id = ?
                    """,
                    (now_text, attempt.operation_key, attempt.attempt_id),
                )
                connection.execute(
                    """
                    UPDATE stage_state
                    SET status = 'pending', updated_at = ?
                    WHERE stage_key = ?
                    """,
                    (now_text, attempt.stage_key),
                )
                _touch_session(connection, lease.session_id, now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as sqlite_error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, sqlite_error) from sqlite_error

    def request_cancel(self, workspace: Path, session_id: str) -> RuntimeSnapshot:
        """Persist a cooperative cancellation request without taking the lease."""

        database = _database_path(workspace, session_id)
        now_text = _iso(_utc(self._now()))
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _ensure_runtime_schema(connection, session_id)
                session_row = connection.execute(
                    "SELECT status FROM session_state WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                terminal = (
                    session_row is not None
                    and str(session_row["status"]) in {"completed", "cancelled"}
                )
                if not terminal and not _cancel_requested(connection):
                    connection.execute(
                        """
                        UPDATE runtime_control
                        SET cancel_requested = 1, cancel_requested_at = ?
                        WHERE singleton = 1
                        """,
                        (now_text,),
                    )
                    _touch_session(connection, session_id, now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(session_id, error) from error
        return self.inspect(workspace, session_id)

    def pause_for_action(
        self,
        lease: ExecutionLease,
        *,
        status: str,
        action_required: dict[str, Any],
    ) -> RuntimeSnapshot:
        """Persist a structured checkpoint and release the active writer."""

        allowed_statuses = {
            "integration_required",
            "auth_required",
            "decision_required",
        }
        if status not in allowed_statuses:
            raise ContractError(
                "The action checkpoint status is unsupported.",
                details={
                    "status": status,
                    "allowed_statuses": sorted(allowed_statuses),
                },
            )
        action_json = _encode_json(action_required)
        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                _recover_interrupted_work(connection, now_text)
                connection.execute(
                    """
                    UPDATE runtime_control
                    SET action_required_json = ?
                    WHERE singleton = 1
                    """,
                    (action_json,),
                )
                connection.execute("DELETE FROM execution_lease WHERE singleton = 1")
                _set_session_status(connection, lease.session_id, status, now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error
        return self.inspect(lease.workspace, lease.session_id)

    def cancellation_requested(self, lease: ExecutionLease) -> bool:
        """Read the cooperative cancellation flag while validating the lease."""

        database = _database_path(lease.workspace, lease.session_id)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN")
                _require_live_lease(connection, lease, _utc(self._now()))
                requested = _cancel_requested(connection)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error
        return requested

    def acknowledge_cancel(self, lease: ExecutionLease) -> None:
        """Stop at a safe point, persist cancellation, and release the lease."""

        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                if not _cancel_requested(connection):
                    raise SessionStateError(
                        "Cancellation cannot be acknowledged before it is requested.",
                        details={"session_id": lease.session_id},
                    )
                _recover_interrupted_work(connection, now_text)
                _clear_action_required(connection)
                connection.execute("DELETE FROM execution_lease WHERE singleton = 1")
                _set_session_status(connection, lease.session_id, "cancelled", now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error

    def finish_execution(self, lease: ExecutionLease) -> None:
        """Mark a fully completed stage plan terminal and release ownership."""

        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                if _next_stage(connection) is not None:
                    raise SessionStateError(
                        "Execution cannot finish while a stage is incomplete.",
                        details={"session_id": lease.session_id},
                    )
                connection.execute("DELETE FROM execution_lease WHERE singleton = 1")
                _clear_action_required(connection)
                _set_session_status(connection, lease.session_id, "completed", now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error

    def release_execution(self, lease: ExecutionLease) -> None:
        """Gracefully pause unfinished work and release ownership."""

        database = _database_path(lease.workspace, lease.session_id)
        now = _utc(self._now())
        now_text = _iso(now)
        with closing(_connect(database)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _require_live_lease(connection, lease, now)
                _recover_interrupted_work(connection, now_text)
                connection.execute("DELETE FROM execution_lease WHERE singleton = 1")
                _clear_action_required(connection)
                _set_session_status(connection, lease.session_id, "paused", now_text)
                connection.commit()
            except CollectorError:
                connection.rollback()
                raise
            except sqlite3.Error as error:
                connection.rollback()
                raise _runtime_write_error(lease.session_id, error) from error

    def inspect(self, workspace: Path, session_id: str) -> RuntimeSnapshot:
        """Read runtime state without migration, lease renewal, or other writes."""

        database = _database_path(workspace, session_id)
        uri = database.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                state = _session_state(connection, session_id)
                has_runtime = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'runtime_schema_info'
                    """
                ).fetchone()
                if has_runtime is None:
                    return RuntimeSnapshot(
                        session_id=session_id,
                        status=str(state["status"]),
                        cancel_requested=False,
                        action_required=None,
                        lease_owner_id=None,
                        lease_expires_at=None,
                        lease_expired=False,
                        next_stage=None,
                        completed_stages=(),
                    )
                control = connection.execute(
                    """
                    SELECT cancel_requested, action_required_json
                    FROM runtime_control
                    WHERE singleton = 1
                    """
                ).fetchone()
                lease = connection.execute(
                    """
                    SELECT owner_id, expires_at
                    FROM execution_lease
                    WHERE singleton = 1
                    """
                ).fetchone()
                stages = connection.execute(
                    """
                    SELECT stage_key, status
                    FROM stage_state
                    ORDER BY sequence
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise SessionStateError(
                "The session runtime state could not be read.",
                details={"session_id": session_id, "reason": str(error)},
            ) from error

        return RuntimeSnapshot(
            session_id=session_id,
            status=str(state["status"]),
            cancel_requested=bool(control and int(control["cancel_requested"])),
            action_required=(
                None
                if control is None or control["action_required_json"] is None
                else _decode_json_object(control["action_required_json"])
            ),
            lease_owner_id=None if lease is None else str(lease["owner_id"]),
            lease_expires_at=None if lease is None else str(lease["expires_at"]),
            lease_expired=(
                lease is not None
                and _parse_utc(str(lease["expires_at"])) <= _utc(self._now())
            ),
            next_stage=next(
                (
                    str(stage["stage_key"])
                    for stage in stages
                    if str(stage["status"]) != "completed"
                ),
                None,
            ),
            completed_stages=tuple(
                str(stage["stage_key"]) for stage in stages if str(stage["status"]) == "completed"
            ),
        )


def _database_path(workspace: Path, session_id: str) -> Path:
    if not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,80}", session_id):
        raise ContractError(
            "session_id contains unsafe characters.",
            details={"session_id": session_id},
        )
    try:
        normalized_workspace = workspace.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise WorkspaceError(
            "The material workspace path cannot be resolved.",
            details={"path": str(workspace), "reason": str(error)},
        ) from error
    if not normalized_workspace.is_dir():
        raise WorkspaceError(
            "The material workspace path is not a directory.",
            details={"path": str(normalized_workspace)},
        )
    sessions_root = (normalized_workspace / CONTROL_DIRECTORY / SESSIONS_DIRECTORY).resolve()
    session_dir = (sessions_root / session_id).resolve()
    if session_dir.parent != sessions_root:
        raise ContractError(
            "session_id resolves outside the material workspace.",
            details={"session_id": session_id},
        )
    database = session_dir / SESSION_DATABASE
    if not database.is_file():
        raise SessionNotFoundError(session_id)
    return database


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _ensure_runtime_schema(connection: sqlite3.Connection, session_id: str) -> None:
    base = connection.execute(
        "SELECT schema_version FROM schema_info WHERE singleton = 1"
    ).fetchone()
    if base is None:
        raise SessionStateError(
            "The session database schema is unsupported.",
            details={"session_id": session_id},
        )
    schema_version = int(base["schema_version"])
    if schema_version != SESSION_SCHEMA_VERSION:
        raise SessionVersionError(
            session_id,
            schema_version,
            SESSION_SCHEMA_VERSION,
        )
    statements = (
        """
        CREATE TABLE IF NOT EXISTS runtime_schema_info (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS execution_lease (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            owner_id TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation >= 1),
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runtime_control (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            cancel_requested INTEGER NOT NULL DEFAULT 0
                CHECK (cancel_requested IN (0, 1)),
            cancel_requested_at TEXT,
            action_required_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS stage_state (
            stage_key TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'running', 'completed')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS stage_attempt (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage_key TEXT NOT NULL REFERENCES stage_state(stage_key),
            operation_key TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
            status TEXT NOT NULL
                CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            result_json TEXT,
            error_json TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS operation_record (
            operation_key TEXT PRIMARY KEY,
            stage_key TEXT NOT NULL REFERENCES stage_state(stage_key),
            status TEXT NOT NULL
                CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
            latest_attempt_id INTEGER NOT NULL REFERENCES stage_attempt(attempt_id),
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)
    control_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(runtime_control)").fetchall()
    }
    if "action_required_json" not in control_columns:
        connection.execute("ALTER TABLE runtime_control ADD COLUMN action_required_json TEXT")
    schema = connection.execute(
        "SELECT schema_version FROM runtime_schema_info WHERE singleton = 1"
    ).fetchone()
    if schema is None:
        connection.execute(
            """
            INSERT INTO runtime_schema_info (singleton, schema_version)
            VALUES (1, ?)
            """,
            (RUNTIME_SCHEMA_VERSION,),
        )
    elif int(schema["schema_version"]) != RUNTIME_SCHEMA_VERSION:
        raise SessionStateError(
            "The session runtime schema is unsupported.",
            details={"session_id": session_id},
        )
    connection.execute(
        """
        INSERT INTO runtime_control (singleton, cancel_requested)
        VALUES (1, 0)
        ON CONFLICT(singleton) DO NOTHING
        """
    )


def _session_state(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT session_id, status, state_version
        FROM session_state
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise SessionStateError(
            "The session database does not contain its expected session row.",
            details={"session_id": session_id},
        )
    return cast(sqlite3.Row, row)


def _ensure_stage_plan(
    connection: sqlite3.Connection,
    stages: tuple[str, ...],
    now_text: str,
) -> None:
    existing = connection.execute("SELECT stage_key FROM stage_state ORDER BY sequence").fetchall()
    if existing:
        existing_plan = tuple(str(row["stage_key"]) for row in existing)
        if existing_plan != stages:
            if _can_insert_fingerprint_stage(connection, existing_plan, stages):
                insertion_sequence = stages.index("fingerprint") + 1
                connection.execute(
                    """
                    UPDATE stage_state
                    SET sequence = sequence + 1000, updated_at = ?
                    WHERE sequence >= ?
                    """,
                    (now_text, insertion_sequence),
                )
                connection.execute(
                    """
                    INSERT INTO stage_state (
                        stage_key, sequence, status, attempt_count, updated_at
                    ) VALUES ('fingerprint', ?, 'pending', 0, ?)
                    """,
                    (insertion_sequence, now_text),
                )
                for sequence, stage_key in enumerate(stages, start=1):
                    connection.execute(
                        """
                        UPDATE stage_state
                        SET sequence = ?, updated_at = ?
                        WHERE stage_key = ?
                        """,
                        (sequence, now_text, stage_key),
                    )
                return
            raise SessionStateError(
                "The persisted execution stage plan cannot be changed on resume.",
                details={
                    "persisted_stages": list(existing_plan),
                    "requested_stages": list(stages),
                },
            )
        return
    connection.executemany(
        """
        INSERT INTO stage_state (stage_key, sequence, status, updated_at)
        VALUES (?, ?, 'pending', ?)
        """,
        ((stage_key, sequence, now_text) for sequence, stage_key in enumerate(stages, start=1)),
    )


def _can_insert_fingerprint_stage(
    connection: sqlite3.Connection,
    existing: tuple[str, ...],
    requested: tuple[str, ...],
) -> bool:
    if requested.count("fingerprint") != 1:
        return False
    fingerprint_index = requested.index("fingerprint")
    if fingerprint_index == 0 or requested[fingerprint_index - 1] != "download":
        return False
    if (
        fingerprint_index + 1 >= len(requested)
        or requested[fingerprint_index + 1] != "understand_and_ingest"
    ):
        return False
    if requested[:fingerprint_index] + requested[fingerprint_index + 1 :] != existing:
        return False
    understanding = connection.execute(
        "SELECT status FROM stage_state WHERE stage_key = 'understand_and_ingest'"
    ).fetchone()
    return understanding is not None and str(understanding["status"]) != "completed"


def _recover_interrupted_work(
    connection: sqlite3.Connection,
    now_text: str,
) -> None:
    connection.execute(
        """
        UPDATE stage_attempt
        SET status = 'interrupted', finished_at = ?
        WHERE status = 'running'
        """,
        (now_text,),
    )
    connection.execute(
        """
        UPDATE operation_record
        SET status = 'interrupted', updated_at = ?
        WHERE status = 'running'
        """,
        (now_text,),
    )
    connection.execute(
        """
        UPDATE stage_state
        SET status = 'pending', updated_at = ?
        WHERE status = 'running'
        """,
        (now_text,),
    )


def _require_live_lease(
    connection: sqlite3.Connection,
    lease: ExecutionLease,
    now: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT owner_id, generation, expires_at
        FROM execution_lease
        WHERE singleton = 1
        """
    ).fetchone()
    if (
        row is None
        or str(row["owner_id"]) != lease.owner_id
        or int(row["generation"]) != lease.generation
        or _parse_utc(str(row["expires_at"])) <= now
    ):
        raise ExecutionLeaseLostError(lease.session_id)
    return cast(sqlite3.Row, row)


def _running_attempt(
    connection: sqlite3.Connection,
    lease: ExecutionLease,
    attempt: StageAttempt,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT attempt_id, stage_key, operation_key, owner_id, lease_generation, status
        FROM stage_attempt
        WHERE attempt_id = ?
        """,
        (attempt.attempt_id,),
    ).fetchone()
    if (
        row is None
        or str(row["stage_key"]) != attempt.stage_key
        or str(row["operation_key"]) != attempt.operation_key
        or str(row["owner_id"]) != lease.owner_id
        or int(row["lease_generation"]) != lease.generation
        or str(row["status"]) != "running"
    ):
        raise SessionStateError(
            "The stage attempt is not active under this execution lease.",
            details={
                "session_id": lease.session_id,
                "attempt_id": attempt.attempt_id,
            },
        )
    return cast(sqlite3.Row, row)


def _next_stage(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """
        SELECT stage_key
        FROM stage_state
        WHERE status != 'completed'
        ORDER BY sequence
        LIMIT 1
        """
    ).fetchone()
    return None if row is None else str(row["stage_key"])


def _cancel_requested(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT cancel_requested
        FROM runtime_control
        WHERE singleton = 1
        """
    ).fetchone()
    return bool(row and int(row["cancel_requested"]))


def _clear_action_required(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE runtime_control
        SET action_required_json = NULL
        WHERE singleton = 1
        """
    )


def _set_session_status(
    connection: sqlite3.Connection,
    session_id: str,
    status: str,
    now_text: str,
) -> None:
    cursor = connection.execute(
        """
        UPDATE session_state
        SET status = ?, state_version = state_version + 1, updated_at = ?
        WHERE session_id = ?
        """,
        (status, now_text, session_id),
    )
    if cursor.rowcount != 1:
        raise SessionStateError(
            "The session database does not contain its expected session row.",
            details={"session_id": session_id},
        )


def _touch_session(
    connection: sqlite3.Connection,
    session_id: str,
    now_text: str,
) -> None:
    cursor = connection.execute(
        """
        UPDATE session_state
        SET state_version = state_version + 1, updated_at = ?
        WHERE session_id = ?
        """,
        (now_text, session_id),
    )
    if cursor.rowcount != 1:
        raise SessionStateError(
            "The session database does not contain its expected session row.",
            details={"session_id": session_id},
        )


def _validate_identifier(value: str, field: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ContractError(
            f"{field} contains unsafe characters.",
            details={field: value},
        )


def _validate_stages(stages: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(stages)
    if not normalized:
        raise ContractError("At least one execution stage is required.")
    for stage in normalized:
        _validate_identifier(stage, "stage_key")
    if len(set(normalized)) != len(normalized):
        raise ContractError("Execution stage keys must be unique.")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SessionStateError(
            "The execution lease contains an invalid timestamp.",
            details={"expires_at": value},
        ) from error
    return _utc(parsed)


def _encode_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ContractError(
            "The stage result must be JSON serializable.",
            details={"reason": str(error)},
        ) from error


def _decode_json(value: Any) -> Any | None:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except json.JSONDecodeError as error:
        raise SessionStateError(
            "A committed idempotent operation contains invalid JSON.",
            details={"reason": str(error)},
        ) from error


def _decode_json_object(value: Any) -> dict[str, Any]:
    decoded = _decode_json(value)
    if not isinstance(decoded, dict):
        raise SessionStateError(
            "A persisted action_required checkpoint must contain a JSON object."
        )
    return cast(dict[str, Any], decoded)


def _runtime_write_error(session_id: str, error: sqlite3.Error) -> SessionStateError:
    return SessionStateError(
        "The session runtime state could not be updated.",
        details={"session_id": session_id, "reason": str(error)},
    )
