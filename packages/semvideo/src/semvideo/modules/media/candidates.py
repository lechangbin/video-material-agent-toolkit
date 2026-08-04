"""Deterministic construction of a continuous candidate timeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from semvideo.adapters.ffmpeg import ScenePoint

from .models import CandidateBoundary, CandidateSegment, CandidateTimeline


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    scene_threshold: float = 0.30
    min_segment_ms: int = 750
    max_segment_ms: int = 12_000
    max_segments: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.scene_threshold <= 1:
            raise ValueError("scene_threshold must be between 0 and 1")
        if self.min_segment_ms <= 0:
            raise ValueError("min_segment_ms must be positive")
        if self.max_segment_ms < self.min_segment_ms * 2:
            raise ValueError(
                "max_segment_ms must be at least twice min_segment_ms so forced "
                "boundaries can satisfy both constraints"
            )
        if self.max_segments is not None and self.max_segments <= 0:
            raise ValueError("max_segments must be positive")


def _select_scene_points(
    duration_ms: int,
    scene_points: Iterable[ScenePoint],
    policy: CandidatePolicy,
) -> list[ScenePoint]:
    eligible = sorted(
        (
            point
            for point in scene_points
            if policy.min_segment_ms
            <= point.timestamp_ms
            <= duration_ms - policy.min_segment_ms
        ),
        key=lambda item: (item.timestamp_ms, -item.score),
    )
    selected: list[ScenePoint] = []
    for point in eligible:
        if selected and point.timestamp_ms - selected[-1].timestamp_ms < policy.min_segment_ms:
            if point.score > selected[-1].score:
                selected[-1] = point
            continue
        selected.append(point)
    while selected and duration_ms - selected[-1].timestamp_ms < policy.min_segment_ms:
        selected.pop()
    return selected


def _add_duration_boundaries(
    duration_ms: int,
    scene_points: list[ScenePoint],
    policy: CandidatePolicy,
) -> dict[int, tuple[set[str], dict[str, float]]]:
    boundaries: dict[int, tuple[set[str], dict[str, float]]] = {
        point.timestamp_ms: ({"scene_change"}, {"scene_change": point.score})
        for point in scene_points
    }
    anchors = [0, *sorted(boundaries), duration_ms]
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left
        if gap <= policy.max_segment_ms:
            continue
        parts = (gap + policy.max_segment_ms - 1) // policy.max_segment_ms
        for part in range(1, parts):
            timestamp = left + round(gap * part / parts)
            if timestamp - left < policy.min_segment_ms:
                continue
            if right - timestamp < policy.min_segment_ms:
                continue
            reasons, scores = boundaries.setdefault(timestamp, (set(), {}))
            reasons.add("max_duration")
            scores["max_duration"] = 1.0
    return boundaries


def build_candidate_timeline(
    *,
    source_video_id: str,
    duration_ms: int,
    scene_points: Iterable[ScenePoint],
    policy: CandidatePolicy | None = None,
) -> CandidateTimeline:
    """Build a gapless timeline without treating evidence frames as boundaries."""

    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    policy = policy or CandidatePolicy()
    selected = _select_scene_points(duration_ms, scene_points, policy)
    raw_boundaries = _add_duration_boundaries(duration_ms, selected, policy)
    ordered = sorted(raw_boundaries.items())

    if policy.max_segments is not None and len(ordered) + 1 > policy.max_segments:
        required = (duration_ms + policy.max_segment_ms - 1) // policy.max_segment_ms
        if required > policy.max_segments:
            raise ValueError(
                "max_segments is too small to honor max_segment_ms for this video"
            )
        scene_only = [
            (timestamp, data)
            for timestamp, data in ordered
            if "max_duration" not in data[0]
        ]
        scene_only.sort(key=lambda item: item[1][1].get("scene_change", 0), reverse=True)
        keep_scenes = {
            timestamp
            for timestamp, _ in scene_only[
                : max(0, policy.max_segments - required)
            ]
        }
        selected = [point for point in selected if point.timestamp_ms in keep_scenes]
        raw_boundaries = _add_duration_boundaries(duration_ms, selected, policy)
        ordered = sorted(raw_boundaries.items())

    boundaries = tuple(
        CandidateBoundary(
            candidate_boundary_id=f"boundary_{index:04d}",
            timestamp_ms=timestamp,
            reasons=tuple(sorted(reasons)),
            scores=dict(sorted(scores.items())),
        )
        for index, (timestamp, (reasons, scores)) in enumerate(ordered, start=1)
    )
    points = [0, *(item.timestamp_ms for item in boundaries), duration_ms]
    segments = tuple(
        CandidateSegment(
            candidate_segment_id=f"candidate_{index + 1:04d}",
            ordinal=index,
            start_ms=start,
            end_ms=end,
            left_boundary_id=boundaries[index - 1].candidate_boundary_id
            if index > 0
            else None,
            right_boundary_id=boundaries[index].candidate_boundary_id
            if index < len(boundaries)
            else None,
        )
        for index, (start, end) in enumerate(zip(points, points[1:]))
    )
    return CandidateTimeline(
        schema_version=1,
        source_video_id=source_video_id,
        duration_ms=duration_ms,
        algorithm={
            "name": "ffmpeg_scene_v1",
            "version": "1",
            "parameters": {
                "scene_threshold": policy.scene_threshold,
                "min_segment_ms": policy.min_segment_ms,
                "max_segment_ms": policy.max_segment_ms,
                "max_segments": policy.max_segments,
            },
        },
        boundaries=boundaries,
        segments=segments,
    )
