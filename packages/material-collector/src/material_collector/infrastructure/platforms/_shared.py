"""Pure parsing and destination helpers shared by platform adapters."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from material_collector.core.media import (
    FetchRequest,
    FetchResult,
    MediaQuality,
    Platform,
    PlatformContext,
)
from material_collector.infrastructure.media_inspection import (
    MediaInspectionError,
    MediaProbe,
)
from material_collector.infrastructure.platforms.errors import PlatformAdapterError

if TYPE_CHECKING:
    from material_collector.infrastructure.platforms.transport import PlatformTransport

_HTML_TAG = re.compile(r"<[^>]+>")
_MEDIA_HOST_SUFFIXES: Mapping[Platform, tuple[str, ...]] = {
    Platform.BILIBILI: ("bilivideo.com", "hdslb.com"),
    Platform.DOUYIN: (
        "bytecdn.cn",
        "douyin.com",
        "douyinstatic.com",
        "douyinvod.com",
        "zjcdn.com",
    ),
    Platform.XIAOHONGSHU: ("xhscdn.com", "xiaohongshu.com"),
}


class MediaInspector(Protocol):
    def __call__(self, path: Path) -> MediaProbe: ...


def clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(_HTML_TAG.sub("", value)).split())


def optional_text(value: object) -> str | None:
    cleaned = clean_text(value)
    return cleaned or None


def timestamp_text(value: object) -> str | None:
    if not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")


def clock_duration(value: object) -> float | None:
    if isinstance(value, int | float):
        return max(float(value), 0.0)
    if not isinstance(value, str):
        return None
    components = value.strip().split(":")
    if not components or any(not component.isdigit() for component in components):
        return None
    if len(components) > 3:
        return None
    total = 0
    for component in components:
        total = total * 60 + int(component)
    return float(total)


def milliseconds_duration(value: object) -> float | None:
    if not isinstance(value, int | float) or value < 0:
        return None
    return float(value) / 1000


def object_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def object_sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return None


async def fetch_to_destination(
    *,
    platform: Platform,
    request: FetchRequest,
    context: PlatformContext,
    transport: PlatformTransport,
    media_inspector: MediaInspector,
    media_url: str,
    referer: str,
) -> FetchResult:
    """Write exactly one caller-owned destination and return its content identity."""

    destination = request.destination
    validate_fetch_destination(platform, destination)
    validate_temporary_media_url(platform, media_url)

    try:
        await transport.download(
            media_url,
            destination,
            context=context,
            referer=referer,
        )
        if not destination.is_file():
            raise PlatformAdapterError(
                "download_incomplete",
                "The media transport returned without creating the destination.",
                platform=platform,
                operation="fetch",
                retryable=True,
            )
        digest = hashlib.sha256()
        size = 0
        with destination.open("rb") as media_file:
            while chunk := media_file.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        try:
            probe = await asyncio.to_thread(media_inspector, destination)
        except MediaInspectionError as exc:
            raise PlatformAdapterError(
                "media_inspection_failed",
                "ffprobe could not validate the downloaded media.",
                platform=platform,
                operation="fetch",
                retryable=True,
            ) from exc
        if (
            probe.video_stream_count < 1
            or probe.width is None
            or probe.height is None
        ):
            raise PlatformAdapterError(
                "media_inspection_failed",
                "The downloaded file has no measurable video stream.",
                platform=platform,
                operation="fetch",
                retryable=True,
            )
        if (
            request.quality is MediaQuality.LOW_PROXY
            and min(probe.width, probe.height) > 720
        ):
            raise PlatformAdapterError(
                "media_quality_exceeded",
                "The downloaded low proxy exceeds the 720p boundary.",
                platform=platform,
                operation="fetch",
                retryable=True,
                details={"width": probe.width, "height": probe.height},
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

    return FetchResult(
        media_unit_id=request.media_unit.stable_id,
        quality=request.quality,
        path=destination,
        size_bytes=size,
        sha256=digest.hexdigest(),
        container=probe.container,
        duration_seconds=probe.duration_seconds,
        width=probe.width,
        height=probe.height,
    )


def validate_fetch_destination(platform: Platform, destination: Path) -> None:
    """Validate the destination before resolving any temporary media URL."""

    if not destination.is_absolute():
        raise PlatformAdapterError(
            "destination_invalid",
            "The download destination must be an absolute path.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    if os.path.lexists(destination):
        raise PlatformAdapterError(
            "destination_exists",
            "The download destination already exists.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    if not destination.parent.is_dir():
        raise PlatformAdapterError(
            "destination_invalid",
            "The caller-owned destination directory does not exist.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )


def validate_temporary_media_url(platform: Platform, media_url: str) -> None:
    """Reject unsafe media origins before the authenticated transport sees them."""

    try:
        parsed = httpx.URL(media_url)
        host = parsed.host
    except Exception as exc:
        raise PlatformAdapterError(
            "media_url_invalid",
            "The platform returned an invalid media URL.",
            platform=platform,
            operation="fetch",
            retryable=False,
        ) from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.userinfo
        or parsed.port not in {None, 443}
    ):
        raise PlatformAdapterError(
            "media_url_invalid",
            "The platform media URL must be an unauthenticated HTTPS URL.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    if host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        raise PlatformAdapterError(
            "media_url_invalid",
            "The platform media URL cannot target the local machine.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    normalized_host = host.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if not any(
            normalized_host == suffix or normalized_host.endswith(f".{suffix}")
            for suffix in _MEDIA_HOST_SUFFIXES[platform]
        ):
            raise PlatformAdapterError(
                "media_url_invalid",
                "The platform media URL is not on an approved platform CDN.",
                platform=platform,
                operation="fetch",
                retryable=False,
            )
        return
    del address
    raise PlatformAdapterError(
        "media_url_invalid",
        "Literal IP media URLs are outside the approved platform CDN origins.",
        platform=platform,
        operation="fetch",
        retryable=False,
    )


def is_strict_short_url(url: str, allowed_hosts: Sequence[str]) -> bool:
    """Return true only for an unauthenticated HTTPS URL on an exact short host."""

    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return False
    host = parsed.host.casefold().rstrip(".") if parsed.host else ""
    return (
        parsed.scheme == "https"
        and not parsed.userinfo
        and parsed.port in {None, 443}
        and host in allowed_hosts
    )
