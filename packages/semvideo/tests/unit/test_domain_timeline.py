from __future__ import annotations

from dataclasses import replace

import pytest

from semvideo.domain import (
    BoundaryDecision,
    BoundaryDecisionKind,
    BoundaryRelationship,
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
    DomainValidationError,
    Segment,
)


def make_timeline() -> CandidateTimeline:
    return CandidateTimeline(
        source_video_id="video_001",
        duration_ms=3_000,
        boundaries=(
            CandidateBoundary("boundary_001", 1_000, ("scene_change",)),
            CandidateBoundary("boundary_002", 2_000, ("maximum_interval",)),
        ),
        segments=(
            CandidateSegment("candidate_001", 0, 0, 1_000, None, "boundary_001"),
            CandidateSegment(
                "candidate_002",
                1,
                1_000,
                2_000,
                "boundary_001",
                "boundary_002",
            ),
            CandidateSegment(
                "candidate_003", 2, 2_000, 3_000, "boundary_002", None
            ),
        ),
    )


def test_candidate_segment_uses_integer_millisecond_half_open_range() -> None:
    segment = Segment("candidate_001", 0, 100, 250, None, None)

    assert segment.duration_ms == 150
    assert Segment is CandidateSegment

    with pytest.raises(DomainValidationError, match="non-empty half-open"):
        CandidateSegment("candidate_001", 0, 100, 100, None, None)
    with pytest.raises(DomainValidationError, match="integer millisecond"):
        CandidateSegment("candidate_001", 0, True, 100, None, None)


def test_candidate_timeline_covers_complete_source_without_overlap() -> None:
    timeline = make_timeline()

    assert timeline.internal_boundary_ids == ("boundary_001", "boundary_002")
    assert timeline.segments[0].start_ms == 0
    assert timeline.segments[-1].end_ms == timeline.duration_ms


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"start_ms": 1}, "start at 0"),
        ({"end_ms": 2_999}, "end at the source duration"),
    ],
)
def test_candidate_timeline_rejects_incomplete_coverage(
    replacement: dict[str, int], message: str
) -> None:
    timeline = make_timeline()
    segments = list(timeline.segments)
    target = 0 if "start_ms" in replacement else -1
    segments[target] = replace(segments[target], **replacement)

    with pytest.raises(DomainValidationError, match=message):
        CandidateTimeline(
            timeline.source_video_id,
            timeline.duration_ms,
            timeline.boundaries,
            tuple(segments),
        )


@pytest.mark.parametrize("new_start", [900, 1_100])
def test_candidate_timeline_rejects_overlap_and_gap(new_start: int) -> None:
    timeline = make_timeline()
    segments = list(timeline.segments)
    segments[1] = replace(segments[1], start_ms=new_start)

    with pytest.raises(DomainValidationError, match="without gaps or overlaps"):
        CandidateTimeline(
            timeline.source_video_id,
            timeline.duration_ms,
            timeline.boundaries,
            tuple(segments),
        )


def test_candidate_timeline_rejects_boundary_at_wrong_timestamp() -> None:
    timeline = make_timeline()
    boundaries = (
        replace(timeline.boundaries[0], timestamp_ms=999),
        timeline.boundaries[1],
    )

    with pytest.raises(DomainValidationError, match="timestamp"):
        CandidateTimeline(
            timeline.source_video_id,
            timeline.duration_ms,
            boundaries,
            timeline.segments,
        )


def test_boundary_decision_accepts_wire_enum_values_and_validates_confidence() -> None:
    decision = BoundaryDecision(
        candidate_boundary_id="boundary_001",
        left_segment_id="candidate_001",
        right_segment_id="candidate_002",
        decision="review",
        relationship="insufficient_evidence",
        reason="The evidence window does not show the full action.",
        confidence=0.6,
        review_reasons=("window_edge",),
    )

    assert decision.decision is BoundaryDecisionKind.REVIEW
    assert decision.relationship is BoundaryRelationship.INSUFFICIENT_EVIDENCE

    with pytest.raises(DomainValidationError, match="between 0 and 1"):
        replace(decision, confidence=1.1)
