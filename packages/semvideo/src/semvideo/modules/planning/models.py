"""Immutable merge-plan output models."""

from __future__ import annotations

from dataclasses import dataclass

from semvideo.domain.timeline import (
    DomainValidationError,
    _require_millisecond,
    _require_non_empty,
)


@dataclass(frozen=True, slots=True)
class FinalSegment:
    """A continuous range formed from one or more adjacent candidates."""

    final_segment_id: str
    ordinal: int
    start_ms: int
    end_ms: int
    candidate_segment_ids: tuple[str, ...]
    removed_boundary_ids: tuple[str, ...] = ()
    review_boundary_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.final_segment_id, "final_segment_id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise DomainValidationError("ordinal must be an integer")
        if self.ordinal < 0:
            raise DomainValidationError("ordinal must be non-negative")
        _require_millisecond(self.start_ms, "start_ms")
        _require_millisecond(self.end_ms, "end_ms", allow_zero=False)
        if self.start_ms >= self.end_ms:
            raise DomainValidationError(
                "final segment must have a non-empty half-open range"
            )

        for field_name in (
            "candidate_segment_ids",
            "removed_boundary_ids",
            "review_boundary_ids",
        ):
            values = tuple(getattr(self, field_name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise DomainValidationError(
                    f"{field_name} must contain non-empty strings"
                )
            if len(values) != len(set(values)):
                raise DomainValidationError(f"{field_name} must not contain duplicates")
            object.__setattr__(self, field_name, values)

        if not self.candidate_segment_ids:
            raise DomainValidationError(
                "final segment must contain at least one candidate segment"
            )
        if set(self.removed_boundary_ids) & set(self.review_boundary_ids):
            raise DomainValidationError(
                "a boundary cannot be both removed and marked for review"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Non-executable data describing the validated final timeline."""

    job_id: str
    candidate_timeline_hash: str
    boundary_decisions_hash: str
    final_segments: tuple[FinalSegment, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_non_empty(self.job_id, "job_id")
        _require_non_empty(self.candidate_timeline_hash, "candidate_timeline_hash")
        _require_non_empty(self.boundary_decisions_hash, "boundary_decisions_hash")
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise DomainValidationError("schema_version must be an integer")
        if self.schema_version != 1:
            raise DomainValidationError("unsupported merge plan schema_version")
        object.__setattr__(self, "final_segments", tuple(self.final_segments))
