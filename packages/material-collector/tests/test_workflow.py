from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from material_collector.application.sessions import (
    CreateSessionRequest,
    RuntimeConstraints,
    SessionApplication,
)
from material_collector.application.source_manifest import SourceManifestApplication
from material_collector.application.workflow import CollectionWorkflow
from material_collector.core.errors import CollectorError
from material_collector.core.fingerprints import FingerprintMatch
from material_collector.core.media import (
    PLATFORM_ORDER,
    AuthProbe,
    AuthStatus,
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
from material_collector.infrastructure.assets import WorkspaceAssetStoreFactory
from material_collector.infrastructure.session_runtime_store import (
    SqliteSessionRuntime as SessionRuntime,
)
from material_collector.infrastructure.session_store import SqliteSessionStore
from material_collector.infrastructure.source_manifest_store import (
    SqliteSourceManifestStore,
)

ENABLED_COLLECTION_PLATFORMS = (Platform.BILIBILI, Platform.DOUYIN)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _create_session(
    tmp_path: Path,
    *,
    max_videos: int = 6,
    add_bilibili_query: bool = False,
    add_douyin_query: bool = False,
    request_timeout_seconds: int = 30,
) -> tuple[Path, str]:
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    queries = [
        {
            "query_id": "query_city",
            "text": "城市更新 旧工业区",
            "target_platforms": [
                "bilibili",
                "douyin",
                "xiaohongshu",
            ],
            "facet_ids": ["facet_change"],
        }
    ]
    if add_bilibili_query:
        queries.append(
            {
                "query_id": "query_city_second",
                "text": "城市更新 公共空间",
                "target_platforms": ["bilibili"],
                "facet_ids": ["facet_change"],
            }
        )
    if add_douyin_query:
        queries.append(
            {
                "query_id": "query_city_douyin_second",
                "text": "城市更新 公共空间",
                "target_platforms": ["douyin"],
                "facet_ids": ["facet_change"],
            }
        )
    _write_json(
        input_path,
        {
            "schema_version": "1.0",
            "theme": "城市变化",
            "full_script": "城市从旧工业走向新的公共生活。",
            "segments": [
                {
                    "segment_id": "seg_city",
                    "order": 1,
                    "text": "城市从旧工业走向新的公共生活。",
                }
            ],
        },
    )
    _write_json(
        plans_path,
        {
            "schema_version": "1.0",
            "plans": [
                {
                    "segment_id": "seg_city",
                    "visual_strategy": "寻找城市更新前后环境",
                    "required_visual_facets": [
                        {
                            "facet_id": "facet_change",
                            "description": "旧工业区和公共空间",
                        }
                    ],
                    "initial_queries": queries,
                }
            ],
        },
    )
    workspace = tmp_path / "workspace"
    session = SessionApplication(store=SqliteSessionStore()).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
            constraints=RuntimeConstraints(
                max_rounds=3,
                max_videos=max_videos,
                auth_wait_seconds=30,
                request_timeout_seconds=request_timeout_seconds,
            ),
        )
    )
    return workspace, session.session_id


class _Authentication:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple[Platform, ...]] = []

    async def probe(self, platform: Platform, auth_profile: str) -> AuthProbe:
        return AuthProbe(
            platform=platform,
            auth_profile=auth_profile,
            status=AuthStatus.VALID,
            checked_at="2026-07-29T00:00:00Z",
        )

    async def ensure_authenticated(
        self,
        platforms: tuple[Platform, ...],
        auth_profile: str,
        wait_seconds: int,
        *,
        progress: Any | None = None,
    ) -> tuple[AuthProbe, ...]:
        del wait_seconds
        self.ensure_calls.append(platforms)
        if progress is not None:
            progress.report(
                "authentication_probe_started",
                {
                    "platform": platforms[0].value,
                    "auth_profile": auth_profile,
                },
            )
        probes: list[AuthProbe] = []
        for platform in platforms:
            probes.append(await self.probe(platform, auth_profile))
        return tuple(probes)

    async def logout(
        self,
        platform: Platform,
        auth_profile: str,
        confirmation: str,
    ) -> None:
        del platform, auth_profile, confirmation


