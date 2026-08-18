"""True-external seams used by the collection workflow."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from material_collector.core.fingerprints import FingerprintMatch
from material_collector.core.media import (
    AssetRecord,
    AuthenticationSelection,
    AuthProbe,
    BrowserChannel,
    FetchRequest,
    FetchResult,
    MediaUnit,
    Platform,
    PlatformContext,
    ResolvedSource,
    SearchBatch,
    SearchRequest,
    TitleViewPublication,
)


class AuthenticationGateway(Protocol):
    """Own persistent browser authentication without exposing credentials."""

    async def probe(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
    ) -> AuthProbe: ...

    async def ensure_authenticated(
        self,
        platforms: tuple[Platform, ...],
        auth_profile: str,
        wait_seconds: int,
        *,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
        progress: ProgressReporter | None = None,
    ) -> AuthenticationSelection: ...

    async def logout(
        self,
        platform: Platform,
        auth_profile: str,
        confirmation: str,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
    ) -> None: ...


class SearchProvider(Protocol):
    """Search one external platform and return normalized candidates."""

    platform: Platform

    async def search(
        self,
        request: SearchRequest,
        context: PlatformContext,
    ) -> SearchBatch: ...


class SearchBrowserSessions(Protocol):
    """Retain and close execution-scoped browser contexts used for search."""

    def search_execution(
        self,
        platforms: tuple[Platform, ...],
        context: PlatformContext,
    ) -> AbstractAsyncContextManager[None]: ...

    async def reset_search_platform(
        self,
        platform: Platform,
        context: PlatformContext,
    ) -> None: ...


class SourceResolver(Protocol):
    """Resolve one stable source into independently processable media units."""

    platform: Platform

    async def resolve(
        self,
        source_id: str,
        canonical_url: str,
        context: PlatformContext,
    ) -> ResolvedSource: ...


class MediaFetcher(Protocol):
    """Fetch one media unit into a caller-owned temporary destination."""

    platform: Platform

    async def fetch(
        self,
        request: FetchRequest,
        context: PlatformContext,
    ) -> FetchResult: ...


class AssetStore(Protocol):
    """Import and resolve durable workspace-owned media assets."""

    def import_fetch(self, fetched: FetchResult) -> AssetRecord: ...

    def publish_title_view(
        self,
        asset: AssetRecord,
        publication: TitleViewPublication,
    ) -> AssetRecord: ...

    def resolve(self, asset: AssetRecord) -> Path: ...

    def allocate_staging_path(
        self,
        session_id: str,
        media_unit_id: str,
        quality: str,
    ) -> Path: ...

    def discard_staging(self, path: Path) -> None: ...


class AssetStoreFactory(Protocol):
    """Select the asset store owned by one explicit material workspace."""

    def for_workspace(self, workspace: Path) -> AssetStore: ...


class MediaFingerprintService(Protocol):
    """Compare local proxy files without invoking a cloud model."""

    def compare(self, left: Path, right: Path) -> FingerprintMatch: ...


class CancellationProbe(Protocol):
    """Cooperative cancellation seam for long-running application modules."""

    def raise_if_cancelled(self) -> None: ...


class ProgressReporter(Protocol):
    """Receive versioned progress records without coupling to stderr or a UI."""

    def report(self, event: str, details: dict[str, object]) -> None: ...


class VideoUnderstandingGateway(Protocol):
    """Invoke an external video-understanding command or owned module."""

    async def understand(
        self,
        media_unit: MediaUnit,
        media_path: Path,
        output_path: Path,
    ) -> Path: ...


class IngestionGateway(Protocol):
    """Submit selected low-resolution segments to the external material library."""

    async def ingest(self, request_path: Path, output_path: Path) -> Path: ...
