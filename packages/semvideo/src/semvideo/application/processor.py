"""Formal V1 processing pipeline used by detached Workers and segment export."""

from __future__ import annotations

import html
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from semvideo.adapters.ffmpeg import (
    AudioStreamFacts,
    FfmpegAdapter,
    FfmpegError,
    MediaFacts,
    SubtitleStreamFacts,
    VideoStreamFacts,
)
from semvideo.adapters.llm import OpenAICompatibleLlm, parse_json_content
from semvideo.application.analysis_media import (
    AnalysisProxyPolicy,
    prepare_analysis_media,
)
from semvideo.application.artifacts import artifact_record
from semvideo.application.cinematography import call_cinematography_model
from semvideo.application.execution_config import FrozenExecutionConfig
from semvideo.application.model_runs import aggregate_usage, write_model_run
from semvideo.application.progress import ProgressSink, stage_progress
from semvideo.application.range_export import (
    RegisteredTaskMedia,
    export_time_range,
)
from semvideo.application.source_store import resolve_source, sha256_file
from semvideo.application.task_store import TaskStore, new_opaque_id, utc_now
from semvideo.application.workspace import WorkspacePaths
from semvideo.config import WorkspaceConfig
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import (
    atomic_write_bytes,
    atomic_write_json,
    read_versioned_json,
    read_versioned_json_lines,
)
from semvideo.infrastructure.locks import (
    LockAcquisitionTimeout,
    ResourceLimits,
    ResourceLockManager,
)
from semvideo.modules.cinematography import (
    CinematographyAnnotation,
    CinematographyEvidence,
    CinematographyResponse,
    ShotEvidencePolicy,
    ShotTimeline,
    build_shot_timeline,
    cinematography_messages,
    extract_cinematography_evidence,
)
from semvideo.modules.cinematography.prompt import (
    PROMPT_VERSION as CINEMATOGRAPHY_PROMPT_VERSION,
)
from semvideo.modules.evidence import (
    ContactSheet,
    EvidenceFrame,
    EvidencePolicy,
    EvidenceTimeline,
    FasterWhisperConfig,
    FasterWhisperTranscriber,
    NativeEvidenceAdapter,
)
from semvideo.modules.evidence.models import DedupDecision
from semvideo.modules.media import (
    CandidateBoundary as MediaCandidateBoundary,
)
from semvideo.modules.media import (
    CandidateSegment as MediaCandidateSegment,
)
from semvideo.modules.media import (
    CandidateTimeline as MediaCandidateTimeline,
)
from semvideo.modules.media import (
    MediaAnalyzer,
    TranscriptSpan,
)
from semvideo.modules.media.candidates import CandidatePolicy
from semvideo.modules.planning import MergePlan
from semvideo.modules.retrieval.records import build_record, read_records, write_records
from semvideo.modules.semantics import (
    SegmentationResponse,
    build_segmentation_proposal,
    response_from_proposal,
    validate_response_timeline,
)
from semvideo.modules.semantics.normalize import (
    canonical_hash,
    proposal_to_plan,
    timeline_from_anchors,
)
from semvideo.modules.semantics.prompt import PROMPT_VERSION, segmentation_messages

_STATE_BY_STAGE = {
    "probe": "probing",
    "segment": "segmenting",
    "evidence": "extracting_evidence",
    "cinematography": "analyzing_cinematography",
    "windows": "building_windows",
    "analyze": "analyzing",
    "reconcile": "reconciling",
    "plan": "planning",
    "summarize": "summarizing",
    "retrieval": "summarizing",
    "render": "rendering",
}
_PIPELINE_IMPLEMENTATION_VERSION = "v1-dev-8"


