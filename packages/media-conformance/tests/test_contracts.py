from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from media_conformance.contracts import (
    EditingDeliveryProfile,
    EditingMediaConformanceRequest,
)


def request_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "editing-media-conformance-request/v1",
        "request_id": "request_001",
        "idempotency_key": "idem_001",
        "source": {
            "asset_id": "asset_001",
            "sha256": "a" * 64,
            "path": str(tmp_path / "source.mp4"),
        },
        "ranges": [
            {"clip_id": "clip_001", "start_seconds": 1.0, "end_seconds": 4.0}
        ],
        "output_directory": str(tmp_path / "outputs"),
        "profile": {},
    }


def test_default_profile_is_the_frozen_v03_delivery_envelope() -> None:
    profile = EditingDeliveryProfile()

    assert profile.model_dump(mode="json") == {
        "schema_version": "editing-delivery-profile/v1",
        "container": "mp4",
        "video_codec": "h264",
        "video_encoder": "libx264",
        "video_profile": "high",
        "pixel_format": "yuv420p",
        "width": 1920,
        "height": 1080,
        "sample_aspect_ratio": "1:1",
        "frame_rate": "30/1",
        "gop_frames": 60,
        "video_track_time_scale": 90000,
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "audio_codec": "aac",
        "audio_profile": "LC",
        "audio_sample_rate": 48000,
        "audio_channels": 2,
        "audio_bit_rate": 192000,
    }


def test_request_rejects_profile_drift_and_bad_half_open_range(tmp_path: Path) -> None:
    payload = request_payload(tmp_path)
    payload["profile"] = {"frame_rate": "60/1"}
    with pytest.raises(ValidationError):
        EditingMediaConformanceRequest.model_validate(payload)

    payload = request_payload(tmp_path)
    payload["ranges"] = [
        {"clip_id": "clip_001", "start_seconds": 4.0, "end_seconds": 4.0}
    ]
    with pytest.raises(ValidationError):
        EditingMediaConformanceRequest.model_validate(payload)
