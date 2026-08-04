from __future__ import annotations

import json
from pathlib import Path

import pytest

from semvideo.adapters.ffmpeg import FfmpegError, MediaFacts, VideoStreamFacts
from semvideo.application.processor import export_segment, export_shot, run_job
from semvideo.application.source_store import (
    register_source,
    resolve_source,
    sha256_file,
)
from semvideo.application.task_store import TaskStore
from semvideo.application.workspace import initialize_workspace
from semvideo.modules.evidence.models import (
    ContactSheet,
    DedupDecision,
    EvidenceFrame,
    EvidenceTimeline,
)
from semvideo.modules.cinematography.models import (
    CameraMotion,
    CinematographyAnnotation,
    CinematographyResponse,
    ShotScale,
)
from semvideo.modules.media.models import (
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
)
from semvideo.modules.media.subtitles import TranscriptSpan
from semvideo.modules.semantics.models import (
    ModelCallResult,
    ModelUsage,
    SegmentationResponse,
    SemanticSegment,
)
from semvideo.errors import (
    ErrorCategory,
    RecoveryAction,
    SemvideoError,
)


class FakeFfmpeg:
    proxy_sources: list[Path] = []
    render_sources: list[Path] = []
    frame_sources: list[Path] = []
    proxy_generation = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def render_segment(self, source: Path, *, output: Path, **kwargs) -> Path:
        type(self).render_sources.append(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-mp4")
        return output

    def extract_frame(self, source: Path, *, output: Path, **kwargs) -> Path:
        from PIL import Image

        type(self).frame_sources.append(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 54), "#336699").save(output)
        return output

    def create_analysis_proxy(
        self,
        source: Path,
        *,
        output: Path,
        **kwargs,
    ) -> Path:
        type(self).proxy_sources.append(source)
        type(self).proxy_generation += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(
            f"fake-analysis-proxy-{type(self).proxy_generation}".encode()
        )
        return output


class FailingRenderFfmpeg(FakeFfmpeg):
    def render_segment(self, source: Path, *, output: Path, **kwargs) -> Path:
        raise FfmpegError("render failed")


class FakeMediaAnalyzer:
    segment_sources: list[Path] = []

    def __init__(self, ffmpeg) -> None:
        pass

    def probe(self, source: Path) -> MediaFacts:
        return MediaFacts(
            schema_version=1,
            source=str(source),
            duration_ms=4000,
            format_name="fake",
            bit_rate=1000,
            video_stream=VideoStreamFacts(
                index=0,
                codec="fake",
                width=640,
                height=360,
                pixel_format="yuv420p",
                avg_frame_rate="25/1",
                nominal_fps=25.0,
                time_base="1/1000",
                rotation_degrees=0,
            ),
            audio_streams=(),
            subtitle_streams=(),
            decodable=True,
        )

    def segment(self, source: Path, *, source_video_id: str, **kwargs):
        type(self).segment_sources.append(source)
        boundary = CandidateBoundary(
            candidate_boundary_id="boundary_0001",
            timestamp_ms=2000,
            reasons=("scene_change",),
            scores={"scene_change": 0.8},
        )
        return CandidateTimeline(
            schema_version=1,
            source_video_id=source_video_id,
            duration_ms=4000,
            algorithm={"name": "fake", "version": "1", "parameters": {}},
            boundaries=(boundary,),
            segments=(
                CandidateSegment(
                    candidate_segment_id="candidate_0001",
                    ordinal=0,
                    start_ms=0,
                    end_ms=2000,
                    left_boundary_id=None,
                    right_boundary_id=boundary.candidate_boundary_id,
                ),
                CandidateSegment(
                    candidate_segment_id="candidate_0002",
                    ordinal=1,
                    start_ms=2000,
                    end_ms=4000,
                    left_boundary_id=boundary.candidate_boundary_id,
                    right_boundary_id=None,
                ),
            ),
        )