def _ffmpeg_adapter(config: WorkspaceConfig) -> FfmpegAdapter:
    return FfmpegAdapter(
        ffmpeg_path=config.media.ffmpeg_path,
        ffprobe_path=config.media.ffprobe_path,
    )


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return (
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            + "\n"
            for row in rows
        )
    ).encode("utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_bytes(path, _jsonl_bytes(rows))


def _checkpoint_valid(
    store: TaskStore,
    job_id: str,
    stage: str,
    *,
    config_hash: str,
    input_hashes: list[str],
) -> bool:
    checkpoint = store.read_checkpoint(job_id, stage)
    if not checkpoint or checkpoint.get("status") != "succeeded":
        return False
    if checkpoint.get("implementation_version") != _PIPELINE_IMPLEMENTATION_VERSION:
        return False
    if checkpoint.get("config_hash") != config_hash:
        return False
    if checkpoint.get("input_hashes") != input_hashes:
        return False
    root = store.job_path(job_id)
    for output in checkpoint.get("outputs") or []:
        path = root / str(output.get("path") or "")
        if not path.is_file() or sha256_file(path) != output.get("content_hash"):
            return False
    return True


def _checkpoint_outputs_intact(
    store: TaskStore,
    job_id: str,
    stage: str,
    *,
    config_hash: str,
) -> bool:
    checkpoint = store.read_checkpoint(job_id, stage)
    if (
        not checkpoint
        or checkpoint.get("status") != "succeeded"
        or checkpoint.get("implementation_version") != _PIPELINE_IMPLEMENTATION_VERSION
        or checkpoint.get("config_hash") != config_hash
    ):
        return False
    root = store.job_path(job_id)
    return all(
        (root / str(output.get("path") or "")).is_file()
        and sha256_file(root / str(output["path"])) == output.get("content_hash")
        for output in checkpoint.get("outputs") or []
    )


def _complete_stage(
    store: TaskStore,
    job_id: str,
    stage: str,
    *,
    attempt_id: str,
    job_root: Path,
    outputs: list[Path],
    config_hash: str,
    input_hashes: list[str],
) -> None:
    store.write_checkpoint(
        job_id,
        stage,
        {
            "status": "succeeded",
            "implementation_version": _PIPELINE_IMPLEMENTATION_VERSION,
            "config_hash": config_hash,
            "input_hashes": input_hashes,
            "outputs": [
                {
                    "path": path.relative_to(job_root).as_posix(),
                    "content_hash": sha256_file(path),
                }
                for path in outputs
            ],
            "completed_at": utc_now(),
        },
        expected_attempt_id=attempt_id,
    )


def _begin_stage(
    store: TaskStore,
    job_id: str,
    attempt_id: str,
    stage: str,
    *,
    progress_sink: ProgressSink | None = None,
) -> None:
    _check_cancel(store, job_id, attempt_id, stage)
    store.write_state(
        job_id,
        state=_STATE_BY_STAGE[stage],
        current_stage=stage,
        attempt_id=attempt_id,
        event_type="stage_started",
        expected_attempt_id=attempt_id,
    )
    if progress_sink is not None:
        stage_names = list(_STATE_BY_STAGE)
        progress_sink(
            stage_progress(
                job_id=job_id,
                stage=stage,
                completed_units=stage_names.index(stage),
                total_units=len(stage_names),
                message=f"开始处理阶段：{stage}",
            )
        )


def _check_cancel(
    store: TaskStore,
    job_id: str,
    attempt_id: str,
    stage: str,
) -> None:
    if store.cancel_requested(job_id):
        raise SemvideoError(
            code="job_cancelled",
            category=ErrorCategory.CANCELLED,
            message="任务已按取消请求在安全检查点停止。",
            retryable=False,
            recovery=RecoveryAction.NONE,
            stage=stage,
            job_id=job_id,
            attempt_id=attempt_id,
            exit_code=7,
        )


@contextmanager
def _resource(
    manager: ResourceLockManager,
    resource: str,
    *,
    store: TaskStore,
    job_id: str,
    attempt_id: str,
    stage: str,
) -> Iterator[None]:
    while True:
        _check_cancel(store, job_id, attempt_id, stage)
        try:
            lease = manager.acquire(resource, timeout=1.0)
            break
        except LockAcquisitionTimeout:
            continue
    with lease:
        yield


def _media_facts_from_dict(value: dict[str, Any]) -> MediaFacts:
    return MediaFacts(
        schema_version=int(value["schema_version"]),
        source=str(value.get("source") or value["source_video_id"]),
        duration_ms=int(value["duration_ms"]),
        format_name=value.get("format_name"),
        bit_rate=value.get("bit_rate"),
        video_stream=VideoStreamFacts(**value["video_stream"]),
        audio_streams=tuple(AudioStreamFacts(**row) for row in value["audio_streams"]),
        subtitle_streams=tuple(
            SubtitleStreamFacts(**row) for row in value["subtitle_streams"]
        ),
        decodable=bool(value["decodable"]),
    )


def _media_timeline_from_dict(value: dict[str, Any]) -> MediaCandidateTimeline:
    return MediaCandidateTimeline(
        schema_version=int(value["schema_version"]),
        source_video_id=str(value["source_video_id"]),
        duration_ms=int(value["duration_ms"]),
        algorithm=dict(value["algorithm"]),
        boundaries=tuple(
            MediaCandidateBoundary(
                candidate_boundary_id=str(row["candidate_boundary_id"]),
                timestamp_ms=int(row["timestamp_ms"]),
                reasons=tuple(row["reasons"]),
                scores=dict(row["scores"]),
            )
            for row in value["boundaries"]
        ),
        segments=tuple(MediaCandidateSegment(**row) for row in value["segments"]),
    )


def _evidence_from_dict(value: dict[str, Any]) -> EvidenceTimeline:
    return EvidenceTimeline(
        schema_version=int(value["schema_version"]),
        source_video_id=str(value["source_video_id"]),
        frames=tuple(
            EvidenceFrame(
                evidence_frame_id=str(row["evidence_frame_id"]),
                timestamp_ms=int(row["timestamp_ms"]),
                artifact_id=str(row["artifact_id"]),
                relative_path=str(row["relative_path"]),
                extraction_reasons=tuple(row["extraction_reasons"]),
                scene_score=row.get("scene_score"),
                dedup=DedupDecision(**row["dedup"]),
            )
            for row in value["frames"]
        ),
        contact_sheets=tuple(
            ContactSheet(
                contact_sheet_id=str(row["contact_sheet_id"]),
                artifact_id=str(row["artifact_id"]),
                relative_path=str(row["relative_path"]),
                rows=int(row["layout"]["rows"]),
                columns=int(row["layout"]["columns"]),
                cell_frame_ids=tuple(row["cell_frame_ids"]),
            )
            for row in value["contact_sheets"]
        ),
        transcript_spans=tuple(
            TranscriptSpan(**row) for row in value["transcript_spans"]
        ),
        ocr_observations=tuple(value.get("ocr_observations") or ()),
    )


def _semantic_anchors(
    timeline: MediaCandidateTimeline,
    *,
    interval_seconds: float,
) -> list[dict[str, Any]]:
    """Build flexible cut anchors independently from extracted evidence frames."""

    at: dict[int, dict[str, Any]] = {
        0: {"reasons": ["source_start"], "scores": {}},
        timeline.duration_ms: {"reasons": ["source_end"], "scores": {}},
    }
    for boundary in timeline.boundaries:
        at[boundary.timestamp_ms] = {
            "reasons": list(boundary.reasons),
            "scores": boundary.scores,
        }
    step_ms = max(1000, round(interval_seconds * 1000))
    cursor = step_ms
    while cursor < timeline.duration_ms:
        row = at.setdefault(cursor, {"reasons": [], "scores": {}})
        if "semantic_interval" not in row["reasons"]:
            row["reasons"].append("semantic_interval")
        cursor += step_ms
    anchors: list[dict[str, Any]] = []
    for index, (timestamp_ms, details) in enumerate(sorted(at.items())):
        anchors.append(
            {
                "anchor_id": f"anchor_{index:04d}",
                "timestamp_ms": timestamp_ms,
                "reasons": details["reasons"],
                "scores": details["scores"],
            }
        )
    return anchors


def _grid_rows(
    evidence: EvidenceTimeline,
    *,
    job_root: Path,
) -> list[dict[str, Any]]:
    frame_by_id = {frame.evidence_frame_id: frame for frame in evidence.frames}
    rows: list[dict[str, Any]] = []
    for sheet in evidence.contact_sheets:
        cells = [
            {
                "cell": index,
                "frame_id": frame_id,
                "timestamp_ms": frame_by_id[frame_id].timestamp_ms,
            }
            for index, frame_id in enumerate(sheet.cell_frame_ids, start=1)
        ]
        rows.append(
            {
                "schema_version": 1,
                "grid_id": sheet.contact_sheet_id,
                "path": (Path("evidence") / sheet.relative_path).as_posix(),
                "cells": cells,
                "start_ms": cells[0]["timestamp_ms"],
                "end_ms": cells[-1]["timestamp_ms"],
            }
        )
    return rows


def _evidence_windows(
    *,
    evidence: EvidenceTimeline,
    grids: list[dict[str, Any]],
    semantic_timeline: Any,
) -> list[dict[str, Any]]:
    """V1 short/medium-video window: one bounded full-context model request."""

    return [
        {
            "schema_version": 1,
            "evidence_window_id": "window_0001",
            "source_video_id": semantic_timeline.source_video_id,
            "ordinal": 0,
            "start_ms": 0,
            "end_ms": semantic_timeline.duration_ms,
            "contact_sheet_ids": [row["grid_id"] for row in grids],
            "evidence_frame_ids": [
                frame.evidence_frame_id for frame in evidence.frames
            ],
            "candidate_boundary_ids": [
                boundary.candidate_boundary_id
                for boundary in semantic_timeline.boundaries
            ],
            "transcript_span_ids": [
                span.transcript_span_id for span in evidence.transcript_spans
            ],
            "overlap": {
                "previous_window_ms": 0,
                "next_window_ms": 0,
            },
            "policy": {
                "name": "long_context_v1",
                "version": 1,
                "maximum_evidence_frames": len(evidence.frames),
            },
        }
    ]


def _model_segment(
    config: WorkspaceConfig,
    messages: list[dict[str, Any]],
    *,
    anchors: list[dict[str, Any]],
    cooldown_path: Path | None = None,
) -> tuple[SegmentationResponse, dict[str, Any]]:
    key = os.environ.get(config.llm.credential_env, "")
    attempts: list[dict[str, Any]] = []
    repair_attempted = False

    def retain(result: Any) -> None:
        provider_attempts = list(result.provider_attempts)
        if provider_attempts:
            attempts.extend(provider_attempts)
        else:
            attempts.append(
                result.model_dump(
                    mode="json",
                    exclude={"provider_attempts"},
                )
            )

    try:
        with OpenAICompatibleLlm(
            config.llm,
            key,
            cooldown_path=cooldown_path,
        ) as client:
            result = client.chat(messages)
            retain(result)
            try:
                response = SegmentationResponse.model_validate(
                    parse_json_content(result.content)
                )
                validate_response_timeline(response, anchors)
            except (json.JSONDecodeError, ValidationError, ValueError) as first_error:
                repair_attempted = True
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": result.content},
                    {
                        "role": "user",
                        "content": (
                            "上一条响应未通过 JSON Schema 验证。请只修正格式、字段、连续覆盖"
                            "和锚点引用错误，不改变基于视频证据的语义判断；只返回严格 JSON。"
                            f"\n校验错误：{str(first_error)[:1500]}"
                        ),
                    },
                ]
                repaired = client.chat(repair_messages)
                retain(repaired)
                try:
                    response = SegmentationResponse.model_validate(
                        parse_json_content(repaired.content)
                    )
                    validate_response_timeline(response, anchors)
                except (
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as second_error:
                    error = SemvideoError(
                        code="model_segmentation_invalid_after_repair",
                        category=ErrorCategory.MODEL_RESPONSE_INVALID,
                        message="模型分段输出经一次格式修复后仍未通过结构验证。",
                        retryable=True,
                        recovery=RecoveryAction.RETRY_SAME,
                        stage="analyze",
                        details={"reason": str(second_error)},
                        exit_code=5,
                    )
                    error.model_attempts = attempts
                    error.repair_attempted = True
                    raise error from second_error
                result = repaired
    except SemvideoError as error:
        if not hasattr(error, "model_attempts"):
            error.model_attempts = [
                *attempts,
                *list(getattr(error, "provider_attempts", [])),
            ]
        if not hasattr(error, "repair_attempted"):
            error.repair_attempted = repair_attempted
        raise
    total_usage = aggregate_usage(attempts)
    return response, {
        **result.model_dump(mode="json", exclude={"provider_attempts"}),
        "usage": total_usage,
        "attempts": attempts,
        "repair_attempted": repair_attempted,
    }


def _plan_dict(plan: MergePlan) -> dict[str, Any]:
    return {
        "schema_version": plan.schema_version,
        "job_id": plan.job_id,
        "candidate_timeline_hash": plan.candidate_timeline_hash,
        "boundary_decisions_hash": plan.boundary_decisions_hash,
        "final_segments": [asdict(row) for row in plan.final_segments],
    }


def _domain_timeline_dict(timeline: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_video_id": timeline.source_video_id,
        "duration_ms": timeline.duration_ms,
        "algorithm": {
            "name": "semantic_anchor_candidates_v1",
            "version": "1",
            "note": "candidate cut positions are independent from evidence frames",
        },
        "boundaries": [asdict(row) for row in timeline.boundaries],
        "segments": [asdict(row) for row in timeline.segments],
    }


def _render_requested(job: dict[str, Any], config: WorkspaceConfig) -> bool:
    overrides = (job.get("request") or {}).get("overrides") or {}
    return bool(
        overrides.get("render_final_segments", config.render.enabled_by_default)
    )


def _segment_artifacts(
    *,
    start_ms: int,
    end_ms: int,
    evidence: EvidenceTimeline,
    windows: list[dict[str, Any]],
) -> dict[str, Any]:
    in_range = [
        frame for frame in evidence.frames if start_ms <= frame.timestamp_ms < end_ms
    ]
    nearest = min(
        in_range or list(evidence.frames),
        key=lambda frame: abs(frame.timestamp_ms - start_ms),
    )
    contact_sheets = [
        str(row["path"])
        for row in windows
        if int(row["end_ms"]) >= start_ms and int(row["start_ms"]) < end_ms
    ]
    return {
        "thumbnail": (Path("evidence") / nearest.relative_path).as_posix(),
        "contact_sheets": contact_sheets,
    }


def _inspection_html(
    *,
    job_id: str,
    records: list[Any],
    windows: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> str:
    cards: list[str] = []
    summary_by_id = {str(row["final_segment_id"]): row for row in summaries}
    for record in records:
        summary = summary_by_id[record.segment_id]
        video = (
            f'<video controls preload="metadata" src="../{html.escape(record.artifacts.video)}"></video>'
            if record.artifacts.video
            else "<p>未预渲染；可使用 segment export 按需导出。</p>"
        )
        cards.append(
            "<article>"
            f"<h2>{html.escape(record.title)}</h2>"
            f"<p><code>{html.escape(record.segment_id)}</code> · "
            f"{record.start_ms / 1000:.3f}s–{record.end_ms / 1000:.3f}s</p>"
            f"<p>{html.escape(record.detailed_summary)}</p>"
            f"<p><strong>模型理由：</strong>{html.escape(str(summary['reason']))}</p>"
            f"<details><summary>片段转写</summary><p>{html.escape(record.transcript.text or '无可用转写')}</p></details>"
            f"{video}"
            "</article>"
        )
    grids = "".join(
        f'<figure><img loading="lazy" src="../{html.escape(str(row["path"]))}">'
        f"<figcaption>{html.escape(str(row['grid_id']))} · "
        f"{row['start_ms'] / 1000:.3f}s–{row['end_ms'] / 1000:.3f}s</figcaption></figure>"
        for row in windows
    )
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Semvideo {html.escape(job_id)}</title>"
        "<style>body{font:16px/1.6 system-ui;max-width:1000px;margin:auto;padding:24px}"
        "article{border:1px solid #ddd;border-radius:12px;padding:16px;margin:16px 0}"
        "video,img{width:100%;max-height:560px;object-fit:contain;background:#111}"
        "figure{margin:16px 0}code{word-break:break-all}</style>"
        f"<body><h1>Semvideo 片段检查 · {html.escape(job_id)}</h1>"
        "<h2>时序九宫格证据</h2>"
        + grids
        + "<h2>正式语义片段</h2>"
        + "".join(cards)
        + "</body></html>"
    )


def _dependency_error(stage: str, exc: BaseException) -> SemvideoError:
    details: dict[str, Any] = {
        "exception_type": type(exc).__name__,
        "reason": str(exc),
    }
    if isinstance(exc, FfmpegError):
        details["returncode"] = exc.returncode
        details["stderr_tail"] = exc.stderr[-2000:]
    invalid_input = (
        stage == "probe" and isinstance(exc, FfmpegError) and exc.returncode is not None
    ) or (
        isinstance(exc, FfmpegError)
        and any(
            marker in str(exc).lower()
            for marker in (
                "no video stream",
                "duration could not",
                "invalid data",
                "could not find codec",
            )
        )
    )
    return SemvideoError(
        code=("media_input_invalid" if invalid_input else f"{stage}_dependency_failed"),
        category=(ErrorCategory.INPUT if invalid_input else ErrorCategory.DEPENDENCY),
        message=(
            "输入文件不是可解码且带有效视频轨的视频。"
            if invalid_input
            else f"{stage} 阶段的本地媒体依赖执行失败。"
        ),
        retryable=False,
        recovery=RecoveryAction.USER_ACTION,
        stage=stage,
        details=details,
        exit_code=3 if invalid_input else 4,
    )


def run_job(
    workspace: WorkspacePaths,
    store: TaskStore,
    job_id: str,
    attempt_id: str,
    progress_sink: ProgressSink | None = None,
) -> dict[str, Any]:
    """Run or resume one durable task package."""

    job = store.read_job(job_id)
    job_root = store.job_path(job_id)
    frozen_config = FrozenExecutionConfig.from_job(job, attempt_id=attempt_id)
    config = frozen_config.config
    config_hash = frozen_config.config_hash
    first_invalid_model_stage = next(
        (
            model_stage
            for model_stage in ("cinematography", "analyze")
            if not _checkpoint_outputs_intact(
                store,
                job_id,
                model_stage,
                config_hash=config_hash,
            )
        ),
        None,
    )
    if not config.credential_present() and first_invalid_model_stage is not None:
        raise SemvideoError(
            code="model_credential_missing",
            category=ErrorCategory.CREDENTIAL,
            message=f"缺少模型凭据环境变量：{config.llm.credential_env}",
            recovery=RecoveryAction.USER_ACTION,
            stage=first_invalid_model_stage,
            job_id=job_id,
            attempt_id=attempt_id,
            details={"credential_env": config.llm.credential_env},
            exit_code=2,
        )
    source, source_meta = resolve_source(workspace, str(job["source_video_id"]))
    source_hash = str(source_meta["content_hash"])
    limits = ResourceLimits.from_mapping(config.concurrency.model_dump())
    locks = ResourceLockManager(workspace.locks, limits)
    ffmpeg = _ffmpeg_adapter(config)
    analyzer = MediaAnalyzer(ffmpeg)

    facts_path = job_root / "media" / "facts.json"
    analysis_policy = AnalysisProxyPolicy(
        max_width=config.media.analysis_proxy_max_width,
        max_height=config.media.analysis_proxy_max_height,
        video_codec=config.media.analysis_proxy_codec,
        preset=config.media.analysis_proxy_preset,
        crf=config.media.analysis_proxy_crf,
    )

    def build_analysis_proxy(output: Path) -> None:
        with _resource(
            locks,
            "media",
            store=store,
            job_id=job_id,
            attempt_id=attempt_id,
            stage="probe",
        ):
            ffmpeg.create_analysis_proxy(
                source,
                output=output,
                expected_duration_ms=facts.duration_ms,
                max_width=analysis_policy.max_width,
                max_height=analysis_policy.max_height,
                video_codec=analysis_policy.video_codec,
                preset=analysis_policy.preset,
                crf=analysis_policy.crf,
            )

    _begin_stage(
        store,
        job_id,
        attempt_id,
        "probe",
        progress_sink=progress_sink,
    )
    try:
        probe_checkpoint_valid = _checkpoint_valid(
            store,
            job_id,
            "probe",
            config_hash=config_hash,
            input_hashes=[source_hash],
        )
        stored_facts_payload = None
        if probe_checkpoint_valid:
            stored_facts_payload = read_versioned_json(facts_path)
            facts = _media_facts_from_dict(stored_facts_payload)
        else:
            with _resource(
                locks,
                "media",
                store=store,
                job_id=job_id,
                attempt_id=attempt_id,
                stage="probe",
            ):
                facts = analyzer.probe(source)
        analysis_media = prepare_analysis_media(
            workspace,
            source_video_id=str(job["source_video_id"]),
            source=source,
            source_hash=source_hash,
            media_facts=facts,
            policy=analysis_policy,
            create_proxy=build_analysis_proxy,
        )
        facts_payload = {
            **facts.to_dict(),
            "source_video_id": job["source_video_id"],
            "content_hash": source_hash,
            "analysis_media": analysis_media.to_provenance(),
        }
        facts_payload.pop("source", None)
        if (
            not probe_checkpoint_valid
            or stored_facts_payload is None
            or stored_facts_payload.get("analysis_media")
            != facts_payload["analysis_media"]
        ):
            atomic_write_json(facts_path, facts_payload)
            _complete_stage(
                store,
                job_id,
                "probe",
                attempt_id=attempt_id,
                job_root=job_root,
                outputs=[facts_path],
                config_hash=config_hash,
                input_hashes=[source_hash],
            )
        analysis_source = analysis_media.path
    except (FfmpegError, OSError, ValueError) as exc:
        raise _dependency_error("probe", exc) from exc

    candidate_path = job_root / "segmentation" / "candidate-timeline.json"
    media_candidate_path = job_root / "segmentation" / "media-candidate-timeline.json"
    shot_timeline_path = job_root / "segmentation" / "shot-timeline.json"
    anchors_path = job_root / "segmentation" / "semantic-anchors.json"
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "segment",
        progress_sink=progress_sink,
    )
    try:
        segment_inputs = [
            sha256_file(facts_path),
            analysis_media.content_hash,
        ]
        if _checkpoint_valid(
            store,
            job_id,
            "segment",
            config_hash=config_hash,
            input_hashes=segment_inputs,
        ):
            media_timeline = _media_timeline_from_dict(
                read_versioned_json(media_candidate_path)
            )
            shot_timeline = ShotTimeline.model_validate(
                read_versioned_json(shot_timeline_path)
            )
            anchors = list(read_versioned_json(anchors_path)["anchors"])
            semantic_timeline = timeline_from_anchors(
                source_video_id=str(job["source_video_id"]),
                duration_ms=facts.duration_ms,
                anchors=anchors,
            )
        else:
            with _resource(
                locks,
                "media",
                store=store,
                job_id=job_id,
                attempt_id=attempt_id,
                stage="segment",
            ):
                media_timeline = analyzer.segment(
                    analysis_source,
                    source_video_id=str(job["source_video_id"]),
                    media_facts=facts,
                    policy=CandidatePolicy(
                        scene_threshold=config.evidence.scene_threshold,
                    ),
                )
            anchors = _semantic_anchors(
                media_timeline,
                interval_seconds=config.evidence.periodic_anchor_seconds,
            )
            shot_timeline = build_shot_timeline(media_timeline)
            semantic_timeline = timeline_from_anchors(
                source_video_id=str(job["source_video_id"]),
                duration_ms=facts.duration_ms,
                anchors=anchors,
            )
            atomic_write_json(
                media_candidate_path,
                media_timeline.to_dict(),
            )
            atomic_write_json(
                candidate_path,
                _domain_timeline_dict(semantic_timeline),
            )
            atomic_write_json(
                shot_timeline_path,
                shot_timeline.model_dump(mode="json"),
            )
            atomic_write_json(
                anchors_path,
                {
                    "schema_version": 1,
                    "source_video_id": job["source_video_id"],
                    "anchors": anchors,
                    "note": "cut anchors are independent from evidence-frame positions",
                },
            )
            _complete_stage(
                store,
                job_id,
                "segment",
                attempt_id=attempt_id,
                job_root=job_root,
                outputs=[
                    candidate_path,
                    media_candidate_path,
                    shot_timeline_path,
                    anchors_path,
                ],
                config_hash=config_hash,
                input_hashes=segment_inputs,
            )
    except (FfmpegError, OSError, ValueError) as exc:
        raise _dependency_error("segment", exc) from exc

    evidence_path = job_root / "evidence" / "evidence-timeline.json"
    transcript_path = job_root / "evidence" / "transcript.json"
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "evidence",
        progress_sink=progress_sink,
    )
    try:
        evidence_inputs = [
            sha256_file(media_candidate_path),
            analysis_media.content_hash,
        ]
        if _checkpoint_valid(
            store,
            job_id,
            "evidence",
            config_hash=config_hash,
            input_hashes=evidence_inputs,
        ):
            evidence = _evidence_from_dict(read_versioned_json(evidence_path))
        else:
            with _resource(
                locks,
                "media",
                store=store,
                job_id=job_id,
                attempt_id=attempt_id,
                stage="evidence",
            ):
                evidence = NativeEvidenceAdapter(ffmpeg=ffmpeg).extract(
                    analysis_source,
                    media_timeline,
                    job_root / "evidence",
                    media_facts=facts,
                    transcript_source=source,
                    transcript_media_facts=facts,
                    evidence_policy=EvidencePolicy(
                        maximum_interval_ms=round(
                            config.evidence.periodic_anchor_seconds * 1000
                        ),
                        max_frames=config.evidence.max_frames,
                    ),
                    transcribe_if_no_subtitles=False,
                )
            if not evidence.transcript_spans and config.evidence.transcribe:
                transcriber = FasterWhisperTranscriber(
                    FasterWhisperConfig(
                        model_name_or_path=config.evidence.whisper_model,
                        compute_type=config.evidence.whisper_compute_type,
                        cpu_threads=config.evidence.asr_cpu_threads,
                        language=config.evidence.language or None,
                    )
                )
                with _resource(
                    locks,
                    "asr",
                    store=store,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    stage="evidence",
                ):
                    transcript = tuple(transcriber.transcribe(source))
                evidence = EvidenceTimeline(
                    schema_version=evidence.schema_version,
                    source_video_id=evidence.source_video_id,
                    frames=evidence.frames,
                    contact_sheets=evidence.contact_sheets,
                    transcript_spans=transcript,
                    ocr_observations=evidence.ocr_observations,
                )
            atomic_write_json(evidence_path, evidence.to_dict())
            atomic_write_json(
                transcript_path,
                {
                    "schema_version": 1,
                    "source_video_id": job["source_video_id"],
                    "spans": [row.to_dict() for row in evidence.transcript_spans],
                },
            )
            evidence_artifacts = [
                job_root / "evidence" / frame.relative_path for frame in evidence.frames
            ] + [
                job_root / "evidence" / sheet.relative_path
                for sheet in evidence.contact_sheets
            ]
            _complete_stage(
                store,
                job_id,
                "evidence",
                attempt_id=attempt_id,
                job_root=job_root,
                outputs=[evidence_path, transcript_path, *evidence_artifacts],
                config_hash=config_hash,
                input_hashes=evidence_inputs,
            )
    except (FfmpegError, OSError, RuntimeError, ValueError) as exc:
        raise _dependency_error("evidence", exc) from exc

    cinematography_evidence_path = (
        job_root / "evidence" / "cinematography" / "index.json"
    )
    cinematography_path = job_root / "semantics" / "cinematography-annotations.jsonl"
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "cinematography",
        progress_sink=progress_sink,
    )
    cinematography_inputs = [
        analysis_media.content_hash,
        sha256_file(shot_timeline_path),
    ]
    if _checkpoint_valid(
        store,
        job_id,
        "cinematography",
        config_hash=config_hash,
        input_hashes=cinematography_inputs,
    ):
        cinematography_evidence = CinematographyEvidence.model_validate(
            read_versioned_json(cinematography_evidence_path)
        )
        cinematography_rows = read_versioned_json_lines(cinematography_path)
    else:
        try:
            with _resource(
                locks,
                "media",
                store=store,
                job_id=job_id,
                attempt_id=attempt_id,
                stage="cinematography",
            ):
                cinematography_evidence = extract_cinematography_evidence(
                    analysis_source,
                    job_root,
                    shot_timeline,
                    ffmpeg=ffmpeg,
                    policy=ShotEvidencePolicy(
                        frames_per_second=(config.cinematography.frames_per_second),
                        minimum_frames_per_shot=(
                            config.cinematography.minimum_frames_per_shot
                        ),
                        maximum_frames_per_shot=(
                            config.cinematography.maximum_frames_per_shot
                        ),
                    ),
                )
            atomic_write_json(
                cinematography_evidence_path,
                cinematography_evidence.model_dump(mode="json"),
            )
            evidence_frame_ids = {
                bundle.shot_id: [frame.frame_id for frame in bundle.frames]
                for bundle in cinematography_evidence.shots
            }
            cinematography_rows: list[dict[str, Any]] = []
            model_outputs: list[Path] = []
            batch_size = config.cinematography.max_shots_per_request
            for offset in range(
                0,
                len(cinematography_evidence.shots),
                batch_size,
            ):
                _check_cancel(
                    store,
                    job_id,
                    attempt_id,
                    "cinematography",
                )
                batch = cinematography_evidence.shots[offset : offset + batch_size]
                required_shot_ids = [bundle.shot_id for bundle in batch]
                messages = cinematography_messages(
                    batch,
                    job_root=job_root,
                )
                model_run_id = new_opaque_id("modelrun")
                started = time.monotonic()
                started_at = utc_now()
                model_input_hash = canonical_hash(
                    {
                        "shot_timeline": shot_timeline.model_dump(mode="json"),
                        "evidence": [
                            bundle.model_dump(mode="json") for bundle in batch
                        ],
                    }
                )
                try:
                    with _resource(
                        locks,
                        "llm",
                        store=store,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        stage="cinematography",
                    ):
                        batch_response, model_result = call_cinematography_model(
                            config,
                            messages,
                            timeline=shot_timeline,
                            evidence_frame_ids=evidence_frame_ids,
                            required_shot_ids=required_shot_ids,
                            cooldown_path=(
                                workspace.provider_cooldowns
                                / (
                                    f"{config.llm.provider}-"
                                    f"{canonical_hash(config.llm.model)[7:23]}.json"
                                )
                            ),
                        )
                except SemvideoError as error:
                    write_model_run(
                        job_root,
                        model_run_id=model_run_id,
                        purpose="cinematography_annotation",
                        provider=config.llm.provider,
                        model=config.llm.model,
                        prompt_version=CINEMATOGRAPHY_PROMPT_VERSION,
                        input_hash=model_input_hash,
                        status="failed",
                        started_at=started_at,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        attempts=list(getattr(error, "model_attempts", [])),
                        repair_attempted=bool(
                            getattr(error, "repair_attempted", False)
                        ),
                        failure=error.as_dict(),
                    )
                    raise
                model_artifacts = write_model_run(
                    job_root,
                    model_run_id=model_run_id,
                    purpose="cinematography_annotation",
                    provider=config.llm.provider,
                    model=config.llm.model,
                    prompt_version=CINEMATOGRAPHY_PROMPT_VERSION,
                    input_hash=model_input_hash,
                    status="succeeded",
                    started_at=started_at,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    attempts=list(model_result.get("attempts", [])),
                    repair_attempted=bool(model_result.get("repair_attempted", False)),
                )
                model_outputs.extend(
                    [model_artifacts.run_path, model_artifacts.raw_path]
                )
                cinematography_rows.extend(
                    {
                        "schema_version": 1,
                        "source_video_id": job["source_video_id"],
                        "model_run_id": model_run_id,
                        **annotation.model_dump(mode="json"),
                    }
                    for annotation in batch_response.annotations
                )
            cinematography_rows.sort(
                key=lambda row: next(
                    shot.ordinal
                    for shot in shot_timeline.shots
                    if shot.shot_id == row["shot_id"]
                )
            )
            _write_jsonl(cinematography_path, cinematography_rows)
            cinematography_artifacts = [
                job_root / frame.path
                for bundle in cinematography_evidence.shots
                for frame in bundle.frames
            ] + [
                job_root / contact_sheet_path
                for bundle in cinematography_evidence.shots
                for contact_sheet_path in bundle.contact_sheet_paths
            ]
            _complete_stage(
                store,
                job_id,
                "cinematography",
                attempt_id=attempt_id,
                job_root=job_root,
                outputs=[
                    cinematography_evidence_path,
                    cinematography_path,
                    *cinematography_artifacts,
                    *model_outputs,
                ],
                config_hash=config_hash,
                input_hashes=cinematography_inputs,
            )
        except SemvideoError:
            raise
        except (FfmpegError, OSError, RuntimeError, ValueError) as exc:
            raise _dependency_error("cinematography", exc) from exc

    windows_path = job_root / "windows" / "evidence-windows.jsonl"
    grids_path = job_root / "windows" / "grids.jsonl"
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "windows",
        progress_sink=progress_sink,
    )
    windows_inputs = [sha256_file(evidence_path)]
    if _checkpoint_valid(
        store,
        job_id,
        "windows",
        config_hash=config_hash,
        input_hashes=windows_inputs,
    ):
        window_rows = read_versioned_json_lines(windows_path)
        grid_rows = read_versioned_json_lines(grids_path)
    else:
        grid_rows = _grid_rows(evidence, job_root=job_root)
        if not grid_rows:
            raise SemvideoError(
                code="visual_evidence_empty",
                category=ErrorCategory.INPUT,
                message="没有提取到可供模型理解的视频画面证据。",
                recovery=RecoveryAction.USER_ACTION,
                stage="windows",
                exit_code=3,
            )
        window_rows = _evidence_windows(
            evidence=evidence,
            grids=grid_rows,
            semantic_timeline=semantic_timeline,
        )
        _write_jsonl(windows_path, window_rows)
        _write_jsonl(grids_path, grid_rows)
        _complete_stage(
            store,
            job_id,
            "windows",
            attempt_id=attempt_id,
            job_root=job_root,
            outputs=[windows_path, grids_path],
            config_hash=config_hash,
            input_hashes=windows_inputs,
        )

    proposal_path = job_root / "semantics" / "segmentation-proposals.jsonl"
    model_run_id: str
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "analyze",
        progress_sink=progress_sink,
    )
    analyze_inputs = [
        sha256_file(windows_path),
        sha256_file(grids_path),
        sha256_file(transcript_path),
    ]
    if _checkpoint_valid(
        store,
        job_id,
        "analyze",
        config_hash=config_hash,
        input_hashes=analyze_inputs,
    ):
        proposal_row = read_versioned_json_lines(proposal_path)[0]
        response = response_from_proposal(proposal_row, anchors=anchors)
        model_run_id = str(proposal_row["model_run_id"])
    else:
        messages = segmentation_messages(
            anchors=anchors,
            grids=grid_rows,
            transcript_spans=[span.to_dict() for span in evidence.transcript_spans],
            job_root=job_root,
        )
        started = time.monotonic()
        started_at = utc_now()
        model_run_id = new_opaque_id("modelrun")
        model_input_hash = canonical_hash(
            {
                "anchors": anchors,
                "windows": window_rows,
                "grids": grid_rows,
                "transcript": [span.to_dict() for span in evidence.transcript_spans],
            }
        )
        try:
            with _resource(
                locks,
                "llm",
                store=store,
                job_id=job_id,
                attempt_id=attempt_id,
                stage="analyze",
            ):
                response, model_result = _model_segment(
                    config,
                    messages,
                    anchors=anchors,
                    cooldown_path=(
                        workspace.provider_cooldowns
                        / f"{config.llm.provider}-{canonical_hash(config.llm.model)[7:23]}.json"
                    ),
                )
        except SemvideoError as error:
            write_model_run(
                job_root,
                model_run_id=model_run_id,
                purpose="semantic_segmentation",
                provider=config.llm.provider,
                model=config.llm.model,
                prompt_version=PROMPT_VERSION,
                input_hash=model_input_hash,
                status="failed",
                started_at=started_at,
                duration_ms=round((time.monotonic() - started) * 1000),
                attempts=list(getattr(error, "model_attempts", [])),
                repair_attempted=bool(getattr(error, "repair_attempted", False)),
                failure=error.as_dict(),
            )
            raise
        model_artifacts = write_model_run(
            job_root,
            model_run_id=model_run_id,
            purpose="semantic_segmentation",
            provider=config.llm.provider,
            model=config.llm.model,
            prompt_version=PROMPT_VERSION,
            input_hash=model_input_hash,
            status="succeeded",
            started_at=started_at,
            duration_ms=round((time.monotonic() - started) * 1000),
            attempts=list(model_result.get("attempts", [])),
            repair_attempted=bool(model_result.get("repair_attempted", False)),
        )
        run_path = model_artifacts.run_path
        raw_response_path = model_artifacts.raw_path
        proposal_row = build_segmentation_proposal(
            evidence_window_id=str(window_rows[0]["evidence_window_id"]),
            model_run_id=model_run_id,
            response=response,
            anchors=anchors,
            evidence_frames=[frame.to_dict() for frame in evidence.frames],
            transcript_spans=[span.to_dict() for span in evidence.transcript_spans],
        )
        _write_jsonl(proposal_path, [proposal_row])
        _complete_stage(
            store,
            job_id,
            "analyze",
            attempt_id=attempt_id,
            job_root=job_root,
            outputs=[proposal_path, run_path, raw_response_path],
            config_hash=config_hash,
            input_hashes=analyze_inputs,
        )

    decisions_path = job_root / "semantics" / "boundary-decisions.jsonl"
    plan_path = job_root / "plans" / "merge-plan.json"
    summaries_path = job_root / "summaries" / "final-summaries.jsonl"
    _begin_stage(
        store,
        job_id,
        attempt_id,
        "reconcile",
        progress_sink=progress_sink,
    )
    try:
        plan, summaries, decisions = proposal_to_plan(
            job_id=job_id,
            model_run_id=model_run_id,
            response=response,
            timeline=semantic_timeline,
            anchors=anchors,
        )
    except ValueError as exc:
        raise SemvideoError(
            code="semantic_plan_invalid",
            category=ErrorCategory.MODEL_RESPONSE_INVALID,
            message="模型分段无法转换为连续、无损的正式时间线。",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            stage="reconcile",
            details={"reason": str(exc)},
            exit_code=5,
        ) from exc
    _write_jsonl(
        decisions_path,
        [
            {
                "schema_version": 1,
                **asdict(decision),
            }
            for decision in decisions
        ],
    )
    _complete_stage(
        store,
        job_id,
        "reconcile",
        attempt_id=attempt_id,
        job_root=job_root,
        outputs=[decisions_path],
        config_hash=config_hash,
        input_hashes=[sha256_file(proposal_path), sha256_file(anchors_path)],
    )

    _begin_stage(
        store,
        job_id,
        attempt_id,
        "plan",
        progress_sink=progress_sink,
    )
    atomic_write_json(plan_path, _plan_dict(plan))
    _complete_stage(
        store,
        job_id,
        "plan",
        attempt_id=attempt_id,
        job_root=job_root,
        outputs=[plan_path],
        config_hash=config_hash,
        input_hashes=[sha256_file(decisions_path)],
    )

    _begin_stage(
        store,
        job_id,
        attempt_id,
        "summarize",
        progress_sink=progress_sink,
    )
    _write_jsonl(summaries_path, summaries)
    _complete_stage(
        store,
        job_id,
        "summarize",
        attempt_id=attempt_id,
        job_root=job_root,
        outputs=[summaries_path],
        config_hash=config_hash,
        input_hashes=[sha256_file(plan_path)],
    )

    _begin_stage(
        store,
        job_id,
        attempt_id,
        "retrieval",
        progress_sink=progress_sink,
    )
    plan_payload = _plan_dict(plan)
    plan_hash = canonical_hash(plan_payload)
    retrieval_path = job_root / "retrieval" / "segments.jsonl"
    render_inputs = [
        sha256_file(plan_path),
        sha256_file(summaries_path),
        sha256_file(transcript_path),
        source_hash,
    ]
    render_already_valid = _render_requested(job, config) and _checkpoint_valid(
        store,
        job_id,
        "render",
        config_hash=config_hash,
        input_hashes=render_inputs,
    )
    if render_already_valid:
        records = read_records(retrieval_path)
    else:
        records = [
            build_record(
                job_id=job_id,
                source_video_id=str(job["source_video_id"]),
                final_segment=asdict(final_segment),
                summary=summary,
                transcript_spans=[span.to_dict() for span in evidence.transcript_spans],
                profile=str(job["profile"]),
                profile_version=1,
                merge_plan_hash=plan_hash,
                artifacts=_segment_artifacts(
                    start_ms=final_segment.start_ms,
                    end_ms=final_segment.end_ms,
                    evidence=evidence,
                    windows=grid_rows,
                ),
            )
            for final_segment, summary in zip(plan.final_segments, summaries)
        ]
        write_records(retrieval_path, records)
        _complete_stage(
            store,
            job_id,
            "retrieval",
            attempt_id=attempt_id,
            job_root=job_root,
            outputs=[retrieval_path],
            config_hash=config_hash,
            input_hashes=[
                sha256_file(summaries_path),
                sha256_file(transcript_path),
            ],
        )

    render_outputs: list[Path] = []
    render_sidecars: list[Path] = []
    if _render_requested(job, config):
        _begin_stage(
            store,
            job_id,
            attempt_id,
            "render",
            progress_sink=progress_sink,
        )
        if render_already_valid:
            records = read_records(retrieval_path)
            render_outputs = [
                job_root / str(record.artifacts.video)
                for record in records
                if record.artifacts.video
            ]
        else:
            try:
                for record in records:
                    _check_cancel(store, job_id, attempt_id, "render")
                    output = job_root / "renders" / f"{record.segment_id}.mp4"
                    with _resource(
                        locks,
                        "render",
                        store=store,
                        job_id=job_id,
                        attempt_id=attempt_id,
                        stage="render",
                    ):
                        ffmpeg.render_segment(
                            source,
                            start_ms=record.start_ms,
                            end_ms=record.end_ms,
                            output=output,
                            video_codec=config.render.video_codec,
                            preset=config.render.preset,
                            crf=config.render.crf,
                            audio_codec=config.render.audio_codec,
                            audio_bitrate=config.render.audio_bitrate,
                        )
                    record.artifacts.video = (Path("renders") / output.name).as_posix()
                    render_outputs.append(output)
                    sidecar = job_root / "renders" / f"{record.segment_id}.export.json"
                    atomic_write_json(
                        sidecar,
                        {
                            "schema_version": 1,
                            "source_hash": source_hash,
                            "start_ms": record.start_ms,
                            "end_ms": record.end_ms,
                            "render_profile_hash": canonical_hash(
                                config.render.model_dump(mode="json")
                            ),
                            "content_hash": sha256_file(output),
                        },
                    )
                    render_sidecars.append(sidecar)
            except (FfmpegError, OSError) as exc:
                raise _dependency_error("render", exc) from exc
            write_records(retrieval_path, records)
            _complete_stage(
                store,
                job_id,
                "render",
                attempt_id=attempt_id,
                job_root=job_root,
                outputs=[
                    retrieval_path,
                    *render_outputs,
                    *render_sidecars,
                ],
                config_hash=config_hash,
                input_hashes=render_inputs,
            )

    report_path = job_root / "reports" / "inspection.html"
    atomic_write_bytes(
        report_path,
        _inspection_html(
            job_id=job_id,
            records=records,
            windows=grid_rows,
            summaries=summaries,
        ).encode("utf-8"),
    )
    artifacts = [
        artifact_record(job_root, facts_path, "media_facts"),
        artifact_record(job_root, candidate_path, "candidate_timeline"),
        artifact_record(job_root, media_candidate_path, "media_candidate_timeline"),
        artifact_record(job_root, shot_timeline_path, "shot_timeline"),
        artifact_record(job_root, anchors_path, "semantic_anchors"),
        artifact_record(job_root, evidence_path, "evidence_timeline"),
        artifact_record(
            job_root,
            cinematography_evidence_path,
            "cinematography_evidence",
        ),
        artifact_record(
            job_root,
            cinematography_path,
            "cinematography_annotations",
        ),
        artifact_record(job_root, transcript_path, "transcript"),
        artifact_record(job_root, windows_path, "evidence_windows"),
        artifact_record(job_root, grids_path, "grid_index"),
        artifact_record(job_root, proposal_path, "segmentation_proposals"),
        artifact_record(job_root, decisions_path, "boundary_decisions"),
        artifact_record(job_root, plan_path, "merge_plan"),
        artifact_record(job_root, summaries_path, "final_summaries"),
        artifact_record(job_root, retrieval_path, "retrieval_segments"),
        artifact_record(
            job_root,
            report_path,
            "inspection_report",
            schema_version=1,
        ),
    ]
    artifacts.extend(
        artifact_record(job_root, path, f"evidence_frame_{path.stem}")
        for path in sorted((job_root / "evidence" / "frames").glob("*"))
        if path.is_file()
    )
    artifacts.extend(
        artifact_record(job_root, path, f"contact_sheet_{path.stem}")
        for path in sorted((job_root / "evidence" / "contact-sheets").glob("*"))
        if path.is_file()
    )
    artifacts.extend(
        artifact_record(
            job_root,
            job_root / frame.path,
            f"cinematography_frame_{frame.frame_id}",
        )
        for bundle in cinematography_evidence.shots
        for frame in bundle.frames
    )
    artifacts.extend(
        artifact_record(
            job_root,
            job_root / contact_sheet_path,
            (f"cinematography_contact_sheet_{bundle.shot_id}_{sheet_index:02d}"),
        )
        for bundle in cinematography_evidence.shots
        for sheet_index, contact_sheet_path in enumerate(
            bundle.contact_sheet_paths,
            start=1,
        )
    )
    artifacts.extend(
        artifact_record(job_root, path, f"model_run_{path.stem}")
        for path in sorted((job_root / "model-runs").glob("*.json"))
    )
    artifacts.extend(
        artifact_record(
            job_root,
            path,
            f"render_{path.stem}",
            schema_version=1,
        )
        for path in render_outputs
    )
    review_count = sum(record.review_required for record in records)
    artifact_by_kind = {row["kind"]: row for row in artifacts}
    manifest_path = job_root / "manifest.json"
    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "source_video_id": job["source_video_id"],
        "profile": {"name": job["profile"], "version": 1},
        "state": "completed",
        "artifacts": artifacts,
        "counts": {
            "candidate_segments": len(semantic_timeline.segments),
            "shots": len(shot_timeline.shots),
            "cinematography_annotations": len(cinematography_rows),
            "final_segments": len(records),
            "review_required": review_count,
        },
    }
    atomic_write_json(manifest_path, manifest)
    result = {
        "job_id": job_id,
        "state": "completed",
        "source_video_id": job["source_video_id"],
        "final_segment_count": len(records),
        "shot_count": len(shot_timeline.shots),
        "cinematography_annotation_count": len(cinematography_rows),
        "rendered_segment_count": len(render_outputs),
        "review_required_count": review_count,
        "segment_records": {
            "artifact_id": artifact_by_kind["retrieval_segments"]["artifact_id"],
            "path": "retrieval/segments.jsonl",
            "count": len(records),
            "schema_version": 1,
        },
        "cinematography_records": {
            "artifact_id": artifact_by_kind["cinematography_annotations"][
                "artifact_id"
            ],
            "path": "semantics/cinematography-annotations.jsonl",
            "count": len(cinematography_rows),
            "schema_version": 1,
        },
        "manifest_artifact_id": "artifact_manifest_pending",
        "report_artifact_id": artifact_by_kind["inspection_report"]["artifact_id"],
        "manifest": "manifest.json",
        "report": "reports/inspection.html",
        "renders": [path.relative_to(job_root).as_posix() for path in render_outputs],
    }
    manifest_artifact_hash = canonical_hash(manifest)[7:31]
    result["manifest_artifact_id"] = f"artifact_{manifest_artifact_hash}"
    store.write_state(
        job_id,
        state="completed",
        current_stage=None,
        attempt_id=attempt_id,
        extra={"result": result},
        event_type="job_completed",
        expected_attempt_id=attempt_id,
    )
    return result


