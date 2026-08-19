"""FFmpeg-backed conformance runtime hidden behind public contracts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from media_conformance.contracts import (
    AssemblySetCompatibility,
    ConformedClip,
    EditingMediaConformanceRequest,
    EditingMediaConformanceResult,
    MeasuredAudioStream,
    MeasuredVideoStream,
)
from media_conformance.errors import ConformanceError


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    request: Path
    result: Path
    state: Path
    cancel: Path
    clips: Path

    @classmethod
    def for_request(cls, request: EditingMediaConformanceRequest) -> RuntimePaths:
        output = request.output_directory.expanduser().resolve()
        root = output / ".media-conformance" / request.request_id
        return cls(
            root=root,
            request=root / "request.json",
            result=root / "result.json",
            state=root / "state.json",
            cancel=root / "cancel.requested",
            clips=output / request.request_id,
        )


class ConformanceRuntime:
    """Synchronous, recoverable task runner with cooperative process cancellation."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._popen = popen

    def prepare(
        self, request: EditingMediaConformanceRequest
    ) -> EditingMediaConformanceResult:
        paths = RuntimePaths.for_request(request)
        self._freeze_request(paths, request)
        if paths.result.exists():
            result = EditingMediaConformanceResult.model_validate_json(
                paths.result.read_text(encoding="utf-8")
            )
            if result.status == "completed" and self._completed_outputs_valid(
                request, result
            ):
                return result
        paths.cancel.unlink(missing_ok=True)
        _atomic_json(paths.state, {"status": "running", "request_id": request.request_id})
        source_hash = _sha256_checked(request.source.path)
        if source_hash != request.source.sha256:
            return self._fail(
                request,
                paths,
                ConformanceError(
                    "source_hash_mismatch",
                    "The immutable source hash does not match the request.",
                    details={"asset_id": request.source.asset_id},
                ),
            )
        source_probe = self._probe(request.source.path)
        self._require_source_eligible(source_probe)
        clips: list[ConformedClip] = []
        try:
            paths.clips.mkdir(parents=True, exist_ok=True)
            for selected_range in request.ranges:
                if paths.cancel.exists():
                    raise ConformanceError(
                        "conformance_cancelled", "Conformance was cancelled."
                    )
                output = paths.clips / f"{selected_range.clip_id}.mp4"
                clip = self._render_clip(
                    request,
                    selected_range.clip_id,
                    selected_range.start_seconds,
                    selected_range.end_seconds,
                    source_probe,
                    output,
                    paths.cancel,
                )
                clips.append(clip)
            assembly = self._verify_set(request, tuple(clips), paths.cancel)
            if not assembly.compatible:
                raise ConformanceError(
                    assembly.error_code or "assembly_set_incompatible",
                    "The conformed clips failed compatibility verification.",
                )
            result = EditingMediaConformanceResult(
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                status="completed",
                profile=request.profile,
                clips=tuple(clips),
                assembly_set=assembly,
            )
            _atomic_json(paths.result, result.model_dump(mode="json"))
            _atomic_json(paths.state, {"status": "completed", "request_id": request.request_id})
            return result
        except ConformanceError as error:
            return self._fail(request, paths, error)
        except OSError as error:
            return self._fail(
                request,
                paths,
                ConformanceError(
                    "conformance_io_failed",
                    "Media conformance could not access a required local file or executable.",
                    details={"exception_type": type(error).__name__},
                    retryable=True,
                ),
            )
        finally:
            if _sha256_checked(request.source.path) != request.source.sha256:
                raise ConformanceError(
                    "source_mutated",
                    "The immutable source changed during conformance.",
                )

    def status(self, request: EditingMediaConformanceRequest) -> dict[str, object]:
        paths = RuntimePaths.for_request(request)
        if paths.result.exists():
            value = json.loads(paths.result.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"status": "corrupt"}
        if paths.state.exists():
            value = json.loads(paths.state.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"status": "corrupt"}
        return {"status": "not_started", "request_id": request.request_id}

    def cancel(self, request: EditingMediaConformanceRequest) -> dict[str, object]:
        paths = RuntimePaths.for_request(request)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.cancel.write_text("cancel\n", encoding="utf-8")
        return {"status": "cancellation_requested", "request_id": request.request_id}

    def verify_existing_set(
        self, request: EditingMediaConformanceRequest
    ) -> AssemblySetCompatibility:
        paths = RuntimePaths.for_request(request)
        if not paths.result.exists():
            raise ConformanceError(
                "conformance_result_missing", "No completed result exists for the request."
            )
        result = EditingMediaConformanceResult.model_validate_json(
            paths.result.read_text(encoding="utf-8")
        )
        return self._verify_set(request, result.clips, paths.cancel)

    def _freeze_request(
        self, paths: RuntimePaths, request: EditingMediaConformanceRequest
    ) -> None:
        payload = request.model_dump(mode="json")
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        paths.root.mkdir(parents=True, exist_ok=True)
        if paths.request.exists():
            if paths.request.read_text(encoding="utf-8") != content:
                raise ConformanceError(
                    "idempotency_conflict",
                    "The request identifier is already bound to different inputs.",
                )
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".request.", suffix=".tmp", dir=paths.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, paths.request)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _probe(self, path: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [
                self._ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise ConformanceError(
                "media_probe_failed",
                "FFprobe could not inspect the media.",
                details={"exit_code": completed.returncode},
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ConformanceError(
                "media_probe_invalid", "FFprobe returned invalid JSON."
            ) from error
        if not isinstance(payload, dict):
            raise ConformanceError("media_probe_invalid", "FFprobe returned invalid data.")
        return payload

    def _require_source_eligible(self, probe: dict[str, Any]) -> None:
        video = _stream(probe, "video")
        width = int(video.get("width", 0))
        height = int(video.get("height", 0))
        sar = _ratio(str(video.get("sample_aspect_ratio") or "1:1"))
        if width <= 0 or height <= 0 or sar is None:
            raise ConformanceError(
                "source_geometry_unknown", "Source display geometry is unavailable."
            )
        ratio = width * sar / height
        if abs(ratio - (16 / 9)) / (16 / 9) > 0.01:
            raise ConformanceError(
                "source_geometry_rejected", "Source display geometry is not 16:9."
            )

    def _render_clip(
        self,
        request: EditingMediaConformanceRequest,
        clip_id: str,
        start: float,
        end: float,
        source_probe: dict[str, Any],
        output: Path,
        cancel: Path,
    ) -> ConformedClip:
        duration = end - start
        temporary = output.with_suffix(".partial.mp4")
        temporary.unlink(missing_ok=True)
        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in source_probe.get("streams", [])
            if isinstance(stream, dict)
        )
        transfer = str(_stream(source_probe, "video").get("color_transfer") or "").lower()
        hdr = transfer in {"smpte2084", "arib-std-b67"}
        filters = (
            "setpts=PTS-STARTPTS,"
            + (
                "zscale=t=linear:npl=100,format=gbrpf32le,"
                "zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                "zscale=t=bt709:m=bt709:r=tv," if hdr else ""
            )
            + "scale=1920:1080:flags=lanczos,setsar=1,fps=30,format=yuv420p,"
            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"
        )
        command = [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(request.source.path),
        ]
        if not has_audio:
            command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0" if has_audio else "1:a:0",
                "-vf",
                filters,
                "-af",
                "asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0",
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-g",
                "60",
                "-keyint_min",
                "60",
                "-sc_threshold",
                "0",
                "-video_track_timescale",
                "90000",
                "-c:a",
                "aac",
                "-profile:a",
                "aac_low",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-b:a",
                "192k",
                "-t",
                f"{duration:.6f}",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
        )
        self._run_process(command, cancel)
        measured = self._probe(temporary)
        video, audio = _measured_streams(measured)
        if not _matches_profile(video, audio):
            temporary.unlink(missing_ok=True)
            raise ConformanceError(
                "conformed_profile_mismatch",
                "Rendered media does not match the frozen delivery profile.",
            )
        os.replace(temporary, output)
        parameters = {
            "profile": request.profile.model_dump(mode="json"),
            "range": [start, end],
            "hdr_tonemap": hdr,
        }
        return ConformedClip(
            clip_id=clip_id,
            relative_path=output.relative_to(request.output_directory.resolve()).as_posix(),
            sha256=_sha256(output),
            size_bytes=output.stat().st_size,
            source_asset_id=request.source.asset_id,
            source_sha256=request.source.sha256,
            start_seconds=start,
            end_seconds=end,
            ffmpeg_version=self._ffmpeg_version(),
            effective_parameters_hash=_canonical_hash(parameters),
            video=video,
            audio=audio,
            profile_valid=True,
        )

    def _verify_set(
        self,
        request: EditingMediaConformanceRequest,
        clips: Sequence[ConformedClip],
        cancel: Path,
    ) -> AssemblySetCompatibility:
        if not clips:
            return AssemblySetCompatibility(
                compatible=False,
                packet_concat_verified=False,
                clip_ids=(),
                error_code="assembly_set_empty",
            )
        signature = _stream_signature(clips[0])
        if any(_stream_signature(clip) != signature for clip in clips[1:]):
            return AssemblySetCompatibility(
                compatible=False,
                packet_concat_verified=False,
                clip_ids=tuple(clip.clip_id for clip in clips),
                error_code="assembly_stream_mismatch",
            )
        paths = RuntimePaths.for_request(request)
        list_path = paths.root / "concat-list.txt"
        smoke_path = paths.root / "concat-smoke.mp4"
        lines: list[str] = []
        for clip in clips:
            absolute = (request.output_directory.resolve() / clip.relative_path).resolve()
            escaped = str(absolute).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        smoke_path.unlink(missing_ok=True)
        try:
            self._run_process(
                [
                    self._ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c",
                    "copy",
                    str(smoke_path),
                ],
                cancel,
            )
            self._probe(smoke_path)
        except ConformanceError:
            return AssemblySetCompatibility(
                compatible=False,
                packet_concat_verified=False,
                clip_ids=tuple(clip.clip_id for clip in clips),
                error_code="assembly_concat_failed",
            )
        finally:
            smoke_path.unlink(missing_ok=True)
        return AssemblySetCompatibility(
            compatible=True,
            packet_concat_verified=True,
            clip_ids=tuple(clip.clip_id for clip in clips),
        )

    def _run_process(self, command: list[str], cancel: Path) -> None:
        descriptor, log_name = tempfile.mkstemp(prefix="media-conformance-", suffix=".log")
        log_path = Path(log_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as error_log:
                process = self._popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_log,
                    text=True,
                    encoding="utf-8",
                )
                while process.poll() is None:
                    if cancel.exists():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise ConformanceError(
                            "conformance_cancelled", "Conformance was cancelled."
                        )
                    time.sleep(0.05)
                process.wait()
            if process.returncode != 0:
                stderr = log_path.read_text(encoding="utf-8", errors="replace")
                raise ConformanceError(
                    "ffmpeg_render_failed",
                    "FFmpeg could not complete the requested media operation.",
                    details={"exit_code": process.returncode, "stderr_tail": stderr[-1000:]},
                )
        finally:
            log_path.unlink(missing_ok=True)

    def _ffmpeg_version(self) -> str:
        completed = subprocess.run(
            [self._ffmpeg, "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.splitlines()[0][:200] if completed.stdout else "unknown"

    def _completed_outputs_valid(
        self,
        request: EditingMediaConformanceRequest,
        result: EditingMediaConformanceResult,
    ) -> bool:
        if result.idempotency_key != request.idempotency_key:
            return False
        return all(
            (request.output_directory.resolve() / clip.relative_path).is_file()
            and _sha256(request.output_directory.resolve() / clip.relative_path) == clip.sha256
            for clip in result.clips
        )

    def _fail(
        self,
        request: EditingMediaConformanceRequest,
        paths: RuntimePaths,
        error: ConformanceError,
    ) -> EditingMediaConformanceResult:
        for partial in paths.clips.glob("*.partial.mp4") if paths.clips.exists() else ():
            partial.unlink(missing_ok=True)
        status: Literal["failed", "cancelled"] = (
            "cancelled" if error.code == "conformance_cancelled" else "failed"
        )
        result = EditingMediaConformanceResult(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            status=status,
            profile=request.profile,
            error=error.as_dict(),
        )
        _atomic_json(paths.result, result.model_dump(mode="json"))
        _atomic_json(paths.state, {"status": status, "request_id": request.request_id})
        return result


def _sha256_checked(path: Path) -> str:
    try:
        return _sha256(path)
    except OSError as error:
        raise ConformanceError(
            "source_unavailable", "The immutable source asset is unavailable."
        ) from error


def _stream(probe: dict[str, Any], kind: str) -> dict[str, Any]:
    for stream in probe.get("streams", []):
        if isinstance(stream, dict) and stream.get("codec_type") == kind:
            return stream
    raise ConformanceError(f"{kind}_stream_missing", f"The {kind} stream is unavailable.")


def _ratio(value: str) -> float | None:
    separator = ":" if ":" in value else "/"
    try:
        left, right = value.split(separator, 1)
        denominator = float(right)
        return float(left) / denominator if denominator else None
    except (ValueError, TypeError):
        return None


def _measured_streams(
    probe: dict[str, Any],
) -> tuple[MeasuredVideoStream, MeasuredAudioStream]:
    video = _stream(probe, "video")
    audio = _stream(probe, "audio")
    return (
        MeasuredVideoStream(
            codec_name=str(video.get("codec_name") or ""),
            profile=str(video.get("profile") or ""),
            pixel_format=str(video.get("pix_fmt") or ""),
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            sample_aspect_ratio=str(video.get("sample_aspect_ratio") or ""),
            frame_rate=str(video.get("avg_frame_rate") or ""),
            time_base=str(video.get("time_base") or ""),
            color_primaries=str(video.get("color_primaries") or ""),
            color_transfer=str(video.get("color_transfer") or ""),
            color_space=str(video.get("color_space") or ""),
        ),
        MeasuredAudioStream(
            codec_name=str(audio.get("codec_name") or ""),
            profile=str(audio.get("profile") or ""),
            sample_rate=int(audio.get("sample_rate", 0)),
            channels=int(audio.get("channels", 0)),
            channel_layout=str(audio.get("channel_layout") or ""),
        ),
    )


def _matches_profile(video: MeasuredVideoStream, audio: MeasuredAudioStream) -> bool:
    return (
        video.codec_name == "h264"
        and video.profile.casefold() == "high"
        and video.pixel_format == "yuv420p"
        and (video.width, video.height) == (1920, 1080)
        and video.sample_aspect_ratio == "1:1"
        and video.frame_rate == "30/1"
        and video.time_base == "1/90000"
        and video.color_primaries == "bt709"
        and video.color_transfer == "bt709"
        and video.color_space == "bt709"
        and audio.codec_name == "aac"
        and audio.sample_rate == 48000
        and audio.channels == 2
    )


def _stream_signature(clip: ConformedClip) -> tuple[object, ...]:
    return tuple(clip.video.model_dump().values()) + tuple(clip.audio.model_dump().values())
