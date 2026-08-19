"""Local media probing and conservative cross-platform work matching.

The implementation invokes FFmpeg directly without a shell.  It deliberately
requires corroborating audio and video evidence before two files are identified
as the same work.
"""

from __future__ import annotations

import json
import struct
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from material_collector.core.errors import CollectorError
from material_collector.core.fingerprints import FingerprintMatch
from material_collector.core.media import (
    DisplayGeometryAssessment,
    GeometryDisposition,
    GeometryStage,
)

_AUDIO_SAMPLE_RATE = 11_025
_AUDIO_ANALYSIS_SECONDS = 120
_VIDEO_SAMPLE_INTERVAL_SECONDS = 2
_VIDEO_SAMPLE_LIMIT = 60
_VIDEO_FRAME_WIDTH = 9
_VIDEO_FRAME_HEIGHT = 8
_VIDEO_FRAME_BYTES = _VIDEO_FRAME_WIDTH * _VIDEO_FRAME_HEIGHT
_MIN_AUDIO_HASHES = 3
_MIN_VIDEO_HASHES = 3
_DEFAULT_AUDIO_THRESHOLD = 0.88
_DEFAULT_VIDEO_THRESHOLD = 0.90
_DEFAULT_DURATION_RELATIVE_TOLERANCE = 0.05
_DEFAULT_DURATION_ABSOLUTE_TOLERANCE = 2.0
_ALIGNMENT_WINDOW = 4


class FingerprintStatus(StrEnum):
    """Whether one fingerprint modality supplied usable evidence."""

    AVAILABLE = "available"
    NO_AUDIO = "no_audio"
    NO_VIDEO = "no_video"
    INSUFFICIENT = "insufficient"


class MediaInspectionError(RuntimeError):
    """FFmpeg could not reliably inspect or fingerprint a local media file."""


