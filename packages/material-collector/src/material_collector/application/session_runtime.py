"""Application-facing contract for durable collection-session execution state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from material_collector.application.sessions import SessionApplication
from material_collector.core.errors import CollectorError


class ExecutionLeaseConflictError(CollectorError):
    """Another live executor owns the session."""

    def __init__(
        self,
        *,
        session_id: str,
        owner_id: str,
        expires_at: str,
    ) -> None:
        super().__init__(
            "session_execution_conflict",
            "Another executor currently owns this collection session.",
            details={
                "session_id": session_id,
                "owner_id": owner_id,
                "expires_at": expires_at,
            },
        )


class ExecutionLeaseLostError(CollectorError):
    """The supplied lease is stale, expired, or no longer owned by this executor."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_execution_lease_lost",
            "The collection-session execution lease is no longer valid.",
            details={"session_id": session_id},
        )


class CancellationRequestedError(CollectorError):
    """The control plane has asked the active executor to stop."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_cancel_requested",
            "Cancellation has been requested for this collection session.",
            details={"session_id": session_id},
        )


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    workspace: Path
    session_id: str
    owner_id: str
    generation: int
    expires_at: str
    next_stage: str | None


@dataclass(frozen=True, slots=True)
class StageAttempt:
    attempt_id: int
    stage_key: str
    operation_key: str
    replayed: bool
    result: Any | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    session_id: str
    status: str
    cancel_requested: bool
    action_required: dict[str, Any] | None
    lease_owner_id: str | None
    lease_expires_at: str | None
    lease_expired: bool
    next_stage: str | None
    completed_stages: tuple[str, ...]


class RuntimeView(BaseModel):
    """Stable application view of one session's execution control state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    cancel_requested: bool
    lease_owner_id: str | None
    lease_expires_at: str | None
    lease_expired: bool
    next_stage: str | None
    completed_stages: tuple[str, ...]


class SessionRuntime(Protocol):
    """Port used by workflow services to persist resumable execution state."""

    def begin_execution(
        self,
        workspace: Path,
        session_id: str,
        *,
        owner_id: str,
        stages: Sequence[str],
    ) -> ExecutionLease: ...

    def heartbeat(self, lease: ExecutionLease) -> ExecutionLease: ...

    def begin_stage(
        self,
        lease: ExecutionLease,
        *,
        stage_key: str,
        operation_key: str,
    ) -> StageAttempt: ...

    def complete_stage(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        result: Any | None = None,
    ) -> str | None: ...

    def completed_operation_result(
        self,
        workspace: Path,
        session_id: str,
        operation_key: str,
    ) -> Any | None: ...

    def begin_operation(
        self,
        lease: ExecutionLease,
        *,
        stage_key: str,
        operation_key: str,
    ) -> StageAttempt: ...

    def complete_operation(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        result: Any | None = None,
    ) -> None: ...

    def fail_operation(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        error: dict[str, Any],
    ) -> None: ...

    def fail_stage(
        self,
        lease: ExecutionLease,
        attempt: StageAttempt,
        *,
        error: dict[str, Any],
    ) -> None: ...

    def request_cancel(self, workspace: Path, session_id: str) -> RuntimeSnapshot: ...

    def pause_for_action(
        self,
        lease: ExecutionLease,
        *,
        status: str,
        action_required: dict[str, Any],
    ) -> RuntimeSnapshot: ...

    def cancellation_requested(self, lease: ExecutionLease) -> bool: ...

    def acknowledge_cancel(self, lease: ExecutionLease) -> None: ...

    def finish_execution(self, lease: ExecutionLease) -> None: ...

    def release_execution(self, lease: ExecutionLease) -> None: ...

    def inspect(self, workspace: Path, session_id: str) -> RuntimeSnapshot: ...


class SessionControlApplication:
    """Application boundary for status inspection and cooperative cancellation."""

    def __init__(
        self,
        *,
        sessions: SessionApplication,
        runtime: SessionRuntime,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime

    def get_status(self, workspace: Path, session_id: str) -> dict[str, Any]:
        session = self._sessions.get_session(workspace, session_id)
        payload = session.model_dump(mode="json", exclude_none=False)
        payload["runtime"] = self._runtime_view(
            self._runtime.inspect(workspace, session_id)
        ).model_dump(mode="json")
        return payload

    def request_cancel(self, workspace: Path, session_id: str) -> dict[str, Any]:
        snapshot = self._runtime.request_cancel(workspace, session_id)
        return {
            "schema_version": "1.0",
            "session_id": snapshot.session_id,
            "workspace_path": str(workspace.resolve()),
            "status": snapshot.status,
            "cancel_requested": snapshot.cancel_requested,
            "action_required": snapshot.action_required,
            "runtime": self._runtime_view(snapshot).model_dump(mode="json"),
            "suggested_action": self._cancel_suggested_action(snapshot),
        }

    @staticmethod
    def _runtime_view(snapshot: RuntimeSnapshot) -> RuntimeView:
        if snapshot.status in {"completed", "cancelled"}:
            state = snapshot.status
        elif (
            snapshot.cancel_requested
            and snapshot.lease_owner_id is not None
            and not snapshot.lease_expired
        ):
            state = "cancelling"
        elif snapshot.cancel_requested and snapshot.status != "cancelled":
            state = "cancel_pending_recovery"
        elif snapshot.lease_owner_id is not None and not snapshot.lease_expired:
            state = "executing"
        elif snapshot.action_required is not None:
            state = "action_required"
        else:
            state = "idle"
        return RuntimeView(
            state=state,
            cancel_requested=snapshot.cancel_requested,
            lease_owner_id=snapshot.lease_owner_id,
            lease_expires_at=snapshot.lease_expires_at,
            lease_expired=snapshot.lease_expired,
            next_stage=snapshot.next_stage,
            completed_stages=snapshot.completed_stages,
        )

    @staticmethod
    def _cancel_suggested_action(snapshot: RuntimeSnapshot) -> str:
        if snapshot.status in {"completed", "cancelled"}:
            return "none"
        if snapshot.lease_owner_id is not None and not snapshot.lease_expired:
            return "wait_for_executor_or_lease_expiry"
        if snapshot.status != "cancelled":
            return "resume_to_acknowledge_cancel"
        return "none"
