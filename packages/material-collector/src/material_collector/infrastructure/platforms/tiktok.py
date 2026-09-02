"""TikTok browser discovery with managed yt-dlp resolution and download."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import quote

from material_collector.core.media import (
    CandidateSource,
    FetchRequest,
    FetchResult,
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
    finalize_downloaded_media,
    validate_fetch_destination,
)
from material_collector.infrastructure.platforms.errors import schema_changed
from material_collector.infrastructure.platforms.transport import PlatformTransport
from material_collector.infrastructure.platforms.youtube import _geometry
from material_collector.infrastructure.yt_dlp_bridge import YtDlpBridge

_SEARCH_FRAGMENT = "/api/search/general/full/"


def _records(payload: Mapping[str, object]) -> Sequence[object]:
    for key in ("item_list", "items", "data"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return value
    raise schema_changed(Platform.TIKTOK, "search", "item_list")


def parse_tiktok_search(
    payload: Mapping[str, object],
    request: SearchRequest,
    *,
    yt_dlp_version: str,
) -> SearchBatch:
    candidates: list[CandidateSource] = []
    for raw in _records(payload):
        if not isinstance(raw, Mapping):
            continue
        item_value = raw.get("item")
        item = cast(
            Mapping[str, object],
            item_value if isinstance(item_value, Mapping) else raw,
        )
        video_id = str(item.get("id") or item.get("aweme_id") or "").strip()
        author_value = item.get("author")
        author = cast(
            Mapping[str, object],
            author_value if isinstance(author_value, Mapping) else {},
        )
        username = str(author.get("unique_id") or author.get("uniqueId") or "").strip()
        title = str(item.get("desc") or item.get("title") or video_id).strip()
        if not video_id or not username:
            continue
        video_value = item.get("video")
        video = cast(
            Mapping[str, object],
            video_value if isinstance(video_value, Mapping) else {},
        )
        info = {
            "width": video.get("width"),
            "height": video.get("height"),
        }
        duration_value = video.get("duration")
        candidates.append(
            CandidateSource(
                platform=Platform.TIKTOK,
                source_id=video_id,
                canonical_url=f"https://www.tiktok.com/@{username}/video/{video_id}",
                title=title,
                author=username,
                duration_seconds=(
                    float(duration_value)
                    if isinstance(duration_value, int | float)
                    else None
                ),
                rank=len(candidates) + 1,
                query_plan_id=request.query_plan_id,
                segment_id=request.segment_id,
                query_id=request.query_id,
                round_number=request.round_number,
                yt_dlp_version=yt_dlp_version,
                geometry_assessment=_geometry(info),
                metadata={"extractor": "tiktok", "username": username},
            )
        )
        if len(candidates) >= request.limit:
            break
    return SearchBatch(
        platform=Platform.TIKTOK,
        request=request,
        candidates=tuple(candidates),
        exhausted=len(candidates) < request.limit,
    )


class TikTokAdapter:
    platform = Platform.TIKTOK

    def __init__(
        self,
        transport: PlatformTransport,
        bridge: YtDlpBridge,
        *,
        media_inspector: MediaInspector = inspect_media,
    ) -> None:
        self._transport = transport
        self._bridge = bridge
        self._media_inspector = media_inspector

    async def search(self, request: SearchRequest, context: PlatformContext) -> SearchBatch:
        payload = await self._transport.capture_json(
            f"https://www.tiktok.com/search/video?q={quote(request.text)}",
            _SEARCH_FRAGMENT,
            platform=self.platform,
            operation="search",
            context=context,
        )
        return parse_tiktok_search(
            payload,
            request,
            yt_dlp_version=self._bridge.runtime.version,
        )

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource:
        info = await asyncio.to_thread(
            self._bridge.resolve,
            self.platform,
            canonical_url,
            browser_channel=context.browser_channel,
            timeout_seconds=context.request_timeout_seconds,
        )
        video_id = str(info.get("id") or source_id)
        title = str(info.get("title") or info.get("description") or video_id)
        duration = info.get("duration")
        unit = MediaUnit(
            platform=self.platform,
            source_id=video_id,
            media_unit_id=video_id,
            canonical_url=canonical_url,
            title=title,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            yt_dlp_version=self._bridge.runtime.version,
            geometry_assessment=_geometry(info),
            metadata={"extractor": "tiktok"},
        )
        return ResolvedSource(candidate_id=f"tiktok:{video_id}", media_units=(unit,))

    async def fetch(self, request: FetchRequest, context: PlatformContext) -> FetchResult:
        validate_fetch_destination(self.platform, request.destination)
        try:
            await asyncio.to_thread(
                self._bridge.download,
                self.platform,
                request.media_unit.canonical_url,
                request.destination,
                request.quality,
                browser_channel=context.browser_channel,
                timeout_seconds=max(context.request_timeout_seconds, 300),
            )
            return await finalize_downloaded_media(
                platform=self.platform,
                request=request,
                media_inspector=self._media_inspector,
            )
        except BaseException:
            request.destination.unlink(missing_ok=True)
            raise
