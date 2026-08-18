"""Stable application interface for non-secret workspace configuration."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from semvideo.adapters.profiles import get_provider_profile
from semvideo.application.workspace import WorkspacePaths
from semvideo.config import WorkspaceConfig, load_workspace_config
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


def set_llm_provider_profile(
    workspace: WorkspacePaths,
    *,
    provider: str,
) -> dict[str, Any]:
    """Select a supported model provider and its fixed safety envelope."""

    profile = get_provider_profile(provider)
    if profile is None or not profile.selectable:
        raise config_error(
            "llm_provider_unsupported",
            f"不支持的模型提供商：{provider}",
            provider=provider,
        )
    current_config = load_workspace_config(workspace.data)
    concurrency = current_config.concurrency.model_copy(
        update={"llm": profile.max_concurrency}
    )
    llm_section = (
        "[llm]\n"
        f"provider = {json.dumps(profile.provider)}\n"
        f"base_url = {json.dumps(profile.base_url)}\n"
        f"model = {json.dumps(profile.model)}\n"
        f"credential_env = {json.dumps(profile.credential_env)}\n"
        "enable_thinking = false\n"
        f"timeout_seconds = {current_config.llm.timeout_seconds}\n"
        f"context_window_tokens = {profile.context_window_tokens}\n"
        f"max_output_tokens = {profile.default_max_output_tokens}\n"
        f"temperature = {current_config.llm.temperature}\n"
        f"max_retries = {current_config.llm.max_retries}\n"
    )
    concurrency_section = (
        "[concurrency]\n"
        f"media = {concurrency.media}\n"
        f"asr = {concurrency.asr}\n"
        f"llm = {concurrency.llm}\n"
        f"render = {concurrency.render}\n"
        f"ffmpeg_cpu = {concurrency.ffmpeg_cpu}\n"
    )
    try:
        current = workspace.config.read_text(encoding="utf-8")
        for name, section in (
            ("concurrency", concurrency_section),
            ("llm", llm_section),
        ):
            pattern = re.compile(rf"(?ms)^\[{name}\]\s*\n.*?(?=^\[|\Z)")
            if pattern.search(current):
                current = pattern.sub(section + "\n", current, count=1)
            else:
                current = current.rstrip() + "\n\n" + section
        candidate = WorkspaceConfig.model_validate(tomllib.loads(current))
        atomic_write_bytes(workspace.config, current.encode("utf-8"))
        config = candidate
    except OSError as exc:
        raise config_error(
            "workspace_config_update_failed",
            "无法更新工作区模型提供商配置。",
            config_path=str(workspace.config),
            reason=str(exc),
        ) from exc
    return {
        "schema_version": 1,
        "provider": config.llm.provider,
        "base_url": config.llm.base_url,
        "model": config.llm.model,
        "credential_env": config.llm.credential_env,
        "context_window_tokens": config.llm.context_window_tokens,
        "max_input_tokens": config.llm.max_input_tokens,
        "max_output_tokens": config.llm.max_output_tokens,
        "max_concurrency": profile.max_concurrency,
        "configured_concurrency": config.concurrency.llm,
    }