def export_segment(
    workspace: WorkspacePaths,
    job_id: str,
    segment_id: str,
    output: Path,
) -> dict[str, Any]:
    """Render one final semantic segment on demand through the shared lock budget."""

    store = TaskStore(workspace)
    job = store.read_job(job_id)
    job_root = store.job_path(job_id)
    retrieval_path = job_root / "retrieval" / "segments.jsonl"
    if not retrieval_path.is_file():
        raise SemvideoError(
            code="segment_records_not_ready",
            category=ErrorCategory.CONFIG,
            message=f"任务尚未产生正式片段记录：{job_id}",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            exit_code=2,
        )
    records = read_records(retrieval_path)
    record = next((row for row in records if row.segment_id == segment_id), None)
    if record is None:
        raise SemvideoError(
            code="segment_not_found",
            category=ErrorCategory.INPUT,
            message=f"正式片段不存在：{segment_id}",
            recovery=RecoveryAction.USER_ACTION,
            entity_id=segment_id,
            exit_code=3,
        )
    source, _ = resolve_source(workspace, str(job["source_video_id"]))
    destination = output.expanduser().resolve()
    registered_output = (
        job_root / record.artifacts.video if record.artifacts.video else None
    )
    if destination.is_relative_to(job_root.resolve()) and (
        registered_output is None or destination != registered_output.resolve()
    ):
        raise SemvideoError(
            code="segment_export_destination_inside_job",
            category=ErrorCategory.CONFIG,
            message="按需导出路径必须位于任务包之外。",
            retryable=True,
            recovery=RecoveryAction.CORRECT_AND_RETRY,
            entity_id=segment_id,
            details={"job_id": job_id},
            exit_code=2,
        )
    export_result = export_time_range(
        workspace,
        job_root=job_root,
        source=source,
        start_ms=record.start_ms,
        end_ms=record.end_ms,
        output=destination,
        registered=(
            RegisteredTaskMedia(
                relative_path=record.artifacts.video,
                invalid_code="registered_segment_artifact_invalid",
                invalid_message=("正式片段已登记的视频产物缺失或哈希不一致。"),
                entity_id=segment_id,
                job_id=job_id,
            )
            if record.artifacts.video
            else None
        ),
    )
    return {
        "schema_version": 1,
        "job_id": job_id,
        "segment_id": segment_id,
        "start_ms": record.start_ms,
        "end_ms": record.end_ms,
        "output": str(export_result.destination),
        "task_artifact": export_result.task_artifact,
        "reused": export_result.reused,
        "content_hash": sha256_file(export_result.destination),
    }


