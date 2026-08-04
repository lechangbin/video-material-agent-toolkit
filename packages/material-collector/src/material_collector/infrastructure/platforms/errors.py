"""Structured errors returned by platform infrastructure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from material_collector.core.errors import CollectorError
from material_collector.core.media import Platform


class PlatformAdapterError(CollectorError):
    """A platform operation failed without exposing credentials or raw responses."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        platform: Platform,
        operation: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_details: dict[str, Any] = {
            "platform": platform.value,
            "operation": operation,
            "retryable": retryable,
        }
        safe_details.update(details or {})
        super().__init__(code, message, details=safe_details)


def schema_changed(platform: Platform, operation: str, field: str) -> PlatformAdapterError:
    """Build a stable error when a documented platform response no longer matches."""

    return PlatformAdapterError(
        "platform_schema_changed",
        "The platform response no longer matches the supported contract.",
        platform=platform,
        operation=operation,
        retryable=False,
        details={"field": field},
    )


def response_rejected(
    platform: Platform,
    operation: str,
    response_code: str,
) -> PlatformAdapterError:
    """Classify public response codes without retaining the raw response."""

    normalized = response_code.casefold()
    if normalized in {"-101", "-100", "unauthorized", "login_required"}:
        return PlatformAdapterError(
            "authentication_lost",
            "The platform rejected the current authenticated session.",
            platform=platform,
            operation=operation,
            retryable=True,
            details={"response_code": response_code},
        )
    if normalized in {"-412", "461", "captcha", "risk_control"}:
        return PlatformAdapterError(
            "challenge_required",
            "The platform requires an interactive challenge.",
            platform=platform,
            operation=operation,
            retryable=True,
            details={"response_code": response_code},
        )
    return PlatformAdapterError(
        "platform_request_rejected",
        "The platform rejected the request.",
        platform=platform,
        operation=operation,
        retryable=True,
        details={"response_code": response_code},
    )
