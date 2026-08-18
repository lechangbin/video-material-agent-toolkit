from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from material_collector.application.media_actions import MediaApplication
from material_collector.application.sessions import (
    CreateSessionRequest,
    SessionApplication,
)
from material_collector.application.source_manifest import SourceManifestApplication
from material_collector.core.errors import ContractError, SessionStateError
from material_collector.core.manifest import WorkGroupMember, WorkGroupRecord
from material_collector.core.media import (
    AssetRecord,
    BrowserChannel,
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
from material_collector.infrastructure.assets import WorkspaceAssetStoreFactory
from material_collector.infrastructure.session_store import SqliteSessionStore
from material_collector.infrastructure.source_manifest_store import (
    SqliteSourceManifestStore,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def create_session(
    tmp_path: Path,
    *,
    platform_scope: tuple[str, ...] = ("bilibili", "douyin", "xiaohongshu"),
) -> tuple[Path, str]:
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(
        input_path,
        {
            "schema_version": "1.0",
            "full_script": "文案",
            "segments": [{"segment_id": "seg_001", "text": "文案"}],
        },
    )
    write_json(
        plans_path,
        {
            "schema_version": "2.0",
            "platform_scope": list(platform_scope),
            "plans": [
                {
                    "segment_id": "seg_001",
                    "visual_strategy": "策略",
                    "required_visual_facets": [{"facet_id": "facet_001", "description": "画面"}],
                    "initial_queries": [
                        {
                            "query_id": "query_001",
                            "text": "查询",
                            "target_platforms": list(platform_scope),
                            "facet_ids": ["facet_001"],
                        }
                    ],
                }
            ],
        },
    )
    workspace = tmp_path / "workspace"
    session = SessionApplication(
        store=SqliteSessionStore(
            now=lambda: FIXED_NOW,
            session_id_factory=lambda _now: "ses_manifest_001",
        )
    ).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
        )
    )
    return workspace, session.session_id


def manifest_application() -> SourceManifestApplication:
    return SourceManifestApplication(store=SqliteSourceManifestStore(now=lambda: FIXED_NOW))


def bilibili_batch() -> SearchBatch:
    request = SearchRequest(
        query_plan_id="qp_seg_001",
        segment_id="seg_001",
        query_id="query_001",
        round_number=1,
        text="城市",
    )
    return SearchBatch(
        platform=Platform.BILIBILI,
        request=request,
        candidates=(
            CandidateSource(
                platform=Platform.BILIBILI,
                source_id="BV1TEST",
                canonical_url="https://www.bilibili.com/video/BV1TEST?spm_id_from=temp",
                title="测试视频",
                author="作者",
                duration_seconds=30,
                rank=1,
                query_plan_id=request.query_plan_id,
                segment_id=request.segment_id,
                query_id=request.query_id,
                round_number=request.round_number,
            ),
        ),
    )


def test_manifest_rejects_a_search_batch_outside_the_frozen_scope(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path, platform_scope=("bilibili",))
    application = manifest_application()
    source = bilibili_batch()
    douyin_candidate = source.candidates[0].model_copy(
        update={
            "platform": Platform.DOUYIN,
            "source_id": "7123456789",
            "canonical_url": "https://www.douyin.com/video/7123456789",
        }
    )
    douyin_batch = source.model_copy(
        update={"platform": Platform.DOUYIN, "candidates": (douyin_candidate,)}
    )

    with pytest.raises(ContractError, match="frozen platform scope"):
        application.record_search_batch(workspace, session_id, douyin_batch)

    result = application.export(workspace, session_id)
    assert result.platform_scope == (Platform.BILIBILI,)
    assert result.candidates == ()


def test_manifest_refuses_to_publish_stored_candidates_outside_frozen_scope(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path, platform_scope=("bilibili",))
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    database_path = (
        workspace
        / ".material-collector"
        / "sessions"
        / session_id
        / "session.sqlite3"
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO candidate_source (
                candidate_id, platform, source_id, canonical_url, title
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "douyin:7123456789",
                "douyin",
                "7123456789",
                "https://www.douyin.com/video/7123456789",
                "越界候选",
            ),
        )

    with pytest.raises(SessionStateError, match="frozen platform scope"):
        application.export(workspace, session_id)