class _Platform:
    def __init__(
        self,
        platform: Platform,
        *,
        duration_seconds: float | None = 30,
        title: str | None = None,
        author: str | None = None,
    ) -> None:
        self.platform = platform
        self.duration_seconds = duration_seconds
        self.title = title or f"{platform.value} 城市更新"
        self.author = author
        self.search_count = 0
        self.resolve_count = 0
        self.fetch_count = 0
        self.search_failures: list[CollectorError] = []
        self.resolve_failures: list[CollectorError] = []
        self.fetch_failures: list[CollectorError] = []
        self.seen_timeouts: list[int] = []
        self.seen_search_limits: list[int] = []

    async def search(
        self,
        request: SearchRequest,
        context: PlatformContext,
    ) -> SearchBatch:
        self.seen_timeouts.append(context.request_timeout_seconds)
        self.seen_search_limits.append(request.limit)
        self.search_count += 1
        if self.search_failures:
            raise self.search_failures.pop(0)
        source_id = f"{self.platform.value}_source"
        host = {
            Platform.BILIBILI: "www.bilibili.com",
            Platform.DOUYIN: "www.douyin.com",
            Platform.XIAOHONGSHU: "www.xiaohongshu.com",
        }[self.platform]
        candidate = CandidateSource(
            platform=self.platform,
            source_id=source_id,
            canonical_url=f"https://{host}/video/{source_id}",
            title=self.title,
            author=self.author,
            duration_seconds=self.duration_seconds,
            rank=1,
            query_plan_id=request.query_plan_id,
            segment_id=request.segment_id,
            query_id=request.query_id,
            round_number=request.round_number,
        )
        return SearchBatch(
            platform=self.platform,
            request=request,
            candidates=(candidate,),
        )

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource:
        self.seen_timeouts.append(context.request_timeout_seconds)
        self.resolve_count += 1
        if self.resolve_failures:
            raise self.resolve_failures.pop(0)
        return ResolvedSource(
            candidate_id=f"{self.platform.value}:{source_id}",
            media_units=(
                MediaUnit(
                    platform=self.platform,
                    source_id=source_id,
                    media_unit_id=f"{source_id}_unit",
                    canonical_url=canonical_url,
                    title=self.title,
                    duration_seconds=self.duration_seconds,
                ),
            ),
        )

    async def fetch(
        self,
        request: FetchRequest,
        context: PlatformContext,
    ) -> FetchResult:
        self.seen_timeouts.append(context.request_timeout_seconds)
        self.fetch_count += 1
        if self.fetch_failures:
            raise self.fetch_failures.pop(0)
        payload = f"proxy:{request.media_unit.stable_id}".encode()
        request.destination.parent.mkdir(parents=True, exist_ok=True)
        request.destination.write_bytes(payload)
        return FetchResult(
            media_unit_id=request.media_unit.stable_id,
            quality=request.quality,
            path=request.destination,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            container="mp4",
            duration_seconds=request.media_unit.duration_seconds,
        )


class _FingerprintService:
    def __init__(
        self,
        *,
        matching_pairs: set[frozenset[str]] | None = None,
        unreliable: bool = False,
    ) -> None:
        self.matching_pairs = matching_pairs or set()
        self.unreliable = unreliable
        self.compare_count = 0

    def compare(self, left: Path, right: Path) -> FingerprintMatch:
        self.compare_count += 1
        left_id = left.read_text(encoding="utf-8").removeprefix("proxy:")
        right_id = right.read_text(encoding="utf-8").removeprefix("proxy:")
        if self.unreliable:
            return FingerprintMatch(
                same_work=False,
                reliable=False,
                reason="test_fingerprint_unavailable",
            )
        same_work = frozenset((left_id, right_id)) in self.matching_pairs
        return FingerprintMatch(
            same_work=same_work,
            reliable=True,
            audio_similarity=1.0 if same_work else 0.0,
            video_similarity=1.0 if same_work else 0.0,
            duration_difference_seconds=0.0,
            reason="test_match" if same_work else "test_distinct",
        )


class _RecordingProgress:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def report(self, event: str, details: dict[str, object]) -> None:
        self.events.append((event, details))


def _make_workflow(
    authentication: _Authentication,
    adapters: dict[Platform, _Platform],
    runtime: SessionRuntime,
    fingerprints: _FingerprintService | None = None,
    progress: _RecordingProgress | None = None,
    lease_maintenance_interval_seconds: float = 5.0,
) -> CollectionWorkflow:
    return CollectionWorkflow(
        authentication=authentication,
        search_providers=adapters,
        source_resolvers=adapters,
        media_fetchers=adapters,
        sessions=SessionApplication(store=SqliteSessionStore()),
        runtime=runtime,
        manifest=SourceManifestApplication(store=SqliteSourceManifestStore()),
        asset_stores=WorkspaceAssetStoreFactory(),
        fingerprints=fingerprints or _FingerprintService(),
        progress=progress,
        lease_maintenance_interval_seconds=lease_maintenance_interval_seconds,
    )


