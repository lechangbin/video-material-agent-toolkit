"""Durable audit records for successful and failed model runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from semvideo.application.artifacts import artifact_record
from semvideo.infrastructure.io import atomic_write_json


@dataclass(frozen=True, slots=True)
class ModelRunArtifacts:
    run_path: Path
    raw_path: Path
    usage: dict[str, int]


def aggregate_usage(attempts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(
            int((attempt.get("usage") or {}).get(key) or 0)
            for attempt in attempts
        )
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }


def write_model_run(
    job_root: Path,
    *,
    model_run_id: str,
    purpose: str,
    provider: str,
    model: str,
    prompt_version: str,
    input_hash: str,
    status: str,
    started_at: str,
    duration_ms: int,
    attempts: list[dict[str, Any]],
    repair_attempted: bool,
    failure: dict[str, Any] | None = None,
) -> ModelRunArtifacts:
    """Atomically retain every received response and the terminal run status."""

    raw_path = job_root / "model-runs" / f"{model_run_id}.raw.json"
    atomic_write_json(
        raw_path,
        {
            "schema_version": 1,
            "model_run_id": model_run_id,
            "attempts": attempts,
        },
    )
    raw_artifact = artifact_record(
        job_root,
        raw_path,
        f"model_run_{raw_path.stem}",
    )
    usage = aggregate_usage(attempts)
    trace_id = next(
        (
            str(attempt["trace_id"])
            for attempt in reversed(attempts)
            if attempt.get("trace_id")
        ),
        None,
    )
    run_path = job_root / "model-runs" / f"{model_run_id}.json"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "model_run_id": model_run_id,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "input_hash": input_hash,
        "status": status,
        "usage": usage,
        "trace_id": trace_id,
        "started_at": started_at,
        "duration_ms": duration_ms,
        "repair_attempted": repair_attempted,
        "raw_response_artifact_id": raw_artifact["artifact_id"],
    }
    if failure is not None:
        payload["failure"] = failure
    atomic_write_json(run_path, payload)
    return ModelRunArtifacts(
        run_path=run_path,
        raw_path=raw_path,
        usage=usage,
    )
