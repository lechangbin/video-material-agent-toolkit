from __future__ import annotations

from dataclasses import replace

import pytest

from semvideo.domain import (
    BoundaryDecision,
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
    DomainValidationError,
)
from semvideo.modules.planning import (
    FinalSegment,
    MergePlan,
    build_merge_plan,
    validate_merge_plan,
)


def make_timeline() -> CandidateTimeline:
    boundaries = tuple(
        CandidateBoundary(f"boundary_{index:03d}", index * 1_000)
        for index in range(1, 4)
    )
    segments = (
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
            "candidate_003",
            2,
            2_000,
            3_000,
            "boundary_002",
            "boundary_003",
        ),
        CandidateSegment(
            "candidate_004", 3, 3_000, 4_000, "boundary_003", None
        ),
    )
    return CandidateTimeline("video_001", 4_000, boundaries, segments)


def decision(
    boundary_number: int,
    kind: str,
    *,
    left_number: int | None = None,
    right_number: int | None = None,
) -> BoundaryDecision:
    left_number = left_number or boundary_number
    right_number = right_number or boundary_number + 1
    relationship = {
        "remove": "same_continuous_event",
        "keep": "same_topic_new_event",
        "review": "insufficient_evidence",
    }[kind]
    return BoundaryDecision(
        candidate_boundary_id=f"boundary_{boundary_number:03d}",
        left_segment_id=f"candidate_{left_number:03d}",
        right_segment_id=f"candidate_{right_number:03d}",
        decision=kind,
        relationship=relationship,
        reason=f"test {kind}",
        review_reasons=("insufficient_context",) if kind == "review" else (),
    )


def make_decisions() -> tuple[BoundaryDecision, ...]:
    return (
        decision(1, "remove"),
        decision(2, "review"),
        decision(3, "keep"),
    )


def build_default_plan() -> MergePlan:
    return build_merge_plan(
        make_timeline(),
        make_decisions(),
        job_id="job_001",
        candidate_timeline_hash="sha256:timeline",
        boundary_decisions_hash="sha256:decisions",
    )


def test_build_merge_plan_merges_only_removed_adjacent_boundary() -> None:
    plan = build_default_plan()

    assert [segment.candidate_segment_ids for segment in plan.final_segments] == [
        ("candidate_001", "candidate_002"),
        ("candidate_003",),
        ("candidate_004",),
    ]
    assert [
        (segment.start_ms, segment.end_ms) for segment in plan.final_segments
    ] == [(0, 2_000), (2_000, 3_000), (3_000, 4_000)]
    assert plan.final_segments[0].removed_boundary_ids == ("boundary_001",)


def test_review_boundary_is_preserved_and_attached_to_left_final_segment() -> None:
    plan = build_default_plan()

    assert plan.final_segments[0].review_boundary_ids == ("boundary_002",)
    assert "boundary_002" not in {
        boundary_id
        for segment in plan.final_segments
        for boundary_id in segment.removed_boundary_ids
    }


def test_build_merge_plan_requires_one_decision_per_boundary() -> None:
    with pytest.raises(DomainValidationError, match="cover every candidate boundary"):
        build_merge_plan(
            make_timeline(),
            make_decisions()[:-1],
            job_id="job_001",
            candidate_timeline_hash="sha256:timeline",
            boundary_decisions_hash="sha256:decisions",
        )

    duplicated = (*make_decisions(), make_decisions()[0])
    with pytest.raises(DomainValidationError, match="exactly one"):
        build_merge_plan(
            make_timeline(),
            duplicated,
            job_id="job_001",
            candidate_timeline_hash="sha256:timeline",
            boundary_decisions_hash="sha256:decisions",
        )


def test_build_merge_plan_rejects_decision_for_wrong_segment_pair() -> None:
    decisions = (
        decision(1, "remove", left_number=2, right_number=3),
        *make_decisions()[1:],
    )

    with pytest.raises(DomainValidationError, match="left segment"):
        build_merge_plan(
            make_timeline(),
            decisions,
            job_id="job_001",
            candidate_timeline_hash="sha256:timeline",
            boundary_decisions_hash="sha256:decisions",
        )


def test_validate_rejects_silently_removed_review_boundary() -> None:
    valid = build_default_plan()
    invalid_first = FinalSegment(
        final_segment_id="segment_0001",
        ordinal=0,
        start_ms=0,
        end_ms=3_000,
        candidate_segment_ids=(
            "candidate_001",
            "candidate_002",
            "candidate_003",
        ),
        removed_boundary_ids=("boundary_001", "boundary_002"),
    )
    invalid = replace(
        valid,
        final_segments=(
            invalid_first,
            replace(
                valid.final_segments[-1],
                ordinal=1,
            ),
        ),
    )

    with pytest.raises(DomainValidationError, match="only a removed boundary"):
        validate_merge_plan(invalid, make_timeline(), make_decisions())


def test_validate_rejects_lost_or_reordered_candidate() -> None:
    valid = build_default_plan()
    invalid = replace(
        valid,
        final_segments=(
            replace(
                valid.final_segments[0],
                candidate_segment_ids=("candidate_002", "candidate_001"),
            ),
            *valid.final_segments[1:],
        ),
    )

    with pytest.raises(DomainValidationError, match="exactly once and in order"):
        validate_merge_plan(invalid, make_timeline(), make_decisions())


def test_validate_rejects_overlap_and_out_of_bounds() -> None:
    valid = build_default_plan()
    overlap = replace(
        valid,
        final_segments=(
            valid.final_segments[0],
            replace(valid.final_segments[1], start_ms=1_999),
            valid.final_segments[2],
        ),
    )
    with pytest.raises(DomainValidationError, match="without gaps or overlaps"):
        validate_merge_plan(overlap, make_timeline(), make_decisions())

    out_of_bounds_segment = FinalSegment(
        final_segment_id="segment_0003",
        ordinal=2,
        start_ms=3_000,
        end_ms=4_001,
        candidate_segment_ids=("candidate_004",),
    )
    out_of_bounds = replace(
        valid,
        final_segments=(*valid.final_segments[:2], out_of_bounds_segment),
    )
    with pytest.raises(DomainValidationError, match="exceed the source duration"):
        validate_merge_plan(out_of_bounds, make_timeline(), make_decisions())
