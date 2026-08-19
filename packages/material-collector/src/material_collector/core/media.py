"""Stable cross-module media discovery and download records."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class Platform(StrEnum):
    BILIBILI = "bilibili"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"


class NetworkRoute(StrEnum):
    """Non-overridable network capability owned by a platform adapter."""

    DOMESTIC_DIRECT = "domestic_direct"
    FOREIGN_PROXY = "foreign_proxy"


class BrowserChannel(StrEnum):
    AUTO = "auto"
    EDGE = "edge"
    CHROME = "chrome"


PLATFORM_ORDER: tuple[Platform, ...] = (
    Platform.BILIBILI,
    Platform.DOUYIN,
    Platform.XIAOHONGSHU,
    Platform.YOUTUBE,
    Platform.TIKTOK,
)


PLATFORM_NETWORK_ROUTES: dict[Platform, NetworkRoute] = {
    Platform.BILIBILI: NetworkRoute.DOMESTIC_DIRECT,
    Platform.DOUYIN: NetworkRoute.DOMESTIC_DIRECT,
    Platform.XIAOHONGSHU: NetworkRoute.DOMESTIC_DIRECT,
    Platform.YOUTUBE: NetworkRoute.FOREIGN_PROXY,
    Platform.TIKTOK: NetworkRoute.FOREIGN_PROXY,
}


class GeometryDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class GeometryStage(StrEnum):
    PLATFORM_METADATA = "platform_metadata"
    REMOTE_PROBE = "remote_probe"
    LOCAL_PROXY = "local_proxy"
    LOCAL_HIGH_QUALITY = "local_high_quality"


class DisplayGeometryAssessment(_Record):
    """Versioned display-geometry evidence for the 16:9 eligibility gate."""

    schema_version: Literal["media-geometry-assessment/v1"] = (
        "media-geometry-assessment/v1"
    )
    stage: GeometryStage
    disposition: GeometryDisposition
    encoded_width: int | None = Field(default=None, ge=1)
    encoded_height: int | None = Field(default=None, ge=1)
    rotation_degrees: int | None = None
    sample_aspect_ratio: str | None = None
    display_aspect_ratio: str | None = None
    normalized_display_ratio: float | None = Field(default=None, gt=0)
    deviation_from_16_9: float | None = Field(default=None, ge=0)
    reason_code: str | None = None


class NetworkRouteEvidence(_Record):
    """Non-sensitive proof of the effective route selected by capability."""

    schema_version: Literal["network-route-evidence/v1"] = "network-route-evidence/v1"
    route: NetworkRoute
    discovery_source: str
    proxy_kind: Literal["http_connect", "socks5"] | None = None
    validated_at: str | None = None


class AuthorizationDisposition(StrEnum):
    AUTHORIZED = "authorized"
    HUMAN_ACTION_REQUIRED = "human_action_required"
    NOT_AUTHORIZED = "not_authorized"


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
    network_route: NetworkRoute = NetworkRoute.DOMESTIC_DIRECT
    route_evidence: NetworkRouteEvidence | None = None
    yt_dlp_version: str | None = None
    authorization_disposition: AuthorizationDisposition = (
        AuthorizationDisposition.AUTHORIZED
    )
    geometry_assessment: DisplayGeometryAssessment | None = None

    @model_validator(mode="before")
    @classmethod
    def freeze_platform_route(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        platform = Platform(str(value.get("platform")))
        expected = PLATFORM_NETWORK_ROUTES[platform]
        supplied = value.get("network_route")
        if supplied is not None and NetworkRoute(supplied) is not expected:
            raise ValueError("network_route cannot override the platform capability")
        return {**value, "network_route": expected}

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
    network_route: NetworkRoute = NetworkRoute.DOMESTIC_DIRECT
    route_evidence: NetworkRouteEvidence | None = None
    yt_dlp_version: str | None = None
    authorization_disposition: AuthorizationDisposition = (
        AuthorizationDisposition.AUTHORIZED
    )
    geometry_assessment: DisplayGeometryAssessment | None = None

    @model_validator(mode="before")
    @classmethod
    def freeze_platform_route(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        platform = Platform(str(value.get("platform")))
        expected = PLATFORM_NETWORK_ROUTES[platform]
        supplied = value.get("network_route")
        if supplied is not None and NetworkRoute(supplied) is not expected:
            raise ValueError("network_route cannot override the platform capability")
        return {**value, "network_route": expected}

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
    geometry_assessment: DisplayGeometryAssessment | None = None


class AssetRecord(_Record):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: str
    sha256: str
    relative_path: str
    display_relative_path: str | None = None
    size_bytes: int = Field(ge=0)
    quality: MediaQuality
    media_unit_id: str
    container: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    geometry_assessment: DisplayGeometryAssessment | None = None


class TitleViewPublication(_Record):
    """Identity and readable metadata needed to publish one title view."""

    session_id: str
    platform: Platform
    source_id: str
    source_title: str
    media_unit_title: str
