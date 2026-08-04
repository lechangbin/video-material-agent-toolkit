"""Turn model segmentation proposals into validated deterministic decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from semvideo.domain import (
    BoundaryDecision,
    BoundaryDecisionKind,
    BoundaryRelationship,
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
    DomainValidationError,
)
from semvideo.modules.planning import MergePlan, build_merge_plan

from .models import SegmentationResponse
from .proposals import validate_response_timeline

_REVIEW_CONFIDENCE_THRESHOLD = 0.65


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def timeline_from_anchors(
    *,
    source_video_id: str,
    duration_ms: int,
    anchors: list[dict[str, Any]],
) -> CandidateTimeline:
    if len(anchors) < 2:
        raise DomainValidationError("at least source start and end anchors are required")
    ordered = sorted(anchors, key=lambda row: int(row["timestamp_ms"]))
    if int(ordered[0]["timestamp_ms"]) != 0:
        raise DomainValidationError("first anchor must be source start")
    if int(ordered[-1]["timestamp_ms"]) != duration_ms:
        raise DomainValidationError("last anchor must be source end")
    internal_boundaries = tuple(
        CandidateBoundary(
            candidate_boundary_id=str(row["anchor_id"]),
            timestamp_ms=int(row["timestamp_ms"]),
            reasons=tuple(row.get("reasons") or ()),
            scores=dict(row.get("scores") or {}),
        )
        for row in ordered[1:-1]
    )
    segments = tuple(
        CandidateSegment(
            candidate_segment_id=f"candidate_{index + 1:04d}",
            ordinal=index,
            start_ms=int(left["timestamp_ms"]),
            end_ms=int(right["timestamp_ms"]),
            left_boundary_id=(
                None if index == 0 else str(left["anchor_id"])
            ),
            right_boundary_id=(
                None if index == len(ordered) - 2 else str(right["anchor_id"])
            ),
        )
        for index, (left, right) in enumerate(zip(ordered, ordered[1:]))
    )
    return CandidateTimeline(
        source_video_id=source_video_id,
        duration_ms=duration_ms,
        boundaries=internal_boundaries,
        segments=segments,
    )


def proposal_to_plan(
    *,
    job_id: str,
    model_run_id: str,
    response: SegmentationResponse,
    timeline: CandidateTimeline,
    anchors: list[dict[str, Any]],
) -> tuple[MergePlan, list[dict[str, Any]], list[BoundaryDecision]]:
    try:
        validate_response_timeline(response, anchors)
    except ValueError as exc:
        raise DomainValidationError(str(exc)) from exc
    anchor_by_id = {str(row["anchor_id"]): row for row in anchors}
    selected_boundary_ids: list[str] = []
    normalized_rows: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(response.segments):
        start_ms = int(anchor_by_id[segment.start_anchor_id]["timestamp_ms"])
        end_ms = int(anchor_by_id[segment.end_anchor_id]["timestamp_ms"])
        if ordinal:
            selected_boundary_ids.append(segment.start_anchor_id)
        normalized_rows.append(
            {
                **segment.model_dump(mode="json"),
                "ordinal": ordinal,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )

    selected = set(selected_boundary_ids)
    confidence_by_anchor_range = [
        (
            int(row["start_ms"]),
            int(row["end_ms"]),
            float(row["confidence"]),
        )
        for row in normalized_rows
    ]
    decisions: list[BoundaryDecision] = []
    for left, right in zip(timeline.segments, timeline.segments[1:]):
        boundary_id = left.right_boundary_id
        assert boundary_id is not None
        keep = boundary_id in selected
        boundary_ms = left.end_ms
        touching_confidences = [
            confidence
            for start_ms, end_ms, confidence in confidence_by_anchor_range
            if start_ms < boundary_ms < end_ms
            or start_ms == boundary_ms
            or end_ms == boundary_ms
        ]
        confidence = min(touching_confidences) if touching_confidences else 0.0
        review = confidence < _REVIEW_CONFIDENCE_THRESHOLD
        decisions.append(
            BoundaryDecision(
                candidate_boundary_id=boundary_id,
                left_segment_id=left.candidate_segment_id,
                right_segment_id=right.candidate_segment_id,
                decision=(
                    BoundaryDecisionKind.REVIEW
                    if review
                    else BoundaryDecisionKind.KEEP
                    if keep
                    else BoundaryDecisionKind.REMOVE
                ),
                relationship=(
                    BoundaryRelationship.INSUFFICIENT_EVIDENCE
                    if review
                    else
                    BoundaryRelationship.DIFFERENT_TOPIC
                    if keep
                    else BoundaryRelationship.SAME_CONTINUOUS_EVENT
                ),
                reason=(
                    "相邻语义判断置信度不足，保留边界并要求复核。"
                    if review
                    else
                    "模型将该锚点选为叙事事件边界。"
                    if keep
                    else "模型在跨镜头上下文中将该锚点视为同一连续事件。"
                ),
                model_run_id=model_run_id,
                confidence=confidence,
                review_reasons=(
                    ("low_model_confidence",) if review else ()
                ),
            )
        )
    timeline_payload = {
        "source_video_id": timeline.source_video_id,
        "duration_ms": timeline.duration_ms,
        "boundaries": [asdict(item) for item in timeline.boundaries],
        "segments": [asdict(item) for item in timeline.segments],
    }
    decision_payload = [asdict(item) for item in decisions]
    plan = build_merge_plan(
        timeline,
        decisions,
        job_id=job_id,
        candidate_timeline_hash=canonical_hash(timeline_payload),
        boundary_decisions_hash=canonical_hash(decision_payload),
    )
    summaries: list[dict[str, Any]] = []
    for final_segment in plan.final_segments:
        row = next(
            (
                candidate
                for candidate in normalized_rows
                if candidate["start_ms"] <= final_segment.start_ms
                and candidate["end_ms"] >= final_segment.end_ms
            ),
            None,
        )
        if row is None:
            raise DomainValidationError(
                "deterministic merge plan range is not covered by a model proposal"
            )
        review_reasons = (
            ["low_model_confidence"]
            if final_segment.review_boundary_ids
            or float(row["confidence"]) < _REVIEW_CONFIDENCE_THRESHOLD
            else []
        )
        summaries.append(
            {
                "schema_version": 1,
                "final_segment_id": final_segment.final_segment_id,
                "model_run_id": model_run_id,
                "title": row["title"],
                "short_summary": row["short_summary"],
                "detailed_summary": row["detailed_summary"],
                "visual_summary": row["visual_summary"],
                "topics": row["topics"],
                "participants": row["participants"],
                "locations": row["locations"],
                "organizations": row["organizations"],
                "objects": row["objects"],
                "actions": row["actions"],
                "keywords": row["keywords"],
                "source_candidate_segment_ids": list(
                    final_segment.candidate_segment_ids
                ),
                "confidence": row["confidence"],
                "review_reasons": review_reasons,
                "reason": row["reason"],
                "event": row["event"],
            }
        )
    return plan, summaries, decisions
