from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from semvideo.application.queries import (
    get_inspection_report,
    get_job,
    get_segment,
    get_shot,
    list_jobs,
    list_segments,
    list_shots,
    read_job_logs,
)
from semvideo.application.task_store import TaskStore
from semvideo.application.workspace import initialize_workspace
from semvideo.infrastructure.process_identity import process_started_at
from semvideo.modules.retrieval.records import build_record, write_records


def test_application_queries_hide_task_package_layout(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state(job["job_id"], state="completed")
    store.write_worker(
        job["job_id"],
        attempt_id="attempt_001",
        pid=999_999,
        process_started_at="2000-01-01T00:00:00.000Z",
        log_path="logs/attempt_001.log",
    )
    job_root = store.job_path(job["job_id"])
    (job_root / "logs" / "attempt_001.log").write_text(
        "first\nsecond\n",
        encoding="utf-8",
    )
    record = build_record(
        job_id=job["job_id"],
        source_video_id=job["source_video_id"],
        final_segment={
            "final_segment_id": "segment_0001",
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": 1000,
            "review_reasons": ["low_model_confidence"],
        },
        summary={
            "title": "测试片段",
            "short_summary": "短摘要",
            "detailed_summary": "详细摘要",
            "confidence": 0.5,
        },
        transcript_spans=[],
        profile="default",
        profile_version=1,
        merge_plan_hash="sha256:test",
    )
    write_records(job_root / "retrieval" / "segments.jsonl", [record])
    (job_root / "segmentation" / "shot-timeline.json").write_text(
        """{
          "schema_version": 1,
          "source_video_id": "video_001",
          "duration_ms": 1000,
          "detector": {"name": "test"},
          "shots": [{
            "shot_id": "shot_0001",
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": 1000,
            "right_boundary": null
          }]
        }""",
        encoding="utf-8",
    )
    (
        job_root / "semantics" / "cinematography-annotations.jsonl"
    ).write_text(
        """{"schema_version":1,"source_video_id":"video_001","model_run_id":"modelrun_001","shot_id":"shot_0001","viewpoints":["aerial"],"shot_scale":{"start":"extreme_wide","end":"wide"},"camera_motions":[{"type":"rise","direction":"up","speed":"slow","temporal_profile":"gradual","start_ms":0,"end_ms":1000,"confidence":0.9}],"cinematography_summary":"航拍大全景逐渐拉高。","cinematography_keywords":["航拍","大全景","逐渐拉高"],"evidence_frame_ids":["shot_0001_frame_01"],"confidence":0.9}
""",
        encoding="utf-8",
    )
    report = job_root / "reports" / "inspection.html"
    report.write_text("<html></html>", encoding="utf-8")

    jobs = list_jobs(workspace, state="completed")
    assert jobs["total"] == 1
    assert jobs["items"][0]["job_id"] == job["job_id"]
    assert get_job(workspace, job["job_id"])["state"] == "completed"
    assert read_job_logs(workspace, job["job_id"])["lines"] == [
        "first",
        "second",
    ]
    segments = list_segments(
        workspace,
        job["job_id"],
        review_only=True,
        offset=0,
        limit=10,
    )
    assert segments["total"] == 1
    assert segments["items"][0]["segment_id"] == "segment_0001"
    assert (
        get_segment(workspace, job["job_id"], "segment_0001")["title"]
        == "测试片段"
    )
    shots = list_shots(
        workspace,
        job["job_id"],
        viewpoint="aerial",
        scale="extreme_wide",
        motion="rise",
        speed="slow",
        keyword="逐渐拉高",
    )
    assert shots["total"] == 1
    assert shots["items"][0]["final_segment_ids"] == ["segment_0001"]
    assert (
        get_shot(workspace, job["job_id"], "shot_0001")[
            "cinematography"
        ]["shot_scale"]["start"]
        == "extreme_wide"
    )
    assert get_inspection_report(workspace, job["job_id"]) == report


def test_follow_logs_drains_final_line_after_terminal_state(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    store.write_state(job["job_id"], state="completed")
    log_path = store.job_path(job["job_id"]) / "logs" / "attempt_001.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("progress\n", encoding="utf-8")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,sys,time;"
                "time.sleep(0.2);"
                "pathlib.Path(sys.argv[1]).open("
                "'a',encoding='utf-8').write('final\\n')"
            ),
            str(log_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    def reap_child() -> None:
        child.wait()
        if os.name == "nt":
            child._handle.Close()  # type: ignore[attr-defined]

    reaper = threading.Thread(target=reap_child)
    reaper.start()
    try:
        store.write_worker(
            job["job_id"],
            attempt_id="attempt_001",
            pid=child.pid,
            process_started_at=process_started_at(child.pid),
            log_path="logs/attempt_001.log",
        )
        lines: list[str] = []

        result = read_job_logs(
            workspace,
            job["job_id"],
            follow=True,
            line_sink=lines.append,
            poll_interval=0.01,
        )

        assert result["state"] == "completed"
        assert lines == ["progress", "final"]
    finally:
        reaper.join(timeout=5)
        if reaper.is_alive():
            child.kill()
            reaper.join(timeout=5)
