from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from semvideo.application.workspace import initialize_workspace
from semvideo.application.jobs import cancel_job, resume_job, submit_job
from semvideo.application.queries import get_job
from semvideo.application.task_store import TaskStore
from semvideo.cli import app
from semvideo.infrastructure.process_identity import process_matches


class _ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers["content-length"])
        request = json.loads(self.rfile.read(length))
        assert request["model"] == "Qwen/Qwen3.6-35B-A3B"
        server = self.server
        request_number = getattr(server, "request_count", 0) + 1
        server.request_count = request_number
        request_received = getattr(server, "request_received", None)
        if request_received is not None:
            request_received.set()
        release_response = getattr(server, "release_response", None)
        if release_response is not None and request_number == 1:
            release_response.wait(timeout=30)
        system_text = str(request["messages"][0]["content"])
        if "镜头语言标注器" in system_text:
            response_content = {
                "annotations": [
                    {
                        "shot_id": "shot_0001",
                        "viewpoints": ["ground"],
                        "shot_scale": {
                            "start": "wide",
                            "end": "wide",
                        },
                        "camera_motions": [
                            {
                                "type": "static",
                                "direction": "none",
                                "speed": "still",
                                "temporal_profile": "constant",
                                "start_ms": 0,
                                "end_ms": 2000,
                                "confidence": 0.9,
                            }
                        ],
                        "cinematography_summary": "固定大全景展示测试图案。",
                        "cinematography_keywords": ["固定镜头", "大全景"],
                        "evidence_frame_ids": ["shot_0001_frame_01"],
                        "confidence": 0.9,
                    }
                ]
            }
        else:
            response_content = {
                "segments": [
                    {
                        "title": "完整测试事件",
                        "short_summary": "测试图案持续运动。",
                        "detailed_summary": "整个两秒视频是一个连续的测试图案运动事件。",
                        "visual_summary": "彩色测试图案持续变化。",
                        "event": "测试图案运动",
                        "start_anchor_id": "anchor_0000",
                        "end_anchor_id": "anchor_0001",
                        "reason": "全片主体和目标连续，没有语义切换。",
                        "topics": ["合成视频"],
                        "participants": [],
                        "locations": [],
                        "organizations": [],
                        "objects": ["测试图案"],
                        "actions": ["运动"],
                        "keywords": ["连续事件"],
                        "confidence": 0.98,
                    }
                ]
            }
        body = json.dumps(
            {
                "id": "local-response",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                response_content,
                                ensure_ascii=False,
                            ),
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-siliconcloud-trace-id", "local-trace")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


def _synthetic_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=12:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def _configure_local_model(workspace, server: ThreadingHTTPServer) -> None:
    config_text = workspace.config.read_text(encoding="utf-8")
    config_text = config_text.replace("transcribe = true", "transcribe = false")
    config_text = config_text.replace(
        'base_url = "https://api.siliconflow.cn/v1"',
        f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
    )
    workspace.config.write_text(config_text, encoding="utf-8")


def _wait_for_state(workspace, job_id: str, expected: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = get_job(workspace, job_id)
        if latest["state"] == expected:
            return latest
        time.sleep(0.1)
    raise AssertionError(
        f"job {job_id} did not reach {expected}; latest={latest}"
    )


def _wait_for_worker_stopped(workspace, job_id: str, timeout: float = 10):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = get_job(workspace, job_id)
        if latest["worker"]["status"] == "stopped":
            return latest
        time.sleep(0.1)
    raise AssertionError(f"worker for {job_id} did not stop; latest={latest}")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_cli_detached_worker_full_success(tmp_path: Path, monkeypatch) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    video = tmp_path / "synthetic.mp4"
    _synthetic_video(video)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _configure_local_model(workspace, server)
        monkeypatch.setenv("SEMVIDEO_API_KEY", "local-test-key")

        result = CliRunner().invoke(
            app,
            [
                "process",
                str(video),
                "--workspace",
                str(workspace.root),
                "--render",
                "--wait",
                "--idempotency-key",
                "integration-request-1",
                "--json",
            ],
        )
        repeated = CliRunner().invoke(
            app,
            [
                "process",
                str(video),
                "--workspace",
                str(workspace.root),
                "--render",
                "--wait",
                "--idempotency-key",
                "integration-request-1",
                "--json",
                "--quiet",
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    progress_events = [
        json.loads(line)
        for line in result.stderr.splitlines()
        if line.strip()
    ]
    assert progress_events
    assert all(event["event"] == "stage_progress" for event in progress_events)
    assert payload["state"] == "completed"
    assert payload["final_segment_count"] == 1
    repeated_payload = json.loads(repeated.stdout)
    assert repeated.exit_code == 0
    assert repeated_payload["job_id"] == payload["job_id"]
    assert repeated_payload["idempotent_reuse"] is True
    assert repeated.stderr == ""
    job_root = Path(payload["job_path"])
    assert (job_root / "retrieval" / "segments.jsonl").is_file()
    assert (job_root / "renders" / "segment_0001.mp4").stat().st_size > 0
    assert (job_root / "reports" / "inspection.html").is_file()

    shown = CliRunner().invoke(
        app,
        [
            "segment",
            "show",
            payload["job_id"],
            "segment_0001",
            "--workspace",
            str(workspace.root),
            "--json",
        ],
    )
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["transcript"]["quality"] == "unavailable"

    shots = CliRunner().invoke(
        app,
        [
            "shot",
            "list",
            payload["job_id"],
            "--workspace",
            str(workspace.root),
            "--motion",
            "static",
            "--scale",
            "wide",
            "--speed",
            "still",
            "--keyword",
            "大全景",
            "--json",
        ],
    )
    assert shots.exit_code == 0, shots.output
    shot_payload = json.loads(shots.stdout)
    assert shot_payload["total"] == 1
    assert shot_payload["items"][0]["shot_id"] == "shot_0001"

    exported_path = tmp_path / "cli-export.mp4"
    exported = CliRunner().invoke(
        app,
        [
            "segment",
            "export",
            payload["job_id"],
            "segment_0001",
            "--workspace",
            str(workspace.root),
            "--output",
            str(exported_path),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert exported_path.stat().st_size > 0

    shot_exported_path = tmp_path / "cli-shot-export.mp4"
    shot_exported = CliRunner().invoke(
        app,
        [
            "shot",
            "export",
            payload["job_id"],
            "shot_0001",
            "--workspace",
            str(workspace.root),
            "--output",
            str(shot_exported_path),
            "--json",
        ],
    )
    assert shot_exported.exit_code == 0, shot_exported.output
    assert shot_exported_path.stat().st_size > 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_real_worker_accepts_cancel_and_stops_at_safe_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    video = tmp_path / "synthetic.mp4"
    _synthetic_video(video)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    server.request_count = 0
    server.request_received = threading.Event()
    server.release_response = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _configure_local_model(workspace, server)
        monkeypatch.setenv("SEMVIDEO_API_KEY", "local-test-key")
        submitted = submit_job(workspace, video)
        assert server.request_received.wait(timeout=20)

        cancellation = cancel_job(
            workspace,
            submitted["job_id"],
            reason="integration cancellation",
        )
        assert cancellation["cancel_requested"] is True
        server.release_response.set()
        terminal = _wait_for_state(
            workspace,
            submitted["job_id"],
            "cancelled",
        )
        terminal = _wait_for_worker_stopped(
            workspace,
            submitted["job_id"],
        )
    finally:
        server.release_response.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert terminal["failure"]["category"] == "cancelled"
    assert terminal["worker"]["status"] == "stopped"
    events = list(TaskStore(workspace).iter_events(submitted["job_id"]))
    assert "cancel_requested" in [event["type"] for event in events]
    assert "job_cancelled" in [event["type"] for event in events]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_real_worker_interruption_resumes_with_new_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    video = tmp_path / "synthetic.mp4"
    _synthetic_video(video)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelHandler)
    server.request_count = 0
    server.request_received = threading.Event()
    server.release_response = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _configure_local_model(workspace, server)
        monkeypatch.setenv("SEMVIDEO_API_KEY", "local-test-key")
        submitted = submit_job(workspace, video)
        assert server.request_received.wait(timeout=20)
        running = get_job(workspace, submitted["job_id"])
        first_attempt = running["attempt_id"]
        store = TaskStore(workspace)
        worker = store.read_worker(submitted["job_id"])
        assert worker is not None
        reusable_stages = ("probe", "segment", "evidence")
        checkpoints_before = {
            stage: (
                store.job_path(submitted["job_id"])
                / "stages"
                / stage
                / "checkpoint.json"
            ).read_bytes()
            for stage in reusable_stages
        }

        os.kill(int(worker["pid"]), signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process_matches(
            int(worker["pid"]),
            str(worker["process_started_at"]),
        ):
            time.sleep(0.1)
        interrupted = get_job(workspace, submitted["job_id"])
        assert interrupted["state"] == "interrupted"
        server.release_response.set()

        resumed = resume_job(workspace, submitted["job_id"])
        assert resumed["attempt_id"] != first_attempt
        terminal = _wait_for_state(
            workspace,
            submitted["job_id"],
            "completed",
        )
        terminal = _wait_for_worker_stopped(
            workspace,
            submitted["job_id"],
        )
        checkpoints_after = {
            stage: (
                store.job_path(submitted["job_id"])
                / "stages"
                / stage
                / "checkpoint.json"
            ).read_bytes()
            for stage in reusable_stages
        }
    finally:
        server.release_response.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert terminal["attempt_id"] == resumed["attempt_id"]
    assert terminal["worker"]["status"] == "stopped"
    assert checkpoints_after == checkpoints_before
    events = list(TaskStore(workspace).iter_events(submitted["job_id"]))
    event_types = [event["type"] for event in events]
    assert "job_interrupted" in event_types
    assert "job_completed" in event_types
