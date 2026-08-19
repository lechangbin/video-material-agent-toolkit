"""Reproducible measurement harness for subagent evidence-tier candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from semvideo.application.subagent_contracts import ContextTier
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import atomic_write_json


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkRun(_Model):
    fixture_id: str
    tier: ContextTier
    context_tokens: int = Field(ge=128_000)
    image_count: int = Field(ge=1)
    serialized_image_tokens: int = Field(ge=1)
    transcript_tokens: int = Field(ge=0)
    cinematography_tokens: int = Field(ge=0)
    request_overhead_tokens: int = Field(ge=0)
    output_reserve_tokens: int = Field(ge=1)
    overlap_tokens: int = Field(ge=0)
    observer_window_count: int = Field(ge=1)
    validation_failures: int = Field(ge=0)
    repair_attempts: int = Field(ge=0, le=2)
    latency_ms: int = Field(ge=0)
    semantic_coverage: float = Field(ge=0, le=1)
    crv_version: str
    semvideo_version: str
    host_metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def total_reserved_tokens(self) -> int:
        return (
            self.serialized_image_tokens
            + self.transcript_tokens
            + self.cinematography_tokens
            + self.request_overhead_tokens
            + self.output_reserve_tokens
            + self.overlap_tokens
        )


class BenchmarkInput(_Model):
    schema_version: str = "subagent-evidence-benchmark-input/v1"
    runs: tuple[BenchmarkRun, ...] = Field(min_length=4)

    @model_validator(mode="after")
    def validate_tiers(self) -> BenchmarkInput:
        if {run.tier for run in self.runs} != set(ContextTier):
            raise ValueError("benchmark must include all four context tiers")
        return self


def build_report(input_path: Path, output_directory: Path) -> dict[str, Any]:
    """Aggregate measurements without inventing production evidence budgets."""

    try:
        benchmark = BenchmarkInput.model_validate_json(
            input_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise _error("subagent_benchmark_input_invalid", "Benchmark input is invalid.") from error
    tiers: dict[str, Any] = {}
    for tier in ContextTier:
        rows = [run for run in benchmark.runs if run.tier is tier]
        overflow = [run.fixture_id for run in rows if run.total_reserved_tokens > run.context_tokens]
        safety_margins = [run.context_tokens - run.total_reserved_tokens for run in rows]
        tiers[tier.value] = {
            "sample_count": len(rows),
            "max_images_observed": max(run.image_count for run in rows),
            "minimum_safety_margin_tokens": min(safety_margins),
            "maximum_observer_window_count": max(run.observer_window_count for run in rows),
            "minimum_semantic_coverage": min(run.semantic_coverage for run in rows),
            "validation_failure_count": sum(run.validation_failures for run in rows),
            "repair_attempt_count": sum(run.repair_attempts for run in rows),
            "maximum_latency_ms": max(run.latency_ms for run in rows),
            "overflow_fixture_ids": overflow,
            "crv_versions": sorted({run.crv_version for run in rows}),
            "semvideo_versions": sorted({run.semvideo_version for run in rows}),
        }
    status = (
        "measured"
        if not any(row["overflow_fixture_ids"] for row in tiers.values())
        else "overflow"
    )
    report = {
        "schema_version": "subagent-evidence-benchmark-result/v1",
        "status": status,
        "tiers": tiers,
        "approval": {
            "status": "pending_human_approval",
            "note": "Convert measured values to profiles only after experiment review.",
        },
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "benchmark-result.json"
    atomic_write_json(json_path, report)
    markdown_path = output_directory / "benchmark-report.md"
    lines = [
        "# Subagent evidence benchmark",
        "",
        f"Status: `{status}`. Production profile approval remains a human decision.",
        "",
        "| Tier | Samples | Max images | Min safety margin | Min coverage | Overflow |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for tier_name, row in tiers.items():
        lines.append(
            f"| {tier_name} | {row['sample_count']} | {row['max_images_observed']} | "
            f"{row['minimum_safety_margin_tokens']} | {row['minimum_semantic_coverage']:.3f} | "
            f"{', '.join(row['overflow_fixture_ids']) or 'none'} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        **report,
        "result_path": str(json_path),
        "report_path": str(markdown_path),
    }


def _error(code: str, message: str) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.INPUT,
        message=message,
        recovery=RecoveryAction.USER_ACTION,
        exit_code=3,
    )
