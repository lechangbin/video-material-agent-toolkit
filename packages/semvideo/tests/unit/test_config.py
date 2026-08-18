from __future__ import annotations

from pathlib import Path

import pytest

from semvideo.config import DEFAULT_CONFIG_TOML, load_workspace_config
from semvideo.errors import SemvideoError


def _agnes_config() -> str:
    return (
        DEFAULT_CONFIG_TOML.replace(
            'provider = "siliconflow"',
            'provider = "agnes"',
        )
        .replace(
            'base_url = "https://api.siliconflow.cn/v1"',
            'base_url = "https://apihub.agnes-ai.com/v1"',
        )
        .replace(
            'model = "Qwen/Qwen3.6-35B-A3B"',
            'model = "agnes-2.5-flash"',
        )
        .replace(
            'credential_env = "SEMVIDEO_API_KEY"',
            'credential_env = "AGNES_API_KEY"',
        )
        .replace(
            "context_window_tokens = 262144",
            "context_window_tokens = 524288",
        )
    )


def test_load_default_workspace_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    config = load_workspace_config(tmp_path)

    assert config.concurrency.media == 2
    assert config.concurrency.ffmpeg_cpu == 2
    assert config.media.ffmpeg_path == "ffmpeg"
    assert config.media.ffprobe_path == "ffprobe"
    assert config.llm.model == "Qwen/Qwen3.6-35B-A3B"
    assert config.llm.context_window_tokens == 262144
    assert config.llm.max_input_tokens == 253952
    assert config.render.enabled_by_default is False


def test_config_rejects_secret_like_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        DEFAULT_CONFIG_TOML + '\napi_key = "must-not-be-accepted"\n',
        encoding="utf-8",
    )

    with pytest.raises(SemvideoError) as raised:
        load_workspace_config(tmp_path)

    assert raised.value.payload.code == "workspace_config_invalid"


def test_load_agnes_profile_uses_512k_context_and_two_slots(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(_agnes_config(), encoding="utf-8")

    config = load_workspace_config(tmp_path)

    assert config.llm.provider == "agnes"
    assert config.llm.model == "agnes-2.5-flash"
    assert config.llm.context_window_tokens == 524288
    assert config.llm.max_output_tokens == 8192
    assert config.llm.max_input_tokens == 516096
    assert config.concurrency.llm == 2


def test_agnes_profile_allows_one_safer_llm_slot(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        _agnes_config().replace("llm = 2", "llm = 1", 1),
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.concurrency.llm == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("context_window_tokens = 524288", "context_window_tokens = 131072"),
        ('credential_env = "AGNES_API_KEY"', 'credential_env = "OTHER_KEY"'),
        ("llm = 2", "llm = 3"),
    ],
)
def test_agnes_profile_rejects_changed_safety_envelope(
    tmp_path: Path,
    old: str,
    new: str,
) -> None:
    (tmp_path / "config.toml").write_text(
        _agnes_config().replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(SemvideoError) as raised:
        load_workspace_config(tmp_path)

    assert raised.value.payload.code == "workspace_config_invalid"
