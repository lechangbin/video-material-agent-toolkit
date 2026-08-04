"""Atomic on-demand export for any validated source-video time range."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from semvideo.adapters.ffmpeg import FfmpegAdapter, FfmpegError
from semvideo.application.source_store import sha256_file
from semvideo.application.workspace import WorkspacePaths
from semvideo.config import load_workspace_config
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import read_versioned_json, unlink_best_effort
from semvideo.infrastructure.locks import ResourceLimits, ResourceLockManager


@dataclass(frozen=True, slots=True)
class RegisteredTaskMedia:
    """A task-package media artifact eligible for verified reuse."""

    relative_path: str
    invalid_code: str
    invalid_message: str
    entity_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class RangeExportResult:
    destination: Path
    task_artifact: str | None
    reused: bool


def _temporary_path(destination: Path) -> Path:
    return destination.with_name(
        f".{destination.stem}.{os.getpid()}.tmp"
        f"{destination.suffix or '.mp4'}"
    )


def _render_dependency_error(exc: BaseException) -> SemvideoError:
    return SemvideoError(
        code="render_dependency_failed",
        category=ErrorCategory.DEPENDENCY,
        message="render 阶段的本地媒体依赖执行失败。",
        retryable=False,
        recovery=RecoveryAction.USER_ACTION,
        stage="render",
        details={
            "exception_type": type(exc).__name__,
            "reason": str(exc),
        },
        exit_code=4,
    )


def _validate_registered_media(
    *,
    job_root: Path,
    registered: RegisteredTaskMedia,
) -> Path:
    registered_output = job_root / registered.relative_path
    manifest = read_versioned_json(job_root / "manifest.json")
    artifact = next(
        (
            row
            for row in manifest.get("artifacts", [])
            if row.get("path") == registered.relative_path
        ),
        None,
    )
    if (
        artifact is None
        or not registered_output.is_file()
        or artifact.get("content_hash") != sha256_file(registered_output)
    ):
        raise SemvideoError(
            code=registered.invalid_code,
            category=ErrorCategory.INTERNAL,
            message=registered.invalid_message,
            retryable=False,
            recovery=RecoveryAction.REPORT_BUG,
            entity_id=registered.entity_id,
            details={"job_id": registered.job_id},
            exit_code=10,
        )
    return registered_output


def export_time_range(
    workspace: WorkspacePaths,
    *,
    job_root: Path,
    source: Path,
    start_ms: int,
    end_ms: int,
    output: Path,
    registered: RegisteredTaskMedia | None = None,
) -> RangeExportResult:
    """Render or copy one range under the shared render budget."""

    destination = output.expanduser().resolve()
    config = load_workspace_config(workspace.data)
    limits = ResourceLimits.from_mapping(config.concurrency.model_dump())
    manager = ResourceLockManager(workspace.locks, limits)
    temporary = _temporary_path(destination)
    try:
        with manager.acquire("render"):
            registered_output = (
                _validate_registered_media(
                    job_root=job_root,
                    registered=registered,
                )
                if registered is not None
                else None
            )
            if registered_output is not None:
                if destination != registered_output.resolve():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    unlink_best_effort(temporary)
                    try:
                        shutil.copy2(registered_output, temporary)
                        os.replace(temporary, destination)
                    finally:
                        unlink_best_effort(temporary)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                unlink_best_effort(temporary)
                try:
                    FfmpegAdapter(
                        ffmpeg_path=config.media.ffmpeg_path,
                        ffprobe_path=config.media.ffprobe_path,
                    ).render_segment(
                        source,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        output=temporary,
                        video_codec=config.render.video_codec,
                        preset=config.render.preset,
                        crf=config.render.crf,
                        audio_codec=config.render.audio_codec,
                        audio_bitrate=config.render.audio_bitrate,
                    )
                    os.replace(temporary, destination)
                finally:
                    unlink_best_effort(temporary)
    except SemvideoError:
        raise
    except (FfmpegError, OSError) as exc:
        raise _render_dependency_error(exc) from exc
    return RangeExportResult(
        destination=destination,
        task_artifact=(
            registered.relative_path if registered is not None else None
        ),
        reused=registered is not None,
    )
