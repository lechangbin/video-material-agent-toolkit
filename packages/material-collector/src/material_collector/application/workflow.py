"""Resumable first-version collection workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from material_collector.application.ports import (
    AssetStoreFactory,
    AuthenticationGateway,
    MediaFetcher,
    MediaFingerprintService,
    ProgressReporter,
    SearchBrowserSessions,
    SearchProvider,
    SourceResolver,
)
from material_collector.application.session_runtime import (
    CancellationRequestedError,
    ExecutionLease,
    SessionRuntime,
)
from material_collector.application.sessions import (
    CONTROL_DIRECTORY,
    SESSIONS_DIRECTORY,
    SessionApplication,
)
from material_collector.application.source_manifest import (
    MAX_AUTOMATIC_DURATION_SECONDS,
    SourceManifestApplication,
)
from material_collector.application.title_views import publish_and_record_title_view
from material_collector.core.contracts import QueryPlans
from material_collector.core.errors import CollectorError, SessionStateError
from material_collector.core.fingerprints import FingerprintMatch
from material_collector.core.manifest import (
    CollectionResult,
    ManifestCandidate,
    ManifestMediaUnit,
    WorkGroupMember,
    WorkGroupRecord,
)
from material_collector.core.media import (
    PLATFORM_ORDER,
    FetchRequest,
    GeometryDisposition,
    MediaQuality,
    MediaUnit,
    Platform,
    PlatformContext,
    SearchRequest,
    TitleViewPublication,
)

WORKFLOW_STAGES: tuple[str, ...] = (
    "authenticate",
    "search",
    "resolve",
    "download",
    "fingerprint",
    "understand_and_ingest",
)
_AUTH_ACCESS_ERROR_CODES = frozenset({"authentication_lost", "challenge_required"})
_OperationResult = TypeVar("_OperationResult")


class _WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkflowIssue(_WorkflowModel):
    stage: str
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class WorkflowAction(_WorkflowModel):
    actor: Literal["agent", "human"]
    type: str
    reason: str
    next_command: str | None = None
    target: str | None = None
    requested_artifacts: tuple[str, ...] = ()


class WorkflowSegmentSummary(_WorkflowModel):
    segment_id: str
    status: str
    candidates_found: int
    media_units_found: int
    proxies_ready: int


class WorkflowResult(_WorkflowModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    workspace_path: str
    platform_scope: tuple[Platform, ...]
    status: str
    result_path: str
    candidates_found: int
    media_units_found: int
    proxies_ready: int
    segments: tuple[WorkflowSegmentSummary, ...] = ()
    issues: tuple[WorkflowIssue, ...]
    action_required: WorkflowAction | None


@dataclass(frozen=True, slots=True)
class _FingerprintTarget:
    media_unit_id: str
    platform: Platform
    path: Path
    author: str | None
    title: str
    duration_seconds: float | None


class CollectionWorkflow:
    """Run real platform work behind one resumable application interface."""

    def __init__(
        self,
        *,
        authentication: AuthenticationGateway,
        search_providers: Mapping[Platform, SearchProvider],
        source_resolvers: Mapping[Platform, SourceResolver],
        media_fetchers: Mapping[Platform, MediaFetcher],
        sessions: SessionApplication,
        runtime: SessionRuntime,
        manifest: SourceManifestApplication,
        asset_stores: AssetStoreFactory,
        fingerprints: MediaFingerprintService,
        search_browser_sessions: SearchBrowserSessions | None = None,
        progress: ProgressReporter | None = None,
        lease_maintenance_interval_seconds: float = 5.0,
        platform_scope: tuple[Platform, ...] | None = None,
    ) -> None:
        if lease_maintenance_interval_seconds <= 0:
            raise ValueError("lease_maintenance_interval_seconds must be positive")
        self._authentication = authentication
        self._search_providers = dict(search_providers)
        self._source_resolvers = dict(source_resolvers)
        self._media_fetchers = dict(media_fetchers)
        self._sessions = sessions
        self._runtime = runtime
        self._manifest = manifest
        self._asset_stores = asset_stores
        self._fingerprints = fingerprints
        self._search_browser_sessions = search_browser_sessions
        self._progress = progress
        self._lease_maintenance_interval_seconds = lease_maintenance_interval_seconds
        required_platforms = platform_scope or tuple(self._search_providers)
        _require_platform_adapters(
            self._search_providers, "search provider", required_platforms
        )
        _require_platform_adapters(
            self._source_resolvers, "source resolver", required_platforms
        )
        _require_platform_adapters(
            self._media_fetchers, "media fetcher", required_platforms
        )

    async def run(
        self,
        workspace: Path,
        session_id: str,
        *,
        show_search_browsers: bool = False,
    ) -> WorkflowResult:
        """Continue one session until external integration or human review is needed."""

        session = self._sessions.get_session(workspace, session_id)
        normalized_workspace = Path(session.workspace_path)
        _collection_input, query_plans = self._sessions.load_frozen_contracts(
            normalized_workspace,
            session_id,
        )
        owner_id = f"worker_{uuid.uuid4().hex}"
        lease = self._runtime.begin_execution(
            normalized_workspace,
            session_id,
            owner_id=owner_id,
            stages=WORKFLOW_STAGES,
        )
        issues: list[WorkflowIssue] = []
        reauthenticated: set[Platform] = set()
        segment_ids = tuple(plan.segment_id for plan in query_plans.plans)
        lease_released = False

        try:
            lease = await self._run_authentication(
                lease,
                session.constraints.auth_wait_seconds,
                query_plans.platform_scope,
            )
            session = self._sessions.get_session(normalized_workspace, session_id)
            if session.selected_browser_channel is None:
                raise SessionStateError(
                    "Authentication completed without freezing a browser channel."
                )
            context = PlatformContext(
                auth_profile=session.constraints.auth_profile,
                browser_channel=session.selected_browser_channel,
                request_timeout_seconds=session.constraints.request_timeout_seconds,
                show_search_browser=show_search_browsers,
            )
            lease, search_issues = await self._run_search(
                lease,
                query_plans,
                context,
                wait_seconds=session.constraints.auth_wait_seconds,
                reauthenticated=reauthenticated,
            )
            issues.extend(search_issues)
            lease, resolve_issues = await self._run_resolution(
                lease,
                context,
                wait_seconds=session.constraints.auth_wait_seconds,
                reauthenticated=reauthenticated,
            )
            issues.extend(resolve_issues)
            lease, download_issues = await self._run_download(
                lease,
                context,
                max_rounds=session.constraints.max_rounds,
                max_videos=session.constraints.max_videos,
                query_plans=query_plans,
                wait_seconds=session.constraints.auth_wait_seconds,
                reauthenticated=reauthenticated,
            )
            issues.extend(download_issues)
            compensation_issues = await self._run_approved_compensation(
                lease,
                context,
                wait_seconds=session.constraints.auth_wait_seconds,
                reauthenticated=reauthenticated,
            )
            issues.extend(compensation_issues)
            lease, fingerprint_issues = await self._run_fingerprint(lease)
            issues.extend(fingerprint_issues)

            manifest = self._manifest.export(normalized_workspace, session_id)
            action = _next_action(manifest, normalized_workspace, session_id)
            checkpoint_status = (
                "integration_required"
                if action.type == "integration_required"
                else "decision_required"
            )
            self._runtime.pause_for_action(
                lease,
                status=checkpoint_status,
                action_required=action.model_dump(mode="json", exclude_none=False),
            )
            lease_released = True
            manifest = self._manifest.export(normalized_workspace, session_id)
            return _workflow_result(
                manifest,
                issues,
                action,
                status=checkpoint_status,
                segment_ids=segment_ids,
            )
        except CancellationRequestedError:
            self._runtime.acknowledge_cancel(lease)
            lease_released = True
            manifest = self._manifest.export(normalized_workspace, session_id)
            return _workflow_result(
                manifest,
                issues,
                None,
                status="cancelled",
                segment_ids=segment_ids,
            )
        except CollectorError as error:
            if error.code.startswith("auth_") or error.code in _AUTH_ACCESS_ERROR_CODES:
                action = WorkflowAction(
                    actor="human",
                    type="auth_required",
                    reason=error.message,
                    next_command=(
                        "material-collector resume "
                        f"--workspace {json.dumps(str(normalized_workspace))} "
                        f"--session-id {session_id}"
                    ),
                    target=error.details.get("platform", context.auth_profile),
                    requested_artifacts=("authenticated_profile",),
                )
                self._runtime.pause_for_action(
                    lease,
                    status="auth_required",
                    action_required=action.model_dump(
                        mode="json",
                        exclude_none=False,
                    ),
                )
                lease_released = True
                manifest = self._manifest.export(normalized_workspace, session_id)
                issues.append(_issue("authenticate", error))
                return _workflow_result(
                    manifest,
                    issues,
                    action,
                    status="auth_required",
                    segment_ids=segment_ids,
                )
            try:
                self._runtime.release_execution(lease)
                lease_released = True
            except CollectorError:
                pass
            raise
        finally:
            if not lease_released:
                try:
                    self._runtime.release_execution(lease)
                except CollectorError:
                    pass

    async def _run_authentication(
        self,
        lease: ExecutionLease,
        wait_seconds: int,
        platform_scope: tuple[Platform, ...],
    ) -> ExecutionLease:
        if lease.next_stage != "authenticate":
            return lease
        attempt = self._runtime.begin_stage(
            lease,
            stage_key="authenticate",
            operation_key=f"{lease.session_id}:authenticate:v1",
        )
        try:
            self._report("authentication_started", {"session_id": lease.session_id})
            session = self._sessions.get_session(lease.workspace, lease.session_id)
            requested_channel = (
                session.selected_browser_channel or session.constraints.browser_channel
            )
            lease, selection = await self._await_with_lease_maintenance(
                lease,
                self._authentication.ensure_authenticated(
                    platform_scope,
                    session.constraints.auth_profile,
                    wait_seconds,
                    browser_channel=requested_channel,
                    progress=self._progress,
                ),
            )
            self._sessions.freeze_browser_channel(
                lease.workspace,
                lease.session_id,
                selection.browser_channel,
            )
            next_stage = self._runtime.complete_stage(
                lease,
                attempt,
                result={
                    "platforms": [
                        {"platform": probe.platform.value, "status": probe.status.value}
                        for probe in selection.probes
                    ],
                    "browser_channel": selection.browser_channel.value,
                },
            )
            return _lease_with_next(lease, next_stage)
        except (CollectorError, OSError) as error:
            self._runtime.fail_stage(
                lease,
                attempt,
                error=_error_payload(error),
            )
            raise

    async def _run_search(
        self,
        lease: ExecutionLease,
        query_plans: QueryPlans,
        context: PlatformContext,
        *,
        wait_seconds: int,
        reauthenticated: set[Platform],
    ) -> tuple[ExecutionLease, tuple[WorkflowIssue, ...]]:
        if lease.next_stage != "search":
            return lease, _issues_from_stage_result(
                self._runtime.completed_operation_result(
                    lease.workspace,
                    lease.session_id,
                    f"{lease.session_id}:search:v1",
                )
            )
        if self._search_browser_sessions is not None:
            async with self._search_browser_sessions.search_execution(
                query_plans.platform_scope,
                context,
            ):
                return await self._run_search_active(
                    lease,
                    query_plans,
                    context,
                    wait_seconds=wait_seconds,
                    reauthenticated=reauthenticated,
                )
        return await self._run_search_active(
            lease,
            query_plans,
            context,
            wait_seconds=wait_seconds,
            reauthenticated=reauthenticated,
        )

    async def _run_search_active(
        self,
        lease: ExecutionLease,
        query_plans: QueryPlans,
        context: PlatformContext,
        *,
        wait_seconds: int,
        reauthenticated: set[Platform],
    ) -> tuple[ExecutionLease, tuple[WorkflowIssue, ...]]:
        attempt = self._runtime.begin_stage(
            lease,
            stage_key="search",
            operation_key=f"{lease.session_id}:search:v1",
        )
        issues: list[WorkflowIssue] = []
        completed_batches = 0
        try:
            for plan in query_plans.plans:
                self._raise_if_cancelled(lease)
                plan_issues: list[WorkflowIssue] = []
                plan_outcomes: dict[
                    Platform,
                    tuple[int, tuple[WorkflowIssue, ...]],
                ] = {}
                self._report(
                    "search_plan_started",
                    {
                        "session_id": lease.session_id,
                        "query_plan_id": plan.query_plan_id,
                    },
                )
                requests_by_platform: dict[Platform, list[SearchRequest]] = {
                    platform: [] for platform in PLATFORM_ORDER
                }
                for query in plan.initial_queries:
                    for platform in query.target_platforms:
                        request = SearchRequest(
                            query_plan_id=plan.query_plan_id,
                            segment_id=plan.segment_id,
                            query_id=query.query_id,
                            round_number=1,
                            text=query.text,
                            limit=query.budget,
                        )
                        requests_by_platform[platform].append(request)
                jobs = [
                    (
                        platform,
                        tuple(requests),
                        asyncio.create_task(
                            self._search_platform(
                                platform,
                                tuple(requests),
                                context,
                                lease,
                                authentication_retry=platform in reauthenticated,
                            )
                        ),
                    )
                    for platform, requests in requests_by_platform.items()
                    if requests
                ]
                lease, platform_results = await self._await_with_lease_maintenance(
                    lease,
                    asyncio.gather(
                        *(job[2] for job in jobs),
                        return_exceptions=True,
                    ),
                )
                pending_auth_retries: list[tuple[Platform, tuple[SearchRequest, ...]]] = []
                task_error: BaseException | None = None
                for (platform, requests, _task), result in zip(
                    jobs,
                    platform_results,
                    strict=True,
                ):
                    if isinstance(result, BaseException):
                        task_error = task_error or result
                        continue
                    platform_count, platform_issues = result
                    plan_outcomes[platform] = (platform_count, platform_issues)
                    issues.extend(platform_issues)
                    plan_issues.extend(platform_issues)
                    if platform not in reauthenticated and any(
                        issue.code in _AUTH_ACCESS_ERROR_CODES
                        for issue in platform_issues
                    ):
                        pending_auth_retries.append((platform, requests))
                if task_error is not None:
                    raise task_error
                for platform, requests in pending_auth_retries:
                    self._raise_if_cancelled(lease)
                    reauthenticated.add(platform)
                    if self._search_browser_sessions is not None:
                        await self._search_browser_sessions.reset_search_platform(
                            platform,
                            context,
                        )
                    lease, _probes = await self._await_with_lease_maintenance(
                        lease,
                        self._authentication.ensure_authenticated(
                            (platform,),
                            context.auth_profile,
                            wait_seconds,
                            browser_channel=context.browser_channel,
                            progress=self._progress,
                        ),
                    )
                    self._raise_if_cancelled(lease)
                    lease, retry_result = await self._await_with_lease_maintenance(
                        lease,
                        self._search_platform(
                            platform,
                            requests,
                            context,
                            lease,
                            authentication_retry=True,
                        ),
                    )
                    retry_count, retry_issues = retry_result
                    plan_outcomes[platform] = (retry_count, retry_issues)
                    issues = [
                        issue
                        for issue in issues
                        if not (
                            issue.code in _AUTH_ACCESS_ERROR_CODES
                            and issue.details.get("platform") == platform.value
                            and issue.details.get("reauthentication_attempted") is not True
                        )
                    ]
                    plan_issues = [
                        issue
                        for issue in plan_issues
                        if not (
                            issue.code in _AUTH_ACCESS_ERROR_CODES
                            and issue.details.get("platform") == platform.value
                            and issue.details.get("reauthentication_attempted") is not True
                        )
                    ]
                    issues.extend(retry_issues)
                    plan_issues.extend(retry_issues)
                lease = self._runtime.heartbeat(lease)
                requested_requests = sum(len(requests) for requests in requests_by_platform.values())
                completed_requests = sum(
                    count for count, _platform_issues in plan_outcomes.values()
                )
                failed_requests = sum(
                    len(platform_issues)
                    for _count, platform_issues in plan_outcomes.values()
                )
                not_attempted_requests = max(
                    0,
                    requested_requests - completed_requests - failed_requests,
                )
                completed_batches += completed_requests
                if not plan_issues:
                    self._report(
                        "search_plan_committed",
                        {
                            "session_id": lease.session_id,
                            "query_plan_id": plan.query_plan_id,
                            "requested_requests": requested_requests,
                            "completed_requests": completed_requests,
                            "failed_requests": 0,
                            "not_attempted_requests": not_attempted_requests,
                        },
                    )
                else:
                    self._report(
                        "search_plan_settled",
                        {
                            "session_id": lease.session_id,
                            "query_plan_id": plan.query_plan_id,
                            "status": "completed_with_issues",
                            "requested_requests": requested_requests,
                            "completed_requests": completed_requests,
                            "failed_requests": failed_requests,
                            "not_attempted_requests": not_attempted_requests,
                        },
                    )
            fatal_auth = _fatal_auth_issue(issues)
            if fatal_auth is not None:
                raise CollectorError(
                    fatal_auth.code,
                    fatal_auth.message,
                    details=fatal_auth.details,
                )
            closed_browser = next(
                (issue for issue in issues if issue.code == "search_browser_closed"),
                None,
            )
            if closed_browser is not None:
                raise CollectorError(
                    closed_browser.code,
                    closed_browser.message,
                    details=closed_browser.details,
                )
            retryable = [issue for issue in issues if _issue_is_retryable(issue)]
            if retryable and completed_batches == 0:
                raise _workflow_retryable_error(
                    "Every platform search remains retryable.",
                    retryable,
                )
            if completed_batches == 0:
                raise SessionStateError(
                    "Every platform search failed; no source batch was committed.",
                    details={"issues": [issue.model_dump(mode="json") for issue in issues]},
                )
            next_stage = self._runtime.complete_stage(
                lease,
                attempt,
                result={
                    "completed_batches": completed_batches,
                    "issues": [
                        issue.model_dump(mode="json")
                        for issue in issues
                    ],
                },
            )
            return _lease_with_next(lease, next_stage), tuple(issues)
        except (CollectorError, OSError) as error:
            self._runtime.fail_stage(lease, attempt, error=_error_payload(error))
            raise

    async def _run_resolution(
        self,
        lease: ExecutionLease,
        context: PlatformContext,
        *,
        wait_seconds: int,
        reauthenticated: set[Platform],
    ) -> tuple[ExecutionLease, tuple[WorkflowIssue, ...]]:
        if lease.next_stage != "resolve":
            return lease, ()
        attempt = self._runtime.begin_stage(
            lease,
            stage_key="resolve",
            operation_key=f"{lease.session_id}:resolve:v1",
        )
        issues: list[WorkflowIssue] = []
        resolved_count = 0
        try:
            manifest = self._manifest.export(lease.workspace, lease.session_id)
            self._report(
                "resolution_started",
                {
                    "session_id": lease.session_id,
                    "candidate_count": len(manifest.candidates),
                },
            )
            candidates_by_platform: dict[
                Platform,
                list[tuple[str, str]],
            ] = {platform: [] for platform in PLATFORM_ORDER}
            for candidate in manifest.candidates:
                if (
                    candidate.geometry_assessment is not None
                    and candidate.geometry_assessment.disposition
                    is GeometryDisposition.REJECTED
                ):
                    issues.append(
                        WorkflowIssue(
                            stage="geometry_prefilter",
                            code="source_geometry_ineligible",
                            message="Platform metadata excludes a non-16:9 source.",
                            details={
                                "platform": candidate.platform,
                                "candidate_id": candidate.candidate_id,
                                "geometry": candidate.geometry_assessment.model_dump(
                                    mode="json"
                                ),
                            },
                        )
                    )
                    continue
                if not candidate.media_units:
                    candidates_by_platform[candidate.platform].append(
                        (candidate.source_id, candidate.canonical_url)
                    )
            jobs = [
                (
                    platform,
                    tuple(candidates),
                    asyncio.create_task(
                        self._resolve_platform(
                            platform,
                            tuple(candidates),
                            context,
                            lease,
                            authentication_retry=platform in reauthenticated,
                        )
                    ),
                )
                for platform, candidates in candidates_by_platform.items()
                if candidates
            ]
            lease, platform_results = await self._await_with_lease_maintenance(
                lease,
                asyncio.gather(
                    *(job[2] for job in jobs),
                    return_exceptions=True,
                ),
            )
            pending_auth_retries: list[tuple[Platform, tuple[tuple[str, str], ...]]] = []
            task_error: BaseException | None = None
            for (platform, candidates, _task), result in zip(
                jobs,
                platform_results,
                strict=True,
            ):
                if isinstance(result, BaseException):
                    task_error = task_error or result
                    continue
                platform_count, platform_issues = result
                resolved_count += platform_count
                issues.extend(platform_issues)
                if platform not in reauthenticated and any(
                    issue.code in _AUTH_ACCESS_ERROR_CODES
                    for issue in platform_issues
                ):
                    pending_auth_retries.append((platform, candidates))
            if task_error is not None:
                raise task_error
            for platform, candidates in pending_auth_retries:
                self._raise_if_cancelled(lease)
                reauthenticated.add(platform)
                lease, _probes = await self._await_with_lease_maintenance(
                    lease,
                    self._authentication.ensure_authenticated(
                        (platform,),
                        context.auth_profile,
                        wait_seconds,
                        browser_channel=context.browser_channel,
                        progress=self._progress,
                    ),
                )
                self._raise_if_cancelled(lease)
                lease, retry_result = await self._await_with_lease_maintenance(
                    lease,
                    self._resolve_platform(
                        platform,
                        candidates,
                        context,
                        lease,
                        authentication_retry=True,
                    ),
                )
                retry_count, retry_issues = retry_result
                resolved_count += retry_count
                issues = [
                    issue
                    for issue in issues
                    if not (
                        issue.code in _AUTH_ACCESS_ERROR_CODES
                        and issue.details.get("platform") == platform.value
                        and issue.details.get("reauthentication_attempted") is not True
                    )
                ]
                issues.extend(retry_issues)
            fatal_auth = _fatal_auth_issue(issues)
            if fatal_auth is not None:
                raise CollectorError(
                    fatal_auth.code,
                    fatal_auth.message,
                    details=fatal_auth.details,
                )
            retryable = [issue for issue in issues if _issue_is_retryable(issue)]
            if retryable:
                raise _workflow_retryable_error(
                    "One or more source resolutions remain retryable.",
                    retryable,
                )
            next_stage = self._runtime.complete_stage(
                lease,
                attempt,
                result={"resolved_media_units": resolved_count},
            )
            return _lease_with_next(lease, next_stage), tuple(issues)
        except (CollectorError, OSError) as error:
            self._runtime.fail_stage(lease, attempt, error=_error_payload(error))
            raise

    async def _search_platform(
        self,
        platform: Platform,
        requests: tuple[SearchRequest, ...],
        context: PlatformContext,
        lease: ExecutionLease,
        *,
        authentication_retry: bool,
    ) -> tuple[int, tuple[WorkflowIssue, ...]]:
        """Serialize one browser profile while other platforms run concurrently."""

        completed = 0
        issues: list[WorkflowIssue] = []
        for request in requests:
            self._raise_if_cancelled(lease)
            operation = self._runtime.begin_operation(
                lease,
                stage_key="search",
                operation_key=_search_operation_key(lease.session_id, platform, request),
            )
            if operation.replayed:
                terminal_issue = _terminal_operation_issue("search", operation.result)
                if terminal_issue is None:
                    completed += 1
                else:
                    issues.append(terminal_issue)
                continue
            try:
                batch = await self._search_providers[platform].search(
                    request,
                    context,
                )
                self._manifest.record_search_batch(
                    lease.workspace,
                    lease.session_id,
                    batch,
                )
                self._runtime.complete_operation(
                    lease,
                    operation,
                    result={"candidate_count": len(batch.candidates)},
                )
                completed += 1
            except (CollectorError, OSError) as error:
                error = _mark_authentication_retry(
                    error,
                    authentication_retry,
                    platform,
                )
                issue = _issue("search", error)
                issues.append(issue)
                if isinstance(error, CollectorError) and not _error_should_retry(error):
                    self._runtime.complete_operation(
                        lease,
                        operation,
                        result={"terminal_error": _error_payload(error)},
                    )
                else:
                    self._runtime.fail_operation(
                        lease,
                        operation,
                        error=_error_payload(error),
                    )
                if isinstance(error, CollectorError) and (
                    error.code in _AUTH_ACCESS_ERROR_CODES
                    or error.code.startswith("auth_")
                    or error.code == "search_browser_closed"
                ):
                    break
        return completed, tuple(issues)

    async def _resolve_platform(
        self,
        platform: Platform,
        candidates: tuple[tuple[str, str], ...],
        context: PlatformContext,
        lease: ExecutionLease,
        *,
        authentication_retry: bool,
    ) -> tuple[int, tuple[WorkflowIssue, ...]]:
        """Resolve one platform serially to preserve its persistent profile lock."""

        completed = 0
        issues: list[WorkflowIssue] = []
        for source_id, canonical_url in candidates:
            self._raise_if_cancelled(lease)
            operation = self._runtime.begin_operation(
                lease,
                stage_key="resolve",
                operation_key=_resolve_operation_key(
                    lease.session_id,
                    platform,
                    source_id,
                ),
            )
            if operation.replayed:
                continue
            try:
                resolved = await self._source_resolvers[platform].resolve(
                    source_id,
                    canonical_url,
                    context,
                )
                self._manifest.record_resolved_source(
                    lease.workspace,
                    lease.session_id,
                    resolved,
                )
                self._runtime.complete_operation(
                    lease,
                    operation,
                    result={"media_unit_count": len(resolved.media_units)},
                )
                completed += len(resolved.media_units)
            except (CollectorError, OSError) as error:
                error = _mark_authentication_retry(
                    error,
                    authentication_retry,
                    platform,
                )
                issue = _issue("resolve", error)
                issues.append(issue)
                if isinstance(error, CollectorError) and not _error_should_retry(error):
                    self._runtime.complete_operation(
                        lease,
                        operation,
                        result={"terminal_error": _error_payload(error)},
                    )
                else:
                    self._runtime.fail_operation(
                        lease,
                        operation,
                        error=_error_payload(error),
                    )
                if isinstance(error, CollectorError) and (
                    error.code in _AUTH_ACCESS_ERROR_CODES
                    or error.code.startswith("auth_")
                ):
                    break
        return completed, tuple(issues)

    async def _run_download(
        self,
        lease: ExecutionLease,
        context: PlatformContext,
        *,
        max_rounds: int,
        max_videos: int,
        query_plans: QueryPlans,
        wait_seconds: int,
        reauthenticated: set[Platform],
    ) -> tuple[ExecutionLease, tuple[WorkflowIssue, ...]]:
        if lease.next_stage != "download":
            return lease, ()
        attempt = self._runtime.begin_stage(
            lease,
            stage_key="download",
            operation_key=f"{lease.session_id}:download:v1",
        )
        issues: list[WorkflowIssue] = []
        proxy_count = 0
        try:
            manifest = self._manifest.export(lease.workspace, lease.session_id)
            admission_per_plan = math.ceil(max_videos / max_rounds)
            admitted = _select_admitted_units(
                manifest,
                query_plans,
                admission_per_plan,
            )
            asset_store = self._asset_stores.for_workspace(lease.workspace)
            for platform, media_unit in admitted:
                self._raise_if_cancelled(lease)
                operation = self._runtime.begin_operation(
                    lease,
                    stage_key="download",
                    operation_key=_download_operation_key(
                        lease.session_id,
                        media_unit.stable_id,
                    ),
                )
                if operation.replayed:
                    proxy_count += 1
                    continue
                self._report(
                    "proxy_download_started",
                    {
                        "session_id": lease.session_id,
                        "platform": platform.value,
                        "media_unit_id": media_unit.stable_id,
                    },
                )
                destination = asset_store.allocate_staging_path(
                    lease.session_id,
                    media_unit.stable_id,
                    MediaQuality.LOW_PROXY.value,
                )
                try:
                    request = FetchRequest(
                        media_unit=media_unit,
                        quality=MediaQuality.LOW_PROXY,
                        destination=destination,
                    )
                    fetched = await self._with_reauthentication(
                        lease,
                        platform,
                        context,
                        wait_seconds,
                        reauthenticated,
                        partial(
                            self._media_fetchers[platform].fetch,
                            request,
                            context,
                        ),
                    )
                    asset = asset_store.import_fetch(fetched)
                    self._manifest.record_asset(
                        lease.workspace,
                        lease.session_id,
                        asset,
                    )
                    self._runtime.complete_operation(
                        lease,
                        operation,
                        result={
                            "asset_id": asset.asset_id,
                            "relative_path": asset.relative_path,
                        },
                    )
                    proxy_count += 1
                    self._report(
                        "proxy_download_committed",
                        {
                            "session_id": lease.session_id,
                            "platform": platform.value,
                            "media_unit_id": media_unit.stable_id,
                        },
                    )
                except (CollectorError, OSError) as error:
                    issue = _issue("download", error)
                    issues.append(issue)
                    if isinstance(error, CollectorError) and not _error_should_retry(error):
                        self._runtime.complete_operation(
                            lease,
                            operation,
                            result={"terminal_error": _error_payload(error)},
                        )
                    else:
                        self._runtime.fail_operation(
                            lease,
                            operation,
                            error=_error_payload(error),
                        )
                finally:
                    try:
                        asset_store.discard_staging(destination)
                    except CollectorError, OSError:
                        pass
                lease = self._runtime.heartbeat(lease)
            fatal_auth = _fatal_auth_issue(issues)
            if fatal_auth is not None:
                raise CollectorError(
                    fatal_auth.code,
                    fatal_auth.message,
                    details=fatal_auth.details,
                )
            retryable = [issue for issue in issues if _issue_is_retryable(issue)]
            if retryable:
                raise _workflow_retryable_error(
                    "One or more proxy downloads remain retryable.",
                    retryable,
                )
            next_stage = self._runtime.complete_stage(
                lease,
                attempt,
                result={"proxies_ready": proxy_count},
            )
            return _lease_with_next(lease, next_stage), tuple(issues)
        except (CollectorError, OSError) as error:
            self._runtime.fail_stage(lease, attempt, error=_error_payload(error))
            raise

    async def _run_approved_compensation(
        self,
        lease: ExecutionLease,
        context: PlatformContext,
        *,
        wait_seconds: int,
        reauthenticated: set[Platform],
    ) -> tuple[WorkflowIssue, ...]:
        """Download newly approved review items even after download-stage completion."""

        manifest = self._manifest.export(lease.workspace, lease.session_id)
        approved = [
            (candidate.platform, _media_from_manifest(candidate, unit))
            for candidate in manifest.candidates
            for unit in candidate.media_units
            if unit.status == "approved" and unit.proxy_asset is None
        ]
        if not approved:
            return ()
        asset_store = self._asset_stores.for_workspace(lease.workspace)
        issues: list[WorkflowIssue] = []
        for platform, media_unit in approved:
            self._raise_if_cancelled(lease)
            destination = asset_store.allocate_staging_path(
                lease.session_id,
                media_unit.stable_id,
                MediaQuality.LOW_PROXY.value,
            )
            try:
                request = FetchRequest(
                    media_unit=media_unit,
                    quality=MediaQuality.LOW_PROXY,
                    destination=destination,
                )
                fetched = await self._with_reauthentication(
                    lease,
                    platform,
                    context,
                    wait_seconds,
                    reauthenticated,
                    partial(
                        self._media_fetchers[platform].fetch,
                        request,
                        context,
                    ),
                )
                asset = asset_store.import_fetch(fetched)
                self._manifest.record_asset(
                    lease.workspace,
                    lease.session_id,
                    asset,
                )
            except (CollectorError, OSError) as error:
                issues.append(_issue("download", error))
            finally:
                try:
                    asset_store.discard_staging(destination)
                except CollectorError, OSError:
                    pass
            lease = self._runtime.heartbeat(lease)
        fatal_auth = _fatal_auth_issue(issues)
        if fatal_auth is not None:
            raise CollectorError(
                fatal_auth.code,
                fatal_auth.message,
                details=fatal_auth.details,
            )
        retryable = [issue for issue in issues if _issue_is_retryable(issue)]
        if retryable:
            raise _workflow_retryable_error(
                "One or more approved review downloads remain retryable.",
                retryable,
            )
        return tuple(issues)

    async def _run_fingerprint(
        self,
        lease: ExecutionLease,
    ) -> tuple[ExecutionLease, tuple[WorkflowIssue, ...]]:
        manifest = self._manifest.export(lease.workspace, lease.session_id)
        if lease.next_stage != "fingerprint":
            if _manifest_needs_fingerprint_refresh(manifest):
                groups, issues = await self._build_work_groups(
                    lease,
                    manifest,
                    persist_operations=False,
                )
                refreshed = self._manifest.replace_work_groups(
                    lease.workspace,
                    lease.session_id,
                    groups,
                )
                self._publish_primary_title_views(lease, refreshed)
                return lease, issues
            return lease, ()

        attempt = self._runtime.begin_stage(
            lease,
            stage_key="fingerprint",
            operation_key=f"{lease.session_id}:fingerprint:v1",
        )
        try:
            groups, issues = await self._build_work_groups(
                lease,
                manifest,
                persist_operations=True,
            )
            grouped = self._manifest.replace_work_groups(
                lease.workspace,
                lease.session_id,
                groups,
            )
            self._publish_primary_title_views(lease, grouped)
            next_stage = self._runtime.complete_stage(
                lease,
                attempt,
                result={
                    "work_group_count": len(groups),
                    "confirmed_duplicate_count": sum(
                        group.status == "confirmed_duplicate" for group in groups
                    ),
                },
            )
            return _lease_with_next(lease, next_stage), issues
        except (CollectorError, OSError) as error:
            self._runtime.fail_stage(lease, attempt, error=_error_payload(error))
            raise

    def _publish_primary_title_views(
        self,
        lease: ExecutionLease,
        manifest: CollectionResult,
    ) -> CollectionResult:
        asset_store = self._asset_stores.for_workspace(lease.workspace)
        current = manifest
        for candidate in manifest.candidates:
            for unit in candidate.media_units:
                for asset in (unit.proxy_asset, unit.high_quality_asset):
                    if asset is None:
                        continue
                    if unit.source_role == "primary":
                        published, recorded = publish_and_record_title_view(
                            manifest=self._manifest,
                            asset_store=asset_store,
                            workspace=lease.workspace,
                            asset=asset,
                            publication=TitleViewPublication(
                                session_id=lease.session_id,
                                platform=candidate.platform,
                                source_id=candidate.source_id,
                                source_title=candidate.title,
                                media_unit_title=unit.title,
                            ),
                        )
                        if recorded is not None:
                            current = recorded
                    else:
                        published = asset.model_copy(
                            update={"display_relative_path": None}
                        )
                    if unit.source_role != "primary" and published != asset:
                        current = self._manifest.record_asset(
                            lease.workspace,
                            lease.session_id,
                            published,
                        )
        return current

    async def _build_work_groups(
        self,
        lease: ExecutionLease,
        manifest: CollectionResult,
        *,
        persist_operations: bool,
    ) -> tuple[tuple[WorkGroupRecord, ...], tuple[WorkflowIssue, ...]]:
        asset_store = self._asset_stores.for_workspace(lease.workspace)
        targets = tuple(
            _FingerprintTarget(
                media_unit_id=unit.media_unit_id,
                platform=candidate.platform,
                path=asset_store.resolve(unit.proxy_asset),
                author=candidate.author,
                title=unit.title,
                duration_seconds=unit.duration_seconds,
            )
            for candidate in manifest.candidates
            for unit in candidate.media_units
            if unit.proxy_asset is not None
        )
        parents = {target.media_unit_id: target.media_unit_id for target in targets}
        unavailable: set[str] = set()
        issues: list[WorkflowIssue] = []
        for left_index, left in enumerate(targets):
            for right in targets[left_index + 1 :]:
                if left.platform == right.platform:
                    continue
                if not _is_suspected_cross_post(left, right):
                    continue
                self._raise_if_cancelled(lease)
                operation = None
                if persist_operations:
                    operation = self._runtime.begin_operation(
                        lease,
                        stage_key="fingerprint",
                        operation_key=_fingerprint_operation_key(
                            lease.session_id,
                            left.media_unit_id,
                            right.media_unit_id,
                        ),
                    )
                if operation is not None and operation.replayed:
                    comparison = FingerprintMatch.model_validate(operation.result)
                else:
                    try:
                        comparison = await asyncio.to_thread(
                            self._fingerprints.compare,
                            left.path,
                            right.path,
                        )
                    except (CollectorError, OSError) as error:
                        comparison = FingerprintMatch(
                            same_work=False,
                            reliable=False,
                            reason=(
                                error.code
                                if isinstance(error, CollectorError)
                                else type(error).__name__
                            ),
                        )
                        issues.append(_fingerprint_issue(left, right, error))
                    self._raise_if_cancelled(lease)
                    if operation is not None:
                        self._runtime.complete_operation(
                            lease,
                            operation,
                            result=comparison.model_dump(
                                mode="json",
                                exclude_none=False,
                            ),
                        )
                if comparison.reliable and comparison.same_work:
                    _union_work_groups(
                        parents,
                        left.media_unit_id,
                        right.media_unit_id,
                    )
                elif not comparison.reliable:
                    unavailable.update((left.media_unit_id, right.media_unit_id))
                    if not any(
                        issue.details.get("left_media_unit_id") == left.media_unit_id
                        and issue.details.get("right_media_unit_id") == right.media_unit_id
                        for issue in issues
                    ):
                        issues.append(
                            WorkflowIssue(
                                stage="fingerprint",
                                code="fingerprint_unavailable",
                                message=(
                                    "Local audio/video evidence could not "
                                    "reliably compare two proxies."
                                ),
                                details={
                                    "left_media_unit_id": left.media_unit_id,
                                    "right_media_unit_id": right.media_unit_id,
                                    "reason": comparison.reason,
                                    "retryable": False,
                                },
                            )
                        )
                lease = self._runtime.heartbeat(lease)
        return (
            _work_groups_from_components(targets, parents, unavailable),
            tuple(issues),
        )

    async def _with_reauthentication(
        self,
        lease: ExecutionLease,
        platform: Platform,
        context: PlatformContext,
        wait_seconds: int,
        reauthenticated: set[Platform],
        operation: Callable[[], Awaitable[_OperationResult]],
    ) -> _OperationResult:
        """Retry one platform call once after serial interactive reauthentication."""

        try:
            _maintained_lease, result = await self._await_with_lease_maintenance(
                lease,
                operation(),
            )
            return result
        except CollectorError as error:
            if error.code not in _AUTH_ACCESS_ERROR_CODES:
                raise
            if platform in reauthenticated:
                raise CollectorError(
                    error.code,
                    error.message,
                    details={
                        **error.details,
                        "reauthentication_attempted": True,
                    },
                ) from error
            reauthenticated.add(platform)
            self._raise_if_cancelled(lease)
            _maintained_lease, _probes = await self._await_with_lease_maintenance(
                lease,
                self._authentication.ensure_authenticated(
                    (platform,),
                    context.auth_profile,
                    wait_seconds,
                    browser_channel=context.browser_channel,
                    progress=self._progress,
                ),
            )
            self._raise_if_cancelled(lease)
            try:
                _maintained_lease, result = await self._await_with_lease_maintenance(
                    lease,
                    operation(),
                )
                return result
            except CollectorError as retried:
                if retried.code in _AUTH_ACCESS_ERROR_CODES:
                    raise CollectorError(
                        retried.code,
                        retried.message,
                        details={
                            **retried.details,
                            "reauthentication_attempted": True,
                        },
                    ) from retried
                raise

    def _raise_if_cancelled(self, lease: ExecutionLease) -> None:
        if self._runtime.cancellation_requested(lease):
            raise CancellationRequestedError(lease.session_id)

    async def _await_with_lease_maintenance(
        self,
        lease: ExecutionLease,
        operation: Awaitable[_OperationResult],
    ) -> tuple[ExecutionLease, _OperationResult]:
        task = asyncio.ensure_future(operation)
        try:
            while True:
                completed, _pending = await asyncio.wait(
                    {task},
                    timeout=self._lease_maintenance_interval_seconds,
                )
                if task in completed:
                    return lease, await task
                if self._runtime.cancellation_requested(lease):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise CancellationRequestedError(lease.session_id)
                lease = self._runtime.heartbeat(lease)
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

    def _report(self, event: str, details: dict[str, object]) -> None:
        if self._progress is not None:
            self._progress.report(event, details)


def _require_platform_adapters(
    adapters: Mapping[Platform, object],
    label: str,
    platforms: tuple[Platform, ...],
) -> None:
    missing = [platform.value for platform in platforms if platform not in adapters]
    if missing:
        raise ValueError(f"Missing {label} adapters for: {', '.join(missing)}")


def _lease_with_next(lease: ExecutionLease, next_stage: str | None) -> ExecutionLease:
    return ExecutionLease(
        workspace=lease.workspace,
        session_id=lease.session_id,
        owner_id=lease.owner_id,
        generation=lease.generation,
        expires_at=lease.expires_at,
        next_stage=next_stage,
    )


def _select_admitted_units(
    manifest: CollectionResult,
    query_plans: QueryPlans,
    admission_per_plan: int,
) -> tuple[tuple[Platform, MediaUnit], ...]:
    selected: list[tuple[Platform, MediaUnit]] = []
    selected_ids: set[str] = set()
    candidate_by_id = {candidate.candidate_id: candidate for candidate in manifest.candidates}
    for plan in query_plans.plans:
        candidate_ids = {
            candidate.candidate_id
            for candidate in manifest.candidates
            if any(link.query_plan_id == plan.query_plan_id for link in candidate.discoveries)
        }
        queues: dict[
            Platform,
            list[tuple[int, tuple[Platform, MediaUnit]]],
        ] = {platform: [] for platform in PLATFORM_ORDER}
        for candidate_id in candidate_ids:
            candidate = candidate_by_id[candidate_id]
            discovery_rank = min(
                link.rank
                for link in candidate.discoveries
                if link.query_plan_id == plan.query_plan_id
            )
            for unit in candidate.media_units:
                if (
                    unit.proxy_asset is not None
                    or unit.status
                    in {
                        "manual_review_required",
                        "duration_unknown",
                        "rejected",
                    }
                    or (
                        unit.status != "approved"
                        and (
                            unit.duration_seconds is None
                            or unit.duration_seconds > MAX_AUTOMATIC_DURATION_SECONDS
                        )
                    )
                ):
                    continue
                media_unit = _media_from_manifest(candidate, unit)
                queues[candidate.platform].append(
                    (
                        discovery_rank,
                        (candidate.platform, media_unit),
                    )
                )
        for platform in PLATFORM_ORDER:
            queues[platform].sort(key=lambda ranked: (ranked[0], ranked[1][1].stable_id))
        admitted_for_plan = 0
        while admitted_for_plan < admission_per_plan:
            made_progress = False
            for platform in PLATFORM_ORDER:
                while queues[platform]:
                    _, item = queues[platform].pop(0)
                    if item[1].stable_id in selected_ids:
                        continue
                    selected.append(item)
                    selected_ids.add(item[1].stable_id)
                    admitted_for_plan += 1
                    made_progress = True
                    break
                if admitted_for_plan >= admission_per_plan:
                    break
            if not made_progress:
                break
    return tuple(selected)


def _media_from_manifest(
    candidate: ManifestCandidate,
    unit: ManifestMediaUnit,
) -> MediaUnit:
    prefix = f"{candidate.platform.value}:"
    media_unit_id = unit.media_unit_id.removeprefix(prefix)
    return MediaUnit(
        platform=candidate.platform,
        source_id=candidate.source_id,
        media_unit_id=media_unit_id,
        canonical_url=unit.canonical_url,
        title=unit.title,
        duration_seconds=unit.duration_seconds,
        part_index=unit.part_index,
        metadata=unit.metadata,
    )


def _next_action(
    manifest: CollectionResult,
    workspace: Path,
    session_id: str,
) -> WorkflowAction:
    processable_primary = any(
        unit.proxy_asset is not None and unit.eligible_for_understanding
        for candidate in manifest.candidates
        for unit in candidate.media_units
    )
    pending_review = any(
        unit.status in {"manual_review_required", "duration_unknown"}
        for candidate in manifest.candidates
        for unit in candidate.media_units
    )
    command = (
        f"material-collector resume --workspace {json.dumps(str(workspace))} "
        f"--session-id {session_id}"
    )
    if pending_review and not processable_primary:
        return WorkflowAction(
            actor="human",
            type="manual_review_required",
            reason="One or more media units exceed the automatic duration boundary.",
            next_command=command,
            target="collection-result.json",
            requested_artifacts=("review_decisions",),
        )
    return WorkflowAction(
        actor="human",
        type="integration_required",
        reason=("External video-understanding and ingestion commands are not configured."),
        next_command=command,
        target="collection-result.json",
        requested_artifacts=("video_understanding_result", "ingestion_result"),
    )


def _workflow_result(
    manifest: CollectionResult,
    issues: list[WorkflowIssue],
    action: WorkflowAction | None,
    *,
    status: str | None = None,
    segment_ids: tuple[str, ...] = (),
) -> WorkflowResult:
    media_units = [unit for candidate in manifest.candidates for unit in candidate.media_units]
    discovered_segment_ids = {
        discovery.segment_id
        for candidate in manifest.candidates
        for discovery in candidate.discoveries
    }
    all_segment_ids = tuple(dict.fromkeys((*segment_ids, *sorted(discovered_segment_ids))))
    segments = tuple(_segment_summary(manifest, segment_id) for segment_id in all_segment_ids)
    result_path = (
        Path(manifest.workspace_path)
        / CONTROL_DIRECTORY
        / SESSIONS_DIRECTORY
        / manifest.session_id
        / "collection-result.json"
    )
    return WorkflowResult(
        session_id=manifest.session_id,
        workspace_path=manifest.workspace_path,
        platform_scope=manifest.platform_scope,
        status=status or (action.type if action else "completed"),
        result_path=str(result_path),
        candidates_found=len(manifest.candidates),
        media_units_found=len(media_units),
        proxies_ready=sum(unit.proxy_asset is not None for unit in media_units),
        segments=segments,
        issues=tuple(issues),
        action_required=action,
    )


def _issue(stage: str, error: BaseException) -> WorkflowIssue:
    if isinstance(error, CollectorError):
        return WorkflowIssue(
            stage=stage,
            code=error.code,
            message=error.message,
            details=error.details,
        )
    return WorkflowIssue(
        stage=stage,
        code="unexpected_error",
        message=str(error),
        details={
            "exception_type": type(error).__name__,
            "retryable": isinstance(error, OSError),
        },
    )


def _issues_from_stage_result(result: Any | None) -> tuple[WorkflowIssue, ...]:
    if not isinstance(result, Mapping):
        return ()
    raw_issues = result.get("issues")
    if not isinstance(raw_issues, list):
        return ()
    try:
        return tuple(WorkflowIssue.model_validate(issue) for issue in raw_issues)
    except (TypeError, ValueError) as error:
        raise SessionStateError(
            "The persisted search-stage issue list is invalid.",
        ) from error


def _terminal_operation_issue(
    stage: str,
    operation_result: Any | None,
) -> WorkflowIssue | None:
    if not isinstance(operation_result, Mapping):
        return None
    terminal_error = operation_result.get("terminal_error")
    if not isinstance(terminal_error, Mapping):
        return None
    try:
        return WorkflowIssue.model_validate(
            {
                **terminal_error,
                "stage": stage,
            }
        )
    except (TypeError, ValueError) as error:
        raise SessionStateError(
            "A persisted terminal operation result is invalid.",
            details={"stage": stage},
        ) from error


def _error_is_retryable(error: CollectorError) -> bool:
    return bool(error.details.get("retryable", False))


def _error_should_retry(error: CollectorError) -> bool:
    return (
        _error_is_retryable(error)
        or error.code.startswith("auth_")
        or error.code in _AUTH_ACCESS_ERROR_CODES
    )


def _issue_is_retryable(issue: WorkflowIssue) -> bool:
    return bool(issue.details.get("retryable", False))


def _workflow_retryable_error(
    message: str,
    issues: list[WorkflowIssue],
) -> CollectorError:
    return CollectorError(
        "workflow_retryable",
        message,
        details={
            "retryable": True,
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
    )


def _fatal_auth_issue(issues: list[WorkflowIssue]) -> WorkflowIssue | None:
    return next(
        (
            issue
            for issue in issues
            if issue.code.startswith("auth_")
            or (
                issue.code in _AUTH_ACCESS_ERROR_CODES
                and issue.details.get("reauthentication_attempted") is True
            )
        ),
        None,
    )


def _mark_authentication_retry(
    error: CollectorError | OSError,
    attempted: bool,
    platform: Platform,
) -> CollectorError | OSError:
    if not isinstance(error, CollectorError):
        return error
    details = {"platform": platform.value, **error.details}
    if attempted and error.code in _AUTH_ACCESS_ERROR_CODES:
        details["reauthentication_attempted"] = True
    if details != error.details:
        return CollectorError(
            error.code,
            error.message,
            details=details,
        )
    return error


def _stable_operation_key(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _search_operation_key(
    session_id: str,
    platform: Platform,
    request: SearchRequest,
) -> str:
    return _stable_operation_key(
        "search-item",
        session_id,
        platform.value,
        request.query_plan_id,
        request.query_id,
        str(request.round_number),
        request.text,
    )


def _resolve_operation_key(
    session_id: str,
    platform: Platform,
    source_id: str,
) -> str:
    return _stable_operation_key(
        "resolve-item",
        session_id,
        platform.value,
        source_id,
    )


def _download_operation_key(session_id: str, media_unit_id: str) -> str:
    return _stable_operation_key("download-item", session_id, media_unit_id)


def _fingerprint_operation_key(
    session_id: str,
    left_media_unit_id: str,
    right_media_unit_id: str,
) -> str:
    left, right = sorted((left_media_unit_id, right_media_unit_id))
    return _stable_operation_key("fingerprint-pair", session_id, left, right)


def _is_suspected_cross_post(
    left: _FingerprintTarget,
    right: _FingerprintTarget,
) -> bool:
    left_author = _normalized_match_text(left.author)
    right_author = _normalized_match_text(right.author)
    if not left_author or left_author != right_author:
        return False
    left_title = _normalized_match_text(left.title)
    right_title = _normalized_match_text(right.title)
    if not left_title or not right_title:
        return False
    if SequenceMatcher(None, left_title, right_title).ratio() < 0.72:
        return False
    if left.duration_seconds is None or right.duration_seconds is None:
        return False
    duration_tolerance = max(
        2.0,
        max(left.duration_seconds, right.duration_seconds) * 0.05,
    )
    return abs(left.duration_seconds - right.duration_seconds) <= duration_tolerance


def _normalized_match_text(value: str | None) -> str:
    if value is None:
        return ""
    return "".join(character for character in value.casefold() if character.isalnum())


def _fingerprint_issue(
    left: _FingerprintTarget,
    right: _FingerprintTarget,
    error: CollectorError | OSError,
) -> WorkflowIssue:
    issue = _issue("fingerprint", error)
    return issue.model_copy(
        update={
            "details": {
                **issue.details,
                "left_media_unit_id": left.media_unit_id,
                "right_media_unit_id": right.media_unit_id,
                "retryable": False,
            }
        }
    )


def _manifest_needs_fingerprint_refresh(manifest: CollectionResult) -> bool:
    return any(
        unit.proxy_asset is not None and unit.work_group_id is None
        for candidate in manifest.candidates
        for unit in candidate.media_units
    )


def _find_work_group(parents: dict[str, str], media_unit_id: str) -> str:
    parent = parents[media_unit_id]
    if parent != media_unit_id:
        parents[media_unit_id] = _find_work_group(parents, parent)
    return parents[media_unit_id]


def _union_work_groups(
    parents: dict[str, str],
    left: str,
    right: str,
) -> None:
    left_root = _find_work_group(parents, left)
    right_root = _find_work_group(parents, right)
    if left_root == right_root:
        return
    primary, secondary = sorted((left_root, right_root))
    parents[secondary] = primary


def _work_groups_from_components(
    targets: tuple[_FingerprintTarget, ...],
    parents: dict[str, str],
    unavailable: set[str],
) -> tuple[WorkGroupRecord, ...]:
    components: dict[str, list[_FingerprintTarget]] = {}
    for target in targets:
        root = _find_work_group(parents, target.media_unit_id)
        components.setdefault(root, []).append(target)
    platform_order = {platform: order for order, platform in enumerate(PLATFORM_ORDER)}
    groups: list[WorkGroupRecord] = []
    for component in components.values():
        ordered = sorted(
            component,
            key=lambda target: (
                platform_order[target.platform],
                target.media_unit_id,
            ),
        )
        member_ids = tuple(target.media_unit_id for target in ordered)
        status: Literal[
            "confirmed_duplicate",
            "independent",
            "fingerprint_unavailable",
        ]
        if len(ordered) > 1:
            status = "confirmed_duplicate"
        elif ordered[0].media_unit_id in unavailable:
            status = "fingerprint_unavailable"
        else:
            status = "independent"
        group_id = "work_" + hashlib.sha256("\0".join(sorted(member_ids)).encode()).hexdigest()[:24]
        groups.append(
            WorkGroupRecord(
                work_group_id=group_id,
                status=status,
                primary_media_unit_id=ordered[0].media_unit_id,
                members=tuple(
                    WorkGroupMember(
                        media_unit_id=target.media_unit_id,
                        platform=target.platform,
                        role="primary" if index == 1 else "fallback",
                        fallback_order=index,
                    )
                    for index, target in enumerate(ordered, start=1)
                ),
            )
        )
    return tuple(sorted(groups, key=lambda group: group.work_group_id))


def _segment_summary(
    manifest: CollectionResult,
    segment_id: str,
) -> WorkflowSegmentSummary:
    candidates = [
        candidate
        for candidate in manifest.candidates
        if any(discovery.segment_id == segment_id for discovery in candidate.discoveries)
    ]
    units = [unit for candidate in candidates for unit in candidate.media_units]
    pending_review = any(
        unit.status in {"manual_review_required", "duration_unknown"} for unit in units
    )
    if not candidates:
        summary_status = "empty"
    elif pending_review:
        summary_status = "decision_required"
    else:
        summary_status = "collected"
    return WorkflowSegmentSummary(
        segment_id=segment_id,
        status=summary_status,
        candidates_found=len(candidates),
        media_units_found=len(units),
        proxies_ready=sum(unit.proxy_asset is not None for unit in units),
    )


def _error_payload(error: BaseException) -> dict[str, Any]:
    issue = _issue("workflow", error)
    return issue.model_dump(mode="json")
