from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from semvideo.adapters.ffmpeg import (
    FfmpegAdapter,
    FfmpegError,
    MediaFacts,
    VideoStreamFacts,
)


def test_create_analysis_proxy_is_bounded_video_only_and_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"4k")
    output = tmp_path / "analysis.mp4"
    commands: list[list[str]] = []
    adapter = FfmpegAdapter()

    def fake_run(command, **kwargs):
        commands.append(list(command))
        output.write_bytes(b"720p")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(adapter, "_run", fake_run)
    monkeypatch.setattr(
        adapter,
        "probe",
        lambda path: MediaFacts(
            schema_version=1,
            source=str(path),
            duration_ms=20_000,
            format_name="mp4",
            bit_rate=1_000,
            video_stream=VideoStreamFacts(
                index=0,
                codec="h264",
                width=1280,
                height=720,
                pixel_format="yuv420p",
                avg_frame_rate="25/1",
                nominal_fps=25.0,
                time_base="1/1000",
                rotation_degrees=0,
            ),
            audio_streams=(),
            subtitle_streams=(),
            decodable=True,
        ),
    )

    result = adapter.create_analysis_proxy(
        source,
        output=output,
        expected_duration_ms=20_000,
        max_width=1280,
        max_height=720,
        video_codec="libx264",
        preset="veryfast",
        crf=23,
    )

    command = commands[0]
    assert result == output
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "-an" in command
    assert "-sn" in command
    assert "-dn" in command
    assert command[command.index("-c:v") + 1] == "libx264"
    scale = command[command.index("-vf") + 1]
    assert "1280" in scale
    assert "720" in scale
    assert "force_original_aspect_ratio=decrease" in scale
    assert command[command.index("-fps_mode:v:0") + 1] == "vfr"


def test_render_preserves_execution_error_when_output_cleanup_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "segment.mp4"
    original_unlink = Path.unlink

    def reject_output_delete(path: Path, *, missing_ok: bool = False) -> None:
        if path == output:
            raise OSError("safe-delete unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_output_delete)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("ffmpeg missing")
        ),
    )

    with pytest.raises(FfmpegError, match="could not execute ffmpeg"):
        FfmpegAdapter().render_segment(
            source,
            start_ms=0,
            end_ms=1000,
            output=output,
        )


def test_capabilities_require_successful_tools_and_exact_encoder_names(
    monkeypatch,
) -> None:
    def fake_which(executable: str) -> str:
        return f"C:/tools/{executable}.exe"

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[0].endswith("ffprobe.exe"):
            return subprocess.CompletedProcess(
                command,
                1,
                "ffprobe version misleading\n",
                "failed",
            )
        if command[1:] == ["-version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "ffmpeg version 8.1.2\n",
                "",
            )
        if command[1:] == ["-hide_banner", "-h", "full"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "-fps_mode[:stream_specifier]\n",
                "",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            " V....D libx264rgb RGB-only encoder\n A....D aac AAC encoder\n",
            "",
        )

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    capabilities = FfmpegAdapter().inspect_capabilities()

    assert capabilities.ffmpeg_path == "C:/tools/ffmpeg.exe"
    assert capabilities.ffprobe_path == "C:/tools/ffprobe.exe"
    assert capabilities.ffmpeg_version == "ffmpeg version 8.1.2"
    assert capabilities.ffprobe_version is None
    assert capabilities.fps_mode_vfr_supported is True
    assert capabilities.encoders == {"libx264": False, "aac": True}
    assert capabilities.ok is False
