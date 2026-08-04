from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from semvideo.modules.cinematography import (
    CinematographyResponse,
    ShotEvidencePolicy,
    ShotRecord,
    ShotTimeline,
    build_shot_timeline,
    extract_cinematography_evidence,
    measure_global_motion,
    plan_shot_evidence,
    validate_cinematography_response,
)
from semvideo.modules.media.models import (
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
)


def _media_timeline() -> CandidateTimeline:
    scene = CandidateBoundary(
        candidate_boundary_id="boundary_scene",
        timestamp_ms=2000,
        reasons=("scene_change",),
        scores={"scene_change": 0.8},
    )
    forced = CandidateBoundary(
        candidate_boundary_id="boundary_forced",
        timestamp_ms=5000,
        reasons=("max_duration",),
        scores={"max_duration": 1.0},
    )
    return CandidateTimeline(
        schema_version=1,
        source_video_id="video_test",
        duration_ms=8000,
        algorithm={
            "name": "ffmpeg_scene_v1",
            "version": "1",
            "parameters": {"scene_threshold": 0.3},
        },
        boundaries=(scene, forced),
        segments=(
            CandidateSegment(
                candidate_segment_id="candidate_1",
                ordinal=0,
                start_ms=0,
                end_ms=2000,
                left_boundary_id=None,
                right_boundary_id="boundary_scene",
            ),
            CandidateSegment(
                candidate_segment_id="candidate_2",
                ordinal=1,
                start_ms=2000,
                end_ms=5000,
                left_boundary_id="boundary_scene",
                right_boundary_id="boundary_forced",
            ),
            CandidateSegment(
                candidate_segment_id="candidate_3",
                ordinal=2,
                start_ms=5000,
                end_ms=8000,
                left_boundary_id="boundary_forced",
                right_boundary_id=None,
            ),
        ),
    )


def test_shot_timeline_excludes_forced_duration_anchors() -> None:
    timeline = build_shot_timeline(_media_timeline())

    assert [(shot.start_ms, shot.end_ms) for shot in timeline.shots] == [
        (0, 2000),
        (2000, 8000),
    ]
    assert timeline.shots[0].right_boundary is not None
    assert (
        timeline.shots[0].right_boundary.source_candidate_boundary_id
        == "boundary_scene"
    )


def test_shot_evidence_is_dense_bounded_and_inside_each_shot() -> None:
    timeline = build_shot_timeline(_media_timeline())

    evidence = plan_shot_evidence(
        timeline,
        ShotEvidencePolicy(
            frames_per_second=2.0,
            minimum_frames_per_shot=4,
            maximum_frames_per_shot=6,
        ),
    )

    first = [row for row in evidence if row.shot_id == "shot_0001"]
    second = [row for row in evidence if row.shot_id == "shot_0002"]
    assert len(first) == 4
    assert len(second) == 6
    assert all(0 <= row.timestamp_ms < 2000 for row in first)
    assert all(2000 <= row.timestamp_ms < 8000 for row in second)


def test_twelve_frame_shot_evidence_uses_two_visible_contact_sheets(
    tmp_path: Path,
) -> None:
    class FrameFfmpeg:
        def extract_frame(
            self,
            source: Path,
            *,
            timestamp_ms: int,
            output: Path,
            **kwargs,
        ) -> Path:
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new(
                "RGB",
                (96, 54),
                (timestamp_ms % 255, 80, 160),
            ).save(output)
            return output

    timeline = ShotTimeline(
        source_video_id="video_test",
        duration_ms=12000,
        detector={"name": "test", "version": "1"},
        shots=[
            ShotRecord(
                shot_id="shot_0001",
                ordinal=0,
                start_ms=0,
                end_ms=12000,
            )
        ],
    )

    evidence = extract_cinematography_evidence(
        tmp_path / "source.mp4",
        tmp_path,
        timeline,
        ffmpeg=FrameFfmpeg(),
        policy=ShotEvidencePolicy(
            frames_per_second=1.0,
            minimum_frames_per_shot=12,
            maximum_frames_per_shot=12,
        ),
    )

    bundle = evidence.shots[0]
    assert len(bundle.frames) == 12
    assert bundle.contact_sheet_paths == [
        "evidence/cinematography/contact-sheets/shot_0001_01.jpg",
        "evidence/cinematography/contact-sheets/shot_0001_02.jpg",
    ]
    assert all((tmp_path / path).is_file() for path in bundle.contact_sheet_paths)


