"""Read-only application use cases for jobs, logs, and final segments."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from semvideo.application.jobs import TERMINAL_STATES, get_job_snapshot
from semvideo.application.task_store import JobNotFoundError, TaskStore
from semvideo.application.workspace import WorkspacePaths
from semvideo.errors import (
    ErrorCategory,
    RecoveryAction,
    SemvideoError,
    config_error,
)
from semvideo.infrastructure.io import (
    read_versioned_json,
    read_versioned_json_lines,
)
from semvideo.modules.retrieval.records import read_records


def get_job(workspace: WorkspacePaths, job_id: str) -> dict[str, Any]:
    return get_job_snapshot(TaskStore(workspace), job_id)


def list_jobs(
    workspace: WorkspacePaths,
    *,
    state: str | None = None,
) -> dict[str, Any]:
    store = TaskStore(workspace)
    items: list[dict[str, Any]] = []
    for job_id in store.list_job_ids():
        snapshot = get_job_snapshot(store, job_id)
        if state and snapshot["state"] != state:
            continue
        items.append(snapshot)
    return {"schema_version": 1, "total": len(items), "items": items}


def read_job_logs(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    follow: bool = False,
    line_sink: Callable[[str], None] | None = None,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Read or follow one Worker's log without exposing package paths."""

    store = TaskStore(workspace)
    worker = store.read_worker(job_id)
    if not worker:
        raise JobNotFoundError(f"{job_id} has no worker log")
    log_path = store.job_path(job_id) / str(worker["log_path"])
    position = 0
    collected: list[str] = []
    lines_emitted = 0
    final_state: str | None = None
    terminal_worker_stopped_seen = False
    while True:
        if log_path.exists():
            with log_path.open(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                handle.seek(position)
                content = handle.read()
                position = handle.tell()
            for line in content.splitlines():
                if line_sink is not None:
                    line_sink(line)
                    lines_emitted += 1
                else:
                    collected.append(line)
        snapshot = store.reconcile_interrupted(job_id)
        final_state = str(snapshot["state"])
        if not follow:
            break
        if final_state in TERMINAL_STATES:
            if store.worker_matches(job_id):
                terminal_worker_stopped_seen = False
            elif terminal_worker_stopped_seen:
                break
            else:
                terminal_worker_stopped_seen = True
        else:
            terminal_worker_stopped_seen = False
        time.sleep(poll_interval)
    return {
        "schema_version": 1,
        "job_id": job_id,
        "state": final_state,
        **(
            {"lines_emitted": lines_emitted}
            if line_sink is not None
            else {"lines": collected}
        ),
    }


def _records_path(
    workspace: WorkspacePaths,
    job_id: str,
) -> tuple[TaskStore, Path]:
    store = TaskStore(workspace)
    store.read_job(job_id)
    path = store.job_path(job_id) / "retrieval" / "segments.jsonl"
    if not path.is_file():
        raise config_error(
            "segment_records_not_ready",
            f"任务尚未产生正式片段记录：{job_id}",
            job_id=job_id,
        )
    return store, path


def list_segments(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    review_only: bool = False,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    _, path = _records_path(workspace, job_id)
    records = read_records(path)
    if review_only:
        records = [record for record in records if record.review_required]
    return {
        "schema_version": 1,
        "job_id": job_id,
        "total": len(records),
        "offset": offset,
        "limit": limit,
        "items": [
            record.compact_dict()
            for record in records[offset : offset + limit]
        ],
    }


def get_segment(
    workspace: WorkspacePaths,
    job_id: str,
    segment_id: str,
) -> dict[str, Any]:
    _, path = _records_path(workspace, job_id)
    record = next(
        (
            item
            for item in read_records(path)
            if item.segment_id == segment_id
        ),
        None,
    )
    if record is None:
        raise SemvideoError(
            code="segment_not_found",
            category=ErrorCategory.INPUT,
            message=f"正式片段不存在：{segment_id}",
            recovery=RecoveryAction.USER_ACTION,
            entity_id=segment_id,
            details={"job_id": job_id},
            exit_code=3,
        )
    return record.model_dump(mode="json")


def _shot_records(
    workspace: WorkspacePaths,
    job_id: str,
) -> list[dict[str, Any]]:
    store = TaskStore(workspace)
    store.read_job(job_id)
    root = store.job_path(job_id)
    timeline_path = root / "segmentation" / "shot-timeline.json"
    annotations_path = (
        root / "semantics" / "cinematography-annotations.jsonl"
    )
    if not timeline_path.is_file() or not annotations_path.is_file():
        raise config_error(
            "shot_records_not_ready",
            f"任务尚未产生镜头语言记录：{job_id}",
            job_id=job_id,
        )
    timeline = read_versioned_json(timeline_path)
    annotation_by_id = {
        str(row["shot_id"]): row
        for row in read_versioned_json_lines(annotations_path)
    }
    final_records = (
        read_records(root / "retrieval" / "segments.jsonl")
        if (root / "retrieval" / "segments.jsonl").is_file()
        else []
    )
    output: list[dict[str, Any]] = []
    for shot in timeline["shots"]:
        shot_id = str(shot["shot_id"])
        annotation = annotation_by_id.get(shot_id)
        if annotation is None:
            raise SemvideoError(
                code="shot_annotation_missing",
                category=ErrorCategory.INTERNAL,
                message=f"镜头缺少已登记的镜头语言标注：{shot_id}",
                recovery=RecoveryAction.REPORT_BUG,
                entity_id=shot_id,
                details={"job_id": job_id},
                exit_code=10,
            )
        start_ms = int(shot["start_ms"])
        end_ms = int(shot["end_ms"])
        output.append(
            {
                "schema_version": 1,
                "job_id": job_id,
                **shot,
                "final_segment_ids": [
                    record.segment_id
                    for record in final_records
                    if record.end_ms > start_ms and record.start_ms < end_ms
                ],
                "cinematography": {
                    key: value
                    for key, value in annotation.items()
                    if key
                    not in {
                        "schema_version",
                        "source_video_id",
                        "shot_id",
                    }
                },
            }
        )
    return output


def list_shots(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    viewpoint: str | list[str] | None = None,
    scale: str | list[str] | None = None,
    motion: str | list[str] | None = None,
    speed: str | list[str] | None = None,
    keyword: str | list[str] | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    records = _shot_records(workspace, job_id)

    def terms(value: str | list[str] | None) -> list[str]:
        if value is None:
            return []
        return [value] if isinstance(value, str) else value

    viewpoints = terms(viewpoint)
    scales = terms(scale)
    motions = terms(motion)
    speeds = terms(speed)
    keywords = terms(keyword)
    if viewpoints:
        records = [
            row
            for row in records
            if all(
                value in row["cinematography"]["viewpoints"]
                for value in viewpoints
            )
        ]
    if scales:
        records = [
            row
            for row in records
            if all(
                value
                in {
                    row["cinematography"]["shot_scale"]["start"],
                    row["cinematography"]["shot_scale"]["end"],
                }
                for value in scales
            )
        ]
    if motions:
        records = [
            row
            for row in records
            if all(
                any(
                    item["type"] == value
                    for item in row["cinematography"]["camera_motions"]
                )
                for value in motions
            )
        ]
    if speeds:
        records = [
            row
            for row in records
            if all(
                any(
                    item["speed"] == value
                    for item in row["cinematography"]["camera_motions"]
                )
                for value in speeds
            )
        ]
    if keywords:
        records = [
            row
            for row in records
            if all(
                value
                in row["cinematography"]["cinematography_keywords"]
                for value in keywords
            )
        ]
    return {
        "schema_version": 1,
        "job_id": job_id,
        "total": len(records),
        "offset": offset,
        "limit": limit,
        "items": records[offset : offset + limit],
    }


def get_shot(
    workspace: WorkspacePaths,
    job_id: str,
    shot_id: str,
) -> dict[str, Any]:
    record = next(
        (
            row
            for row in _shot_records(workspace, job_id)
            if row["shot_id"] == shot_id
        ),
        None,
    )
    if record is None:
        raise SemvideoError(
            code="shot_not_found",
            category=ErrorCategory.INPUT,
            message=f"镜头不存在：{shot_id}",
            recovery=RecoveryAction.USER_ACTION,
            entity_id=shot_id,
            details={"job_id": job_id},
            exit_code=3,
        )
    return record


def get_inspection_report(
    workspace: WorkspacePaths,
    job_id: str,
) -> Path:
    store = TaskStore(workspace)
    store.read_job(job_id)
    path = store.job_path(job_id) / "reports" / "inspection.html"
    if not path.is_file():
        raise JobNotFoundError(f"inspection report for {job_id}")
    return path