class FakeEvidenceAdapter:
    extract_sources: list[Path] = []
    transcript_sources: list[Path] = []

    def __init__(self, **kwargs) -> None:
        pass

    def extract(
        self,
        source: Path,
        timeline: CandidateTimeline,
        output_dir: Path,
        **kwargs,
    ) -> EvidenceTimeline:
        type(self).extract_sources.append(source)
        type(self).transcript_sources.append(
            kwargs.get("transcript_source", source)
        )
        frame_dir = output_dir / "frames"
        sheet_dir = output_dir / "contact-sheets"
        frame_dir.mkdir(parents=True, exist_ok=True)
        sheet_dir.mkdir(parents=True, exist_ok=True)
        (frame_dir / "frame_0001.jpg").write_bytes(b"frame")
        (sheet_dir / "sheet_0001.jpg").write_bytes(b"sheet")
        frame = EvidenceFrame(
            evidence_frame_id="frame_0001",
            timestamp_ms=1000,
            artifact_id="artifact_frame_0001",
            relative_path="frames/frame_0001.jpg",
            extraction_reasons=("candidate_context",),
            scene_score=None,
            dedup=DedupDecision(decision="kept", reason="first_frame"),
        )
        return EvidenceTimeline(
            schema_version=1,
            source_video_id=timeline.source_video_id,
            frames=(frame,),
            contact_sheets=(
                ContactSheet(
                    contact_sheet_id="sheet_0001",
                    artifact_id="artifact_sheet_0001",
                    relative_path="contact-sheets/sheet_0001.jpg",
                    rows=3,
                    columns=3,
                    cell_frame_ids=(frame.evidence_frame_id,),
                ),
            ),
            transcript_spans=(
                TranscriptSpan(
                    transcript_span_id="transcript_0001",
                    start_ms=0,
                    end_ms=4000,
                    text="第一事件，随后第二事件。",
                    source="embedded_subtitle",
                    language="zh",
                ),
            ),
        )


def _fake_model(config, messages, **kwargs):
    return (
        SegmentationResponse(
            segments=[
                SemanticSegment(
                    title="第一事件",
                    short_summary="第一事件发生。",
                    detailed_summary="视频前半段展示第一事件。",
                    visual_summary="第一画面。",
                    event="第一事件",
                    start_anchor_id="anchor_0000",
                    end_anchor_id="anchor_0001",
                    reason="事件目标改变。",
                    topics=["测试"],
                    keywords=["第一"],
                    confidence=0.9,
                ),
                SemanticSegment(
                    title="第二事件",
                    short_summary="第二事件发生。",
                    detailed_summary="视频后半段展示第二事件。",
                    visual_summary="第二画面。",
                    event="第二事件",
                    start_anchor_id="anchor_0001",
                    end_anchor_id="anchor_0002",
                    reason="事件目标改变。",
                    topics=["测试"],
                    keywords=["第二"],
                    confidence=0.8,
                ),
            ]
        ),
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
            "trace_id": "trace-test",
            "raw_response": {"id": "response-test"},
            "attempts": [
                {
                    "content": "{}",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                    "trace_id": "trace-test",
                    "response_headers": {},
                    "raw_response": {"id": "response-test"},
                }
            ],
            "repair_attempted": False,
        },
    )


def _fake_cinematography_model(
    config,
    messages,
    *,
    timeline,
    evidence_frame_ids,
    required_shot_ids,
    **kwargs,
):
    annotations = []
    for shot_id in required_shot_ids:
        shot = next(row for row in timeline.shots if row.shot_id == shot_id)
        annotations.append(
            CinematographyAnnotation(
                shot_id=shot_id,
                viewpoints=["aerial"],
                shot_scale=ShotScale(
                    start="extreme_wide",
                    end="wide",
                ),
                camera_motions=[
                    CameraMotion(
                        type="rise",
                        direction="up",
                        speed="slow",
                        temporal_profile="gradual",
                        start_ms=shot.start_ms,
                        end_ms=shot.end_ms,
                        confidence=0.9,
                    )
                ],
                cinematography_summary="航拍大全景逐渐拉高。",
                cinematography_keywords=["航拍", "大全景", "逐渐拉高"],
                evidence_frame_ids=list(evidence_frame_ids[shot_id]),
                confidence=0.9,
            )
        )
    return (
        CinematographyResponse(annotations=annotations),
        {
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
            "trace_id": "trace-cinematography",
            "raw_response": {"id": "response-cinematography"},
            "attempts": [
                {
                    "content": "{}",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "total_tokens": 30,
                    },
                    "trace_id": "trace-cinematography",
                    "response_headers": {},
                    "raw_response": {"id": "response-cinematography"},
                }
            ],
            "repair_attempted": False,
        },
    )


