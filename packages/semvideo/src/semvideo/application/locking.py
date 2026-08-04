"""Application-level mapping for transient local lock failures."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.locks import (
    LockAcquisitionTimeout,
    exclusive_file_lock,
)


@contextmanager
def application_file_lock(
    path: Path,
    *,
    timeout: float = 30.0,
) -> Iterator[None]:
    """Expose infrastructure lock contention as a stable application error."""

    try:
        with exclusive_file_lock(path, timeout=timeout):
            yield
    except LockAcquisitionTimeout as exc:
        raise SemvideoError(
            code="lock_acquisition_timeout",
            category=ErrorCategory.RESOURCE_TRANSIENT,
            message="等待本地共享资源锁超时。",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            details={"lock_name": path.name},
            exit_code=6,
        ) from exc