async def _assert_cancellation_stops_pending_operation(
    workflow: CollectionWorkflow,
    runtime: SessionRuntime,
    workspace: Path,
    session_id: str,
    started: asyncio.Event,
    closed: asyncio.Event,
) -> None:
    running = asyncio.create_task(workflow.run(workspace, session_id))
    await asyncio.wait_for(started.wait(), timeout=1)

    runtime.request_cancel(workspace, session_id)
    result = await asyncio.wait_for(running, timeout=1)

    assert result.status == "cancelled"
    assert closed.is_set()
    assert runtime.inspect(workspace, session_id).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_authentication_stops_waiting_and_closes_gateway(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(lease_ttl_seconds=60)
    started = asyncio.Event()
    closed = asyncio.Event()

    class _WaitingAuthentication(_Authentication):
        async def ensure_authenticated(
            self,
            platforms: tuple[Platform, ...],
            auth_profile: str,
            wait_seconds: int,
            *,
            progress: Any | None = None,
        ) -> tuple[AuthProbe, ...]:
            del platforms, auth_profile, wait_seconds, progress
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    workflow = _make_workflow(
        _WaitingAuthentication(),
        adapters,
        runtime,
        lease_maintenance_interval_seconds=0.01,
    )
    await _assert_cancellation_stops_pending_operation(
        workflow,
        runtime,
        workspace,
        session_id,
        started,
        closed,
    )


@pytest.mark.asyncio
async def test_cancel_during_platform_search_stops_pending_requests(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(lease_ttl_seconds=60)
    started = asyncio.Event()
    closed = asyncio.Event()

    class _WaitingSearch(_Platform):
        async def search(
            self,
            request: SearchRequest,
            context: PlatformContext,
        ) -> SearchBatch:
            del request, context
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.BILIBILI] = _WaitingSearch(Platform.BILIBILI)
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        runtime,
        lease_maintenance_interval_seconds=0.01,
    )
    await _assert_cancellation_stops_pending_operation(
        workflow,
        runtime,
        workspace,
        session_id,
        started,
        closed,
    )


@pytest.mark.asyncio
async def test_cancel_during_source_resolution_stops_pending_requests(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(lease_ttl_seconds=60)
    started = asyncio.Event()
    closed = asyncio.Event()

    class _WaitingResolution(_Platform):
        async def resolve(
            self,
            source_id: str,
            canonical_url: str,
            context: PlatformContext,
        ) -> ResolvedSource:
            del source_id, canonical_url, context
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.BILIBILI] = _WaitingResolution(Platform.BILIBILI)
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        runtime,
        lease_maintenance_interval_seconds=0.01,
    )
    await _assert_cancellation_stops_pending_operation(
        workflow,
        runtime,
        workspace,
        session_id,
        started,
        closed,
    )


@pytest.mark.asyncio
async def test_cancel_during_proxy_download_stops_pending_fetch(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    runtime = SessionRuntime(lease_ttl_seconds=60)
    started = asyncio.Event()
    closed = asyncio.Event()

    class _WaitingFetch(_Platform):
        async def fetch(
            self,
            request: FetchRequest,
            context: PlatformContext,
        ) -> FetchResult:
            del request, context
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
            raise AssertionError("unreachable")

    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.BILIBILI] = _WaitingFetch(Platform.BILIBILI)
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        runtime,
        lease_maintenance_interval_seconds=0.01,
    )
    await _assert_cancellation_stops_pending_operation(
        workflow,
        runtime,
        workspace,
        session_id,
        started,
        closed,
    )


@pytest.mark.asyncio
async def test_workflow_forwards_authentication_progress_to_caller(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    progress = _RecordingProgress()
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        progress=progress,
    )

    await workflow.run(workspace, session_id)

    assert (
        "authentication_probe_started",
        {"platform": "bilibili", "auth_profile": "default"},
    ) in progress.events


