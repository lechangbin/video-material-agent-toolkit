from __future__ import annotations

import pytest

from semvideo.adapters.ffmpeg import ScenePoint
from semvideo.modules.media.candidates import CandidatePolicy, build_candidate_timeline


def test_candidate_timeline_is_continuous_and_json_friendly() -> None:
    timeline = build_candidate_timeline(
        source_video_id="video_001",
        duration_ms=30_000,
        scene_points=[
            ScenePoint(timestamp_ms=5_000, score=0.8),
            ScenePoint(timestamp_ms=5_200, score=0.4),
            ScenePoint(timestamp_ms=18_000, score=0.9),
        ],
        policy=CandidatePolicy(
            min_segment_ms=750,
            max_segment_ms=10_000,
        ),
    )

    assert timeline.segments[0].start_ms == 0
    assert timeline.segments[-1].end_ms == 30_000
    assert all(
        left.end_ms == right.start_ms
        for left, right in zip(timeline.segments, timeline.segments[1:])
    )
    assert all(
        segment.end_ms - segment.start_ms <= 10_000
        for segment in timeline.segments
    )
    assert 5_000 in [item.timestamp_ms for item in timeline.boundaries]
    assert 5_200 not in [item.timestamp_ms for item in timeline.boundaries]
    assert timeline.to_dict()["segments"][0]["candidate_segment_id"] == "candidate_0001"


def test_candidate_limit_rejects_impossible_max_duration() -> None:
    with pytest.raises(ValueError, match="too small"):
        build_candidate_timeline(
            source_video_id="video_001",
            duration_ms=30_000,
            scene_points=[],
            policy=CandidatePolicy(max_segment_ms=10_000, max_segments=2),
        )


def test_candidate_policy_rejects_conflicting_duration_constraints() -> None:
    with pytest.raises(ValueError, match="at least twice"):
        CandidatePolicy(min_segment_ms=750, max_segment_ms=1000)
