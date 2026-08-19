"""Authoritative public contracts for editing-media conformance."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EditingDeliveryProfile(_Contract):
    schema_version: Literal["editing-delivery-profile/v1"] = "editing-delivery-profile/v1"
    container: Literal["mp4"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    video_encoder: Literal["libx264"] = "libx264"
    video_profile: Literal["high"] = "high"
    pixel_format: Literal["yuv420p"] = "yuv420p"
    width: Literal[1920] = 1920
    height: Literal[1080] = 1080
    sample_aspect_ratio: Literal["1:1"] = "1:1"
    frame_rate: Literal["30/1"] = "30/1"
    gop_frames: Literal[60] = 60
    video_track_time_scale: Literal[90000] = 90000
    color_primaries: Literal["bt709"] = "bt709"
    color_transfer: Literal["bt709"] = "bt709"
    color_space: Literal["bt709"] = "bt709"
    audio_codec: Literal["aac"] = "aac"
    audio_profile: Literal["LC"] = "LC"
    audio_sample_rate: Literal[48000] = 48000
    audio_channels: Literal[2] = 2
    audio_bit_rate: Literal[192000] = 192000


class ImmutableSourceAsset(_Contract):
    asset_id: str
    sha256: str
    path: Path

    @field_validator("asset_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("asset_id is not a safe identifier")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class SelectedRange(_Contract):
    clip_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @field_validator("clip_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("clip_id is not a safe identifier")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> SelectedRange:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("selected range must be half-open with end greater than start")
        return self


class EditingMediaConformanceRequest(_Contract):
    schema_version: Literal["editing-media-conformance-request/v1"] = (
        "editing-media-conformance-request/v1"
    )
    request_id: str
    idempotency_key: str
    source: ImmutableSourceAsset
    ranges: tuple[SelectedRange, ...] = Field(min_length=1)
    output_directory: Path
    profile: EditingDeliveryProfile = Field(default_factory=EditingDeliveryProfile)

    @field_validator("request_id", "idempotency_key")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("request identifiers must be filesystem-safe")
        return value

    @model_validator(mode="after")
    def validate_unique_clips(self) -> EditingMediaConformanceRequest:
        ids = [item.clip_id for item in self.ranges]
        if len(ids) != len(set(ids)):
            raise ValueError("clip identifiers must be unique")
        return self


class MeasuredVideoStream(_Contract):
    codec_name: str
    profile: str
    pixel_format: str
    width: int
    height: int
    sample_aspect_ratio: str
    frame_rate: str
    time_base: str
    color_primaries: str
    color_transfer: str
    color_space: str


class MeasuredAudioStream(_Contract):
    codec_name: str
    profile: str
    sample_rate: int
    channels: int
    channel_layout: str


class ConformedClip(_Contract):
    clip_id: str
    relative_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    source_asset_id: str
    source_sha256: str
    start_seconds: float
    end_seconds: float
    ffmpeg_version: str
    effective_parameters_hash: str
    video: MeasuredVideoStream
    audio: MeasuredAudioStream
    profile_valid: bool


class AssemblySetCompatibility(_Contract):
    compatible: bool
    packet_concat_verified: bool
    clip_ids: tuple[str, ...]
    error_code: str | None = None


class EditingMediaConformanceResult(_Contract):
    schema_version: Literal["editing-media-conformance-result/v1"] = (
        "editing-media-conformance-result/v1"
    )
    request_id: str
    idempotency_key: str
    status: Literal["completed", "failed", "cancelled"]
    profile: EditingDeliveryProfile
    clips: tuple[ConformedClip, ...] = ()
    assembly_set: AssemblySetCompatibility | None = None
    error: dict[str, object] | None = None


def contract_schema_bundle() -> dict[str, dict[str, object]]:
    return {
        "request": EditingMediaConformanceRequest.model_json_schema(mode="validation"),
        "result": EditingMediaConformanceResult.model_json_schema(mode="validation"),
        "profile": EditingDeliveryProfile.model_json_schema(mode="validation"),
    }
