from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from material_collector.infrastructure.media_inspection import (
    FingerprintStatus,
    LocalMediaFingerprintService,
    MediaInspectionError,
    compare_fingerprints,
    fingerprint_media,
    inspect_media,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg and ffprobe are required for media inspection integration tests.",
)


def _ffmpeg(*arguments: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        shell=False,
    )


@pytest.fixture(scope="module")
def sample_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media-inspection")
    original = root / "original.mp4"
    transcoded = root / "transcoded.mkv"
    different = root / "different.mp4"
    silent = root / "silent.mp4"

    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=128x96:rate=10:duration=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=12",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        str(original),
    )
    _ffmpeg(
        "-i",
        str(original),
        "-c:v",
        "libvpx-vp9",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-c:a",
        "libopus",
        str(transcoded),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "smptebars=size=128x96:rate=10:duration=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=44100:duration=12",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        str(different),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=128x96:rate=10:duration=12",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        str(silent),
    )
    return {
        "original": original,
        "transcoded": transcoded,
        "different": different,
        "silent": silent,
    }


def test_probe_returns_duration_dimensions_and_container(
    sample_media: dict[str, Path],
) -> None:
    probe = inspect_media(sample_media["original"])

    assert probe.duration_seconds == pytest.approx(12, abs=0.1)
    assert (probe.width, probe.height) == (128, 96)
    assert probe.container is not None
    assert "mp4" in probe.container
    assert probe.video_stream_count == 1
    assert probe.audio_stream_count == 1


def test_probe_rejects_faststart_media_truncated_after_readable_header(
    tmp_path: Path,
) -> None:
    complete = tmp_path / "complete-faststart.mp4"
    truncated = tmp_path / "truncated-faststart.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=128x96:rate=10:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=8",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        str(complete),
    )
    payload = complete.read_bytes()
    truncated.write_bytes(payload[: len(payload) * 3 // 4])

    with pytest.raises(MediaInspectionError, match="decod"):
        inspect_media(truncated)


def test_same_content_survives_container_and_codec_transcode(
    sample_media: dict[str, Path],
) -> None:
    original = fingerprint_media(sample_media["original"])
    transcoded = fingerprint_media(sample_media["transcoded"])

    comparison = compare_fingerprints(original, transcoded)

    assert comparison.reliable is True
    assert comparison.same_work is True
    assert comparison.audio_similarity is not None
    assert comparison.audio_similarity >= 0.88
    assert comparison.video_similarity is not None
    assert comparison.video_similarity >= 0.90


def test_application_fingerprint_service_returns_stable_match(
    sample_media: dict[str, Path],
) -> None:
    service = LocalMediaFingerprintService()

    comparison = service.compare(
        sample_media["original"],
        sample_media["transcoded"],
    )

    assert comparison.reliable is True
    assert comparison.same_work is True


def test_different_audio_and_video_are_not_same_work(
    sample_media: dict[str, Path],
) -> None:
    original = fingerprint_media(sample_media["original"])
    different = fingerprint_media(sample_media["different"])

    comparison = compare_fingerprints(original, different)

    assert comparison.reliable is True
    assert comparison.same_work is False


def test_no_audio_is_explicit_and_cannot_confirm_same_work(
    sample_media: dict[str, Path],
) -> None:
    original = fingerprint_media(sample_media["original"])
    silent = fingerprint_media(sample_media["silent"])

    assert silent.audio_status is FingerprintStatus.NO_AUDIO
    assert silent.audio_hashes == ()
    comparison = compare_fingerprints(original, silent)
    assert comparison.reliable is False
    assert comparison.same_work is False
    assert comparison.reason == "both_audio_and_video_fingerprints_are_required"
