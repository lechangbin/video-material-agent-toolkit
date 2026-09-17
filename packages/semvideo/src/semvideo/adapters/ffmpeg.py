"""FFmpeg/ffprobe process adapter.

All command construction lives here so application modules never need to know
about FFmpeg argument ordering or deprecated compatibility flags.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from semvideo.infrastructure.io import unlink_best_effort


class FfmpegError(RuntimeError):
    """A local FFmpeg command could not produce the requested artifact."""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] = (),
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class FfmpegCapabilities:
    ffmpeg_path: str | None
    ffprobe_path: str | None
    ffmpeg_version: str | None
    ffprobe_version: str | None
    fps_mode_vfr_supported: bool
    encoders: dict[str, bool]

    @property
    def ok(self) -> bool:
        return (
            self.ffmpeg_path is not None
            and self.ffprobe_path is not None
            and self.ffmpeg_version is not None
            and self.ffprobe_version is not None
            and self.fps_mode_vfr_supported
            and all(self.encoders.values())
        )


@dataclass(frozen=True, slots=True)
class VideoStreamFacts:
    index: int
    codec: str | None
    width: int | None
    height: int | None
    pixel_format: str | None
    avg_frame_rate: str | None
    nominal_fps: float | None
    time_base: str | None
    rotation_degrees: int
    variable_frame_rate: bool | None = None


@dataclass(frozen=True, slots=True)
class AudioStreamFacts:
    index: int
    codec: str | None
    channels: int | None
    sample_rate: int | None
    time_base: str | None
    language: str | None


@dataclass(frozen=True, slots=True)
class SubtitleStreamFacts:
    index: int
    codec: str | None
    language: str | None
    title: str | None
    is_text: bool


@dataclass(frozen=True, slots=True)
class MediaFacts:
    schema_version: int
    source: str
    duration_ms: int
    format_name: str | None
    bit_rate: int | None
    video_stream: VideoStreamFacts
    audio_streams: tuple[AudioStreamFacts, ...]
    subtitle_streams: tuple[SubtitleStreamFacts, ...]
    decodable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScenePoint:
    timestamp_ms: int
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TEXT_SUBTITLE_CODECS = {
    "ass",
    "mov_text",
    "srt",
    "ssa",
    "subrip",
    "text",
    "webvtt",
}
_SCENE_FRAME_RE = re.compile(r"(?:frame:\d+\s+)?pts:\S+\s+pts_time:(-?\d+(?:\.\d+)?)")
_SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=(-?\d+(?:\.\d+)?)")


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _rational(value: object) -> float | None:
    raw = str(value or "")
    if "/" not in raw:
        return _as_float(raw)
    numerator, denominator = raw.split("/", 1)
    denominator_value = _as_float(denominator)
    numerator_value = _as_float(numerator)
    if not denominator_value or numerator_value is None:
        return None
    return numerator_value / denominator_value


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        return _as_int(tags["rotate"]) or 0
    for item in stream.get("side_data_list") or ():
        if "rotation" in item:
            return _as_int(item["rotation"]) or 0
    return 0


class FfmpegAdapter:
    """Small, injectable boundary around local FFmpeg executables."""

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        timeout_seconds: float = 600,
        verify_decode: bool = True,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.timeout_seconds = timeout_seconds
        self.verify_decode = verify_decode

    def inspect_capabilities(self) -> FfmpegCapabilities:
        """Probe the configured tools without raising for missing capabilities."""

        ffmpeg = shutil.which(self.ffmpeg_path)
        ffprobe = shutil.which(self.ffprobe_path)
        ffmpeg_version = None
        ffprobe_version = None
        fps_mode_supported = False
        available_encoders: set[str] = set()
        if ffmpeg is not None:
            try:
                version = self._run([ffmpeg, "-version"], timeout_seconds=10)
                ffmpeg_version = (
                    version.stdout.splitlines()[0]
                    if version.stdout
                    else None
                )
            except FfmpegError:
                pass
            try:
                help_result = self._run(
                    [ffmpeg, "-hide_banner", "-h", "full"],
                    timeout_seconds=20,
                )
                fps_mode_supported = "-fps_mode" in (
                    help_result.stdout + help_result.stderr
                )
            except FfmpegError:
                pass
            try:
                encoder_result = self._run(
                    [ffmpeg, "-hide_banner", "-encoders"],
                    timeout_seconds=20,
                )
                encoder_output = encoder_result.stdout + encoder_result.stderr
                available_encoders = set(
                    re.findall(
                        r"(?m)^\s*[A-Z.]{6}\s+(\S+)",
                        encoder_output,
                    )
                )
            except FfmpegError:
                pass
        if ffprobe is not None:
            try:
                version = self._run([ffprobe, "-version"], timeout_seconds=10)
                ffprobe_version = (
                    version.stdout.splitlines()[0]
                    if version.stdout
                    else None
                )
            except FfmpegError:
                pass
        return FfmpegCapabilities(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            ffmpeg_version=ffmpeg_version,
            ffprobe_version=ffprobe_version,
            fps_mode_vfr_supported=fps_mode_supported,
            encoders={
                name: name in available_encoders
                for name in ("libx264", "aac")
            },
        )

    def _run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FfmpegError(
                f"could not execute {command[0]}: {exc}",
                command=command,
            ) from exc
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout or "").strip()
            raise FfmpegError(
                f"{Path(command[0]).name} failed: {diagnostic[-3000:]}",
                command=command,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result

    def probe(self, source: Path) -> MediaFacts:
        source = source.resolve()
        result = self._run(
            (
                self.ffprobe_path,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(source),
            )
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FfmpegError("ffprobe returned invalid JSON") from exc

        streams = payload.get("streams") or []
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        if video is None:
            raise FfmpegError("input has no video stream")
        duration = (
            _as_float((payload.get("format") or {}).get("duration"))
            or _as_float(video.get("duration"))
            or 0.0
        )
        if duration <= 0:
            raise FfmpegError("video duration could not be determined")
        if self.verify_decode:
            self._run(
                (
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                )
            )

        def tags_for(stream: dict[str, Any]) -> dict[str, Any]:
            return cast(dict[str, Any], stream.get("tags") or {})
        audio_streams = tuple(
            AudioStreamFacts(
                index=int(stream["index"]),
                codec=stream.get("codec_name"),
                channels=_as_int(stream.get("channels")),
                sample_rate=_as_int(stream.get("sample_rate")),
                time_base=stream.get("time_base"),
                language=tags_for(stream).get("language"),
            )
            for stream in streams
            if stream.get("codec_type") == "audio"
        )
        subtitle_streams = tuple(
            SubtitleStreamFacts(
                index=int(stream["index"]),
                codec=stream.get("codec_name"),
                language=tags_for(stream).get("language"),
                title=tags_for(stream).get("title"),
                is_text=str(stream.get("codec_name") or "").lower()
                in _TEXT_SUBTITLE_CODECS,
            )
            for stream in streams
            if stream.get("codec_type") == "subtitle"
        )
        format_payload = payload.get("format") or {}
        average_rate = _rational(video.get("avg_frame_rate"))
        nominal_rate = _rational(video.get("r_frame_rate"))
        return MediaFacts(
            schema_version=1,
            source=str(source),
            duration_ms=round(duration * 1000),
            format_name=format_payload.get("format_name"),
            bit_rate=_as_int(format_payload.get("bit_rate")),
            video_stream=VideoStreamFacts(
                index=int(video["index"]),
                codec=video.get("codec_name"),
                width=_as_int(video.get("width")),
                height=_as_int(video.get("height")),
                pixel_format=video.get("pix_fmt"),
                avg_frame_rate=video.get("avg_frame_rate"),
                nominal_fps=average_rate,
                time_base=video.get("time_base"),
                rotation_degrees=_rotation(video),
                variable_frame_rate=(
                    abs(average_rate - nominal_rate) > 0.001
                    if average_rate is not None and nominal_rate is not None
                    else None
                ),
            ),
            audio_streams=audio_streams,
            subtitle_streams=subtitle_streams,
            decodable=True,
        )

    def detect_scenes(self, source: Path, *, threshold: float = 0.3) -> list[ScenePoint]:
        if not 0 <= threshold <= 1:
            raise ValueError("scene threshold must be between 0 and 1")
        result = self._run(
            (
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "info",
                "-i",
                str(source.resolve()),
                "-an",
                "-vf",
                f"select='gt(scene,{threshold:g})',metadata=print",
                "-fps_mode:v:0",
                "vfr",
                "-f",
                "null",
                "-",
            )
        )
        points: list[ScenePoint] = []
        pending_timestamp: int | None = None
        for line in result.stderr.splitlines():
            frame_match = _SCENE_FRAME_RE.search(line)
            if frame_match:
                pending_timestamp = max(0, round(float(frame_match.group(1)) * 1000))
                continue
            score_match = _SCENE_SCORE_RE.search(line)
            if score_match and pending_timestamp is not None:
                points.append(
                    ScenePoint(
                        timestamp_ms=pending_timestamp,
                        score=float(score_match.group(1)),
                    )
                )
                pending_timestamp = None
        unique: dict[int, ScenePoint] = {}
        for point in points:
            current = unique.get(point.timestamp_ms)
            if current is None or point.score > current.score:
                unique[point.timestamp_ms] = point
        return [unique[key] for key in sorted(unique)]

    def extract_frame(
        self,
        source: Path,
        *,
        timestamp_ms: int,
        output: Path,
        max_width: int = 960,
    ) -> Path:
        if timestamp_ms < 0:
            raise ValueError("timestamp_ms cannot be negative")
        if max_width <= 0:
            raise ValueError("max_width must be positive")
        output.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            (
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{timestamp_ms / 1000:.3f}",
                "-i",
                str(source.resolve()),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_width}:-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "2",
                "-fps_mode:v:0",
                "vfr",
                str(output.resolve()),
            )
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise FfmpegError(f"FFmpeg did not create frame at {timestamp_ms} ms")
        return output

    def extract_subtitle(
        self,
        source: Path,
        *,
        stream_index: int,
        output: Path,
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            (
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source.resolve()),
                "-map",
                f"0:{stream_index}",
                "-c:s",
                "srt",
                str(output.resolve()),
            )
        )
        if not output.is_file():
            raise FfmpegError(f"FFmpeg did not extract subtitle stream {stream_index}")
        return output

    def create_analysis_proxy(
        self,
        source: Path,
        *,
        output: Path,
        expected_duration_ms: int,
        max_width: int = 1280,
        max_height: int = 720,
        video_codec: str = "libx264",
        preset: str = "veryfast",
        crf: int = 23,
    ) -> Path:
        """Create a bounded, video-only derivative on the original timeline."""

        if expected_duration_ms <= 0:
            raise ValueError("expected_duration_ms must be positive")
        if max_width < 2 or max_height < 2:
            raise ValueError("analysis proxy dimensions must be at least 2")
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                (
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source.resolve()),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-dn",
                    "-vf",
                    (
                        f"scale={max_width}:{max_height}:"
                        "force_original_aspect_ratio=decrease:"
                        "force_divisible_by=2"
                    ),
                    "-c:v",
                    video_codec,
                    "-preset",
                    preset,
                    "-crf",
                    str(crf),
                    "-pix_fmt",
                    "yuv420p",
                    "-fps_mode:v:0",
                    "vfr",
                    "-movflags",
                    "+faststart",
                    str(output.resolve()),
                ),
                timeout_seconds=max(
                    self.timeout_seconds,
                    expected_duration_ms / 1000 * 8,
                ),
            )
            if not output.is_file() or output.stat().st_size == 0:
                raise FfmpegError("FFmpeg did not create the analysis proxy")
            rendered = self.probe(output)
            width = rendered.video_stream.width
            height = rendered.video_stream.height
            if width is None or height is None:
                raise FfmpegError(
                    "analysis proxy dimensions could not be verified"
                )
            if abs(rendered.video_stream.rotation_degrees) % 180 == 90:
                width, height = height, width
            if width > max_width or height > max_height:
                raise FfmpegError(
                    "analysis proxy exceeds configured bounds: "
                    f"{width}x{height} > {max_width}x{max_height}"
                )
            tolerance = max(250, round(expected_duration_ms * 0.01))
            if abs(rendered.duration_ms - expected_duration_ms) > tolerance:
                raise FfmpegError(
                    "analysis proxy duration is outside tolerance: "
                    f"expected {expected_duration_ms} ms, "
                    f"got {rendered.duration_ms} ms"
                )
        except BaseException:
            unlink_best_effort(output)
            raise
        return output

    def render_segment(
        self,
        source: Path,
        *,
        start_ms: int,
        end_ms: int,
        output: Path,
        video_codec: str = "libx264",
        preset: str = "veryfast",
        crf: int = 20,
        audio_codec: str = "aac",
        audio_bitrate: str = "128k",
    ) -> Path:
        """Render one exact half-open source range as a standalone MP4."""

        if start_ms < 0 or end_ms <= start_ms:
            raise ValueError("render range must be non-empty and non-negative")
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._run(
                (
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{start_ms / 1000:.3f}",
                    "-i",
                    str(source.resolve()),
                    "-t",
                    f"{(end_ms - start_ms) / 1000:.3f}",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-c:v",
                    video_codec,
                    "-preset",
                    preset,
                    "-crf",
                    str(crf),
                    "-c:a",
                    audio_codec,
                    "-b:a",
                    audio_bitrate,
                    "-movflags",
                    "+faststart",
                    str(output.resolve()),
                ),
                timeout_seconds=max(
                    self.timeout_seconds,
                    (end_ms - start_ms) / 1000 * 8,
                ),
            )
            if not output.is_file() or output.stat().st_size == 0:
                raise FfmpegError(
                    f"FFmpeg did not render segment {start_ms}-{end_ms} ms"
                )
            rendered = self.probe(output)
            expected_duration = end_ms - start_ms
            tolerance = max(250, round(expected_duration * 0.01))
            if abs(rendered.duration_ms - expected_duration) > tolerance:
                raise FfmpegError(
                    "rendered segment duration is outside tolerance: "
                    f"expected {expected_duration} ms, got {rendered.duration_ms} ms"
                )
        except BaseException:
            unlink_best_effort(output)
            raise
        return output
