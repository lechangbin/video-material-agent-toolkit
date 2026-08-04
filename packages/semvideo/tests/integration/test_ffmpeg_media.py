from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from semvideo.adapters.ffmpeg import FfmpegAdapter
from semvideo.modules.evidence.frames import EvidencePolicy, extract_evidence_frames
from semvideo.modules.media.candidates import CandidatePolicy
from semvideo.modules.media.service import MediaAnalyzer


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True)


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    output = tmp_path / "scene-cuts.mp4"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:d=1:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x180:d=1:r=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=1:r=10",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ]
    )
    return output


def test_probe_scene_timeline_and_frame_extraction(
    synthetic_video: Path,
    tmp_path: Path,
) -> None:
    adapter = FfmpegAdapter()
    analyzer = MediaAnalyzer(adapter)
    facts = analyzer.probe(synthetic_video)
    timeline = analyzer.segment(
        synthetic_video,
        source_video_id="video_test",
        media_facts=facts,
        policy=CandidatePolicy(
            scene_threshold=0.2,
            min_segment_ms=200,
            max_segment_ms=1500,
        ),
    )
    frames, discarded = extract_evidence_frames(
        synthetic_video,
        tmp_path / "evidence",
        timeline,
        ffmpeg=adapter,
        policy=EvidencePolicy(
            frames_per_candidate=1,
            maximum_interval_ms=1000,
            max_frames=8,
            dedup_hamming_threshold=0,
        ),
    )

    assert 2950 <= facts.duration_ms <= 3050
    assert facts.video_stream.width == 320
    assert timeline.segments[0].start_ms == 0
    assert timeline.segments[-1].end_ms == facts.duration_ms
    assert any(
        "scene_change" in boundary.reasons for boundary in timeline.boundaries
    )
    assert frames
    assert all(
        (tmp_path / "evidence" / frame.relative_path).is_file()
        for frame in frames
    )
    assert all(item.dedup.decision == "discarded" for item in discarded)


def test_frame_extraction_uses_stream_fps_mode(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.touch()
    output = tmp_path / "frame.jpg"
    captured: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        output.write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("semvideo.adapters.ffmpeg.subprocess.run", side_effect=fake_run):
        FfmpegAdapter().extract_frame(source, timestamp_ms=500, output=output)

    assert "-fps_mode:v:0" in captured
    assert captured[captured.index("-fps_mode:v:0") + 1] == "vfr"
    assert "-vsync" not in captured
