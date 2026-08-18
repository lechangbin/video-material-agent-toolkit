"""Stable cross-module media discovery and download records."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Platform(StrEnum):
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"


class BrowserChannel(StrEnum):
    AUTO = "auto"
    EDGE = "edge"
    CHROME = "chrome"


PLATFORM_ORDER: tuple[Platform, ...] = (
    Platform.BILIBILI,
    Platform.DOUYIN,
    Platform.XIAOHONGSHU,
)


class AuthStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    CHALLENGE_REQUIRED = "challenge_required"
    PROBE_FAILED = "probe_failed"


class AuthProbe(_Record):
    schema_version: Literal["1.0"] = "1.0"
    platform: Platform
    auth_profile: str
    browser_channel: BrowserChannel = BrowserChannel.CHROME
    status: AuthStatus
    checked_at: str
    reason_code: str | None = None


class PlatformContext(_Record):
    auth_profile: str
    browser_channel: BrowserChannel = BrowserChannel.CHROME
    request_timeout_seconds: int = Field(default=30, ge=1)
    show_search_browser: bool = False

    @field_validator("browser_channel")
    @classmethod
    def require_frozen_channel(cls, value: BrowserChannel) -> BrowserChannel:
        if value is BrowserChannel.AUTO:
            raise ValueError("PlatformContext requires a frozen browser channel.")
        return value


class AuthenticationSelection(_Record):
    browser_channel: BrowserChannel
    probes: tuple[AuthProbe, ...]


class SearchRequest(_Record):
    schema_version: Literal["1.0"] = "1.0"
    query_plan_id: str
    segment_id: str
    query_id: str
    round_number: int = Field(ge=1)
    text: str
    limit: int = Field(default=20, ge=1)


class CandidateSource(_Record):
    schema_version: Literal["1.0"] = "1.0"
    platform: Platform
    source_id: str
    canonical_url: str
    title: str
    author: str | None = None
    description: str | None = None
    published_at: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    rank: int = Field(ge=1)
    query_plan_id: str
    segment_id: str
    query_id: str
    round_number: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def candidate_id(self) -> str:
        return f"{self.platform.value}:{self.source_id}"


class SearchBatch(_Record):
    schema_version: Literal["1.0"] = "1.0"
    platform: Platform
    request: SearchRequest
    candidates: tuple[CandidateSource, ...]
    exhausted: bool = False
    warnings: tuple[str, ...] = ()


class MediaUnit(_Record):
    schema_version: Literal["1.0"] = "1.0"
    platform: Platform
    source_id: str
    media_unit_id: str
    canonical_url: str
    title: str
    duration_seconds: float | None = Field(default=None, ge=0)
    part_index: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def stable_id(self) -> str:
        return f"{self.platform.value}:{self.media_unit_id}"


class ResolvedSource(_Record):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str
    media_units: tuple[MediaUnit, ...]


class MediaQuality(StrEnum):
    LOW_PROXY = "low_proxy"
    HIGH = "high"


class FetchRequest(_Record):
    schema_version: Literal["1.0"] = "1.0"
    media_unit: MediaUnit
    quality: MediaQuality
    destination: Path


class FetchResult(_Record):
    schema_version: Literal["1.0"] = "1.0"
    media_unit_id: str
    quality: MediaQuality
    path: Path
    size_bytes: int = Field(ge=0)
    sha256: str
    container: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class AssetRecord(_Record):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: str
    sha256: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    quality: MediaQuality
    media_unit_id: str
    container: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
