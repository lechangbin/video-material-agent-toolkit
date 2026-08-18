"""Freeze and restore the non-secret configuration owned by one job."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from semvideo.config import WorkspaceConfig
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class FrozenExecutionConfig:
    """Validated job-owned configuration and its integrity hash."""

    config: WorkspaceConfig
    config_hash: str

    @classmethod
    def freeze(cls, config: WorkspaceConfig) -> FrozenExecutionConfig:
        payload = config.model_dump(mode="json")
        return cls(config=config, config_hash=_canonical_hash(payload))

    @classmethod
    def from_job(
        cls,
        job: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
    ) -> FrozenExecutionConfig:
        job_id = str(job.get("job_id") or "") or None
        raw = job.get("execution_config")
        recorded_hash = job.get("execution_config_hash")
        if not isinstance(raw, Mapping) or not isinstance(recorded_hash, str):
            raise SemvideoError(
                code="job_execution_config_missing",
                category=ErrorCategory.CONFIG,
                message="任务缺少提交时冻结的执行配置，不能安全恢复。",
                recovery=RecoveryAction.REPORT_BUG,
                job_id=job_id,
                attempt_id=attempt_id,
                details={
                    "required_fields": ["execution_config", "execution_config_hash"]
                },
                exit_code=2,
            )
        payload = dict(raw)
        actual_hash = _canonical_hash(payload)
        if actual_hash != recorded_hash:
            raise SemvideoError(
                code="job_execution_config_integrity_failed",
                category=ErrorCategory.CONFIG,
                message="任务冻结配置的完整性校验失败。",
                recovery=RecoveryAction.REPORT_BUG,
                job_id=job_id,
                attempt_id=attempt_id,
                details={
                    "recorded_hash": recorded_hash,
                    "actual_hash": actual_hash,
                },
                exit_code=2,
            )
        try:
            config = WorkspaceConfig.model_validate(payload)
        except ValidationError as exc:
            raise SemvideoError(
                code="job_execution_config_invalid",
                category=ErrorCategory.CONFIG,
                message="任务冻结配置不再满足当前执行契约。",
                recovery=RecoveryAction.REPORT_BUG,
                job_id=job_id,
                attempt_id=attempt_id,
                details={"reason": str(exc)},
                exit_code=2,
            ) from exc
        return cls(config=config, config_hash=recorded_hash)

    def as_job_fields(self) -> dict[str, Any]:
        return {
            "execution_config": self.config.model_dump(mode="json"),
            "execution_config_hash": self.config_hash,
        }
