"""Xiaohongshu authenticated search, note resolution, and media download."""

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

_NOTE_ID = re.compile(r"(?i)(?<![0-9a-f])([0-9a-f]{24})(?![0-9a-f])")
_NOTE_URL = re.compile(
    r"(?i)https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-f]{24})"
)
_SHORT_HOSTS = ("xhslink.com",)
_SEARCH_FRAGMENT = "/api/sns/web/v2/search/notes"
_DETAIL_FRAGMENT = "/api/sns/web/v1/feed"


def parse_xiaohongshu_note_id(value: str) -> str:
    """Extract a stable note ID without retaining share tokens."""

    url_match = _NOTE_URL.search(value)
    if url_match is not None:
        return url_match.group(1).casefold()
    if _NOTE_ID.fullmatch(value) is not None:
        return value.casefold()
    raise PlatformAdapterError(
        "source_id_invalid",
        "A Xiaohongshu source must be a stable note ID or note URL.",
        platform=Platform.XIAOHONGSHU,
        operation="resolve",
        retryable=False,
    )


def _check_response(payload: Mapping[str, Any], operation: str) -> None:
    code = payload.get("code", 0)
    success = payload.get("success")
    if code not in {0, "0", None} or success is False:
        raise response_rejected(Platform.XIAOHONGSHU, operation, str(code))


def _search_items(payload: Mapping[str, Any]) -> Sequence[object]:
    _check_response(payload, "search")
    data = object_mapping(payload.get("data"))
    if data is None:
        raise schema_changed(Platform.XIAOHONGSHU, "search", "data")
    items = object_sequence(data.get("items"))
    if items is None:
        raise schema_changed(Platform.XIAOHONGSHU, "search", "data.items")
    return items


