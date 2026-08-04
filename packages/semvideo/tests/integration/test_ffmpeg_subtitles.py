from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from semvideo.adapters.ffmpeg import FfmpegAdapter
from semvideo.modules.media.subtitles import extract_embedded_subtitles


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed",
)


def test_embedded_text_subtitle_detection_and_extraction(tmp_path: Path) -> None:
    subtitle = tmp_path / "input.srt"
    subtitle.write_text(
        "1\n00:00:00,200 --> 00:00:01,200\n边防巡逻\n",
        encoding="utf-8",
    )
    video = tmp_path / "with-subtitles.mkv"
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
            "color=c=black:s=320x180:d=2:r=10",
            "-f",
            "srt",
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "1:s:0",
            "-c:v",
            "libx264",
            "-c:s",
            "srt",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    adapter = FfmpegAdapter()
    facts = adapter.probe(video)
    spans = extract_embedded_subtitles(
        video,
        tmp_path / "subtitles",
        ffmpeg=adapter,
        media_facts=facts,
    )

    assert len(facts.subtitle_streams) == 1
    assert facts.subtitle_streams[0].is_text
    assert spans[0].text == "边防巡逻"
    assert spans[0].source == "embedded_subtitle"