def _prepare_pipeline_job(tmp_path: Path, monkeypatch):
    from semvideo.application import processor

    monkeypatch.setattr(processor, "FfmpegAdapter", FakeFfmpeg)
    monkeypatch.setattr(
        "semvideo.application.range_export.FfmpegAdapter",
        FakeFfmpeg,
    )
    monkeypatch.setattr(processor, "MediaAnalyzer", FakeMediaAnalyzer)
    monkeypatch.setattr(processor, "NativeEvidenceAdapter", FakeEvidenceAdapter)
    monkeypatch.setattr(
        processor,
        "call_cinematography_model",
        _fake_cinematography_model,
    )
    monkeypatch.setenv("SEMVIDEO_API_KEY", "local-test-key")
    workspace = initialize_workspace(tmp_path / "workspace")
    source_path = tmp_path / "input.mp4"
    source_path.write_bytes(b"source-video")
    source = register_source(workspace, source_path)
    store = TaskStore(workspace)
    job = store.create_job(
        source_video_id=source["source_video_id"],
        request={"overrides": {"render_final_segments": False}},
    )
    store.write_state(
        job["job_id"],
        state="queued",
        attempt_id="attempt_test",
    )
    return workspace, store, job


class FourKMediaAnalyzer(FakeMediaAnalyzer):
    def probe(self, source: Path) -> MediaFacts:
        facts = super().probe(source)
        return MediaFacts(
            schema_version=facts.schema_version,
            source=facts.source,
            duration_ms=facts.duration_ms,
            format_name=facts.format_name,
            bit_rate=facts.bit_rate,
            video_stream=VideoStreamFacts(
                index=facts.video_stream.index,
                codec=facts.video_stream.codec,
                width=3840,
                height=2160,
                pixel_format=facts.video_stream.pixel_format,
                avg_frame_rate=facts.video_stream.avg_frame_rate,
                nominal_fps=facts.video_stream.nominal_fps,
                time_base=facts.video_stream.time_base,
                rotation_degrees=facts.video_stream.rotation_degrees,
            ),
            audio_streams=facts.audio_streams,
            subtitle_streams=facts.subtitle_streams,
            decodable=facts.decodable,
        )


