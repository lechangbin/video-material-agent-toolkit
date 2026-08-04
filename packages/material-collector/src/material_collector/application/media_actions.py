"""Review, result export and on-demand high-quality media use cases."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from material_collector.application.ports import AssetStoreFactory, MediaFetcher
from material_collector.application.sessions import CONTROL_DIRECTORY, SESSIONS_DIRECTORY
from material_collector.application.source_manifest import SourceManifestApplication
from material_collector.core.errors import CollectorError, ContractError
from material_collector.core.manifest import (
    CollectionResult,
    ManifestCandidate,
    ManifestMediaUnit,
)
from material_collector.core.media import (
    FetchRequest,
    MediaQuality,
    MediaUnit,
    Platform,
    PlatformContext,
)


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewItem(_OutputModel):
    media_unit_id: str
    platform: Platform
    title: str
    canonical_url: str
    duration_seconds: float | None
    status: str


class ReviewList(_OutputModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    workspace_path: str
    status: Literal["ok"] = "ok"
    items: tuple[ReviewItem, ...]


class HighQualityFetchView(_OutputModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    workspace_path: str
    status: Literal["high_quality_ready"] = "high_quality_ready"
    media_unit_id: str
    asset_path: str
    sha256: str
    size_bytes: int
    result_path: str


class MediaApplication:
    """Expose media actions without coupling business rules to Typer."""

    def __init__(
        self,
        *,
        manifest: SourceManifestApplication,
        asset_stores: AssetStoreFactory,
        media_fetchers: Mapping[Platform, MediaFetcher] | None = None,
    ) -> None:
        self._media_fetchers = dict(media_fetchers or {})
        self._manifest = manifest
        self._asset_stores = asset_stores

    def list_reviews(self, workspace: Path, session_id: str) -> ReviewList:
        result = self._manifest.export(workspace, session_id)
        items = tuple(
            ReviewItem(
                media_unit_id=unit.media_unit_id,
                platform=candidate.platform,
                title=unit.title,
                canonical_url=unit.canonical_url,
                duration_seconds=unit.duration_seconds,
                status=unit.status,
            )
            for candidate in result.candidates
            for unit in candidate.media_units
            if unit.status in {"manual_review_required", "duration_unknown"}
        )
        return ReviewList(
            session_id=session_id,
            workspace_path=result.workspace_path,
            items=items,
        )

    def decide_review(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
        *,
        approved: bool,
        actor: str = "human",
    ) -> CollectionResult:
        return self._manifest.decide_review(
            workspace,
            session_id,
            media_unit_id,
            approved=approved,
            actor=actor,
        )

    def export_result(self, workspace: Path, session_id: str) -> CollectionResult:
        return self._manifest.export(workspace, session_id)

    def platform_for_media(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
    ) -> Platform:
        result = self._manifest.export(workspace, session_id)
        candidate, _unit = _find_media_unit(result, media_unit_id)
        return candidate.platform

    async def fetch_high_quality(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
        *,
        auth_profile: str,
        request_timeout_seconds: int = 30,
    ) -> HighQualityFetchView:
        result = self._manifest.export(workspace, session_id)
        candidate, media_unit = _find_media_unit(result, media_unit_id)
        if media_unit.status == "rejected":
            raise ContractError(
                "Rejected media cannot be fetched.",
                details={"media_unit_id": media_unit_id},
            )
        if media_unit.high_quality_asset is not None:
            asset = media_unit.high_quality_asset
        else:
            fetcher = self._media_fetchers.get(candidate.platform)
            if fetcher is None:
                raise ContractError(
                    "No media fetcher is configured for this platform.",
                    details={"platform": candidate.platform.value},
                )
            normalized_workspace = Path(result.workspace_path)
            asset_store = self._asset_stores.for_workspace(
                normalized_workspace
            )
            temporary = asset_store.allocate_staging_path(
                session_id,
                media_unit.media_unit_id,
                MediaQuality.HIGH.value,
            )
            try:
                fetched = await fetcher.fetch(
                    FetchRequest(
                        media_unit=_to_media_unit(candidate, media_unit_id),
                        quality=MediaQuality.HIGH,
                        destination=temporary,
                    ),
                    PlatformContext(
                        auth_profile=auth_profile,
                        request_timeout_seconds=request_timeout_seconds,
                    ),
                )
                asset = asset_store.import_fetch(fetched)
                result = self._manifest.record_asset(
                    normalized_workspace,
                    session_id,
                    asset,
                )
            finally:
                try:
                    asset_store.discard_staging(temporary)
                except CollectorError:
                    pass
        return HighQualityFetchView(
            session_id=session_id,
            workspace_path=result.workspace_path,
            media_unit_id=media_unit_id,
            asset_path=str(Path(result.workspace_path) / asset.relative_path),
            sha256=asset.sha256,
            size_bytes=asset.size_bytes,
            result_path=str(
                Path(result.workspace_path)
                / CONTROL_DIRECTORY
                / SESSIONS_DIRECTORY
                / session_id
                / "collection-result.json"
            ),
        )


def _find_media_unit(
    result: CollectionResult,
    media_unit_id: str,
) -> tuple[ManifestCandidate, ManifestMediaUnit]:
    for candidate in result.candidates:
        for unit in candidate.media_units:
            if unit.media_unit_id == media_unit_id:
                return candidate, unit
    raise ContractError(
        "The requested media unit does not exist.",
        details={"media_unit_id": media_unit_id},
    )


def _to_media_unit(
    candidate: ManifestCandidate,
    media_unit_id: str,
) -> MediaUnit:
    for unit in candidate.media_units:
        if unit.media_unit_id != media_unit_id:
            continue
        local_id = unit.media_unit_id.removeprefix(f"{candidate.platform.value}:")
        return MediaUnit(
            platform=candidate.platform,
            source_id=candidate.source_id,
            media_unit_id=local_id,
            canonical_url=unit.canonical_url,
            title=unit.title,
            duration_seconds=unit.duration_seconds,
            part_index=unit.part_index,
            metadata=unit.metadata,
        )
    raise ContractError(
        "The requested media unit does not belong to its source.",
        details={"media_unit_id": media_unit_id},
    )
