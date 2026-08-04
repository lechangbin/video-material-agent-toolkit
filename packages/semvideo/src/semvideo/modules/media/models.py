"""JSON-friendly candidate timeline domain values."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CandidateBoundary:
    candidate_boundary_id: str
    timestamp_ms: int
    reasons: tuple[str, ...]
    scores: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


@dataclass(frozen=True, slots=True)
class CandidateSegment:
    candidate_segment_id: str
    ordinal: int
    start_ms: int
    end_ms: int
    left_boundary_id: str | None
    right_boundary_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CandidateTimeline:
    schema_version: int
    source_video_id: str
    duration_ms: int
    algorithm: dict[str, Any]
    boundaries: tuple[CandidateBoundary, ...]
    segments: tuple[CandidateSegment, ...]

    def __post_init__(self) -> None:
        if self.duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        expected_start = 0
        for ordinal, segment in enumerate(self.segments):
            if segment.ordinal != ordinal:
                raise ValueError("candidate ordinals must be contiguous")
            if segment.start_ms != expected_start or segment.end_ms <= segment.start_ms:
                raise ValueError("candidate segments must continuously cover the timeline")
            expected_start = segment.end_ms
        if not self.segments or expected_start != self.duration_ms:
            raise ValueError("candidate segments must end at duration_ms")
        if len(self.boundaries) != len(self.segments) - 1:
            raise ValueError("each adjacent segment pair must share one boundary")
        for index, boundary in enumerate(self.boundaries):
            left = self.segments[index]
            right = self.segments[index + 1]
            if left.end_ms != boundary.timestamp_ms or right.start_ms != boundary.timestamp_ms:
                raise ValueError("boundary timestamp must match adjacent segments")
            if left.right_boundary_id != boundary.candidate_boundary_id:
                raise ValueError("left segment has incorrect right boundary")
            if right.left_boundary_id != boundary.candidate_boundary_id:
                raise ValueError("right segment has incorrect left boundary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_video_id": self.source_video_id,
            "duration_ms": self.duration_ms,
            "algorithm": self.algorithm,
            "boundaries": [item.to_dict() for item in self.boundaries],
            "segments": [item.to_dict() for item in self.segments],
        }