def parse_xiaohongshu_search(
    payload: Mapping[str, Any],
    request: SearchRequest,
) -> SearchBatch:
    candidates: list[CandidateSource] = []
    for item in _search_items(payload):
        record = object_mapping(item)
        if record is None:
            raise schema_changed(Platform.XIAOHONGSHU, "search", "data.items[]")
        note = object_mapping(record.get("note_card")) or record
        note_id = record.get("id", record.get("note_id", note.get("note_id")))
        if not isinstance(note_id, str) or _NOTE_ID.fullmatch(note_id) is None:
            raise schema_changed(Platform.XIAOHONGSHU, "search", "data.items[].id")
        title = clean_text(note.get("display_title", note.get("title")))
        if not title:
            title = f"小红书笔记 {note_id}"
        user = object_mapping(note.get("user"))
        video = object_mapping(note.get("video"))
        candidates.append(
            CandidateSource(
                platform=Platform.XIAOHONGSHU,
                source_id=note_id.casefold(),
                canonical_url=f"https://www.xiaohongshu.com/explore/{note_id.casefold()}",
                title=title,
                author=optional_text(user.get("nickname")) if user else None,
                description=optional_text(note.get("desc")),
                published_at=timestamp_text(note.get("time")),
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
    data = object_mapping(payload.get("data"))
    has_more = data.get("has_more") if data else None
    return SearchBatch(
        platform=Platform.XIAOHONGSHU,
        request=request,
        candidates=tuple(candidates),
        exhausted=has_more in {False},
    )


def _detail_note(payload: Mapping[str, Any], note_id: str) -> Mapping[str, Any]:
    _check_response(payload, "resolve")
    data = object_mapping(payload.get("data"))
    if data is None:
        raise schema_changed(Platform.XIAOHONGSHU, "resolve", "data")
    note = object_mapping(data.get("note"))
    if note is None:
        items = object_sequence(data.get("items"))
        first = object_mapping(items[0]) if items else None
        note = object_mapping(first.get("note_card")) if first else None
    if note is None:
        raise schema_changed(Platform.XIAOHONGSHU, "resolve", "data.items[0].note_card")
    returned_id = note.get("note_id", note.get("id"))
    if returned_id != note_id:
        raise schema_changed(Platform.XIAOHONGSHU, "resolve", "note.note_id")
    return note


def _video_streams(note: Mapping[str, Any]) -> Sequence[object]:
    video = object_mapping(note.get("video"))
    media = object_mapping(video.get("media")) if video else None
    stream = object_mapping(media.get("stream")) if media else None
    streams = object_sequence(stream.get("h264")) if stream else None
    if not streams:
        raise PlatformAdapterError(
            "media_stream_unavailable",
            "Xiaohongshu did not return an H.264 media stream.",
            platform=Platform.XIAOHONGSHU,
            operation="fetch",
            retryable=True,
        )
    return streams


def _stream_duration(note: Mapping[str, Any]) -> float | None:
    try:
        first = object_mapping(_video_streams(note)[0])
    except PlatformAdapterError:
        return None
    return milliseconds_duration(first.get("duration")) if first else None


def parse_xiaohongshu_media_url(
    note: Mapping[str, Any],
    quality: MediaQuality,
) -> str:
    usable: list[tuple[int, int, str]] = []
    for item in _video_streams(note):
        stream = object_mapping(item)
        if stream is None:
            continue
        url = stream.get("master_url")
        if not isinstance(url, str):
            backups = object_sequence(stream.get("backup_urls"))
            url = backups[0] if backups else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            continue
        width = stream.get("width")
        height = stream.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        if width <= 0 or height <= 0:
            continue
        usable.append((min(width, height), width * height, url))
    if not usable:
        raise schema_changed(Platform.XIAOHONGSHU, "fetch", "video.media.stream.h264")
    usable.sort(key=lambda item: (item[0], item[1]))
    if quality is MediaQuality.HIGH:
        return usable[-1][2]
    eligible = [item for item in usable if item[0] <= 720]
    if eligible:
        return eligible[-1][2]
    raise PlatformAdapterError(
        "media_stream_unavailable",
        "Xiaohongshu did not return a stream at or below 720p.",
        platform=Platform.XIAOHONGSHU,
        operation="fetch",
        retryable=True,
    )


def parse_xiaohongshu_detail(
    payload: Mapping[str, Any],
    note_id: str,
) -> tuple[MediaUnit, Mapping[str, Any]]:
    note = _detail_note(payload, note_id)
    title = clean_text(note.get("title", note.get("display_title")))
    if not title:
        title = f"小红书笔记 {note_id}"
    unit = MediaUnit(
        platform=Platform.XIAOHONGSHU,
        source_id=note_id,
        media_unit_id=note_id,
        canonical_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        title=title,
        duration_seconds=_stream_duration(note),
        metadata={"note_id": note_id},
    )
    return unit, note


class XiaohongshuAdapter:
    """Implement all three frozen platform seams for Xiaohongshu."""

    platform = Platform.XIAOHONGSHU

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
        url = (
            "https://www.xiaohongshu.com/search_result"
            f"?keyword={quote(request.text)}&source=web_search_result_notes"
        )
        payload = await self._transport.capture_json(
            url,
            _SEARCH_FRAGMENT,
            platform=self.platform,
            operation="search",
            context=context,
        )
        return parse_xiaohongshu_search(payload, request)

    async def _stable_id(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> str:
        for value in (source_id, canonical_url):
            try:
                return parse_xiaohongshu_note_id(value)
            except PlatformAdapterError:
                pass
        if is_strict_short_url(canonical_url, _SHORT_HOSTS):
            resolved = await self._transport.resolve_url(
                canonical_url,
                platform=self.platform,
                context=context,
            )
            return parse_xiaohongshu_note_id(resolved)
        raise PlatformAdapterError(
            "source_id_invalid",
            "The Xiaohongshu source does not resolve to a stable note identity.",
            platform=self.platform,
            operation="resolve",
            retryable=False,
        )

    async def _detail(
        self,
        note_id: str,
        context: PlatformContext,
    ) -> tuple[MediaUnit, Mapping[str, Any]]:
        canonical = f"https://www.xiaohongshu.com/explore/{note_id}"
        payload = await self._transport.capture_json(
            canonical,
            _DETAIL_FRAGMENT,
            platform=self.platform,
            operation="resolve",
            context=context,
        )
        return parse_xiaohongshu_detail(payload, note_id)

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource:
        note_id = await self._stable_id(source_id, canonical_url, context)
        unit, _ = await self._detail(note_id, context)
        return ResolvedSource(
            candidate_id=f"{self.platform.value}:{note_id}",
            media_units=(unit,),
        )

    async def fetch(
        self,
        request: FetchRequest,
        context: PlatformContext,
    ) -> FetchResult:
        validate_fetch_destination(self.platform, request.destination)
        note_id_value = request.media_unit.metadata.get("note_id")
        if not isinstance(note_id_value, str):
            raise PlatformAdapterError(
                "media_unit_invalid",
                "The Xiaohongshu media unit is missing its stable note identity.",
                platform=self.platform,
                operation="fetch",
                retryable=False,
            )
        _, note = await self._detail(note_id_value, context)
        media_url = parse_xiaohongshu_media_url(note, request.quality)
        return await fetch_to_destination(
            platform=self.platform,
            request=request,
            context=context,
            transport=self._transport,
            media_inspector=self._media_inspector,
            media_url=media_url,
            referer=request.media_unit.canonical_url,
        )
