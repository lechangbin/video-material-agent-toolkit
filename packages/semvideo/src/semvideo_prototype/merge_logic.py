"""Portable, deterministic merge-plan logic for the prototype."""

from __future__ import annotations

from typing import Any


def build_merge_plan(
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Group adjacent candidates by removing approved candidate boundaries."""
    if not candidates:
        return {"schema_version": 1, "final_segments": []}

    decision_by_boundary = {
        decision["candidate_boundary_id"]: decision for decision in decisions
    }
    groups: list[list[dict[str, Any]]] = [[candidates[0]]]
    removed_by_group: list[list[str]] = [[]]
    review_by_group: list[list[str]] = [[]]

    for candidate in candidates[1:]:
        boundary_id = candidate["left_boundary_id"]
        decision = decision_by_boundary.get(boundary_id)
        if decision and decision["decision"] == "remove":
            groups[-1].append(candidate)
            removed_by_group[-1].append(boundary_id)
        else:
            if decision and decision["decision"] == "review":
                review_by_group[-1].append(boundary_id)
            groups.append([candidate])
            removed_by_group.append([])
            review_by_group.append([])

    final_segments: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        final_segments.append(
            {
                "final_segment_id": f"segment_{index:04d}",
                "ordinal": index - 1,
                "start_ms": group[0]["start_ms"],
                "end_ms": group[-1]["end_ms"],
                "candidate_segment_ids": [
                    candidate["candidate_segment_id"] for candidate in group
                ],
                "removed_boundary_ids": removed_by_group[index - 1],
                "review_boundary_ids": review_by_group[index - 1],
            }
        )

    validate_merge_plan(candidates, final_segments)
    return {"schema_version": 1, "final_segments": final_segments}


def validate_merge_plan(
    candidates: list[dict[str, Any]],
    final_segments: list[dict[str, Any]],
) -> None:
    """Raise ValueError when a plan loses, reorders, overlaps, or gaps input."""
    expected_ids = [candidate["candidate_segment_id"] for candidate in candidates]
    actual_ids = [
        candidate_id
        for segment in final_segments
        for candidate_id in segment["candidate_segment_ids"]
    ]
    if actual_ids != expected_ids:
        raise ValueError("merge plan must preserve every candidate exactly once and in order")

    by_id = {
        candidate["candidate_segment_id"]: candidate for candidate in candidates
    }
    previous_end = candidates[0]["start_ms"] if candidates else 0
    for segment in final_segments:
        members = [by_id[candidate_id] for candidate_id in segment["candidate_segment_ids"]]
        if segment["start_ms"] != members[0]["start_ms"]:
            raise ValueError("final segment start does not match its first candidate")
        if segment["end_ms"] != members[-1]["end_ms"]:
            raise ValueError("final segment end does not match its last candidate")
        if segment["start_ms"] != previous_end:
            raise ValueError("final segments must cover the timeline without gaps")
        for left, right in zip(members, members[1:]):
            if left["end_ms"] != right["start_ms"]:
                raise ValueError("only adjacent candidates may merge")
        previous_end = segment["end_ms"]
