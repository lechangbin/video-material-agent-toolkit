"""Deterministic source-timeline domain objects.

All time values are integer milliseconds on the source-video timeline.
Ranges use half-open semantics: ``[start_ms, end_ms)``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


class DomainValidationError(ValueError):
    """Raised when a domain invariant is violated."""


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")


def _require_millisecond(value: int, field_name: str, *, allow_zero: bool = True) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an integer millisecond value")
    if value < 0 or (not allow_zero and value == 0):
        qualifier = "positive" if not allow_zero else "non-negative"
        raise DomainValidationError(f"{field_name} must be {qualifier}")


@dataclass(frozen=True, slots=True)
class CandidateBoundary:
    """A deterministic time anchor proposed as a possible semantic boundary."""

    candidate_boundary_id: str
    timestamp_ms: int
    reasons: tuple[str, ...] = ()
    scores: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.candidate_boundary_id, "candidate_boundary_id")
        _require_millisecond(self.timestamp_ms, "timestamp_ms")

        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
            raise DomainValidationError("boundary reasons must be non-empty strings")
        object.__setattr__(self, "reasons", reasons)

        scores = dict(self.scores)
        for name, score in scores.items():
            if not isinstance(name, str) or not name.strip():
                raise DomainValidationError("boundary score names must be non-empty strings")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise DomainValidationError("boundary scores must be numeric")
        object.__setattr__(self, "scores", scores)


@dataclass(frozen=True, slots=True)
class CandidateSegment:
    """The smallest timeline range between adjacent candidate boundaries."""

    candidate_segment_id: str
    ordinal: int
    start_ms: int
    end_ms: int
    left_boundary_id: str | None
    right_boundary_id: str | None

    def __post_init__(self) -> None:
        _require_non_empty(self.candidate_segment_id, "candidate_segment_id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise DomainValidationError("ordinal must be an integer")
        if self.ordinal < 0:
            raise DomainValidationError("ordinal must be non-negative")
        _require_millisecond(self.start_ms, "start_ms")
        _require_millisecond(self.end_ms, "end_ms", allow_zero=False)
        if self.start_ms >= self.end_ms:
            raise DomainValidationError(
                "candidate segment must have a non-empty half-open range"
            )
        for field_name, boundary_id in (
            ("left_boundary_id", self.left_boundary_id),
            ("right_boundary_id", self.right_boundary_id),
        ):
            if boundary_id is not None:
                _require_non_empty(boundary_id, field_name)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


# ``Segment`` is the concise programmatic name for the domain's Candidate Segment.
# Keeping the formal name available avoids ambiguity at persistence boundaries.
Segment = CandidateSegment


@dataclass(frozen=True, slots=True)
class CandidateTimeline:
    """A fully validated partition of one source video's timeline."""

    source_video_id: str
    duration_ms: int
    boundaries: tuple[CandidateBoundary, ...]
    segments: tuple[CandidateSegment, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.source_video_id, "source_video_id")
        _require_millisecond(self.duration_ms, "duration_ms", allow_zero=False)
        object.__setattr__(self, "boundaries", tuple(self.boundaries))
        object.__setattr__(self, "segments", tuple(self.segments))
        self._validate()

    def _validate(self) -> None:
        if not self.segments:
            raise DomainValidationError("candidate timeline must contain a segment")

        segment_ids = [segment.candidate_segment_id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise DomainValidationError("candidate segment ids must be unique")

        boundary_ids = [boundary.candidate_boundary_id for boundary in self.boundaries]
        if len(boundary_ids) != len(set(boundary_ids)):
            raise DomainValidationError("candidate boundary ids must be unique")
        boundary_by_id = {
            boundary.candidate_boundary_id: boundary for boundary in self.boundaries
        }

        if self.segments[0].start_ms != 0:
            raise DomainValidationError("candidate timeline must start at 0")
        if self.segments[-1].end_ms != self.duration_ms:
            raise DomainValidationError(
                "candidate timeline must end at the source duration"
            )
        if self.segments[0].left_boundary_id is not None:
            raise DomainValidationError(
                "first candidate segment cannot have a left boundary"
            )
        if self.segments[-1].right_boundary_id is not None:
            raise DomainValidationError(
                "last candidate segment cannot have a right boundary"
            )

        used_boundary_ids: list[str] = []
        for expected_ordinal, segment in enumerate(self.segments):
            if segment.ordinal != expected_ordinal:
                raise DomainValidationError(
                    "candidate segment ordinals must be contiguous from 0"
                )
            if segment.end_ms > self.duration_ms:
                raise DomainValidationError(
                    "candidate segment cannot exceed the source duration"
                )

        for left, right in zip(self.segments, self.segments[1:]):
            if left.end_ms != right.start_ms:
                raise DomainValidationError(
                    "candidate segments must cover the timeline without gaps or overlaps"
                )
            if left.right_boundary_id is None:
                raise DomainValidationError(
                    "an internal candidate edge must reference a boundary"
                )
            if left.right_boundary_id != right.left_boundary_id:
                raise DomainValidationError(
                    "adjacent candidate segments must share the same boundary"
                )
            boundary = boundary_by_id.get(left.right_boundary_id)
            if boundary is None:
                raise DomainValidationError(
                    "candidate segment references an unknown boundary"
                )
            if boundary.timestamp_ms != left.end_ms:
                raise DomainValidationError(
                    "candidate boundary timestamp must equal its shared segment edge"
                )
            used_boundary_ids.append(boundary.candidate_boundary_id)

        if set(used_boundary_ids) != set(boundary_ids):
            raise DomainValidationError(
                "every candidate boundary must define exactly one internal segment edge"
            )
        if len(used_boundary_ids) != len(boundary_ids):
            raise DomainValidationError(
                "a candidate boundary cannot define more than one segment edge"
            )

    @property
    def internal_boundary_ids(self) -> tuple[str, ...]:
        return tuple(
            segment.right_boundary_id
            for segment in self.segments[:-1]
            if segment.right_boundary_id is not None
        )
