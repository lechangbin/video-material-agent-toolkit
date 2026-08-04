"""Structured progress events shared by Worker and CLI observers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeAlias

from semvideo.application.task_store import utc_now

ProgressSink: TypeAlias = Callable[[dict[str, Any]], None]


def stage_progress(
    *,
    job_id: str,
    stage: str,
    completed_units: int,
    total_units: int,
    unit: str = "pipeline_stage",
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event": "stage_progress",
        "job_id": job_id,
        "stage": stage,
        "completed_units": completed_units,
        "total_units": total_units,
        "unit": unit,
        "message": message,
        "timestamp": utc_now(),
    }
