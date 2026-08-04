from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from material_collector.core.errors import WorkspaceError
from material_collector.core.media import AssetRecord, FetchResult, MediaQuality
from material_collector.infrastructure.assets import WorkspaceAssetStore


def fetched_file(path: Path, content: bytes) -> FetchResult:
    path.write_bytes(content)
    return FetchResult(
        media_unit_id="bilibili:BV1:1",
        quality=MediaQuality.LOW_PROXY,
        path=path,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        container="mp4",
    )


def test_import_is_content_addressed_and_preserves_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first_source = tmp_path / "first.mp4"
    second_source = tmp_path / "second.mp4"
    content = b"same-media-bytes"
    store = WorkspaceAssetStore(workspace)

    first = store.import_fetch(fetched_file(first_source, content))
    second = store.import_fetch(fetched_file(second_source, content))

    assert first.asset_id == second.asset_id
    assert first.relative_path == second.relative_path
    assert first_source.read_bytes() == content
    assert second_source.read_bytes() == content
    assert store.resolve(first).read_bytes() == content


def test_declared_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"bytes")
    fetched = FetchResult(
        media_unit_id="douyin:1",
        quality=MediaQuality.LOW_PROXY,
        path=source,
        size_bytes=5,
        sha256="0" * 64,
    )

    with pytest.raises(WorkspaceError):
        WorkspaceAssetStore(tmp_path / "workspace").import_fetch(fetched)


def test_asset_relative_path_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    store = WorkspaceAssetStore(workspace)
    forged = AssetRecord(
        asset_id="sha256:forged",
        sha256=hashlib.sha256(b"outside").hexdigest(),
        relative_path="../outside.mp4",
        size_bytes=7,
        quality=MediaQuality.LOW_PROXY,
        media_unit_id="xiaohongshu:1",
    )

    with pytest.raises(WorkspaceError):
        store.resolve(forged)


def test_staging_paths_are_unique_and_safely_discarded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = WorkspaceAssetStore(workspace)

    first = store.allocate_staging_path(
        "ses_test",
        "bilibili:BV1:cid1",
        "low_proxy",
    )
    second = store.allocate_staging_path(
        "ses_test",
        "bilibili:BV1:cid1",
        "low_proxy",
    )

    assert first != second
    first.write_bytes(b"partial")
    store.discard_staging(first)
    assert not first.exists()

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"keep")
    with pytest.raises(WorkspaceError):
        store.discard_staging(outside)
    assert outside.read_bytes() == b"keep"


def test_staging_filename_does_not_embed_long_media_identity(tmp_path: Path) -> None:
    workspace = tmp_path / ("w" * 30)
    store = WorkspaceAssetStore(workspace)

    staging = store.allocate_staging_path(
        "ses_short",
        "bilibili:" + ("x" * 200),
        "low_proxy",
    )

    assert len(staging.name) == 36
    staging.write_bytes(b"partial")
    store.discard_staging(staging)
