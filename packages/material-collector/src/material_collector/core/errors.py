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


class ContractVersionError(CollectorError):
    """A versioned input uses a contract version this release will not read."""

    def __init__(
        self,
        document: str,
        received_version: Any,
        *supported_versions: str,
    ) -> None:
        super().__init__(
            "contract_version_unsupported",
            f"{document} schema version is unsupported.",
            details={
                "document": document,
                "received_version": received_version,
                "supported_versions": list(supported_versions),
            },
        )


class SessionNotFoundError(CollectorError):
    """The requested session cannot be found in the supplied workspace."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "session_not_found",
            "The requested collection session does not exist in this workspace.",
            details={"session_id": session_id},
        )


class SessionVersionError(CollectorError):
    """A durable session predates the only schema supported by this release."""

    def __init__(
        self,
        session_id: str,
        received_version: int,
        *supported_versions: int,
    ) -> None:
        super().__init__(
            "session_version_unsupported",
            "The collection session schema version is unsupported.",
            details={
                "session_id": session_id,
                "received_version": received_version,
                "supported_versions": list(supported_versions),
            },
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
