from __future__ import annotations

from pathlib import Path

import pytest

from semvideo.config import DEFAULT_CONFIG_TOML, load_workspace_config
from semvideo.errors import SemvideoError


def test_load_default_workspace_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    config = load_workspace_config(tmp_path)

    assert config.concurrency.media == 2
    assert config.concurrency.ffmpeg_cpu == 2
    assert config.media.ffmpeg_path == "ffmpeg"
    assert config.media.ffprobe_path == "ffprobe"
    assert config.llm.model == "Qwen/Qwen3.6-35B-A3B"
    assert config.render.enabled_by_default is False


def test_config_rejects_secret_like_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        DEFAULT_CONFIG_TOML + '\napi_key = "must-not-be-accepted"\n',
        encoding="utf-8",
    )

    with pytest.raises(SemvideoError) as raised:
        load_workspace_config(tmp_path)

    assert raised.value.payload.code == "workspace_config_invalid"
