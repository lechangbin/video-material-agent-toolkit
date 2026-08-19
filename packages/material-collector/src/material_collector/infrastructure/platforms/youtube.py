"""Authorized YouTube discovery, resolution, and download through managed yt-dlp."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from material_collector.core.media import (
    AuthorizationDisposition,
    CandidateSource,
    DisplayGeometryAssessment,
    FetchRequest,
    FetchResult,
    GeometryDisposition,
    GeometryStage,
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
from material_collector.infrastructure.yt_dlp_bridge import YtDlpBridge


def _geometry(info: Mapping[str, Any]) -> DisplayGeometryAssessment:
    width = info.get("width")
    height = info.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width < 1 or height < 1:
        return DisplayGeometryAssessment(
            stage=GeometryStage.PLATFORM_METADATA,
            disposition=GeometryDisposition.UNKNOWN,
            reason_code="display_geometry_missing",
        )
    ratio = width / height
    target = 16 / 9
    deviation = abs(ratio - target) / target
    accepted = deviation <= 0.01
    return DisplayGeometryAssessment(
        stage=GeometryStage.PLATFORM_METADATA,
        disposition=(
            GeometryDisposition.ACCEPTED if accepted else GeometryDisposition.REJECTED
        ),
        encoded_width=width,
        encoded_height=height,
        normalized_display_ratio=ratio,
        deviation_from_16_9=deviation,
        reason_code=None if accepted else "not_16_9",
    )


def _canonical_url(info: Mapping[str, Any]) -> str:
    video_id = str(info.get("id") or "").strip()
    url = str(info.get("webpage_url") or "").strip()
    if url.startswith(("https://www.youtube.com/watch", "https://youtu.be/")):
        return url
    return f"https://www.youtube.com/watch?v={video_id}"


def _candidate(info: Mapping[str, Any], request: SearchRequest, rank: int, version: str) -> CandidateSource:
    video_id = str(info.get("id") or "").strip()
    title = str(info.get("title") or "").strip()
    if not video_id or not title:
        raise ValueError("yt-dlp returned a YouTube result without id or title")
    duration = info.get("duration")
    return CandidateSource(
        platform=Platform.YOUTUBE,
        source_id=video_id,
        canonical_url=_canonical_url(info),
        title=title,
        author=str(info.get("uploader") or "").strip() or None,
        description=str(info.get("description") or "").strip() or None,
        published_at=str(info.get("timestamp") or "").strip() or None,
        duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        rank=rank,
        query_plan_id=request.query_plan_id,
        segment_id=request.segment_id,
        query_id=request.query_id,
        round_number=request.round_number,
        yt_dlp_version=version,
        authorization_disposition=AuthorizationDisposition.AUTHORIZED,
        geometry_assessment=_geometry(info),
        metadata={"extractor": "youtube"},
    )


class YouTubeAdapter:
    platform = Platform.YOUTUBE

    def __init__(
        self,
        bridge: YtDlpBridge,
        *,
        media_inspector: MediaInspector = inspect_media,
    ) -> None:
        self._bridge = bridge
        self._media_inspector = media_inspector

    async def search(self, request: SearchRequest, context: PlatformContext) -> SearchBatch:
        entries = await asyncio.to_thread(
            self._bridge.search_youtube,
            request.text,
            limit=request.limit,
            timeout_seconds=context.request_timeout_seconds,
        )
        candidates = tuple(
            _candidate(entry, request, rank, self._bridge.runtime.version)
            for rank, entry in enumerate(entries, start=1)
        )
        return SearchBatch(
            platform=self.platform,
            request=request,
            candidates=candidates,
            exhausted=len(candidates) < request.limit,
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
            timeout_seconds=context.request_timeout_seconds,
        )
        video_id = str(info.get("id") or source_id)
        title = str(info.get("title") or video_id)
        duration = info.get("duration")
        unit = MediaUnit(
            platform=self.platform,
            source_id=video_id,
            media_unit_id=video_id,
            canonical_url=_canonical_url(info),
            title=title,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            yt_dlp_version=self._bridge.runtime.version,
            geometry_assessment=_geometry(info),
            metadata={"extractor": "youtube"},
        )
        return ResolvedSource(candidate_id=f"youtube:{video_id}", media_units=(unit,))

    async def fetch(self, request: FetchRequest, context: PlatformContext) -> FetchResult:
        validate_fetch_destination(self.platform, request.destination)
        try:
            await asyncio.to_thread(
                self._bridge.download,
                self.platform,
                request.media_unit.canonical_url,
                request.destination,
                request.quality,
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
