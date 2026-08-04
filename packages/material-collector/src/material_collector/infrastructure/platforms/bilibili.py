"""Bilibili search, BV/P/CID resolution, and progressive media download."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

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
    clock_duration,
    fetch_to_destination,
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

_BVID = re.compile(r"(?i)(BV[0-9A-Za-z]{10})")
_VIEW_ENDPOINT = "https://api.bilibili.com/x/web-interface/view"
_SEARCH_ENDPOINT = "https://api.bilibili.com/x/web-interface/search/type"
_PLAY_ENDPOINT = "https://api.bilibili.com/x/player/playurl"


def parse_bilibili_bvid(source_id: str, canonical_url: str = "") -> str:
    """Extract a stable BV identifier from a source identifier or URL."""

    match = _BVID.search(source_id) or _BVID.search(canonical_url)
    if match is None:
        raise PlatformAdapterError(
            "source_id_invalid",
            "A Bilibili source must contain a stable BV identifier.",
            platform=Platform.BILIBILI,
            operation="resolve",
            retryable=False,
        )
    return f"BV{match.group(1)[2:]}"


def parse_bilibili_search(
    payload: Mapping[str, Any],
    request: SearchRequest,
) -> SearchBatch:
    code = payload.get("code")
    if code not in {0, "0"}:
        raise response_rejected(Platform.BILIBILI, "search", str(code))
    data = object_mapping(payload.get("data"))
    if data is None:
        raise schema_changed(Platform.BILIBILI, "search", "data")
    results = object_sequence(data.get("result"))
    if results is None:
        raise schema_changed(Platform.BILIBILI, "search", "data.result")

    candidates: list[CandidateSource] = []
    for item in results:
        record = object_mapping(item)
        if record is None:
            raise schema_changed(Platform.BILIBILI, "search", "data.result[]")
        bvid_value = record.get("bvid")
        if not isinstance(bvid_value, str) or _BVID.fullmatch(bvid_value) is None:
            raise schema_changed(Platform.BILIBILI, "search", "data.result[].bvid")
        title = clean_text(record.get("title"))
        if not title:
            raise schema_changed(Platform.BILIBILI, "search", "data.result[].title")
        candidates.append(
            CandidateSource(
                platform=Platform.BILIBILI,
                source_id=bvid_value,
                canonical_url=f"https://www.bilibili.com/video/{bvid_value}",
                title=title,
                author=optional_text(record.get("author")),
                description=optional_text(record.get("description")),
                published_at=timestamp_text(record.get("pubdate")),
                duration_seconds=clock_duration(record.get("duration")),
                rank=len(candidates) + 1,
                query_plan_id=request.query_plan_id,
                segment_id=request.segment_id,
                query_id=request.query_id,
                round_number=request.round_number,
            )
        )
        if len(candidates) >= request.limit:
            break

    total = data.get("numResults")
    exhausted = isinstance(total, int) and total <= len(candidates)
    return SearchBatch(
        platform=Platform.BILIBILI,
        request=request,
        candidates=tuple(candidates),
        exhausted=exhausted,
    )


def parse_bilibili_source(payload: Mapping[str, Any], bvid: str) -> ResolvedSource:
    code = payload.get("code")
    if code not in {0, "0"}:
        raise response_rejected(Platform.BILIBILI, "resolve", str(code))
    data = object_mapping(payload.get("data"))
    if data is None:
        raise schema_changed(Platform.BILIBILI, "resolve", "data")
    pages = object_sequence(data.get("pages"))
    if pages is None or not pages:
        raise schema_changed(Platform.BILIBILI, "resolve", "data.pages")
    source_title = clean_text(data.get("title"))
    if not source_title:
        raise schema_changed(Platform.BILIBILI, "resolve", "data.title")

    units: list[MediaUnit] = []
    for fallback_index, item in enumerate(pages, start=1):
        page = object_mapping(item)
        if page is None:
            raise schema_changed(Platform.BILIBILI, "resolve", "data.pages[]")
        cid = page.get("cid")
        page_number = page.get("page", fallback_index)
        if not isinstance(cid, int | str) or not str(cid).isdigit():
            raise schema_changed(Platform.BILIBILI, "resolve", "data.pages[].cid")
        if not isinstance(page_number, int) or page_number < 1:
            raise schema_changed(Platform.BILIBILI, "resolve", "data.pages[].page")
        part = clean_text(page.get("part")) or source_title
        duration = clock_duration(page.get("duration"))
        units.append(
            MediaUnit(
                platform=Platform.BILIBILI,
                source_id=bvid,
                media_unit_id=f"{bvid}:p{page_number}:cid{cid}",
                canonical_url=f"https://www.bilibili.com/video/{bvid}?p={page_number}",
                title=part,
                duration_seconds=duration,
                part_index=page_number,
                metadata={"bvid": bvid, "cid": str(cid)},
            )
        )
    return ResolvedSource(
        candidate_id=f"{Platform.BILIBILI.value}:{bvid}",
        media_units=tuple(units),
    )


def parse_bilibili_media_url(payload: Mapping[str, Any]) -> str:
    code = payload.get("code")
    if code not in {0, "0"}:
        raise response_rejected(Platform.BILIBILI, "fetch", str(code))
    data = object_mapping(payload.get("data"))
    if data is None:
        raise schema_changed(Platform.BILIBILI, "fetch", "data")
    streams = object_sequence(data.get("durl"))
    if streams is None or not streams:
        raise PlatformAdapterError(
            "media_stream_unavailable",
            "Bilibili did not return a progressive media stream.",
            platform=Platform.BILIBILI,
            operation="fetch",
            retryable=True,
        )
    first = object_mapping(streams[0])
    media_url = first.get("url") if first is not None else None
    if not isinstance(media_url, str) or not media_url.startswith(("https://", "http://")):
        raise schema_changed(Platform.BILIBILI, "fetch", "data.durl[0].url")
    return media_url


class BilibiliAdapter:
    """Implement all three frozen platform seams for Bilibili."""

    platform = Platform.BILIBILI

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
        query = urlencode(
            {
                "search_type": "video",
                "keyword": request.text,
                "page": request.round_number,
                "page_size": request.limit,
            }
        )
        payload = await self._transport.capture_json(
            f"{_SEARCH_ENDPOINT}?{query}",
            "/x/web-interface/search/type",
            platform=self.platform,
            operation="search",
            context=context,
        )
        return parse_bilibili_search(payload, request)

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource:
        bvid = parse_bilibili_bvid(source_id, canonical_url)
        payload = await self._transport.capture_json(
            f"{_VIEW_ENDPOINT}?{urlencode({'bvid': bvid})}",
            "/x/web-interface/view",
            platform=self.platform,
            operation="resolve",
            context=context,
        )
        return parse_bilibili_source(payload, bvid)

    async def fetch(
        self,
        request: FetchRequest,
        context: PlatformContext,
    ) -> FetchResult:
        validate_fetch_destination(self.platform, request.destination)
        metadata = request.media_unit.metadata
        bvid = metadata.get("bvid")
        cid = metadata.get("cid")
        if not isinstance(bvid, str) or not isinstance(cid, str):
            raise PlatformAdapterError(
                "media_unit_invalid",
                "The Bilibili media unit is missing its BV or CID identity.",
                platform=self.platform,
                operation="fetch",
                retryable=False,
            )
        quality = 64 if request.quality is MediaQuality.LOW_PROXY else 120
        query = urlencode({"bvid": bvid, "cid": cid, "qn": quality, "fnval": 0})
        payload = await self._transport.capture_json(
            f"{_PLAY_ENDPOINT}?{query}",
            "/x/player/playurl",
            platform=self.platform,
            operation="fetch",
            context=context,
        )
        media_url = parse_bilibili_media_url(payload)
        return await fetch_to_destination(
            platform=self.platform,
            request=request,
            context=context,
            transport=self._transport,
            media_inspector=self._media_inspector,
            media_url=media_url,
            referer=request.media_unit.canonical_url,
        )
