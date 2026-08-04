"""Application interface for durable source records and result projection."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from material_collector.core.manifest import CollectionResult, WorkGroupRecord
from material_collector.core.media import AssetRecord, ResolvedSource, SearchBatch

MAX_AUTOMATIC_DURATION_SECONDS = 20 * 60


class SourceManifestStore(Protocol):
    """Persistence seam for the source manifest module."""

    def record_search_batch(
        self,
        workspace: Path,
        session_id: str,
        batch: SearchBatch,
    ) -> CollectionResult: ...

    def record_resolved_source(
        self,
        workspace: Path,
        session_id: str,
        resolved: ResolvedSource,
    ) -> CollectionResult: ...

    def record_asset(
        self,
        workspace: Path,
        session_id: str,
        asset: AssetRecord,
    ) -> CollectionResult: ...

    def decide_review(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
        *,
        approved: bool,
        actor: str,
    ) -> CollectionResult: ...

    def export(self, workspace: Path, session_id: str) -> CollectionResult: ...

    def replace_work_groups(
        self,
        workspace: Path,
        session_id: str,
        groups: tuple[WorkGroupRecord, ...],
    ) -> CollectionResult: ...


class SourceManifestApplication:
    """Stable application interface hiding SQLite and filesystem mechanics."""

    def __init__(
        self,
        *,
        store: SourceManifestStore,
    ) -> None:
        self._store = store

    def record_search_batch(
        self,
        workspace: Path,
        session_id: str,
        batch: SearchBatch,
    ) -> CollectionResult:
        return self._store.record_search_batch(workspace, session_id, batch)

    def record_resolved_source(
        self,
        workspace: Path,
        session_id: str,
        resolved: ResolvedSource,
    ) -> CollectionResult:
        return self._store.record_resolved_source(workspace, session_id, resolved)

    def record_asset(
        self,
        workspace: Path,
        session_id: str,
        asset: AssetRecord,
    ) -> CollectionResult:
        return self._store.record_asset(workspace, session_id, asset)

    def decide_review(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
        *,
        approved: bool,
        actor: str,
    ) -> CollectionResult:
        return self._store.decide_review(
            workspace,
            session_id,
            media_unit_id,
            approved=approved,
            actor=actor,
        )

    def export(self, workspace: Path, session_id: str) -> CollectionResult:
        return self._store.export(workspace, session_id)

    def replace_work_groups(
        self,
        workspace: Path,
        session_id: str,
        groups: tuple[WorkGroupRecord, ...],
    ) -> CollectionResult:
        """Atomically replace the session's complete work-group snapshot."""

        return self._store.replace_work_groups(workspace, session_id, groups)