def test_manifest_merges_discoveries_and_strips_temporary_query(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()

    first = application.record_search_batch(workspace, session_id, bilibili_batch())
    second = application.record_search_batch(workspace, session_id, bilibili_batch())

    assert len(first.candidates) == 1
    assert len(second.candidates[0].discoveries) == 1
    assert second.candidates[0].canonical_url == ("https://www.bilibili.com/video/BV1TEST")
    result_path = (
        workspace / ".material-collector" / "sessions" / session_id / "collection-result.json"
    )
    assert json.loads(result_path.read_text(encoding="utf-8"))["schema_version"] == "2.0"


def test_resolved_units_apply_duration_review_boundary(tmp_path: Path) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    result = application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=1",
                    title="短分P",
                    duration_seconds=60,
                    part_index=1,
                ),
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid2",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=2",
                    title="长分P",
                    duration_seconds=1300,
                    part_index=2,
                ),
            ),
        ),
    )

    assert [unit.status for unit in result.candidates[0].media_units] == [
        "discovered",
        "manual_review_required",
    ]


def test_resolved_media_identity_metadata_survives_manifest_round_trip(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())

    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=1",
                    title="分P",
                    duration_seconds=60,
                    part_index=1,
                    metadata={"bvid": "BV1TEST", "cid": "cid1"},
                ),
            ),
        ),
    )

    exported = application.export(workspace, session_id)

    assert exported.candidates[0].media_units[0].metadata == {
        "bvid": "BV1TEST",
        "cid": "cid1",
    }


def test_legacy_manifest_recovers_platform_identity_metadata(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=1",
                    title="旧会话分P",
                    duration_seconds=60,
                ),
            ),
        ),
    )
    database = workspace / ".material-collector" / "sessions" / session_id / "session.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_unit SET metadata_json = '{}' WHERE media_unit_id = ?",
            ("bilibili:BV1TEST:cid1",),
        )

    exported = application.export(workspace, session_id)

    assert exported.candidates[0].media_units[0].metadata == {
        "bvid": "BV1TEST",
        "cid": "cid1",
    }


@pytest.mark.asyncio
async def test_fetch_hq_receives_persisted_media_identity_metadata(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    manifest = manifest_application()
    manifest.record_search_batch(workspace, session_id, bilibili_batch())
    manifest.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=1",
                    title="分P",
                    duration_seconds=60,
                    metadata={"bvid": "BV1TEST", "cid": "cid1"},
                ),
            ),
        ),
    )
    asset_store_factory = WorkspaceAssetStoreFactory()
    asset_store = asset_store_factory.for_workspace(workspace)
    proxy_payload = b"low-proxy"
    proxy_source = tmp_path / "proxy.mp4"
    proxy_source.write_bytes(proxy_payload)
    proxy = asset_store.import_fetch(
        FetchResult(
            media_unit_id="bilibili:BV1TEST:cid1",
            quality=MediaQuality.LOW_PROXY,
            path=proxy_source,
            size_bytes=len(proxy_payload),
            sha256=hashlib.sha256(proxy_payload).hexdigest(),
            container="mp4",
        )
    )
    proxy = asset_store.publish_title_view(
        proxy,
        session_id=session_id,
        platform=Platform.BILIBILI,
        source_id="BV1TEST",
        source_title="测试视频",
        media_unit_title="分P",
    )
    manifest.record_asset(workspace, session_id, proxy)
    manifest.replace_work_groups(
        workspace,
        session_id,
        (
            WorkGroupRecord(
                work_group_id="wg_hq_primary",
                status="independent",
                primary_media_unit_id="bilibili:BV1TEST:cid1",
                members=(
                    WorkGroupMember(
                        media_unit_id="bilibili:BV1TEST:cid1",
                        platform=Platform.BILIBILI,
                        role="primary",
                        fallback_order=1,
                    ),
                ),
            ),
        ),
    )

    fetch_count = 0

    class MetadataRequiringFetcher:
        platform = Platform.BILIBILI

        async def fetch(
            self,
            request: FetchRequest,
            context: PlatformContext,
        ) -> FetchResult:
            nonlocal fetch_count
            fetch_count += 1
            assert context.request_timeout_seconds == 77
            assert context.browser_channel is BrowserChannel.EDGE
            assert request.media_unit.metadata == {
                "bvid": "BV1TEST",
                "cid": "cid1",
            }
            payload = b"high-quality"
            request.destination.parent.mkdir(parents=True, exist_ok=True)
            request.destination.write_bytes(payload)
            return FetchResult(
                media_unit_id=request.media_unit.stable_id,
                quality=request.quality,
                path=request.destination,
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                container="mp4",
            )

    media = MediaApplication(
        media_fetchers={Platform.BILIBILI: MetadataRequiringFetcher()},
        manifest=manifest,
        asset_stores=asset_store_factory,
    )
    result = await media.fetch_high_quality(
        workspace,
        session_id,
        "bilibili:BV1TEST:cid1",
        auth_profile="default",
        browser_channel=BrowserChannel.EDGE,
        request_timeout_seconds=77,
    )

    assert result.status == "high_quality_ready"
    exported = manifest.export(workspace, session_id)
    asset = exported.candidates[0].media_units[0].high_quality_asset
    persisted_proxy = exported.candidates[0].media_units[0].proxy_asset
    assert asset is not None
    assert persisted_proxy is not None
    assert persisted_proxy.relative_path == proxy.relative_path
    assert persisted_proxy.display_relative_path == proxy.display_relative_path
    assert asset.display_relative_path is not None
    display = workspace / asset.display_relative_path
    assert display.parent.name == "high-quality"
    assert display.read_bytes() == b"high-quality"

    replay = await media.fetch_high_quality(
        workspace,
        session_id,
        "bilibili:BV1TEST:cid1",
        auth_profile="default",
        browser_channel=BrowserChannel.EDGE,
        request_timeout_seconds=77,
    )

    assert fetch_count == 1
    assert replay.asset_path == result.asset_path
    replayed_asset = manifest.export(workspace, session_id).candidates[0].media_units[
        0
    ].high_quality_asset
    assert replayed_asset is not None
    assert replayed_asset.display_relative_path == asset.display_relative_path


