from __future__ import annotations

import json

from semvideo.application import cinematography as application
from semvideo.config import WorkspaceConfig
from semvideo.modules.cinematography.models import ShotRecord, ShotTimeline
from semvideo.modules.semantics.models import ModelCallResult, ModelUsage


class FakeLlm:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass

    def chat(self, messages):
        return ModelCallResult(
            content=json.dumps(
                {
                    "annotations": [
                        {
                            "shot_id": "shot_0001",
                            "viewpoints": ["aerial"],
                            "shot_scale": {
                                "start": "extreme_wide",
                                "end": "wide",
                            },
                            "camera_motions": [
                                {
                                    "type": "rise",
                                    "direction": "up",
                                    "speed": "slow",
                                    "temporal_profile": "gradual",
                                    "start_ms": 0,
                                    "end_ms": 4000,
                                    "confidence": 0.9,
                                }
                            ],
                            "cinematography_summary": "航拍大全景逐渐拉高。",
                            "cinematography_keywords": [
                                "航拍",
                                "大全景",
                                "逐渐拉高",
                            ],
                            "evidence_frame_ids": ["shot_0001_frame_01"],
                            "confidence": 0.9,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            usage=ModelUsage(
                input_tokens=20,
                output_tokens=10,
                total_tokens=30,
            ),
            raw_response={"id": "test"},
        )


def test_call_cinematography_model_validates_structured_annotations(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SEMVIDEO_API_KEY", "test")
    monkeypatch.setattr(application, "OpenAICompatibleLlm", FakeLlm)
    timeline = ShotTimeline(
        source_video_id="video_test",
        duration_ms=4000,
        detector={"name": "test"},
        shots=[
            ShotRecord(
                shot_id="shot_0001",
                ordinal=0,
                start_ms=0,
                end_ms=4000,
            )
        ],
    )

    response, result = application.call_cinematography_model(
        WorkspaceConfig(),
        [],
        timeline=timeline,
        evidence_frame_ids={
            "shot_0001": ["shot_0001_frame_01"],
        },
        required_shot_ids=["shot_0001"],
    )

    assert response.annotations[0].viewpoints == ["aerial"]
    assert response.annotations[0].camera_motions[0].type == "rise"
    assert result["usage"]["total_tokens"] == 30
