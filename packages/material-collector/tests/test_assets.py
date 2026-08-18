from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from material_collector.core.errors import CollectorError, WorkspaceError
from material_collector.core.media import (
    AssetRecord,
    FetchResult,
    MediaQuality,
    Platform,
    TitleViewPublication,
)
from material_collector.infrastructure import assets as asset_module
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


def test_title_view_prefers_hard_links_and_preserves_previous_titles(
    tmp_path: Path,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))

    first = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_titles",
            platform=Platform.BILIBILI,
            source_id="BV1:source",
            source_title="CON.txt",
            media_unit_title='第一段：城市/秋色? "全景"',
        ),
    )
    second = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_titles",
            platform=Platform.BILIBILI,
            source_id="BV1:source",
            source_title="更新后的标题",
            media_unit_title="更新后的分段",
        ),
    )

    assert first.display_relative_path is not None
    assert second.display_relative_path is not None
    first_path = store.workspace / first.display_relative_path
    second_path = store.workspace / second.display_relative_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert first_path.parts[-3].startswith("_CON.txt--")
    assert "__bilibili__" in first_path.parts[-3]
    assert first_path.name.startswith("第一段_城市_秋色_ _全景_--")
    assert os.path.samefile(store.resolve(asset), first_path)
    assert os.path.samefile(store.resolve(asset), second_path)


def test_title_view_falls_back_to_verified_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"copy-me"))
    monkeypatch.setattr(
        "material_collector.infrastructure.assets.os.link",
        lambda *_args: _raise_os_error(),
    )

    published = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_copy",
            platform=Platform.BILIBILI,
            source_id="BV1COPY",
            source_title="复制回退",
            media_unit_title="素材",
        ),
    )

    assert published.display_relative_path is not None
    display = store.workspace / published.display_relative_path
    assert display.read_bytes() == b"copy-me"
    assert not os.path.samefile(store.resolve(asset), display)


def test_title_view_bounds_long_windows_paths_without_title_collisions(
    tmp_path: Path,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))

    first = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_long_titles",
            platform=Platform.BILIBILI,
            source_id="BV1LONG",
            source_title="秋" * 200 + "甲",
            media_unit_title="枫" * 200 + "甲",
        ),
    )
    second = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_long_titles",
            platform=Platform.BILIBILI,
            source_id="BV1LONG",
            source_title="秋" * 200 + "乙",
            media_unit_title="枫" * 200 + "乙",
        ),
    )

    assert first.display_relative_path is not None
    assert second.display_relative_path is not None
    first_path = store.workspace / first.display_relative_path
    second_path = store.workspace / second.display_relative_path
    assert len(str(first_path)) <= 259
    assert len(str(second_path)) <= 259
    assert first_path != second_path
    assert first_path.read_bytes() == second_path.read_bytes() == b"video"


def test_lossy_title_sanitization_retains_raw_title_identity(tmp_path: Path) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))

    first = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_lossy_titles",
            platform=Platform.BILIBILI,
            source_id="BV1SAME",
            source_title="秋色/全景",
            media_unit_title="秋色/全景",
        ),
    )
    second = store.publish_title_view(
        asset,
        TitleViewPublication(
            session_id="ses_lossy_titles",
            platform=Platform.BILIBILI,
            source_id="BV1SAME",
            source_title="秋色?全景",
            media_unit_title="秋色?全景",
        ),
    )

    assert first.display_relative_path is not None
    assert second.display_relative_path is not None
    assert first.display_relative_path != second.display_relative_path
    assert (store.workspace / first.display_relative_path).is_file()
    assert (store.workspace / second.display_relative_path).is_file()


def test_copy_fallback_verifies_before_publishing_visible_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))
    monkeypatch.setattr(
        "material_collector.infrastructure.assets.os.link",
        lambda *_args: _raise_os_error(),
    )

    def corrupt_copy(
        _source: object,
        output: object,
        *,
        length: int,
    ) -> None:
        del length
        output.write(b"corrupt")  # type: ignore[attr-defined]

    monkeypatch.setattr(
        "material_collector.infrastructure.assets.shutil.copyfileobj",
        corrupt_copy,
    )

    with pytest.raises(CollectorError) as captured:
        store.publish_title_view(
            asset,
            TitleViewPublication(
                session_id="ses_verify_first",
                platform=Platform.BILIBILI,
                source_id="BV1VERIFY",
                source_title="校验后发布",
                media_unit_title="素材",
            ),
        )

    display = store.workspace / captured.value.details["display_relative_path"]
    assert captured.value.code == "named_view_publish_failed"
    assert not display.exists()


def test_retry_repairs_an_invalid_existing_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))
    monkeypatch.setattr(
        "material_collector.infrastructure.assets.os.link",
        lambda *_args: _raise_os_error(),
    )
    publication = TitleViewPublication(
        session_id="ses_repair_copy",
        platform=Platform.BILIBILI,
        source_id="BV1REPAIR",
        source_title="修复入口",
        media_unit_title="素材",
    )
    first = store.publish_title_view(asset, publication)
    assert first.display_relative_path is not None
    display = store.workspace / first.display_relative_path
    display.write_bytes(b"corrupt")

    repaired = store.publish_title_view(asset, publication)

    assert repaired.display_relative_path == first.display_relative_path
    assert display.read_bytes() == b"video"


def test_title_view_reports_retryable_structured_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WorkspaceAssetStore(tmp_path / "workspace")
    asset = store.import_fetch(fetched_file(tmp_path / "source.mp4", b"video"))
    monkeypatch.setattr(
        "material_collector.infrastructure.assets.os.link",
        lambda *_args: _raise_os_error(),
    )
    monkeypatch.setattr(
        asset_module,
        "_copy_atomically",
        lambda *_args, **_kwargs: _raise_os_error(),
    )

    with pytest.raises(CollectorError) as captured:
        store.publish_title_view(
            asset,
            TitleViewPublication(
                session_id="ses_failure",
                platform=Platform.BILIBILI,
                source_id="BV1FAIL",
                source_title="发布失败",
                media_unit_title="素材",
            ),
        )

    assert captured.value.code == "named_view_publish_failed"
    assert captured.value.details["retryable"] is True
    assert store.resolve(asset).read_bytes() == b"video"


def _raise_os_error() -> None:
    raise OSError("simulated filesystem failure")
