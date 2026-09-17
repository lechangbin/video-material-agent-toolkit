"""Versioned non-secret workspace configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from semvideo.adapters.profiles import get_provider_profile

from .errors import config_error


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConcurrencyConfig(StrictModel):
    media: int = Field(default=2, ge=1)
    asr: int = Field(default=1, ge=1)
    llm: int = Field(default=2, ge=1)
    render: int = Field(default=1, ge=1)
    ffmpeg_cpu: int = Field(default=2, ge=1)


class EvidenceConfig(StrictModel):
    scene_threshold: float = Field(default=0.30, gt=0.0, lt=1.0)
    periodic_anchor_seconds: float = Field(default=4.0, ge=1.0)
    max_frames: int = Field(default=81, ge=9, le=360)
    transcribe: bool = True
    language: str = "zh"
    whisper_model: str = "base"
    whisper_compute_type: str = "int8"
    asr_cpu_threads: int = Field(default=4, ge=1)


class CinematographyConfig(StrictModel):
    frames_per_second: float = Field(default=1.0, gt=0.0, le=10.0)
    minimum_frames_per_shot: int = Field(default=4, ge=2, le=12)
    maximum_frames_per_shot: int = Field(default=9, ge=2, le=24)
    max_shots_per_request: int = Field(default=4, ge=1, le=12)

    @field_validator("maximum_frames_per_shot")
    @classmethod
    def validate_frame_bounds(cls, value: int, info: ValidationInfo) -> int:
        minimum = info.data.get("minimum_frames_per_shot")
        if minimum is not None and value < minimum:
            raise ValueError(
                "maximum_frames_per_shot must be at least minimum_frames_per_shot"
            )
        return value


class MediaConfig(StrictModel):
    ffmpeg_path: str = Field(default="ffmpeg", min_length=1)
    ffprobe_path: str = Field(default="ffprobe", min_length=1)
    analysis_proxy_max_width: int = Field(default=1280, ge=2)
    analysis_proxy_max_height: int = Field(default=720, ge=2)
    analysis_proxy_codec: str = Field(default="libx264", min_length=1)
    analysis_proxy_preset: str = Field(default="veryfast", min_length=1)
    analysis_proxy_crf: int = Field(default=23, ge=0, le=51)


class LlmConfig(StrictModel):
    provider: str = "siliconflow"
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "Qwen/Qwen3.6-35B-A3B"
    credential_env: str = "SEMVIDEO_API_KEY"
    enable_thinking: bool = False
    timeout_seconds: float = Field(default=600.0, gt=0)
    context_window_tokens: int = Field(default=256 * 1024, ge=16 * 1024)
    max_output_tokens: int = Field(default=8192, ge=512)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_retries: int = Field(default=4, ge=0, le=10)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_token_budget(self) -> LlmConfig:
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError(
                "max_output_tokens must be smaller than context_window_tokens"
            )
        return self

    @property
    def max_input_tokens(self) -> int:
        return self.context_window_tokens - self.max_output_tokens


class RenderConfig(StrictModel):
    enabled_by_default: bool = False
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=20, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"


class WorkspaceConfig(StrictModel):
    schema_version: int = 1
    profile: str = "default"
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    cinematography: CinematographyConfig = Field(default_factory=CinematographyConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)

    @model_validator(mode="after")
    def validate_provider_profile(self) -> WorkspaceConfig:
        profile = get_provider_profile(self.llm.provider)
        if profile is None:
            raise ValueError(f"unsupported model provider: {self.llm.provider}")
        mismatches = profile.configuration_mismatches(
            base_url=self.llm.base_url,
            model=self.llm.model,
            credential_env=self.llm.credential_env,
            context_window_tokens=self.llm.context_window_tokens,
            max_output_tokens=self.llm.max_output_tokens,
            enable_thinking=self.llm.enable_thinking,
        )
        if mismatches:
            raise ValueError(
                f"{profile.provider} provider profile requires fixed fields: "
                + ", ".join(mismatches)
            )
        if self.concurrency.llm > profile.max_concurrency:
            raise ValueError(
                f"{profile.provider} llm concurrency cannot exceed "
                f"{profile.max_concurrency}"
            )
        return self

    def credential_present(self) -> bool:
        return bool(os.environ.get(self.llm.credential_env))


DEFAULT_CONFIG_TOML = """\
schema_version = 1
profile = "default"

[concurrency]
media = 2
asr = 1
llm = 2
render = 1
ffmpeg_cpu = 2

[media]
ffmpeg_path = "ffmpeg"
ffprobe_path = "ffprobe"
analysis_proxy_max_width = 1280
analysis_proxy_max_height = 720
analysis_proxy_codec = "libx264"
analysis_proxy_preset = "veryfast"
analysis_proxy_crf = 23

[evidence]
scene_threshold = 0.30
periodic_anchor_seconds = 4.0
max_frames = 81
transcribe = true
language = "zh"
whisper_model = "base"
whisper_compute_type = "int8"
asr_cpu_threads = 4

[cinematography]
frames_per_second = 1.0
minimum_frames_per_shot = 4
maximum_frames_per_shot = 9
max_shots_per_request = 4

[llm]
provider = "siliconflow"
base_url = "https://api.siliconflow.cn/v1"
model = "Qwen/Qwen3.6-35B-A3B"
credential_env = "SEMVIDEO_API_KEY"
enable_thinking = false
timeout_seconds = 600.0
context_window_tokens = 262144
max_output_tokens = 8192
temperature = 0.1
max_retries = 4

[render]
enabled_by_default = false
video_codec = "libx264"
preset = "veryfast"
crf = 20
audio_codec = "aac"
audio_bitrate = "128k"
"""


def load_workspace_config(data_root: Path) -> WorkspaceConfig:
    config_path = data_root / "config.toml"
    if not config_path.is_file():
        raise config_error(
            "workspace_config_missing",
            f"工作区配置不存在：{config_path}",
            config_path=str(config_path),
        )
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        return WorkspaceConfig.model_validate(raw)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise config_error(
            "workspace_config_invalid",
            f"工作区配置无效：{config_path}",
            config_path=str(config_path),
            reason=str(exc),
        ) from exc
