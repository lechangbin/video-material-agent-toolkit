from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from semvideo.modules.cinematography.models import (
    GlobalMotionMeasurement,
    ShotEvidenceBundle,
    ShotEvidenceFrame,
)
from semvideo.modules.cinematography.prompt import cinematography_messages


def test_prompt_contains_controlled_camera_language_and_motion_evidence(
    tmp_path: Path,
) -> None:
    sheet = tmp_path / "sheet.jpg"
    Image.new("RGB", (32, 32), "blue").save(sheet)
    bundle = ShotEvidenceBundle(
        shot_id="shot_0001",
        start_ms=0,
        end_ms=4000,
        frames=[
            ShotEvidenceFrame(
                frame_id="shot_0001_frame_01",
                shot_id="shot_0001",
                timestamp_ms=500,
                path="frame.jpg",
            )
        ],
        contact_sheet_path="sheet.jpg",
        motion_measurement=GlobalMotionMeasurement(
            shot_id="shot_0001",
            sample_pairs=3,
            translation_x_ratio=0.0,
            translation_y_ratio=0.05,
            scale_change_ratio=0.04,
            mean_frame_change=0.2,
            fit_improvement=0.5,
        ),
    )

    messages = cinematography_messages([bundle], job_root=tmp_path)

    text = messages[1]["content"][0]["text"]
    shot_text = messages[1]["content"][1]["text"]
    assert "aerial" in text
    assert "push_in" in text
    assert "rise" in text
    assert "不要把主体自身移动误判" in text
    assert "scale_change_ratio" in shot_text
    assert messages[1]["content"][2]["type"] == "image_url"


def test_prompt_uploads_every_contact_sheet_and_maps_all_frame_ids(
    tmp_path: Path,
) -> None:
    for name in ("sheet_01.jpg", "sheet_02.jpg"):
        Image.new("RGB", (32, 32), "blue").save(tmp_path / name)
    frames = [
        ShotEvidenceFrame(
            frame_id=f"shot_0001_frame_{index:02d}",
            shot_id="shot_0001",
            timestamp_ms=index * 1000,
            path=f"frame_{index:02d}.jpg",
        )
        for index in range(1, 13)
    ]
    bundle = ShotEvidenceBundle(
        shot_id="shot_0001",
        start_ms=0,
        end_ms=13000,
        frames=frames,
        contact_sheet_path="sheet_01.jpg",
        contact_sheet_paths=["sheet_01.jpg", "sheet_02.jpg"],
        motion_measurement=GlobalMotionMeasurement(
            shot_id="shot_0001",
            sample_pairs=11,
            translation_x_ratio=0.0,
            translation_y_ratio=0.0,
            scale_change_ratio=0.0,
            mean_frame_change=0.1,
            fit_improvement=0.2,
        ),
    )

    messages = cinematography_messages([bundle], job_root=tmp_path)

    content = messages[1]["content"]
    images = [item for item in content if item["type"] == "image_url"]
    shot_payloads = [
        json.loads(item["text"])
        for item in content
        if item["type"] == "text" and '"contact_sheets"' in item["text"]
    ]
    assert len(images) == 2
    assert len(shot_payloads) == 1
    assert len(shot_payloads[0]["contact_sheets"]) == 2
    mapped_frame_ids = {
        frame_id
        for sheet in shot_payloads[0]["contact_sheets"]
        for frame_id in sheet["frame_ids"]
    }
    assert mapped_frame_ids == {frame.frame_id for frame in frames}
