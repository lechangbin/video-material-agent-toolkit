"""Build a gapless shot timeline from deterministic scene-change facts."""

from __future__ import annotations

from semvideo.modules.media.models import CandidateTimeline

from .models import ShotBoundary, ShotRecord, ShotTimeline


def build_shot_timeline(timeline: CandidateTimeline) -> ShotTimeline:
    """Project only true scene-change candidates, excluding duration anchors."""

    scene_boundaries = [
        boundary
        for boundary in timeline.boundaries
        if "scene_change" in boundary.reasons
    ]
    boundaries = [
        ShotBoundary(
            timestamp_ms=boundary.timestamp_ms,
            kind="hard_cut_candidate",
            score=float(boundary.scores["scene_change"]),
            detector=str(timeline.algorithm.get("name") or "unknown"),
            source_candidate_boundary_id=boundary.candidate_boundary_id,
        )
        for boundary in scene_boundaries
    ]
    points = [0, *(boundary.timestamp_ms for boundary in boundaries), timeline.duration_ms]
    shots = [
        ShotRecord(
            shot_id=f"shot_{index + 1:04d}",
            ordinal=index,
            start_ms=start,
            end_ms=end,
            right_boundary=boundaries[index] if index < len(boundaries) else None,
        )
        for index, (start, end) in enumerate(zip(points, points[1:]))
    ]
    return ShotTimeline(
        source_video_id=timeline.source_video_id,
        duration_ms=timeline.duration_ms,
        detector={
            "name": str(timeline.algorithm.get("name") or "unknown"),
            "version": str(timeline.algorithm.get("version") or "unknown"),
            "parameters": dict(timeline.algorithm.get("parameters") or {}),
            "projection": "scene_change_only_v1",
        },
        shots=shots,
    )
