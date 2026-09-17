"""Stable structured errors shared by the CLI, Worker, and Agent Skill."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCategory(StrEnum):
    INVOCATION = "invocation_error"
    CONFIG = "config_error"
    INPUT = "input_error"
    DEPENDENCY = "dependency_error"
    CREDENTIAL = "credential_error"
    PROVIDER_TRANSIENT = "provider_transient"
    RESOURCE_TRANSIENT = "resource_transient"
    MODEL_RESPONSE_INVALID = "model_response_invalid"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    INTERNAL = "internal_error"


class RecoveryAction(StrEnum):
    CORRECT_AND_RETRY = "correct_and_retry"
    RETRY_SAME = "retry_same"
    RESUME = "resume"
    USER_ACTION = "user_action"
    REPORT_BUG = "report_bug"
    NONE = "none"


class ErrorPayload(BaseModel):
    schema_version: int = 1
    code: str
    category: ErrorCategory
    message: str
    retryable: bool
    recovery: RecoveryAction
    stage: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    entity_id: str | None = None
    diagnostic_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SemvideoError(Exception):
    """Expected error carrying the stable machine-readable contract."""

    exit_code = 6

    def __init__(
        self,
        *,
        code: str,
        category: ErrorCategory,
        message: str,
        retryable: bool = False,
        recovery: RecoveryAction = RecoveryAction.USER_ACTION,
        stage: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_attempts: list[dict[str, Any]] = []
        self.model_attempts: list[dict[str, Any]] = []
        self.repair_attempted = False
        self.payload = ErrorPayload(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            recovery=recovery,
            stage=stage,
            job_id=job_id,
            attempt_id=attempt_id,
            entity_id=entity_id,
            details=details or {},
        )
        if exit_code is not None:
            self.exit_code = exit_code

    def with_context(
        self,
        *,
        job_id: str | None = None,
        attempt_id: str | None = None,
        stage: str | None = None,
    ) -> SemvideoError:
        update = {
            "job_id": job_id or self.payload.job_id,
            "attempt_id": attempt_id or self.payload.attempt_id,
            "stage": stage or self.payload.stage,
        }
        self.payload = self.payload.model_copy(update=update)
        return self

    def as_dict(self) -> dict[str, Any]:
        return self.payload.model_dump(mode="json", exclude_none=True)


def input_error(code: str, message: str, **details: Any) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.INPUT,
        message=message,
        recovery=RecoveryAction.USER_ACTION,
        details=details,
        exit_code=3,
    )


def config_error(code: str, message: str, **details: Any) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.CONFIG,
        message=message,
        retryable=True,
        recovery=RecoveryAction.CORRECT_AND_RETRY,
        details=details,
        exit_code=2,
    )