def test_assets_and_review_decisions_are_projected(tmp_path: Path) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST",
                    title="未知时长",
                ),
            ),
        ),
    )
    reviewed = application.decide_review(
        workspace,
        session_id,
        "bilibili:BV1TEST:cid1",
        approved=True,
        actor="human",
    )
    unit = reviewed.candidates[0].media_units[0]
    assert unit.review is not None
    assert unit.review.decision == "approved"

    projected = application.record_asset(
        workspace,
        session_id,
        AssetRecord(
            asset_id="sha256:" + "a" * 64,
            sha256="a" * 64,
            relative_path="assets/sha256/aa/file.mp4",
            size_bytes=10,
            quality=MediaQuality.LOW_PROXY,
            media_unit_id="bilibili:BV1TEST:cid1",
        ),
    )
    assert projected.candidates[0].media_units[0].proxy_asset is not None


def test_wrong_platform_host_is_rejected(tmp_path: Path) -> None:
    workspace, session_id = create_session(tmp_path)
    batch = bilibili_batch()
    candidate = batch.candidates[0].model_copy(
        update={"canonical_url": "https://example.com/video/BV1TEST"}
    )

    with pytest.raises(ContractError):
        manifest_application().record_search_batch(
            workspace,
            session_id,
            batch.model_copy(update={"candidates": (candidate,)}),
        )