class LocalMediaFingerprintService:
    """Application adapter with per-process fingerprint caching."""

    def __init__(
        self,
        *,
        ffmpeg_executable: str = "ffmpeg",
        ffprobe_executable: str = "ffprobe",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._ffmpeg = ffmpeg_executable
        self._ffprobe = ffprobe_executable
        self._timeout = timeout_seconds
        self._cache: dict[Path, MediaFingerprint] = {}

    def compare(self, left: Path, right: Path) -> FingerprintMatch:
        try:
            comparison = compare_fingerprints(
                self._fingerprint(left),
                self._fingerprint(right),
            )
        except (MediaInspectionError, OSError) as error:
            raise CollectorError(
                "fingerprint_failed",
                "A local media fingerprint could not be generated.",
                details={
                    "retryable": False,
                    "reason": str(error),
                },
            ) from error
        return FingerprintMatch(
            same_work=comparison.same_work,
            reliable=comparison.reliable,
            audio_similarity=comparison.audio_similarity,
            video_similarity=comparison.video_similarity,
            duration_difference_seconds=comparison.duration_difference_seconds,
            reason=comparison.reason,
        )

    def _fingerprint(self, path: Path) -> MediaFingerprint:
        normalized = path.expanduser().resolve(strict=True)
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached
        generated = fingerprint_media(
            normalized,
            ffmpeg_executable=self._ffmpeg,
            ffprobe_executable=self._ffprobe,
            timeout_seconds=self._timeout,
        )
        self._cache[normalized] = generated
        return generated


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """Credential-free structural facts reported by ffprobe."""

    path: Path
    duration_seconds: float | None
    container: str | None
    width: int | None
    height: int | None
    video_stream_count: int
    audio_stream_count: int
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None
    rotation_degrees: int | None = None


@dataclass(frozen=True, slots=True)
class MediaFingerprint:
    """Comparable local fingerprints plus the probe that explains availability."""

    probe: MediaProbe
    audio_status: FingerprintStatus
    audio_hashes: tuple[int, ...]
    video_status: FingerprintStatus
    video_hashes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FingerprintComparison:
    """Conservative duplicate decision with independently inspectable evidence."""

    same_work: bool
    reliable: bool
    audio_similarity: float | None
    video_similarity: float | None
    duration_difference_seconds: float | None
    reason: str


def inspect_media(
    path: Path,
    *,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
    timeout_seconds: float = 30.0,
) -> MediaProbe:
    """Return structural facts only after every primary media packet decodes."""

    probe = _probe_media(
        path,
        ffprobe_executable=ffprobe_executable,
        timeout_seconds=timeout_seconds,
    )
    if probe.video_stream_count:
        _run(
            [
                ffmpeg_executable,
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-nostdin",
                "-i",
                str(probe.path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-sn",
                "-dn",
                "-f",
                "null",
                "-",
            ],
            timeout_seconds=_decode_timeout_seconds(probe, timeout_seconds),
            operation="media decoding",
        )
    return probe


def _probe_media(
    path: Path,
    *,
    ffprobe_executable: str,
    timeout_seconds: float,
) -> MediaProbe:
    """Return structural metadata without decoding the complete timeline."""

    media_path = _validated_media_path(path)
    completed = _run(
        [
            ffprobe_executable,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,format_name:"
                "stream=codec_type,width,height,sample_aspect_ratio,display_aspect_ratio:"
                "stream_tags=rotate:stream_side_data=rotation"
            ),
            "-of",
            "json",
            str(media_path),
        ],
        timeout_seconds=timeout_seconds,
        operation="ffprobe",
    )
    try:
        payload = cast(dict[str, Any], json.loads(completed.stdout.decode("utf-8")))
        streams = cast(list[dict[str, Any]], payload.get("streams", []))
        format_record = cast(dict[str, Any], payload.get("format", {}))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise MediaInspectionError("ffprobe returned invalid JSON.") from error

    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    primary_video = video_streams[0] if video_streams else {}
    duration = _optional_float(format_record.get("duration"))
    container = _optional_string(format_record.get("format_name"))
    width = _optional_positive_int(primary_video.get("width"))
    height = _optional_positive_int(primary_video.get("height"))
    sample_aspect_ratio = _optional_string(primary_video.get("sample_aspect_ratio"))
    display_aspect_ratio = _optional_string(primary_video.get("display_aspect_ratio"))
    rotation = _rotation(primary_video)
    return MediaProbe(
        path=media_path,
        duration_seconds=duration,
        container=container,
        width=width,
        height=height,
        video_stream_count=len(video_streams),
        audio_stream_count=len(audio_streams),
        sample_aspect_ratio=sample_aspect_ratio,
        display_aspect_ratio=display_aspect_ratio,
        rotation_degrees=rotation,
    )


def assess_display_geometry(
    probe: MediaProbe,
    *,
    stage: GeometryStage,
    tolerance: float = 0.01,
) -> DisplayGeometryAssessment:
    """Assess normalized display geometry against 16:9 without changing composition."""

    if tolerance < 0:
        raise ValueError("tolerance must not be negative")
    width = probe.width
    height = probe.height
    if width is None or height is None:
        return DisplayGeometryAssessment(
            stage=stage,
            disposition=GeometryDisposition.UNKNOWN,
            encoded_width=width,
            encoded_height=height,
            rotation_degrees=probe.rotation_degrees,
            sample_aspect_ratio=probe.sample_aspect_ratio,
            display_aspect_ratio=probe.display_aspect_ratio,
            reason_code="display_geometry_missing",
        )
    sar = _ratio(probe.sample_aspect_ratio) or 1.0
    display_width = width * sar
    display_height = float(height)
    rotation = (probe.rotation_degrees or 0) % 360
    if rotation in {90, 270}:
        display_width, display_height = display_height, display_width
    ratio = display_width / display_height
    target = 16 / 9
    deviation = abs(ratio - target) / target
    disposition = (
        GeometryDisposition.ACCEPTED
        if deviation <= tolerance
        else GeometryDisposition.REJECTED
    )
    return DisplayGeometryAssessment(
        stage=stage,
        disposition=disposition,
        encoded_width=width,
        encoded_height=height,
        rotation_degrees=probe.rotation_degrees,
        sample_aspect_ratio=probe.sample_aspect_ratio,
        display_aspect_ratio=probe.display_aspect_ratio,
        normalized_display_ratio=ratio,
        deviation_from_16_9=deviation,
        reason_code=None if disposition is GeometryDisposition.ACCEPTED else "not_16_9",
    )


def fingerprint_media(
    path: Path,
    *,
    ffmpeg_executable: str = "ffmpeg",
    ffprobe_executable: str = "ffprobe",
    timeout_seconds: float = 120.0,
) -> MediaFingerprint:
    """Generate Chromaprint audio hashes and fixed-sample grayscale dHashes."""

    probe = _probe_media(
        path,
        ffprobe_executable=ffprobe_executable,
        timeout_seconds=min(timeout_seconds, 30.0),
    )
    audio_hashes: tuple[int, ...] = ()
    if probe.audio_stream_count:
        audio_bytes = _run(
            [
                ffmpeg_executable,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(probe.path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(_AUDIO_SAMPLE_RATE),
                "-t",
                str(_AUDIO_ANALYSIS_SECONDS),
                "-fp_format",
                "raw",
                "-f",
                "chromaprint",
                "pipe:1",
            ],
            timeout_seconds=timeout_seconds,
            operation="audio fingerprinting",
        ).stdout
        audio_hashes = _unpack_chromaprint(audio_bytes)
        audio_status = (
            FingerprintStatus.AVAILABLE
            if len(audio_hashes) >= _MIN_AUDIO_HASHES
            else FingerprintStatus.INSUFFICIENT
        )
    else:
        audio_status = FingerprintStatus.NO_AUDIO

    video_hashes: tuple[int, ...] = ()
    if probe.video_stream_count:
        raw_frames = _run(
            [
                ffmpeg_executable,
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(probe.path),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                (
                    f"fps=1/{_VIDEO_SAMPLE_INTERVAL_SECONDS},"
                    f"scale={_VIDEO_FRAME_WIDTH}:{_VIDEO_FRAME_HEIGHT}:flags=area,"
                    "format=gray"
                ),
                "-frames:v",
                str(_VIDEO_SAMPLE_LIMIT),
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            timeout_seconds=timeout_seconds,
            operation="video fingerprinting",
        ).stdout
        video_hashes = _frames_to_dhash(raw_frames)
        video_status = (
            FingerprintStatus.AVAILABLE
            if len(video_hashes) >= _MIN_VIDEO_HASHES
            else FingerprintStatus.INSUFFICIENT
        )
    else:
        video_status = FingerprintStatus.NO_VIDEO

    return MediaFingerprint(
        probe=probe,
        audio_status=audio_status,
        audio_hashes=audio_hashes,
        video_status=video_status,
        video_hashes=video_hashes,
    )


def compare_fingerprints(
    left: MediaFingerprint,
    right: MediaFingerprint,
    *,
    audio_threshold: float = _DEFAULT_AUDIO_THRESHOLD,
    video_threshold: float = _DEFAULT_VIDEO_THRESHOLD,
    duration_relative_tolerance: float = _DEFAULT_DURATION_RELATIVE_TOLERANCE,
    duration_absolute_tolerance: float = _DEFAULT_DURATION_ABSOLUTE_TOLERANCE,
) -> FingerprintComparison:
    """Return true only when independent audio, video and duration evidence agrees."""

    _validate_threshold("audio_threshold", audio_threshold)
    _validate_threshold("video_threshold", video_threshold)
    _validate_nonnegative("duration_relative_tolerance", duration_relative_tolerance)
    _validate_nonnegative("duration_absolute_tolerance", duration_absolute_tolerance)

    if (
        left.audio_status is not FingerprintStatus.AVAILABLE
        or right.audio_status is not FingerprintStatus.AVAILABLE
        or left.video_status is not FingerprintStatus.AVAILABLE
        or right.video_status is not FingerprintStatus.AVAILABLE
    ):
        return FingerprintComparison(
            same_work=False,
            reliable=False,
            audio_similarity=None,
            video_similarity=None,
            duration_difference_seconds=_duration_difference(left, right),
            reason="both_audio_and_video_fingerprints_are_required",
        )

    left_duration = left.probe.duration_seconds
    right_duration = right.probe.duration_seconds
    if left_duration is None or right_duration is None:
        return FingerprintComparison(
            same_work=False,
            reliable=False,
            audio_similarity=None,
            video_similarity=None,
            duration_difference_seconds=None,
            reason="duration_is_required",
        )

    duration_difference = abs(left_duration - right_duration)
    duration_limit = max(
        duration_absolute_tolerance,
        max(left_duration, right_duration) * duration_relative_tolerance,
    )
    audio_similarity = _aligned_similarity(
        left.audio_hashes,
        right.audio_hashes,
        bit_width=32,
    )
    video_similarity = _aligned_similarity(
        left.video_hashes,
        right.video_hashes,
        bit_width=64,
    )
    duration_matches = duration_difference <= duration_limit
    same_work = (
        duration_matches
        and audio_similarity >= audio_threshold
        and video_similarity >= video_threshold
    )
    return FingerprintComparison(
        same_work=same_work,
        reliable=True,
        audio_similarity=audio_similarity,
        video_similarity=video_similarity,
        duration_difference_seconds=duration_difference,
        reason="corroborating_evidence_matched" if same_work else "evidence_did_not_match",
    )


def _run(
    command: list[str],
    *,
    timeout_seconds: float,
    operation: str,
) -> subprocess.CompletedProcess[bytes]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise MediaInspectionError(f"{operation} timed out.") from error
    except OSError as error:
        raise MediaInspectionError(f"{operation} could not be started.") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if len(detail) > 500:
            detail = f"{detail[:500]}..."
        raise MediaInspectionError(
            f"{operation} failed with exit code {completed.returncode}: {detail}"
        )
    return completed


def _validated_media_path(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise MediaInspectionError("The media path does not exist or cannot be resolved.") from error
    if not resolved.is_file():
        raise MediaInspectionError("The media path is not a regular file.")
    return resolved


def _decode_timeout_seconds(probe: MediaProbe, minimum_seconds: float) -> float:
    """Allow complete 720p validation to run at least as long as the media timeline."""

    if minimum_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    duration = probe.duration_seconds
    if duration is None:
        return max(minimum_seconds, 300.0)
    return max(minimum_seconds, min(duration + 30.0, 3_600.0))


def _unpack_chromaprint(value: bytes) -> tuple[int, ...]:
    if len(value) % 4:
        raise MediaInspectionError("Chromaprint returned a malformed raw fingerprint.")
    return tuple(item[0] for item in struct.iter_unpack("<I", value))


def _frames_to_dhash(value: bytes) -> tuple[int, ...]:
    complete_length = len(value) - (len(value) % _VIDEO_FRAME_BYTES)
    frames = (
        value[offset : offset + _VIDEO_FRAME_BYTES]
        for offset in range(0, complete_length, _VIDEO_FRAME_BYTES)
    )
    return tuple(_dhash(frame) for frame in frames)


def _dhash(frame: bytes) -> int:
    result = 0
    for row in range(_VIDEO_FRAME_HEIGHT):
        start = row * _VIDEO_FRAME_WIDTH
        for column in range(_VIDEO_FRAME_WIDTH - 1):
            result <<= 1
            result |= frame[start + column] > frame[start + column + 1]
    return result


def _aligned_similarity(
    left: Sequence[int],
    right: Sequence[int],
    *,
    bit_width: int,
) -> float:
    best = 0.0
    for offset in range(-_ALIGNMENT_WINDOW, _ALIGNMENT_WINDOW + 1):
        left_start = max(0, offset)
        right_start = max(0, -offset)
        overlap = min(len(left) - left_start, len(right) - right_start)
        if overlap <= 0:
            continue
        different_bits = sum(
            (
                left[left_start + index] ^ right[right_start + index]
            ).bit_count()
            for index in range(overlap)
        )
        similarity = 1.0 - different_bits / (overlap * bit_width)
        best = max(best, similarity)
    return best


def _duration_difference(
    left: MediaFingerprint,
    right: MediaFingerprint,
) -> float | None:
    if left.probe.duration_seconds is None or right.probe.duration_seconds is None:
        return None
    return abs(left.probe.duration_seconds - right.probe.duration_seconds)


def _optional_float(value: object) -> float | None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float, str))
    ):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _ratio(value: str | None) -> float | None:
    if not value or value in {"0:1", "N/A"}:
        return None
    separator = ":" if ":" in value else "/" if "/" in value else None
    if separator is None:
        return None
    numerator_text, denominator_text = value.split(separator, 1)
    try:
        numerator = float(numerator_text)
        denominator = float(denominator_text)
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return numerator / denominator


def _rotation(stream: dict[str, Any]) -> int | None:
    tags = stream.get("tags")
    if isinstance(tags, dict):
        value = tags.get("rotate")
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            pass
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if not isinstance(item, dict) or "rotation" not in item:
                continue
            try:
                return int(float(str(item["rotation"])))
            except (TypeError, ValueError):
                continue
    return None


def _validate_threshold(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between zero and one")


def _validate_nonnegative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative")
