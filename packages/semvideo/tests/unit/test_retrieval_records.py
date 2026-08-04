from __future__ import annotations

from pathlib import Path

import pytest

from semvideo.modules.retrieval.records import (
    build_record,
    list_records,
    read_records,
    write_records,
)


def test_retrieval_record_contains_full_segment_transcript(tmp_path: Path) -> None:
    record = build_record(
        job_id="job_1",
        source_video_id="video_1",
        final_segment={
            "final_segment_id": "segment_1",
            "ordinal": 0,
            "start_ms": 1000,
            "end_ms": 5000,
        },
        summary={
            "title": "直升机巡逻",
            "short_summary": "官兵执行巡逻。",
            "detailed_summary": "官兵搭乘直升机执行空中巡逻。",
            "visual_summary": "直升机在山谷上空飞行。",
            "topics": ["边防"],
            "keywords": ["直升机"],
            "confidence": 0.9,
        },
        transcript_spans=[
            {
                "transcript_span_id": "span_1",
                "start_ms": 500,
                "end_ms": 2000,
                "text": "边防官兵",
                "language": "zh",
                "source": "asr",
            },
            {
                "transcript_span_id": "span_2",
                "start_ms": 2000,
                "end_ms": 4800,
                "text": "搭乘直升机展开空中巡逻",
                "language": "zh",
                "source": "asr",
            },
        ],
        profile="default",
        profile_version=1,
        merge_plan_hash="sha256:plan",
    )

    assert record.transcript.text == "边防官兵 搭乘直升机展开空中巡逻"
    assert record.transcript.clipped_span_ids == ["span_1"]
    assert "空中巡逻" in record.search_text
    assert record.artifacts.video is None

    path = tmp_path / "retrieval" / "segments.jsonl"
    write_records(path, [record])
    assert read_records(path)[0] == record
    compact = list_records(path)
    assert compact["items"][0]["title"] == "直升机巡逻"
    assert "transcript" not in compact["items"][0]


def test_read_records_rejects_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "segments.jsonl"
    path.write_text(
        '{"schema_version":999,"segment_id":"future"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version"):
        read_records(path)