def test_global_motion_suppresses_local_foreground_movement(
    tmp_path: Path,
) -> None:
    background = Image.new("RGB", (64, 36))
    pixels = background.load()
    for y in range(background.height):
        for x in range(background.width):
            value = (x * 17 + y * 31) % 256
            pixels[x, y] = (value, value, value)
    left = background.copy()
    right = background.copy()
    ImageDraw.Draw(left).rectangle((4, 8, 19, 27), fill="white")
    ImageDraw.Draw(right).rectangle((36, 8, 51, 27), fill="white")
    left_path = tmp_path / "left.jpg"
    right_path = tmp_path / "right.jpg"
    left.save(left_path, quality=100)
    right.save(right_path, quality=100)

    measurement = measure_global_motion(
        "shot_0001",
        [left_path, right_path],
    )

    assert measurement.foreground_suppression == "tile_median_v1"
    assert abs(measurement.translation_x_ratio) <= 1 / 64
    assert abs(measurement.translation_y_ratio) <= 1 / 36
    assert len(measurement.speed_curve) == 1


def test_global_motion_preserves_accelerating_speed_curve(
    tmp_path: Path,
) -> None:
    texture = Image.new("RGB", (64, 36))
    pixels = texture.load()
    for y in range(texture.height):
        for x in range(texture.width):
            value = (x * 29 + y * 13) % 256
            pixels[x, y] = (value, value, value)
    frame_paths: list[Path] = []
    for index, offset in enumerate((0, 0, 1, 3, 6)):
        path = tmp_path / f"frame_{index}.png"
        ImageChops.offset(texture, offset, 0).save(path)
        frame_paths.append(path)

    measurement = measure_global_motion("shot_0001", frame_paths)

    assert len(measurement.speed_curve) == 4
    assert measurement.speed_curve[-1] > measurement.speed_curve[1]
    assert measurement.temporal_profile_hint == "accelerating"
    assert measurement.direction_change_count == 0


def test_cinematography_response_requires_complete_valid_shot_coverage() -> None:
    timeline = build_shot_timeline(_media_timeline())
    response = CinematographyResponse.model_validate(
        {
            "annotations": [
                {
                    "shot_id": "shot_0001",
                    "viewpoints": ["aerial"],
                    "shot_scale": {
                        "start": "extreme_wide",
                        "end": "extreme_wide",
                    },
                    "camera_motions": [
                        {
                            "type": "rise",
                            "direction": "up",
                            "speed": "slow",
                            "temporal_profile": "gradual",
                            "start_ms": 0,
                            "end_ms": 2000,
                            "confidence": 0.9,
                        }
                    ],
                    "cinematography_summary": "航拍大全景逐渐拉高。",
                    "cinematography_keywords": ["航拍", "大全景", "逐渐拉高"],
                    "evidence_frame_ids": ["shot_0001_frame_01"],
                    "confidence": 0.9,
                },
                {
                    "shot_id": "shot_0002",
                    "viewpoints": ["ground"],
                    "shot_scale": {"start": "wide", "end": "medium"},
                    "camera_motions": [
                        {
                            "type": "push_in",
                            "direction": "forward",
                            "speed": "slow",
                            "temporal_profile": "gradual",
                            "start_ms": 2000,
                            "end_ms": 8000,
                            "confidence": 0.8,
                        }
                    ],
                    "cinematography_summary": "地面镜头缓慢推进。",
                    "cinematography_keywords": ["缓慢推进"],
                    "evidence_frame_ids": ["shot_0002_frame_01"],
                    "confidence": 0.8,
                },
            ]
        }
    )

    validate_cinematography_response(
        response,
        timeline,
        evidence_frame_ids={
            "shot_0001": ["shot_0001_frame_01"],
            "shot_0002": ["shot_0002_frame_01"],
        },
    )

    invalid = response.model_copy(
        update={"annotations": response.annotations[:1]}
    )
    with pytest.raises(ValueError, match="cover every shot"):
        validate_cinematography_response(
            invalid,
            timeline,
            evidence_frame_ids={
                "shot_0001": ["shot_0001_frame_01"],
                "shot_0002": ["shot_0002_frame_01"],
            },
        )