def export_shot(
    workspace: WorkspacePaths,
    job_id: str,
    shot_id: str,
    output: Path,
) -> dict[str, Any]:
    """Render one detected shot without mutating the completed task package."""

    store = TaskStore(workspace)
    job = store.read_job(job_id)
    job_root = store.job_path(job_id)
    timeline_path = job_root / "segmentation" / "shot-timeline.json"
    if not timeline_path.is_file():
        raise SemvideoError(
            code="shot_records_not_ready",
            category=ErrorCategory.CONFIG,
            message=f"任务尚未产生镜头记录：{job_id}",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            exit_code=2,
        )
    timeline = ShotTimeline.model_validate(read_versioned_json(timeline_path))
    shot = next(
        (row for row in timeline.shots if row.shot_id == shot_id),
        None,
    )
    if shot is None:
        raise SemvideoError(
            code="shot_not_found",
            category=ErrorCategory.INPUT,
            message=f"镜头不存在：{shot_id}",
            recovery=RecoveryAction.USER_ACTION,
            entity_id=shot_id,
            details={"job_id": job_id},
            exit_code=3,
        )
    destination = output.expanduser().resolve()
    if destination.is_relative_to(job_root.resolve()):
        raise SemvideoError(
            code="shot_export_destination_inside_job",
            category=ErrorCategory.CONFIG,
            message="按需导出路径必须位于任务包之外。",
            retryable=True,
            recovery=RecoveryAction.CORRECT_AND_RETRY,
            entity_id=shot_id,
            details={"job_id": job_id},
            exit_code=2,
        )
    source, _ = resolve_source(workspace, str(job["source_video_id"]))
    export_result = export_time_range(
        workspace,
        job_root=job_root,
        source=source,
        start_ms=shot.start_ms,
        end_ms=shot.end_ms,
        output=destination,
    )
    return {
        "schema_version": 1,
        "job_id": job_id,
        "shot_id": shot_id,
        "start_ms": shot.start_ms,
        "end_ms": shot.end_ms,
        "output": str(export_result.destination),
        "content_hash": sha256_file(export_result.destination),
    }