def test_pipeline_analyzes_4k_proxy_but_exports_original(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    monkeypatch.setattr(processor, "MediaAnalyzer", FourKMediaAnalyzer)
    monkeypatch.setattr(processor, "_model_segment", _fake_model)
    FakeFfmpeg.proxy_sources.clear()
    FakeFfmpeg.render_sources.clear()
    FakeFfmpeg.frame_sources.clear()
    FakeFfmpeg.proxy_generation = 0
    FourKMediaAnalyzer.segment_sources.clear()
    FakeEvidenceAdapter.extract_sources.clear()
    FakeEvidenceAdapter.transcript_sources.clear()

    run_job(workspace, store, job["job_id"], "attempt_test")

    job_root = store.job_path(job["job_id"])
    facts = json.loads(
        (job_root / "media" / "facts.json").read_text(encoding="utf-8")
    )
    assert facts["analysis_media"]["kind"] == "analysis_proxy"
    assert facts["analysis_media"]["policy"]["max_height"] == 720
    source_video_id = str(store.read_job(job["job_id"])["source_video_id"])
    analysis_source = (
        workspace.sources
        / source_video_id
        / facts["analysis_media"]["relative_path"]
    )
    original_source, _ = resolve_source(workspace, source_video_id)
    assert FourKMediaAnalyzer.segment_sources == [analysis_source]
    assert FakeEvidenceAdapter.extract_sources == [analysis_source]
    assert FakeEvidenceAdapter.transcript_sources == [original_source]
    assert FakeFfmpeg.proxy_sources == [original_source]
    assert FakeFfmpeg.frame_sources
    assert set(FakeFfmpeg.frame_sources) == {analysis_source}

    first_proxy_hash = facts["analysis_media"]["content_hash"]
    analysis_source.write_bytes(b"tampered-proxy")
    store.write_state(
        job["job_id"],
        state="queued",
        attempt_id="attempt_proxy_repair",
    )
    run_job(
        workspace,
        store,
        job["job_id"],
        "attempt_proxy_repair",
    )
    repaired_facts = json.loads(
        (job_root / "media" / "facts.json").read_text(encoding="utf-8")
    )
    assert repaired_facts["analysis_media"]["content_hash"] != first_proxy_hash
    assert repaired_facts["analysis_media"]["content_hash"] == sha256_file(
        analysis_source
    )
    assert FourKMediaAnalyzer.segment_sources == [
        analysis_source,
        analysis_source,
    ]

    export_segment(
        workspace,
        job["job_id"],
        "segment_0001",
        tmp_path / "segment.mp4",
    )
    export_shot(
        workspace,
        job["job_id"],
        "shot_0001",
        tmp_path / "shot.mp4",
    )
    assert FakeFfmpeg.render_sources[-2:] == [original_source, original_source]


class _InvalidResponseLlm:
    call_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass

    def chat(self, messages):
        type(self).call_count += 1
        return ModelCallResult(
            content="not-json",
            usage=ModelUsage(
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
            ),
            trace_id=f"trace-{self.call_count}",
            raw_response={"id": f"response-{self.call_count}"},
        )


class _ProviderFailureLlm(_InvalidResponseLlm):
    def chat(self, messages):
        error = SemvideoError(
            code="provider_network_failed",
            category=ErrorCategory.PROVIDER_TRANSIENT,
            message="provider unavailable",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            stage="analyze",
            exit_code=5,
        )
        error.provider_attempts = [
            {
                "attempt": 1,
                "outcome": "network_error",
                "error_type": "ConnectError",
                "error": "connection refused",
            }
        ]
        raise error


def test_invalid_model_responses_are_audited_after_repair_exhaustion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    _InvalidResponseLlm.call_count = 0
    monkeypatch.setattr(
        processor,
        "OpenAICompatibleLlm",
        _InvalidResponseLlm,
    )

    with pytest.raises(SemvideoError) as raised:
        run_job(workspace, store, job["job_id"], "attempt_test")

    assert raised.value.payload.code == "model_segmentation_invalid_after_repair"
    model_run_files = sorted(
        store.job_path(job["job_id"]).joinpath("model-runs").glob("*.json")
    )
    run_file = next(
        path
        for path in model_run_files
        if ".raw." not in path.name
        and json.loads(path.read_text(encoding="utf-8")).get("purpose")
        == "semantic_segmentation"
    )
    model_run_id = json.loads(
        run_file.read_text(encoding="utf-8")
    )["model_run_id"]
    raw_file = next(
        path
        for path in model_run_files
        if path.name == f"{model_run_id}.raw.json"
    )
    run = json.loads(run_file.read_text(encoding="utf-8"))
    raw = json.loads(raw_file.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert "raw_response_artifact_id" in run
    assert "raw_response_artifact" not in run
    assert run["failure"]["category"] == "model_response_invalid"
    assert run["usage"] == {
        "input_tokens": 20,
        "output_tokens": 4,
        "total_tokens": 24,
    }
    assert len(raw["attempts"]) == 2


def test_final_provider_failure_creates_failed_model_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    monkeypatch.setattr(
        processor,
        "OpenAICompatibleLlm",
        _ProviderFailureLlm,
    )

    with pytest.raises(SemvideoError) as raised:
        run_job(workspace, store, job["job_id"], "attempt_test")

    assert raised.value.payload.category == ErrorCategory.PROVIDER_TRANSIENT
    model_run_files = sorted(
        store.job_path(job["job_id"]).joinpath("model-runs").glob("*.json")
    )
    run_file = next(
        path
        for path in model_run_files
        if ".raw." not in path.name
        and json.loads(path.read_text(encoding="utf-8")).get("purpose")
        == "semantic_segmentation"
    )
    model_run_id = json.loads(
        run_file.read_text(encoding="utf-8")
    )["model_run_id"]
    raw_file = next(
        path
        for path in model_run_files
        if path.name == f"{model_run_id}.raw.json"
    )
    run = json.loads(run_file.read_text(encoding="utf-8"))
    raw = json.loads(raw_file.read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert "raw_response_artifact_id" in run
    assert "raw_response_artifact" not in run
    assert run["failure"]["category"] == "provider_transient"
    assert run["usage"]["total_tokens"] == 0
    assert raw["attempts"] == [
        {
            "attempt": 1,
            "error": "connection refused",
            "error_type": "ConnectError",
            "outcome": "network_error",
        }
    ]


def test_missing_credential_reports_first_invalid_model_stage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    monkeypatch.setattr(processor, "_model_segment", _fake_model)
    run_job(workspace, store, job["job_id"], "attempt_test")
    store.invalidate_checkpoint(job["job_id"], "analyze")
    store.write_state(
        job["job_id"],
        state="queued",
        attempt_id="attempt_retry",
    )
    monkeypatch.delenv("SEMVIDEO_API_KEY")

    with pytest.raises(SemvideoError) as raised:
        run_job(
            workspace,
            store,
            job["job_id"],
            "attempt_retry",
        )

    assert raised.value.payload.code == "model_credential_missing"
    assert raised.value.payload.stage == "analyze"


def test_formal_pipeline_and_lazy_export(tmp_path, monkeypatch) -> None:
    from semvideo.application import processor

    monkeypatch.setattr(processor, "FfmpegAdapter", FakeFfmpeg)
    monkeypatch.setattr(
        "semvideo.application.range_export.FfmpegAdapter",
        FakeFfmpeg,
    )
    monkeypatch.setattr(processor, "MediaAnalyzer", FakeMediaAnalyzer)
    monkeypatch.setattr(processor, "NativeEvidenceAdapter", FakeEvidenceAdapter)
    monkeypatch.setattr(processor, "_model_segment", _fake_model)
    monkeypatch.setattr(
        processor,
        "call_cinematography_model",
        _fake_cinematography_model,
    )
    monkeypatch.setenv("SEMVIDEO_API_KEY", "local-test-key")

    workspace = initialize_workspace(tmp_path / "workspace")
    workspace.config.write_text(
        workspace.config.read_text(encoding="utf-8").replace(
            "minimum_frames_per_shot = 4\nmaximum_frames_per_shot = 9",
            "minimum_frames_per_shot = 12\nmaximum_frames_per_shot = 12",
        ),
        encoding="utf-8",
    )
    source_path = tmp_path / "input.mp4"
    source_path.write_bytes(b"source-video")
    source = register_source(workspace, source_path)
    store = TaskStore(workspace)
    job = store.create_job(
        source_video_id=source["source_video_id"],
        request={"overrides": {"render_final_segments": False}},
    )
    store.write_state(
        job["job_id"],
        state="queued",
        attempt_id="attempt_test",
    )

    progress_events = []
    result = run_job(
        workspace,
        store,
        job["job_id"],
        "attempt_test",
        progress_sink=progress_events.append,
    )

    assert result["state"] == "completed"
    assert [event["stage"] for event in progress_events] == [
        "probe",
        "segment",
        "evidence",
        "cinematography",
        "windows",
        "analyze",
        "reconcile",
        "plan",
        "summarize",
        "retrieval",
    ]
    assert all(event["event"] == "stage_progress" for event in progress_events)
    assert all(event["job_id"] == job["job_id"] for event in progress_events)
    assert result["final_segment_count"] == 2
    assert result["shot_count"] == 2
    assert result["cinematography_annotation_count"] == 2
    assert result["renders"] == []
    job_root = store.job_path(job["job_id"])
    assert (job_root / "manifest.json").is_file()
    assert (job_root / "retrieval" / "segments.jsonl").is_file()
    assert (job_root / "reports" / "inspection.html").is_file()
    manifest = json.loads(
        (job_root / "manifest.json").read_text(encoding="utf-8")
    )
    model_run = json.loads(
        next(
            path
            for path in (job_root / "model-runs").glob("*.json")
            if ".raw." not in path.name
        ).read_text(encoding="utf-8")
    )
    assert model_run["raw_response_artifact_id"] in {
        artifact["artifact_id"] for artifact in manifest["artifacts"]
    }
    cinematography_index = json.loads(
        (
            job_root
            / "evidence"
            / "cinematography"
            / "index.json"
        ).read_text(encoding="utf-8")
    )
    referenced_cinematography_evidence = {
        frame["path"]
        for shot in cinematography_index["shots"]
        for frame in shot["frames"]
    } | {
        contact_sheet_path
        for shot in cinematography_index["shots"]
        for contact_sheet_path in shot["contact_sheet_paths"]
    }
    manifest_paths = {
        artifact["path"] for artifact in manifest["artifacts"]
    }
    assert referenced_cinematography_evidence <= manifest_paths

    window = json.loads(
        (job_root / "windows" / "evidence-windows.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert window["source_video_id"] == source["source_video_id"]
    assert window["contact_sheet_ids"] == ["sheet_0001"]
    assert window["overlap"] == {
        "previous_window_ms": 0,
        "next_window_ms": 0,
    }
    assert window["policy"]["name"] == "long_context_v1"
    assert window["policy"]["version"] == 1
    assert "grid_ids" not in window
    assert "overlap_left_ms" not in window

    proposal = json.loads(
        (job_root / "semantics" / "segmentation-proposals.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert proposal["evidence_window_id"] == "window_0001"
    assert proposal["model_run_id"].startswith("modelrun_")
    assert proposal["warnings"] == []
    assert "response" not in proposal
    assert "evidence_window_ids" not in proposal
    first_proposal = proposal["segments"][0]
    assert first_proposal["proposal_segment_id"] == "proposal_segment_0001"
    assert first_proposal["start_boundary_id"] is None
    assert first_proposal["end_boundary_id"] == "anchor_0001"
    assert first_proposal["summary"] == "第一事件发生。"
    assert first_proposal["narrative_event"] == "第一事件"
    assert first_proposal["boundary_reason"] == "事件目标改变。"
    assert first_proposal["evidence_frame_ids"] == ["frame_0001"]
    assert first_proposal["transcript_span_ids"] == ["transcript_0001"]
    assert first_proposal["needs_boundary_refinement"] is False
    assert proposal["segments"][1]["start_boundary_id"] == "anchor_0001"
    assert proposal["segments"][1]["end_boundary_id"] is None

    protected_package_files = {
        relative: (job_root / relative).read_bytes()
        for relative in (
            "retrieval/segments.jsonl",
            "manifest.json",
            "stages/retrieval/checkpoint.json",
            "reports/inspection.html",
        )
    }
    output = tmp_path / "exported.mp4"
    exported = export_segment(
        workspace,
        job["job_id"],
        "segment_0001",
        output,
    )
    assert output.read_bytes() == b"fake-mp4"
    assert exported["start_ms"] == 0
    assert exported["end_ms"] == 2000
    assert exported["task_artifact"] is None
    assert not list((job_root / "renders").iterdir())
    for relative, original in protected_package_files.items():
        assert (job_root / relative).read_bytes() == original

    shot_output = tmp_path / "shot.mp4"
    shot_exported = export_shot(
        workspace,
        job["job_id"],
        "shot_0001",
        shot_output,
    )
    assert shot_output.read_bytes() == b"fake-mp4"
    assert shot_exported["start_ms"] == 0
    assert shot_exported["end_ms"] == 2000
    for relative, original in protected_package_files.items():
        assert (job_root / relative).read_bytes() == original


def test_lazy_export_preserves_render_error_when_temp_cleanup_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    monkeypatch.setattr(processor, "_model_segment", _fake_model)
    run_job(workspace, store, job["job_id"], "attempt_test")
    monkeypatch.setattr(
        "semvideo.application.range_export.FfmpegAdapter",
        FailingRenderFfmpeg,
    )
    original_unlink = Path.unlink

    def reject_export_temp_delete(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(".exported.") and path.suffix == ".mp4":
            raise OSError("safe-delete unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_export_temp_delete)

    with pytest.raises(SemvideoError) as raised:
        export_segment(
            workspace,
            job["job_id"],
            "segment_0001",
            tmp_path / "exported.mp4",
        )

    assert raised.value.payload.details["exception_type"] == "FfmpegError"
    assert raised.value.payload.details["reason"] == "render failed"


def test_worker_and_lazy_export_use_configured_media_executables(
    tmp_path,
    monkeypatch,
) -> None:
    from semvideo.application import processor

    workspace, store, job = _prepare_pipeline_job(tmp_path, monkeypatch)
    config_text = workspace.config.read_text(encoding="utf-8")
    workspace.config.write_text(
        config_text.replace(
            'ffmpeg_path = "ffmpeg"',
            'ffmpeg_path = "C:/tools/ffmpeg.exe"',
        ).replace(
            'ffprobe_path = "ffprobe"',
            'ffprobe_path = "C:/tools/ffprobe.exe"',
        ),
        encoding="utf-8",
    )
    created_with: list[dict[str, object]] = []

    def configured_adapter(**kwargs):
        created_with.append(kwargs)
        return FakeFfmpeg()

    monkeypatch.setattr(processor, "FfmpegAdapter", configured_adapter)
    monkeypatch.setattr(
        "semvideo.application.range_export.FfmpegAdapter",
        configured_adapter,
    )
    monkeypatch.setattr(processor, "_model_segment", _fake_model)

    run_job(workspace, store, job["job_id"], "attempt_test")
    export_segment(
        workspace,
        job["job_id"],
        "segment_0001",
        tmp_path / "exported.mp4",
    )

    assert created_with == [
        {
            "ffmpeg_path": "C:/tools/ffmpeg.exe",
            "ffprobe_path": "C:/tools/ffprobe.exe",
        },
        {
            "ffmpeg_path": "C:/tools/ffmpeg.exe",
            "ffprobe_path": "C:/tools/ffprobe.exe",
        },
    ]
