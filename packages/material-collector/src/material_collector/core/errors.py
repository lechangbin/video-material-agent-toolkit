"""Structured application errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class CollectorError(Exception):
    """Base error crossing the application interface."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class ContractError(CollectorError):
    """A versioned input contract is invalid."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("contract_invalid", message, details=details)


class SessionNotFoundError(CollectorError):
    """The requested session cannot be found in the supplied workspace."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_not_found",
            "The requested collection session does not exist in this workspace.",
            details={"session_id": session_id},
        )


class WorkspaceError(CollectorError):
    """The supplied material workspace cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("workspace_invalid", message, details=details)


class SessionStateError(CollectorError):
    """A session exists but its durable state cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("session_state_invalid", message, details=details)
