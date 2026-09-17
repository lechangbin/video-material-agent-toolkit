from __future__ import annotations

from pathlib import Path

from material_collector.core.media import GeometryDisposition, GeometryStage
from material_collector.infrastructure.media_inspection import (
    MediaProbe,
    assess_display_geometry,
)


def probe(
    width: int | None,
    height: int | None,
    *,
    sar: str | None = "1:1",
    rotation: int | None = None,
) -> MediaProbe:
    return MediaProbe(
        path=Path("video.mp4"),
        duration_seconds=1,
        container="mp4",
        width=width,
        height=height,
        video_stream_count=1,
        audio_stream_count=0,
        sample_aspect_ratio=sar,
        rotation_degrees=rotation,
    )


def test_geometry_normalizes_rotation_and_sample_aspect_ratio() -> None:
    rotated = assess_display_geometry(
        probe(1080, 1920, rotation=90), stage=GeometryStage.LOCAL_PROXY
    )
    anamorphic = assess_display_geometry(
        probe(1440, 1080, sar="4:3"), stage=GeometryStage.LOCAL_PROXY
    )

    assert rotated.disposition is GeometryDisposition.ACCEPTED
    assert anamorphic.disposition is GeometryDisposition.ACCEPTED


def test_geometry_enforces_one_percent_boundary_and_preserves_unknown() -> None:
    accepted = assess_display_geometry(
        probe(1795, 1000), stage=GeometryStage.PLATFORM_METADATA
    )
    rejected = assess_display_geometry(
        probe(1800, 1000), stage=GeometryStage.PLATFORM_METADATA
    )
    unknown = assess_display_geometry(
        probe(None, None), stage=GeometryStage.PLATFORM_METADATA
    )

    assert accepted.disposition is GeometryDisposition.ACCEPTED
    assert rejected.disposition is GeometryDisposition.REJECTED
    assert unknown.disposition is GeometryDisposition.UNKNOWN
    assert unknown.reason_code == "display_geometry_missing"
