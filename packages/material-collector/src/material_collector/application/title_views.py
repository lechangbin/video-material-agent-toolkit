"""Shared application operation for publishing and persisting title views."""

from pathlib import Path

from material_collector.application.ports import AssetStore
from material_collector.application.source_manifest import SourceManifestApplication
from material_collector.core.manifest import CollectionResult
from material_collector.core.media import AssetRecord, TitleViewPublication


def publish_and_record_title_view(
    *,
    manifest: SourceManifestApplication,
    asset_store: AssetStore,
    workspace: Path,
    asset: AssetRecord,
    publication: TitleViewPublication,
) -> tuple[AssetRecord, CollectionResult | None]:
    """Publish one readable view and persist only a changed asset projection."""

    published = asset_store.publish_title_view(asset, publication)
    if published == asset:
        return published, None
    result = manifest.record_asset(
        workspace,
        publication.session_id,
        published,
    )
    return published, result