def test_work_groups_are_projected_and_mark_only_primary_for_understanding(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=1",
                    title="主来源",
                    duration_seconds=60,
                ),
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid2",
                    canonical_url="https://www.bilibili.com/video/BV1TEST?p=2",
                    title="回退来源",
                    duration_seconds=60,
                ),
            ),
        ),
    )
    before_assets = application.export(workspace, session_id)
    assert not any(
        unit.eligible_for_understanding
        for candidate in before_assets.candidates
        for unit in candidate.media_units
    )
    for media_unit_id in (
        "bilibili:BV1TEST:cid1",
        "bilibili:BV1TEST:cid2",
    ):
        application.record_asset(
            workspace,
            session_id,
            AssetRecord(
                asset_id=f"sha256:{media_unit_id}",
                sha256="0" * 64,
                relative_path=f"assets/{media_unit_id}.mp4",
                size_bytes=1,
                quality=MediaQuality.LOW_PROXY,
                media_unit_id=media_unit_id,
            ),
        )
    group = WorkGroupRecord(
        work_group_id="wg_sha256_001",
        status="confirmed_duplicate",
        primary_media_unit_id="bilibili:BV1TEST:cid1",
        members=(
            WorkGroupMember(
                media_unit_id="bilibili:BV1TEST:cid1",
                platform=Platform.BILIBILI,
                role="primary",
                fallback_order=1,
            ),
            WorkGroupMember(
                media_unit_id="bilibili:BV1TEST:cid2",
                platform=Platform.BILIBILI,
                role="fallback",
                fallback_order=2,
            ),
        ),
    )

    result = application.replace_work_groups(
        workspace,
        session_id,
        (group,),
    )

    assert result.work_groups == (group,)
    units = result.candidates[0].media_units
    assert (
        units[0].work_group_id,
        units[0].source_role,
        units[0].fallback_order,
        units[0].eligible_for_understanding,
    ) == ("wg_sha256_001", "primary", 1, True)
    assert (
        units[1].work_group_id,
        units[1].source_role,
        units[1].fallback_order,
        units[1].eligible_for_understanding,
    ) == ("wg_sha256_001", "fallback", 2, False)
    result_path = (
        workspace / ".material-collector" / "sessions" / session_id / "collection-result.json"
    )
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["work_groups"][0]["primary_media_unit_id"] == ("bilibili:BV1TEST:cid1")


def test_legacy_session_migrates_work_group_tables_idempotently(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST",
                    title="旧会话来源",
                    duration_seconds=60,
                ),
            ),
        ),
    )
    database = workspace / ".material-collector" / "sessions" / session_id / "session.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE work_group_member")
        connection.execute("DROP TABLE work_group")

    assert application.export(workspace, session_id).work_groups == ()
    assert application.export(workspace, session_id).work_groups == ()

    group = WorkGroupRecord(
        work_group_id="wg_legacy_001",
        status="independent",
        primary_media_unit_id="bilibili:BV1TEST:cid1",
        members=(
            WorkGroupMember(
                media_unit_id="bilibili:BV1TEST:cid1",
                platform=Platform.BILIBILI,
                role="primary",
                fallback_order=1,
            ),
        ),
    )
    first = application.replace_work_groups(workspace, session_id, (group,))
    second = application.replace_work_groups(workspace, session_id, (group,))

    assert first.work_groups == (group,)
    assert second.work_groups == (group,)


def test_invalid_work_group_snapshot_preserves_last_committed_groups(
    tmp_path: Path,
) -> None:
    workspace, session_id = create_session(tmp_path)
    application = manifest_application()
    application.record_search_batch(workspace, session_id, bilibili_batch())
    application.record_resolved_source(
        workspace,
        session_id,
        ResolvedSource(
            candidate_id="bilibili:BV1TEST",
            media_units=(
                MediaUnit(
                    platform=Platform.BILIBILI,
                    source_id="BV1TEST",
                    media_unit_id="BV1TEST:cid1",
                    canonical_url="https://www.bilibili.com/video/BV1TEST",
                    title="有效来源",
                    duration_seconds=60,
                ),
            ),
        ),
    )
    valid = WorkGroupRecord(
        work_group_id="wg_valid_001",
        status="independent",
        primary_media_unit_id="bilibili:BV1TEST:cid1",
        members=(
            WorkGroupMember(
                media_unit_id="bilibili:BV1TEST:cid1",
                platform=Platform.BILIBILI,
                role="primary",
                fallback_order=1,
            ),
        ),
    )
    application.replace_work_groups(workspace, session_id, (valid,))
    invalid = WorkGroupRecord(
        work_group_id="wg_invalid_001",
        status="fingerprint_unavailable",
        primary_media_unit_id="bilibili:missing",
        members=(
            WorkGroupMember(
                media_unit_id="bilibili:missing",
                platform=Platform.BILIBILI,
                role="primary",
                fallback_order=1,
            ),
        ),
    )

    with pytest.raises(ContractError):
        application.replace_work_groups(workspace, session_id, (invalid,))

    assert application.export(workspace, session_id).work_groups == (valid,)
