from __future__ import annotations

import pytest
from semvideo.application.execution_config import FrozenExecutionConfig
from semvideo.config import WorkspaceConfig
from semvideo.errors import SemvideoError


def test_frozen_execution_config_round_trips_independently() -> None:
    frozen = FrozenExecutionConfig.freeze(WorkspaceConfig())
    job = {"job_id": "job_001", **frozen.as_job_fields()}

    restored = FrozenExecutionConfig.from_job(job, attempt_id="attempt_001")

    assert restored.config == WorkspaceConfig()
    assert restored.config_hash == frozen.config_hash


def test_frozen_execution_config_rejects_missing_and_tampered_jobs() -> None:
    with pytest.raises(SemvideoError) as missing:
        FrozenExecutionConfig.from_job({"job_id": "job_missing"})
    assert missing.value.payload.code == "job_execution_config_missing"

    frozen = FrozenExecutionConfig.freeze(WorkspaceConfig())
    job = {"job_id": "job_bad", **frozen.as_job_fields()}
    job["execution_config"]["profile"] = "tampered"
    with pytest.raises(SemvideoError) as tampered:
        FrozenExecutionConfig.from_job(job)
    assert tampered.value.payload.code == "job_execution_config_integrity_failed"
