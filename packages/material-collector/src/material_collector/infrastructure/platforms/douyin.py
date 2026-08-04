"""Douyin authenticated search, stable work resolution, and media download."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from material_collector.core.media import (
    CandidateSource,
    FetchRequest,
    FetchResult,
    MediaQuality,
    MediaUnit,
    Platform,
    PlatformContext,
    ResolvedSource,
    SearchBatch,
    SearchRequest,
)
from material_collector.infrastructure.media_inspection import inspect_media
from material_collector.infrastructure.platforms._shared import (
    MediaInspector,
    clean_text,
    fetch_to_destination,
    is_strict_short_url,
    milliseconds_duration,
    object_mapping,
    object_sequence,
    optional_text,
    timestamp_text,
    validate_fetch_destination,
)
from material_collector.infrastructure.platforms.errors import (
    PlatformAdapterError,
    response_rejected,
    schema_changed,
)
from material_collector.infrastructure.platforms.transport import (
    PlatformTransport,
    PlaywrightPlatformTransport,
)

_WORK_ID = re.compile(r"(?<!\d)(\d{10,24})(?!\d)")
_VIDEO_URL = re.compile(r"https?://(?:www\.)?douyin\.com/video/(\d{10,24})")
_SHORT_HOSTS = ("v.douyin.com", "iesdouyin.com")
_SEARCH_FRAGMENT = "/aweme/v1/web/general/search/"
_DETAIL_FRAGMENT = "/aweme/v1/web/aweme/detail/"


def parse_douyin_work_id(value: str) -> str:
    """Extract only stable Douyin work identities; never retain share parameters."""

    video_match = _VIDEO_URL.search(value)
    if video_match is not None:
        return video_match.group(1)
    if value.isdigit() and _WORK_ID.fullmatch(value) is not None:
        return value
    raise PlatformAdapterError(
        "source_id_invalid",
        "A Douyin source must be a stable work ID or /video/ URL.",
        platform=Platform.DOUYIN,
        operation="resolve",
        retryable=False,
    )


def _search_records(payload: Mapping[str, Any]) -> Sequence[object]:
    code = payload.get("status_code", payload.get("code", 0))
    if code not in {0, "0", None}:
        raise response_rejected(Platform.DOUYIN, "search", str(code))
    data = payload.get("data")
    if isinstance(data, Mapping):
        data = data.get("data", data.get("items"))
    records = object_sequence(data)
    if records is None:
        raise schema_changed(Platform.DOUYIN, "search", "data")
    return records


def _aweme_from_record(value: object, operation: str) -> Mapping[str, Any]:
    record = object_mapping(value)
    if record is None:
        raise schema_changed(Platform.DOUYIN, operation, "data[]")
    for key in ("aweme_info", "aweme_detail", "aweme_mix_info"):
        nested = object_mapping(record.get(key))
        if nested is not None:
            return nested
    return record


def parse_douyin_search(
    payload: Mapping[str, Any],
    request: SearchRequest,
) -> SearchBatch:
    candidates: list[CandidateSource] = []
    for item in _search_records(payload):
        aweme = _aweme_from_record(item, "search")
        work_id = aweme.get("aweme_id")
        title = clean_text(aweme.get("desc"))
        if not isinstance(work_id, str) or _WORK_ID.fullmatch(work_id) is None:
            raise schema_changed(Platform.DOUYIN, "search", "data[].aweme_info.aweme_id")
        if not title:
            title = f"抖音作品 {work_id}"
        author = object_mapping(aweme.get("author"))
        video = object_mapping(aweme.get("video"))
        candidates.append(
            CandidateSource(
                platform=Platform.DOUYIN,
                source_id=work_id,
                canonical_url=f"https://www.douyin.com/video/{work_id}",
                title=title,
                author=optional_text(author.get("nickname")) if author else None,
                description=optional_text(aweme.get("desc")),
                published_at=timestamp_text(aweme.get("create_time")),
                duration_seconds=(
                    milliseconds_duration(video.get("duration")) if video else None
                ),
                rank=len(candidates) + 1,
                query_plan_id=request.query_plan_id,
                segment_id=request.segment_id,
                query_id=request.query_id,
                round_number=request.round_number,
            )
        )
        if len(candidates) >= request.limit:
            break
    has_more = payload.get("has_more")
    return SearchBatch(
        platform=Platform.DOUYIN,
        request=request,
        candidates=tuple(candidates),
        exhausted=has_more in {0},
    )


def parse_douyin_detail(
    payload: Mapping[str, Any],
    work_id: str,
) -> tuple[MediaUnit, Mapping[str, Any]]:
    code = payload.get("status_code", payload.get("code", 0))
    if code not in {0, "0", None}:
        raise response_rejected(Platform.DOUYIN, "resolve", str(code))
    raw = payload.get("aweme_detail", payload.get("data"))
    aweme = _aweme_from_record(raw, "resolve")
    returned_id = aweme.get("aweme_id")
    if returned_id != work_id:
        raise schema_changed(Platform.DOUYIN, "resolve", "aweme_detail.aweme_id")
    title = clean_text(aweme.get("desc")) or f"抖音作品 {work_id}"
    video = object_mapping(aweme.get("video"))
    duration = milliseconds_duration(video.get("duration")) if video else None
    unit = MediaUnit(
        platform=Platform.DOUYIN,
        source_id=work_id,
        media_unit_id=work_id,
        canonical_url=f"https://www.douyin.com/video/{work_id}",
        title=title,
        duration_seconds=duration,
        metadata={"aweme_id": work_id},
    )
    return unit, aweme


def _address_url(value: object) -> str | None:
    address = object_mapping(value)
    urls = object_sequence(address.get("url_list")) if address else None
    if not urls:
        return None
    return next(
        (
            url
            for url in urls
            if isinstance(url, str) and url.startswith(("https://", "http://"))
        ),
        None,
    )


def _douyin_stream_edge(stream: Mapping[str, Any]) -> int | None:
    for dimensions in (stream, object_mapping(stream.get("play_addr"))):
        if dimensions is None:
            continue
        width = dimensions.get("width")
        height = dimensions.get("height")
        if (
            isinstance(width, int)
            and isinstance(height, int)
            and width > 0
            and height > 0
        ):
            return min(width, height)
    gear_name = stream.get("gear_name")
    if isinstance(gear_name, str):
        encoded_edges = [
            int(value)
            for value in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", gear_name)
        ]
        if encoded_edges:
            return min(encoded_edges)
    return None


def parse_douyin_media_url(
    aweme: Mapping[str, Any],
    quality: MediaQuality,
) -> str:
    video = object_mapping(aweme.get("video"))
    if video is None:
        raise schema_changed(Platform.DOUYIN, "fetch", "aweme_detail.video")
    variants: list[tuple[int, str]] = []
    bit_rates = object_sequence(video.get("bit_rate"))
    for value in bit_rates or ():
        stream = object_mapping(value)
        if stream is None:
            continue
        url = _address_url(stream.get("play_addr"))
        edge = _douyin_stream_edge(stream)
        if url is not None and edge is not None:
            variants.append((edge, url))
    if variants:
        variants.sort(key=lambda item: item[0])
        if quality is MediaQuality.HIGH:
            return variants[-1][1]
        eligible = [item for item in variants if item[0] <= 720]
        if eligible:
            return eligible[-1][1]
        raise PlatformAdapterError(
            "media_stream_unavailable",
            "Douyin did not return a stream at or below 720p.",
            platform=Platform.DOUYIN,
            operation="fetch",
            retryable=True,
        )
    if quality is MediaQuality.HIGH:
        for address_key in ("play_addr", "download_addr"):
            url = _address_url(video.get(address_key))
            if url is not None:
                return url
    raise PlatformAdapterError(
        "media_stream_unavailable",
        (
            "Douyin did not return a stream at or below 720p."
            if quality is MediaQuality.LOW_PROXY
            else "Douyin did not return a downloadable media stream."
        ),
        platform=Platform.DOUYIN,
        operation="fetch",
        retryable=True,
    )


class DouyinAdapter:
    """Implement all three frozen platform seams for Douyin."""

    platform = Platform.DOUYIN

    def __init__(
        self,
        transport: PlatformTransport | None = None,
        *,
        media_inspector: MediaInspector = inspect_media,
    ) -> None:
        self._transport = transport or PlaywrightPlatformTransport()
        self._media_inspector = media_inspector

    async def search(
        self,
        request: SearchRequest,
        context: PlatformContext,
    ) -> SearchBatch:
        url = f"https://www.douyin.com/search/{quote(request.text)}?type=video"
        payload = await self._transport.capture_json(
            url,
            _SEARCH_FRAGMENT,
            platform=self.platform,
            operation="search",
            context=context,
        )
        return parse_douyin_search(payload, request)

    async def _stable_id(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> str:
        for value in (source_id, canonical_url):
            try:
                return parse_douyin_work_id(value)
            except PlatformAdapterError:
                pass
        if is_strict_short_url(canonical_url, _SHORT_HOSTS):
            resolved = await self._transport.resolve_url(
                canonical_url,
                platform=self.platform,
                context=context,
            )
            return parse_douyin_work_id(resolved)
        raise PlatformAdapterError(
            "source_id_invalid",
            "The Douyin source does not resolve to a stable work identity.",
            platform=self.platform,
            operation="resolve",
            retryable=False,
        )

    async def _detail(
        self,
        work_id: str,
        context: PlatformContext,
    ) -> tuple[MediaUnit, Mapping[str, Any]]:
        canonical = f"https://www.douyin.com/video/{work_id}"
        payload = await self._transport.capture_json(
            canonical,
            _DETAIL_FRAGMENT,
            platform=self.platform,
            operation="resolve",
            context=context,
        )
        return parse_douyin_detail(payload, work_id)

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource:
        work_id = await self._stable_id(source_id, canonical_url, context)
        unit, _ = await self._detail(work_id, context)
        return ResolvedSource(
            candidate_id=f"{self.platform.value}:{work_id}",
            media_units=(unit,),
        )

    async def fetch(
        self,
        request: FetchRequest,
        context: PlatformContext,
    ) -> FetchResult:
        validate_fetch_destination(self.platform, request.destination)
        work_id_value = request.media_unit.metadata.get("aweme_id")
        if not isinstance(work_id_value, str):
            raise PlatformAdapterError(
                "media_unit_invalid",
                "The Douyin media unit is missing its stable work identity.",
                platform=self.platform,
                operation="fetch",
                retryable=False,
            )
        _, aweme = await self._detail(work_id_value, context)
        media_url = parse_douyin_media_url(aweme, request.quality)
        return await fetch_to_destination(
            platform=self.platform,
            request=request,
            context=context,
            transport=self._transport,
            media_inspector=self._media_inspector,
            media_url=media_url,
            referer=request.media_unit.canonical_url,
        )