@pytest.mark.asyncio
async def test_workflow_collects_sources_and_pauses_for_integration(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert result.candidates_found == 2
    assert result.media_units_found == 2
    assert result.proxies_ready == 2
    assert result.action_required is not None
    assert result.action_required.type == "integration_required"
    assert result.action_required.target == "collection-result.json"
    assert result.action_required.requested_artifacts == (
        "video_understanding_result",
        "ingestion_result",
    )
    assert result.segments[0].segment_id == "seg_city"
    assert result.segments[0].candidates_found == 2
    assert Path(result.result_path).is_file()
    snapshot = SessionRuntime().inspect(workspace, session_id)
    assert snapshot.status == "integration_required"
    assert snapshot.action_required is not None
    assert snapshot.action_required["type"] == "integration_required"

    repeated = await workflow.run(workspace, session_id)
    assert repeated.status == "integration_required"
    assert sum(adapter.search_count for adapter in adapters.values()) == 2
    assert sum(adapter.fetch_count for adapter in adapters.values()) == 2


@pytest.mark.asyncio
async def test_workflow_uses_frozen_request_timeout_for_every_platform_call(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(
        tmp_path,
        max_videos=9,
        request_timeout_seconds=47,
    )
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    await workflow.run(workspace, session_id)

    assert all(timeout == 47 for adapter in adapters.values() for timeout in adapter.seen_timeouts)


@pytest.mark.asyncio
async def test_each_search_expression_uses_per_platform_limit_of_twenty(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(
        tmp_path,
        max_videos=9,
        add_bilibili_query=True,
    )
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    await workflow.run(workspace, session_id)

    assert adapters[Platform.BILIBILI].seen_search_limits == [20, 20]
    assert adapters[Platform.DOUYIN].seen_search_limits == [20]
    assert adapters[Platform.XIAOHONGSHU].seen_search_limits == []


@pytest.mark.asyncio
async def test_workflow_routes_long_or_unknown_media_to_human_review(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=3)
    adapters = {
        Platform.BILIBILI: _Platform(
            Platform.BILIBILI,
            duration_seconds=20 * 60 + 1,
        ),
        Platform.DOUYIN: _Platform(Platform.DOUYIN, duration_seconds=None),
        Platform.XIAOHONGSHU: _Platform(
            Platform.XIAOHONGSHU,
            duration_seconds=20 * 60 + 5,
        ),
    }
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "decision_required"
    assert result.proxies_ready == 0
    assert result.action_required is not None
    assert result.action_required.type == "manual_review_required"
    assert sum(adapter.fetch_count for adapter in adapters.values()) == 0


@pytest.mark.asyncio
async def test_same_author_and_title_downloads_every_platform_before_fingerprint(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {
        platform: _Platform(
            platform,
            title="城市更新完整记录",
            author="同一创作者",
        )
        for platform in PLATFORM_ORDER
    }
    fingerprints = _FingerprintService(
        matching_pairs={
            frozenset(
                (
                    f"{left.value}:{left.value}_source_unit",
                    f"{right.value}:{right.value}_source_unit",
                )
            )
            for index, left in enumerate(PLATFORM_ORDER)
            for right in PLATFORM_ORDER[index + 1 :]
        }
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        fingerprints,
    )

    result = await workflow.run(workspace, session_id)

    assert result.candidates_found == 2
    assert result.proxies_ready == 2
    assert adapters[Platform.BILIBILI].fetch_count == 1
    assert adapters[Platform.DOUYIN].fetch_count == 1
    assert adapters[Platform.XIAOHONGSHU].fetch_count == 0
    manifest = workflow._manifest.export(workspace, session_id)
    assert len(manifest.work_groups) == 1
    group = manifest.work_groups[0]
    assert group.status == "confirmed_duplicate"
    assert group.primary_media_unit_id.startswith("bilibili:")
    assert [member.platform for member in group.members] == list(
        ENABLED_COLLECTION_PLATFORMS
    )
    assert [member.fallback_order for member in group.members] == [1, 2]
    assert (
        sum(
            unit.eligible_for_understanding
            for candidate in manifest.candidates
            for unit in candidate.media_units
        )
        == 1
    )
    comparisons = fingerprints.compare_count

    await workflow.run(workspace, session_id)

    assert fingerprints.compare_count == comparisons


@pytest.mark.asyncio
async def test_unavailable_fingerprints_keep_candidates_independent(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {
        platform: _Platform(
            platform,
            title="同一作品标题",
            author="同一作者",
        )
        for platform in PLATFORM_ORDER
    }
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        _FingerprintService(unreliable=True),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    manifest = workflow._manifest.export(workspace, session_id)
    assert len(manifest.work_groups) == 2
    assert {group.status for group in manifest.work_groups} == {"fingerprint_unavailable"}
    assert all(
        unit.eligible_for_understanding
        for candidate in manifest.candidates
        for unit in candidate.media_units
    )


@pytest.mark.asyncio
async def test_unrelated_cross_platform_candidates_skip_fingerprinting(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    fingerprints = _FingerprintService()
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        fingerprints,
    )

    await workflow.run(workspace, session_id)

    assert fingerprints.compare_count == 0
    manifest = workflow._manifest.export(workspace, session_id)
    assert len(manifest.work_groups) == 2
    assert {group.status for group in manifest.work_groups} == {"independent"}


@pytest.mark.asyncio
async def test_pending_manual_review_does_not_block_processable_primary(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=6)
    adapters = {
        Platform.BILIBILI: _Platform(
            Platform.BILIBILI,
            duration_seconds=20 * 60 + 1,
        ),
        Platform.DOUYIN: _Platform(
            Platform.DOUYIN,
            duration_seconds=30,
        ),
        Platform.XIAOHONGSHU: _Platform(
            Platform.XIAOHONGSHU,
            duration_seconds=30,
        ),
    }
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert result.action_required is not None
    assert result.action_required.type == "integration_required"


@pytest.mark.asyncio
async def test_partial_platform_search_failure_continues_with_committed_sources(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].search_failures.append(
        CollectorError(
            "platform_request_rejected",
            "temporary",
            details={"platform": "douyin", "retryable": True},
        )
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert result.candidates_found == 1
    assert result.media_units_found == 1
    assert result.proxies_ready == 1
    assert [issue.code for issue in result.issues] == [
        "platform_search_temporarily_disabled",
        "platform_request_rejected",
    ]
    assert adapters[Platform.DOUYIN].resolve_count == 0
    assert adapters[Platform.DOUYIN].fetch_count == 0


@pytest.mark.asyncio
async def test_partial_search_reports_settled_plan_instead_of_committed(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].search_failures.append(
        CollectorError(
            "platform_request_rejected",
            "temporary",
            details={"platform": "douyin", "retryable": True},
        )
    )
    progress = _RecordingProgress()
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        progress=progress,
    )

    await workflow.run(workspace, session_id)

    assert (
        "search_plan_settled",
        {
            "session_id": session_id,
            "query_plan_id": "qp_seg_city",
            "status": "completed_with_issues",
            "requested_requests": 3,
            "completed_requests": 1,
            "failed_requests": 1,
            "not_attempted_requests": 1,
        },
    ) in progress.events
    assert all(event != "search_plan_committed" for event, _details in progress.events)


@pytest.mark.asyncio
async def test_all_platform_searches_retryable_keeps_search_stage_resumable(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    for platform in ENABLED_COLLECTION_PLATFORMS:
        adapter = adapters[platform]
        adapter.search_failures.append(
            CollectorError(
                "platform_request_rejected",
                "temporary",
                details={"platform": adapter.platform.value, "retryable": True},
            )
        )
    runtime = SessionRuntime(lease_ttl_seconds=900)
    workflow = _make_workflow(_Authentication(), adapters, runtime)

    with pytest.raises(CollectorError) as captured:
        await workflow.run(workspace, session_id)

    assert captured.value.code == "workflow_retryable"
    assert runtime.inspect(workspace, session_id).next_stage == "search"

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert all(
        adapters[platform].search_count == 2
        for platform in ENABLED_COLLECTION_PLATFORMS
    )
    assert adapters[Platform.XIAOHONGSHU].search_count == 0


@pytest.mark.asyncio
async def test_terminal_search_failure_is_not_replayed_as_success(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.BILIBILI].search_failures.append(
        CollectorError(
            "platform_schema_changed",
            "terminal",
            details={"platform": "bilibili", "retryable": False},
        )
    )
    for platform in (Platform.DOUYIN,):
        adapters[platform].search_failures.extend(
            [
                CollectorError(
                    "platform_response_timeout",
                    "temporary",
                    details={"platform": platform.value, "retryable": True},
                ),
                CollectorError(
                    "platform_response_timeout",
                    "still temporary",
                    details={"platform": platform.value, "retryable": True},
                ),
            ]
        )
    runtime = SessionRuntime(lease_ttl_seconds=900)
    workflow = _make_workflow(_Authentication(), adapters, runtime)

    for _attempt in range(2):
        with pytest.raises(CollectorError) as captured:
            await workflow.run(workspace, session_id)
        assert captured.value.code == "workflow_retryable"
        assert runtime.inspect(workspace, session_id).next_stage == "search"

    assert adapters[Platform.BILIBILI].search_count == 1
    assert adapters[Platform.DOUYIN].search_count == 2
    assert adapters[Platform.XIAOHONGSHU].search_count == 0


@pytest.mark.asyncio
async def test_settled_search_issues_survive_resume_from_later_stage(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.BILIBILI].search_failures.append(
        CollectorError(
            "platform_response_timeout",
            "temporary",
            details={"platform": "bilibili", "retryable": True},
        )
    )
    adapters[Platform.DOUYIN].resolve_failures.append(
        CollectorError(
            "platform_response_timeout",
            "temporary",
            details={"platform": "douyin", "retryable": True},
        )
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    with pytest.raises(CollectorError) as captured:
        await workflow.run(workspace, session_id)
    assert captured.value.code == "workflow_retryable"

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert [issue.code for issue in result.issues] == [
        "platform_search_temporarily_disabled",
        "platform_response_timeout",
    ]
    assert result.issues[1].details["platform"] == "bilibili"


@pytest.mark.asyncio
async def test_resume_retries_only_failed_resolution_subitem(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].resolve_failures.append(
        CollectorError(
            "platform_request_rejected",
            "temporary",
            details={"platform": "douyin", "retryable": True},
        )
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    with pytest.raises(CollectorError) as captured:
        await workflow.run(workspace, session_id)
    assert captured.value.code == "workflow_retryable"
    first_searches = sum(adapter.search_count for adapter in adapters.values())
    first_resolves = {platform: adapter.resolve_count for platform, adapter in adapters.items()}

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert sum(adapter.search_count for adapter in adapters.values()) == first_searches
    assert adapters[Platform.BILIBILI].resolve_count == first_resolves[Platform.BILIBILI]
    assert adapters[Platform.XIAOHONGSHU].resolve_count == first_resolves[Platform.XIAOHONGSHU]
    assert adapters[Platform.DOUYIN].resolve_count == first_resolves[Platform.DOUYIN] + 1


@pytest.mark.asyncio
async def test_resume_retries_only_failed_download_subitem(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].fetch_failures.append(
        CollectorError(
            "platform_request_rejected",
            "temporary",
            details={"platform": "douyin", "retryable": True},
        )
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    with pytest.raises(CollectorError) as captured:
        await workflow.run(workspace, session_id)
    assert captured.value.code == "workflow_retryable"
    first_fetches = {platform: adapter.fetch_count for platform, adapter in adapters.items()}

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert adapters[Platform.BILIBILI].fetch_count == first_fetches[Platform.BILIBILI]
    assert adapters[Platform.XIAOHONGSHU].fetch_count == first_fetches[Platform.XIAOHONGSHU]
    assert adapters[Platform.DOUYIN].fetch_count == first_fetches[Platform.DOUYIN] + 1


@pytest.mark.asyncio
async def test_authentication_loss_reauthenticates_once_and_retries_only_platform(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].search_failures.append(
        CollectorError(
            "authentication_lost",
            "expired",
            details={"platform": "douyin", "retryable": True},
        )
    )
    authentication = _Authentication()
    progress = _RecordingProgress()
    workflow = _make_workflow(
        authentication,
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        progress=progress,
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "integration_required"
    assert authentication.ensure_calls == [
        ENABLED_COLLECTION_PLATFORMS,
        (Platform.DOUYIN,),
    ]
    assert adapters[Platform.DOUYIN].search_count == 2
    assert adapters[Platform.BILIBILI].search_count == 1
    assert adapters[Platform.XIAOHONGSHU].search_count == 0
    assert [
        details["platform"]
        for event, details in progress.events
        if event == "authentication_probe_started"
    ] == ["bilibili", "douyin"]
    settled = next(
        details
        for event, details in progress.events
        if event == "search_plan_settled"
    )
    assert settled["requested_requests"] == 3
    assert settled["completed_requests"] == 2
    assert settled["failed_requests"] == 0
    assert settled["not_attempted_requests"] == 1


@pytest.mark.asyncio
async def test_second_authentication_loss_pauses_for_human(tmp_path: Path) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=9)
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].search_failures.extend(
        [
            CollectorError(
                "authentication_lost",
                "expired",
                details={"platform": "douyin", "retryable": True},
            ),
            CollectorError(
                "authentication_lost",
                "still expired",
                details={"platform": "douyin", "retryable": True},
            ),
        ]
    )
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "auth_required"
    assert result.action_required is not None
    assert result.action_required.type == "auth_required"
    assert adapters[Platform.BILIBILI].search_count == 1
    assert adapters[Platform.XIAOHONGSHU].search_count == 0


@pytest.mark.asyncio
async def test_rendered_search_challenge_pauses_for_human(tmp_path: Path) -> None:
    workspace, session_id = _create_session(
        tmp_path,
        max_videos=9,
        add_douyin_query=True,
    )
    adapters = {platform: _Platform(platform) for platform in PLATFORM_ORDER}
    adapters[Platform.DOUYIN].search_failures.extend(
        [
            CollectorError(
                "challenge_required",
                "interactive verification required",
                details={"platform": "douyin", "retryable": True},
            ),
            CollectorError(
                "challenge_required",
                "verification still required",
                details={"platform": "douyin", "retryable": True},
            ),
        ]
    )
    progress = _RecordingProgress()
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        SessionRuntime(lease_ttl_seconds=900),
        progress=progress,
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "auth_required"
    assert result.action_required is not None
    assert result.action_required.type == "auth_required"
    assert result.action_required.target == "douyin"
    assert sum(adapter.resolve_count for adapter in adapters.values()) == 0
    assert sum(adapter.fetch_count for adapter in adapters.values()) == 0
    settled = next(
        details
        for event, details in progress.events
        if event == "search_plan_settled"
    )
    assert settled["requested_requests"] == 4
    assert settled["completed_requests"] == 1
    assert settled["failed_requests"] == 1
    assert settled["not_attempted_requests"] == 2


@pytest.mark.asyncio
async def test_approved_manual_review_is_downloaded_on_resume_without_new_round(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(tmp_path, max_videos=3)
    adapters = {
        Platform.BILIBILI: _Platform(
            Platform.BILIBILI,
            duration_seconds=20 * 60 + 1,
        ),
        Platform.DOUYIN: _Platform(
            Platform.DOUYIN,
            duration_seconds=20 * 60 + 2,
        ),
        Platform.XIAOHONGSHU: _Platform(
            Platform.XIAOHONGSHU,
            duration_seconds=20 * 60 + 3,
        ),
    }
    runtime = SessionRuntime(lease_ttl_seconds=900)
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        runtime,
    )
    first = await workflow.run(workspace, session_id)
    assert first.status == "decision_required"
    manifest = workflow._manifest.export(workspace, session_id)
    reviewed_id = next(
        unit.media_unit_id
        for candidate in manifest.candidates
        if candidate.platform == Platform.BILIBILI
        for unit in candidate.media_units
    )
    workflow._manifest.decide_review(
        workspace,
        session_id,
        reviewed_id,
        approved=True,
        actor="human",
    )

    resumed = await workflow.run(workspace, session_id)

    assert resumed.status == "integration_required"
    assert adapters[Platform.BILIBILI].fetch_count == 1
    assert sum(adapter.search_count for adapter in adapters.values()) == 2


@pytest.mark.asyncio
async def test_cancel_is_observed_between_serial_platform_requests(
    tmp_path: Path,
) -> None:
    workspace, session_id = _create_session(
        tmp_path,
        max_videos=9,
        add_bilibili_query=True,
    )
    runtime = SessionRuntime(lease_ttl_seconds=900)

    class _CancelAfterFirstSearch(_Platform):
        async def search(
            self,
            request: SearchRequest,
            context: PlatformContext,
        ) -> SearchBatch:
            batch = await super().search(request, context)
            if self.search_count == 1:
                runtime.request_cancel(workspace, session_id)
            return batch

    adapters = {
        Platform.BILIBILI: _CancelAfterFirstSearch(Platform.BILIBILI),
        Platform.DOUYIN: _Platform(Platform.DOUYIN),
        Platform.XIAOHONGSHU: _Platform(Platform.XIAOHONGSHU),
    }
    workflow = _make_workflow(
        _Authentication(),
        adapters,
        runtime,
    )

    result = await workflow.run(workspace, session_id)

    assert result.status == "cancelled"
    assert adapters[Platform.BILIBILI].search_count == 1
