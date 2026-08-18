from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from semvideo.adapters.ffmpeg import FfmpegCapabilities
from semvideo.application.diagnostics import run_doctor
from semvideo.application.task_store import TaskStore
from semvideo.application.workspace import initialize_workspace
from semvideo.cli import app, main
from semvideo.config import load_workspace_config
from semvideo.modules.retrieval.records import build_record, write_records


def _write_ready_shot_records(
    store: TaskStore,
    job_id: str,
) -> None:
    job_root = store.job_path(job_id)
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
        """{"schema_version":1,"source_video_id":"video_001","model_run_id":"modelrun_001","shot_id":"shot_0001","viewpoints":["aerial"],"shot_scale":{"start":"wide","end":"wide"},"camera_motions":[{"type":"static","direction":"none","speed":"still","temporal_profile":"constant","start_ms":0,"end_ms":1000,"confidence":0.9}],"cinematography_summary":"航拍固定大全景。","cinematography_keywords":["航拍","固定镜头"],"evidence_frame_ids":["shot_0001_frame_01"],"confidence":0.9}
""",
        encoding="utf-8",
    )


def test_json_invocation_error_is_structured(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(sys, "argv", ["semvideo", "process", "--json"])

    with pytest.raises(SystemExit) as raised:
        main()

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert raised.value.code == 2
    assert captured.out == ""
    assert payload["category"] == "invocation_error"
    assert payload["retryable"] is True
    assert payload["recovery"] == "correct_and_retry"


def test_review_only_filters_before_pagination(tmp_path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    records = []
    for ordinal, review in enumerate((False, True, True)):
        start_ms = ordinal * 1000
        records.append(
            build_record(
                job_id=job["job_id"],
                source_video_id=job["source_video_id"],
                final_segment={
                    "final_segment_id": f"segment_{ordinal + 1:04d}",
                    "ordinal": ordinal,
                    "start_ms": start_ms,
                    "end_ms": start_ms + 1000,
                    "review_reasons": (
                        ["low_model_confidence"] if review else []
                    ),
                },
                summary={
                    "title": f"片段 {ordinal}",
                    "short_summary": "摘要",
                    "detailed_summary": "详细摘要",
                    "confidence": 0.5 if review else 0.9,
                },
                transcript_spans=[],
                profile="default",
                profile_version=1,
                merge_plan_hash="sha256:test",
            )
        )
    write_records(
        store.job_path(job["job_id"]) / "retrieval" / "segments.jsonl",
        records,
    )

    result = CliRunner().invoke(
        app,
        [
            "segment",
            "list",
            job["job_id"],
            "--workspace",
            str(workspace.root),
            "--review-only",
            "--limit",
            "1",
            "--json",
        ],
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["total"] == 2
    assert len(payload["items"]) == 1
    assert payload["items"][0]["segment_id"] == "segment_0002"


def test_doctor_reports_pyav_as_required_asr_capability(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str):
        if name == "av":
            return None
        if name == "faster_whisper":
            return object()
        return original_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    result = CliRunner().invoke(
        app,
        [
            "doctor",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["pyav"]["ok"] is False
    assert payload["asr_adapter"]["ok"] is False
    assert payload["ok"] is False


def test_doctor_reports_cleanup_warning_when_host_rejects_temp_delete(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    original_unlink = Path.unlink

    def reject_doctor_temp_delete(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(".doctor-atomic-"):
            raise OSError("safe-delete unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_doctor_temp_delete)

    payload = run_doctor(workspace)

    assert payload["atomic_replace"]["ok"] is True
    assert payload["atomic_replace"]["warnings"] == [
        {
            "code": "diagnostic_cleanup_failed",
            "path": str(workspace.runtime / f".doctor-atomic-{os.getpid()}.json"),
        }
    ]


def test_doctor_uses_configured_media_tools_and_reports_render_capabilities(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
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

    class FakeCapabilityAdapter:
        def __init__(self, **kwargs) -> None:
            created_with.append(kwargs)

        def inspect_capabilities(self) -> FfmpegCapabilities:
            return FfmpegCapabilities(
                ffmpeg_path="C:/tools/ffmpeg.exe",
                ffprobe_path="C:/tools/ffprobe.exe",
                ffmpeg_version="ffmpeg version 8.1.2",
                ffprobe_version="ffprobe version 8.1.2",
                fps_mode_vfr_supported=True,
                encoders={"libx264": True, "aac": True},
            )

    monkeypatch.setattr(
        "semvideo.application.diagnostics.FfmpegAdapter",
        FakeCapabilityAdapter,
    )

    payload = run_doctor(workspace)

    assert payload["ffmpeg"]["ok"] is True
    assert payload["ffmpeg"]["path"] == "C:/tools/ffmpeg.exe"
    assert payload["ffmpeg"]["encoders"] == {"libx264": True, "aac": True}
    assert payload["ffprobe"]["ok"] is True
    assert payload["ffprobe"]["path"] == "C:/tools/ffprobe.exe"
    assert payload["ffprobe"]["version"] == "ffprobe version 8.1.2"
    assert created_with == [
        {
            "ffmpeg_path": "C:/tools/ffmpeg.exe",
            "ffprobe_path": "C:/tools/ffprobe.exe",
        }
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows/MSYS path compatibility")
def test_process_normalizes_msys_source_and_workspace_paths(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    workspace_sentinel = object()

    def fake_discover_workspace(*, explicit=None):
        captured["workspace_path"] = explicit
        return workspace_sentinel

    def fake_submit_job(workspace, input_video, **kwargs):
        captured["workspace"] = workspace
        captured["input_video"] = input_video
        return {
            "schema_version": 1,
            "job_id": "job_test",
            "state": "queued",
        }

    monkeypatch.setattr(
        "semvideo.cli.discover_workspace",
        fake_discover_workspace,
    )
    monkeypatch.setattr("semvideo.cli.submit_job", fake_submit_job)

    result = CliRunner().invoke(
        app,
        [
            "process",
            "/c/Users/test/video.mp4",
            "--workspace",
            "/d/semvideo-workspace",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["workspace_path"] == Path("D:/semvideo-workspace")
    assert captured["input_video"] == Path("C:/Users/test/video.mp4")
    assert captured["workspace"] is workspace_sentinel


@pytest.mark.skipif(os.name != "nt", reason="Windows path compatibility")
def test_process_preserves_native_root_relative_path(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "semvideo.cli.discover_workspace",
        lambda *, explicit=None: object(),
    )

    def fake_submit_job(workspace, input_video, **kwargs):
        captured["input_video"] = input_video
        return {
            "schema_version": 1,
            "job_id": "job_test",
            "state": "queued",
        }

    monkeypatch.setattr("semvideo.cli.submit_job", fake_submit_job)

    result = CliRunner().invoke(
        app,
        [
            "process",
            r"\c\relative-video.mp4",
            "--workspace",
            "C:/workspace",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["input_video"] == Path(r"\c\relative-video.mp4")


@pytest.mark.skipif(os.name != "nt", reason="Windows/MSYS path compatibility")
def test_segment_export_normalizes_msys_output_path(monkeypatch) -> None:
    captured: dict[str, object] = {}
    workspace_sentinel = object()

    monkeypatch.setattr(
        "semvideo.cli.discover_workspace",
        lambda *, explicit=None: workspace_sentinel,
    )

    def fake_export_segment(workspace, job_id, segment_id, output):
        captured["workspace"] = workspace
        captured["output"] = output
        return {
            "schema_version": 1,
            "job_id": job_id,
            "segment_id": segment_id,
            "output": str(output),
        }

    monkeypatch.setattr(
        "semvideo.application.processor.export_segment",
        fake_export_segment,
    )

    result = CliRunner().invoke(
        app,
        [
            "segment",
            "export",
            "job_test",
            "segment_0001",
            "--output",
            "/e/exports/segment.mp4",
            "--workspace",
            "/d/semvideo-workspace",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert captured["workspace"] is workspace_sentinel
    assert captured["output"] == Path("E:/exports/segment.mp4")


def test_config_set_media_tools_updates_workspace_through_cli(
    tmp_path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    workspace.config.write_text(
        workspace.config.read_text(encoding="utf-8").replace(
            "analysis_proxy_crf = 23",
            "analysis_proxy_crf = 27",
        ),
        encoding="utf-8",
    )
    ffmpeg = tmp_path / "runtime" / "ffmpeg.exe"
    ffprobe = tmp_path / "runtime" / "ffprobe.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.touch()
    ffprobe.touch()

    result = CliRunner().invoke(
        app,
        [
            "config",
            "set-media-tools",
            "--ffmpeg",
            str(ffmpeg),
            "--ffprobe",
            str(ffprobe),
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    config = load_workspace_config(workspace.data)
    assert payload == {
        "schema_version": 1,
        "ffmpeg_path": str(ffmpeg.resolve()),
        "ffprobe_path": str(ffprobe.resolve()),
    }
    assert config.media.ffmpeg_path == str(ffmpeg.resolve())
    assert config.media.ffprobe_path == str(ffprobe.resolve())
    assert config.media.analysis_proxy_crf == 27


def test_config_set_llm_provider_selects_agnes_safety_profile(tmp_path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")

    result = CliRunner().invoke(
        app,
        [
            "config",
            "set-llm-provider",
            "agnes",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    config = load_workspace_config(workspace.data)
    assert payload == {
        "schema_version": 1,
        "provider": "agnes",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "model": "agnes-2.5-flash",
        "credential_env": "AGNES_API_KEY",
        "context_window_tokens": 524288,
        "max_input_tokens": 516096,
        "max_output_tokens": 8192,
        "max_concurrency": 2,
        "configured_concurrency": 2,
    }
    assert config.llm.model == "agnes-2.5-flash"
    assert config.concurrency.llm == 2

    shown = CliRunner().invoke(
        app,
        [
            "config",
            "show",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )
    assert shown.exit_code == 0, shown.stderr
    shown_payload = json.loads(shown.stdout)
    assert shown_payload["llm"]["context_window_tokens"] == 524288
    assert shown_payload["llm"]["max_input_tokens"] == 516096
    assert shown_payload["llm"]["credential_present"] is False

    restored = CliRunner().invoke(
        app,
        [
            "config",
            "set-llm-provider",
            "siliconflow",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )
    assert restored.exit_code == 0, restored.stderr
    restored_payload = json.loads(restored.stdout)
    assert restored_payload["provider"] == "siliconflow"
    assert restored_payload["model"] == "Qwen/Qwen3.6-35B-A3B"
    assert restored_payload["credential_env"] == "SEMVIDEO_API_KEY"


def test_job_logs_help_offers_quiet_follow_mode() -> None:
    root_command = get_command(app)
    job_command = root_command.commands["job"]
    logs_command = job_command.commands["logs"]
    option_names = {
        option
        for parameter in logs_command.params
        for option in parameter.opts
    }

    assert "--follow" in option_names
    assert "--quiet" in option_names


def test_job_admission_cli_returns_application_capacity_snapshot(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    store.create_job(job_id="job_001", source_video_id="video_001")

    result = CliRunner().invoke(
        app,
        [
            "job",
            "admission",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["configured_limit"] == 2
    assert payload["active_count"] == 1
    assert payload["available_submission_slots"] == 1
    assert payload["active_job_ids"] == ["job_001"]


def test_shot_show_cli_returns_structured_record(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    _write_ready_shot_records(store, job["job_id"])

    result = CliRunner().invoke(
        app,
        [
            "shot",
            "show",
            job["job_id"],
            "shot_0001",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["shot_id"] == "shot_0001"
    assert payload["cinematography"]["viewpoints"] == ["aerial"]


def test_shot_show_unknown_id_returns_json_input_error(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    _write_ready_shot_records(store, job["job_id"])

    result = CliRunner().invoke(
        app,
        [
            "shot",
            "show",
            job["job_id"],
            "shot_missing",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "shot_not_found"
    assert payload["category"] == "input_error"
    assert payload["entity_id"] == "shot_missing"


def test_shot_show_not_ready_returns_json_config_error(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")

    result = CliRunner().invoke(
        app,
        [
            "shot",
            "show",
            job["job_id"],
            "shot_0001",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "shot_records_not_ready"
    assert payload["category"] == "config_error"
    assert payload["recovery"] == "correct_and_retry"


def test_shot_export_rejects_destination_inside_task_package(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    store = TaskStore(workspace)
    job = store.create_job(job_id="job_001", source_video_id="video_001")
    _write_ready_shot_records(store, job["job_id"])
    output = store.job_path(job["job_id"]) / "illegal-export.mp4"

    result = CliRunner().invoke(
        app,
        [
            "shot",
            "export",
            job["job_id"],
            "shot_0001",
            "--workspace",
            str(workspace.root),
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "shot_export_destination_inside_job"
    assert payload["category"] == "config_error"
    assert not output.exists()
