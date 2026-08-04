"""Stable semantic-proposal persistence and timeline validation."""

from __future__ import annotations

from typing import Any

from semvideo.modules.semantics.models import (
    SegmentationResponse,
    SemanticSegment,
)


def validate_response_timeline(
    response: SegmentationResponse,
    anchors: list[dict[str, Any]],
) -> None:
    """Validate one model response against the ordered semantic anchors."""

    ordered = sorted(anchors, key=lambda row: int(row["timestamp_ms"]))
    if len(ordered) < 2:
        raise ValueError("at least source start and end anchors are required")
    index_by_id = {
        str(row["anchor_id"]): index for index, row in enumerate(ordered)
    }
    expected_start = str(ordered[0]["anchor_id"])
    for segment in response.segments:
        if segment.start_anchor_id != expected_start:
            raise ValueError(
                f"non-contiguous start {segment.start_anchor_id}; "
                f"expected {expected_start}"
            )
        if (
            segment.start_anchor_id not in index_by_id
            or segment.end_anchor_id not in index_by_id
        ):
            raise ValueError("unknown anchor reference")
        if (
            index_by_id[segment.end_anchor_id]
            <= index_by_id[segment.start_anchor_id]
        ):
            raise ValueError("segment end anchor must follow its start anchor")
        expected_start = segment.end_anchor_id
    if expected_start != str(ordered[-1]["anchor_id"]):
        raise ValueError("segments do not cover the source end")


def build_segmentation_proposal(
    *,
    evidence_window_id: str,
    model_run_id: str,
    response: SegmentationResponse,
    anchors: list[dict[str, Any]],
    evidence_frames: list[dict[str, Any]],
    transcript_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project an internal model response into the stable proposal Schema."""

    validate_response_timeline(response, anchors)
    ordered = sorted(anchors, key=lambda row: int(row["timestamp_ms"]))
    anchor_by_id = {str(row["anchor_id"]): row for row in ordered}
    source_start_id = str(ordered[0]["anchor_id"])
    source_end_id = str(ordered[-1]["anchor_id"])
    segments: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(response.segments):
        start_ms = int(anchor_by_id[segment.start_anchor_id]["timestamp_ms"])
        end_ms = int(anchor_by_id[segment.end_anchor_id]["timestamp_ms"])
        frame_ids = [
            str(row["evidence_frame_id"])
            for row in evidence_frames
            if start_ms <= int(row["timestamp_ms"]) < end_ms
        ]
        span_ids = [
            str(row["transcript_span_id"])
            for row in transcript_spans
            if int(row["end_ms"]) > start_ms and int(row["start_ms"]) < end_ms
        ]
        segments.append(
            {
                "proposal_segment_id": f"proposal_segment_{ordinal + 1:04d}",
                "start_boundary_id": (
                    None
                    if segment.start_anchor_id == source_start_id
                    else segment.start_anchor_id
                ),
                "end_boundary_id": (
                    None
                    if segment.end_anchor_id == source_end_id
                    else segment.end_anchor_id
                ),
                "title": segment.title,
                "summary": segment.short_summary,
                "narrative_event": segment.event,
                "boundary_reason": segment.reason,
                "evidence_frame_ids": frame_ids,
                "transcript_span_ids": span_ids,
                "confidence": segment.confidence,
                "needs_boundary_refinement": segment.confidence < 0.65,
                # Compatible optional fields preserve rich summary material
                # after checkpoint recovery.
                "detailed_summary": segment.detailed_summary,
                "visual_summary": segment.visual_summary,
                "topics": segment.topics,
                "participants": segment.participants,
                "locations": segment.locations,
                "organizations": segment.organizations,
                "objects": segment.objects,
                "actions": segment.actions,
                "keywords": segment.keywords,
            }
        )
    return {
        "schema_version": 1,
        "evidence_window_id": evidence_window_id,
        "model_run_id": model_run_id,
        "segments": segments,
        "warnings": [],
    }


def response_from_proposal(
    proposal: dict[str, Any],
    *,
    anchors: list[dict[str, Any]],
) -> SegmentationResponse:
    """Restore the internal response from a stable persisted proposal."""

    ordered = sorted(anchors, key=lambda row: int(row["timestamp_ms"]))
    if len(ordered) < 2:
        raise ValueError("at least source start and end anchors are required")
    source_start_id = str(ordered[0]["anchor_id"])
    source_end_id = str(ordered[-1]["anchor_id"])
    response = SegmentationResponse(
        segments=[
            SemanticSegment(
                title=str(row["title"]),
                short_summary=str(row["summary"]),
                detailed_summary=str(
                    row.get("detailed_summary") or row["summary"]
                ),
                visual_summary=str(row.get("visual_summary") or ""),
                event=str(row["narrative_event"]),
                start_anchor_id=str(
                    row.get("start_boundary_id") or source_start_id
                ),
                end_anchor_id=str(
                    row.get("end_boundary_id") or source_end_id
                ),
                reason=str(row["boundary_reason"]),
                topics=list(row.get("topics") or []),
                participants=list(row.get("participants") or []),
                locations=list(row.get("locations") or []),
                organizations=list(row.get("organizations") or []),
                objects=list(row.get("objects") or []),
                actions=list(row.get("actions") or []),
                keywords=list(row.get("keywords") or []),
                confidence=float(row["confidence"]),
            )
            for row in proposal["segments"]
        ]
    )
    validate_response_timeline(response, anchors)
    return response
