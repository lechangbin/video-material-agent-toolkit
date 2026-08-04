"""Build and validate merge plans without media or model side effects."""

from __future__ import annotations

from collections.abc import Sequence

from semvideo.domain import (
    BoundaryDecision,
    BoundaryDecisionKind,
    CandidateSegment,
    CandidateTimeline,
    DomainValidationError,
)

from .models import FinalSegment, MergePlan


def _index_and_validate_decisions(
    timeline: CandidateTimeline,
    decisions: Sequence[BoundaryDecision],
) -> dict[str, BoundaryDecision]:
    decision_by_boundary: dict[str, BoundaryDecision] = {}
    for decision in decisions:
        if decision.candidate_boundary_id in decision_by_boundary:
            raise DomainValidationError(
                "each candidate boundary must have exactly one boundary decision"
            )
        decision_by_boundary[decision.candidate_boundary_id] = decision

    expected = set(timeline.internal_boundary_ids)
    actual = set(decision_by_boundary)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise DomainValidationError(
            "boundary decisions must cover every candidate boundary exactly once"
            + (f" ({', '.join(details)})" if details else "")
        )

    for left, right in zip(timeline.segments, timeline.segments[1:]):
        boundary_id = left.right_boundary_id
        assert boundary_id is not None
        decision = decision_by_boundary[boundary_id]
        if decision.left_segment_id != left.candidate_segment_id:
            raise DomainValidationError(
                "boundary decision left segment does not match the candidate timeline"
            )
        if decision.right_segment_id != right.candidate_segment_id:
            raise DomainValidationError(
                "boundary decision right segment does not match the candidate timeline"
            )
    return decision_by_boundary


def build_merge_plan(
    timeline: CandidateTimeline,
    decisions: Sequence[BoundaryDecision],
    *,
    job_id: str,
    candidate_timeline_hash: str,
    boundary_decisions_hash: str,
) -> MergePlan:
    """Group adjacent candidates only when their shared boundary is removed."""

    decision_by_boundary = _index_and_validate_decisions(timeline, decisions)
    groups: list[list[CandidateSegment]] = [[timeline.segments[0]]]
    removed_by_group: list[list[str]] = [[]]
    review_by_group: list[list[str]] = [[]]

    for candidate in timeline.segments[1:]:
        boundary_id = candidate.left_boundary_id
        assert boundary_id is not None
        decision = decision_by_boundary[boundary_id]
        if decision.decision is BoundaryDecisionKind.REMOVE:
            groups[-1].append(candidate)
            removed_by_group[-1].append(boundary_id)
            continue

        # KEEP and REVIEW both preserve the timeline boundary. A REVIEW marker is
        # attached to the final segment on its left, matching timeline order.
        if decision.decision is BoundaryDecisionKind.REVIEW:
            review_by_group[-1].append(boundary_id)
        groups.append([candidate])
        removed_by_group.append([])
        review_by_group.append([])

    final_segments = tuple(
        FinalSegment(
            final_segment_id=f"segment_{index + 1:04d}",
            ordinal=index,
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
            candidate_segment_ids=tuple(
                candidate.candidate_segment_id for candidate in group
            ),
            removed_boundary_ids=tuple(removed_by_group[index]),
            review_boundary_ids=tuple(review_by_group[index]),
        )
        for index, group in enumerate(groups)
    )
    plan = MergePlan(
        job_id=job_id,
        candidate_timeline_hash=candidate_timeline_hash,
        boundary_decisions_hash=boundary_decisions_hash,
        final_segments=final_segments,
    )
    validate_merge_plan(plan, timeline, decisions)
    return plan


def validate_merge_plan(
    plan: MergePlan,
    timeline: CandidateTimeline,
    decisions: Sequence[BoundaryDecision],
) -> None:
    """Reject loss, reorder, overlap, gaps, non-adjacent merge, and review removal."""

    decision_by_boundary = _index_and_validate_decisions(timeline, decisions)
    if not plan.final_segments:
        raise DomainValidationError("merge plan must contain a final segment")

    expected_candidate_ids = [
        candidate.candidate_segment_id for candidate in timeline.segments
    ]
    actual_candidate_ids = [
        candidate_id
        for final_segment in plan.final_segments
        for candidate_id in final_segment.candidate_segment_ids
    ]
    if actual_candidate_ids != expected_candidate_ids:
        raise DomainValidationError(
            "merge plan must preserve every candidate exactly once and in order"
        )

    candidate_by_id = {
        candidate.candidate_segment_id: candidate
        for candidate in timeline.segments
    }
    removed_seen: list[str] = []
    review_seen: list[str] = []
    previous_end = 0

    for expected_ordinal, final_segment in enumerate(plan.final_segments):
        if final_segment.ordinal != expected_ordinal:
            raise DomainValidationError(
                "final segment ordinals must be contiguous from 0"
            )
        if final_segment.start_ms != previous_end:
            raise DomainValidationError(
                "final segments must cover the timeline without gaps or overlaps"
            )
        if final_segment.end_ms > timeline.duration_ms:
            raise DomainValidationError(
                "final segment cannot exceed the source duration"
            )

        members = [
            candidate_by_id[candidate_id]
            for candidate_id in final_segment.candidate_segment_ids
        ]
        if final_segment.start_ms != members[0].start_ms:
            raise DomainValidationError(
                "final segment start must match its first candidate"
            )
        if final_segment.end_ms != members[-1].end_ms:
            raise DomainValidationError(
                "final segment end must match its last candidate"
            )

        expected_removed: list[str] = []
        for left, right in zip(members, members[1:]):
            if left.end_ms != right.start_ms:
                raise DomainValidationError("only adjacent candidates may merge")
            boundary_id = left.right_boundary_id
            if boundary_id is None or boundary_id != right.left_boundary_id:
                raise DomainValidationError("only adjacent candidates may merge")
            if (
                decision_by_boundary[boundary_id].decision
                is not BoundaryDecisionKind.REMOVE
            ):
                raise DomainValidationError(
                    "a final segment may cross only a removed boundary"
                )
            expected_removed.append(boundary_id)

        if list(final_segment.removed_boundary_ids) != expected_removed:
            raise DomainValidationError(
                "removed boundary ids must exactly describe merged candidate edges"
            )

        for boundary_id in final_segment.review_boundary_ids:
            decision = decision_by_boundary.get(boundary_id)
            if decision is None or decision.decision is not BoundaryDecisionKind.REVIEW:
                raise DomainValidationError(
                    "review boundary ids must reference review decisions"
                )
            if boundary_id != members[-1].right_boundary_id:
                raise DomainValidationError(
                    "a review boundary must be attached to the final segment on its left"
                )

        removed_seen.extend(final_segment.removed_boundary_ids)
        review_seen.extend(final_segment.review_boundary_ids)
        previous_end = final_segment.end_ms

    if previous_end != timeline.duration_ms:
        raise DomainValidationError(
            "final segments must cover the full source duration"
        )

    expected_removed = [
        boundary_id
        for boundary_id in timeline.internal_boundary_ids
        if decision_by_boundary[boundary_id].decision is BoundaryDecisionKind.REMOVE
    ]
    expected_review = [
        boundary_id
        for boundary_id in timeline.internal_boundary_ids
        if decision_by_boundary[boundary_id].decision is BoundaryDecisionKind.REVIEW
    ]
    if removed_seen != expected_removed:
        raise DomainValidationError(
            "merge plan must account for every removed boundary exactly once"
        )
    if review_seen != expected_review:
        raise DomainValidationError(
            "merge plan must preserve every review boundary exactly once"
        )
