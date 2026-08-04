from __future__ import annotations

from semvideo.modules.semantics.models import SegmentationResponse
from semvideo.modules.semantics.normalize import proposal_to_plan, timeline_from_anchors


def test_model_proposal_becomes_adjacent_merge_plan() -> None:
    anchors = [
        {"anchor_id": "anchor_0000", "timestamp_ms": 0, "reasons": ["start"]},
        {"anchor_id": "anchor_0001", "timestamp_ms": 1000, "reasons": ["scene"]},
        {"anchor_id": "anchor_0002", "timestamp_ms": 2000, "reasons": ["scene"]},
        {"anchor_id": "anchor_0003", "timestamp_ms": 3000, "reasons": ["end"]},
    ]
    timeline = timeline_from_anchors(
        source_video_id="video_1",
        duration_ms=3000,
        anchors=anchors,
    )
    response = SegmentationResponse.model_validate(
        {
            "segments": [
                {
                    "title": "连续动作",
                    "short_summary": "第一部分。",
                    "detailed_summary": "跨镜头连续动作。",
                    "event": "动作",
                    "start_anchor_id": "anchor_0000",
                    "end_anchor_id": "anchor_0002",
                    "reason": "任务阶段变化",
                    "confidence": 0.9,
                },
                {
                    "title": "新事件",
                    "short_summary": "第二部分。",
                    "detailed_summary": "新事件开始。",
                    "event": "事件",
                    "start_anchor_id": "anchor_0002",
                    "end_anchor_id": "anchor_0003",
                    "reason": "事件改变",
                    "confidence": 0.8,
                },
            ]
        }
    )

    plan, summaries, decisions = proposal_to_plan(
        job_id="job_1",
        model_run_id="run_1",
        response=response,
        timeline=timeline,
        anchors=anchors,
    )

    assert [segment.candidate_segment_ids for segment in plan.final_segments] == [
        ("candidate_0001", "candidate_0002"),
        ("candidate_0003",),
    ]
    assert summaries[0]["final_segment_id"] == "segment_0001"
    assert [decision.decision.value for decision in decisions] == ["remove", "keep"]


def test_low_confidence_never_auto_removes_candidate_boundaries() -> None:
    anchors = [
        {"anchor_id": "anchor_0000", "timestamp_ms": 0},
        {"anchor_id": "anchor_0001", "timestamp_ms": 1000},
        {"anchor_id": "anchor_0002", "timestamp_ms": 2000},
    ]
    timeline = timeline_from_anchors(
        source_video_id="video_1",
        duration_ms=2000,
        anchors=anchors,
    )
    response = SegmentationResponse.model_validate(
        {
            "segments": [
                {
                    "title": "不确定事件",
                    "short_summary": "证据不足。",
                    "detailed_summary": "模型不能确定内部是否应合并。",
                    "event": "不确定事件",
                    "start_anchor_id": "anchor_0000",
                    "end_anchor_id": "anchor_0002",
                    "reason": "视觉证据模糊",
                    "confidence": 0.4,
                }
            ]
        }
    )

    plan, summaries, decisions = proposal_to_plan(
        job_id="job_1",
        model_run_id="run_1",
        response=response,
        timeline=timeline,
        anchors=anchors,
    )

    assert decisions[0].decision.value == "review"
    assert len(plan.final_segments) == 2
    assert all(summary["review_reasons"] for summary in summaries)
