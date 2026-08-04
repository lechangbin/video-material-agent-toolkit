"""Workspace capability diagnostics exposed independently from the CLI."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Any

from semvideo.adapters.ffmpeg import FfmpegAdapter
from semvideo.application.workspace import WorkspacePaths
from semvideo.config import load_workspace_config
from semvideo.infrastructure.io import (
    atomic_write_json,
    read_json,
    unlink_best_effort,
)
from semvideo.infrastructure.locks import exclusive_file_lock


def run_doctor(workspace: WorkspacePaths) -> dict[str, Any]:
    config = load_workspace_config(workspace.data)
    cleanup_warnings: list[dict[str, str]] = []
    ffmpeg_capabilities = FfmpegAdapter(
        ffmpeg_path=config.media.ffmpeg_path,
        ffprobe_path=config.media.ffprobe_path,
    ).inspect_capabilities()

    diagnostic_path = workspace.runtime / f".doctor-atomic-{os.getpid()}.json"
    try:
        atomic_write_json(diagnostic_path, {"schema_version": 1, "ok": True})
        atomic_ok = read_json(diagnostic_path).get("ok") is True
    finally:
        cleanup_error = unlink_best_effort(diagnostic_path)
        if cleanup_error is not None:
            cleanup_warnings.append(
                {
                    "code": "diagnostic_cleanup_failed",
                    "path": str(diagnostic_path),
                }
            )
    try:
        with exclusive_file_lock(
            workspace.locks / "doctor-capability.lock",
            timeout=1,
        ):
            lock_ok = True
    except (OSError, TimeoutError):
        lock_ok = False

    pyav_available = False
    pyav_version = None
    if importlib.util.find_spec("av") is not None:
        try:
            pyav_module = importlib.import_module("av")
            pyav_available = callable(getattr(pyav_module, "open", None))
            pyav_version = getattr(pyav_module, "__version__", None)
        except (ImportError, OSError):
            pyav_available = False
    faster_whisper_available = (
        importlib.util.find_spec("faster_whisper") is not None
    )
    checks: dict[str, Any] = {
        "python": {
            "ok": sys.version_info[:3] == (3, 14, 6),
            "version": ".".join(map(str, sys.version_info[:3])),
        },
        "ffmpeg": {
            "ok": (
                ffmpeg_capabilities.ffmpeg_path is not None
                and ffmpeg_capabilities.ffmpeg_version is not None
                and ffmpeg_capabilities.fps_mode_vfr_supported
                and all(ffmpeg_capabilities.encoders.values())
            ),
            "path": ffmpeg_capabilities.ffmpeg_path,
            "version": ffmpeg_capabilities.ffmpeg_version,
            "fps_mode_vfr_supported": (
                ffmpeg_capabilities.fps_mode_vfr_supported
            ),
            "encoders": ffmpeg_capabilities.encoders,
        },
        "ffprobe": {
            "ok": (
                ffmpeg_capabilities.ffprobe_path is not None
                and ffmpeg_capabilities.ffprobe_version is not None
            ),
            "path": ffmpeg_capabilities.ffprobe_path,
            "version": ffmpeg_capabilities.ffprobe_version,
        },
        "workspace": {"ok": True, "root": str(workspace.root)},
        "atomic_replace": {
            "ok": atomic_ok,
            "warnings": cleanup_warnings,
        },
        "file_lock": {"ok": lock_ok},
        "pillow": {"ok": importlib.util.find_spec("PIL") is not None},
        "pyav": {
            "ok": pyav_available,
            "version": pyav_version,
            "open_available": pyav_available,
        },
        "asr_adapter": {
            "ok": faster_whisper_available and pyav_available,
            "faster_whisper_available": faster_whisper_available,
            "pyav_available": pyav_available,
            "model_download_attempted": False,
        },
        "native_evidence": {
            "ok": importlib.util.find_spec("semvideo.modules.evidence")
            is not None,
            "crv_dependency": False,
        },
        "credential": {
            "ok": config.credential_present(),
            "environment_variable": config.llm.credential_env,
        },
        "model": {
            "provider": config.llm.provider,
            "model": config.llm.model,
            "paid_request_sent": False,
        },
    }
    safe_limits = {
        "media": 2,
        "asr": 2,
        "llm": 2,
        "render": 2,
        "ffmpeg_cpu": 2,
    }
    configured_limits = config.concurrency.model_dump()
    checks["concurrency"] = {
        "ok": True,
        "configured": configured_limits,
        "warnings": [
            f"{name}={value} 高于当前开发机建议操作上限 {safe_limits[name]}"
            for name, value in configured_limits.items()
            if value > safe_limits[name]
        ],
    }
    checks["ok"] = all(
        item.get("ok", True)
        for item in checks.values()
        if isinstance(item, dict)
    )
    return checks
