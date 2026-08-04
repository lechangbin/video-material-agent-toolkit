"""Stable application interface for non-secret workspace configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from semvideo.application.workspace import WorkspacePaths
from semvideo.config import load_workspace_config
from semvideo.errors import config_error
from semvideo.infrastructure.io import atomic_write_bytes


def set_media_tools(
    workspace: WorkspacePaths,
    *,
    ffmpeg_path: str | Path,
    ffprobe_path: str | Path,
) -> dict[str, Any]:
    """Persist explicit FFmpeg executables without exposing raw file mutation."""

    resolved = {
        "ffmpeg_path": Path(ffmpeg_path).expanduser().resolve(),
        "ffprobe_path": Path(ffprobe_path).expanduser().resolve(),
    }
    for name, path in resolved.items():
        if not path.is_file():
            raise config_error(
                "media_tool_not_found",
                f"媒体工具不存在：{path}",
                field=name,
                path=str(path),
            )
    existing_media = load_workspace_config(workspace.data).media
    media_section = (
        "[media]\n"
        f"ffmpeg_path = {json.dumps(str(resolved['ffmpeg_path']), ensure_ascii=False)}\n"
        f"ffprobe_path = {json.dumps(str(resolved['ffprobe_path']), ensure_ascii=False)}\n"
        f"analysis_proxy_max_width = {existing_media.analysis_proxy_max_width}\n"
        f"analysis_proxy_max_height = {existing_media.analysis_proxy_max_height}\n"
        f"analysis_proxy_codec = {json.dumps(existing_media.analysis_proxy_codec)}\n"
        f"analysis_proxy_preset = {json.dumps(existing_media.analysis_proxy_preset)}\n"
        f"analysis_proxy_crf = {existing_media.analysis_proxy_crf}\n"
    )
    try:
        current = workspace.config.read_text(encoding="utf-8")
        pattern = re.compile(r"(?ms)^\[media\]\s*\n.*?(?=^\[|\Z)")
        if pattern.search(current):
            updated = pattern.sub(
                lambda _: media_section + "\n",
                current,
                count=1,
            )
        else:
            updated = current.rstrip() + "\n\n" + media_section
        atomic_write_bytes(workspace.config, updated.encode("utf-8"))
        config = load_workspace_config(workspace.data)
    except OSError as exc:
        raise config_error(
            "workspace_config_update_failed",
            "无法更新工作区媒体工具配置。",
            config_path=str(workspace.config),
            reason=str(exc),
        ) from exc
    return {
        "schema_version": 1,
        "ffmpeg_path": config.media.ffmpeg_path,
        "ffprobe_path": config.media.ffprobe_path,
    }
