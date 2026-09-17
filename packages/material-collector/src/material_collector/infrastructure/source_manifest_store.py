"""Transactional source records and atomic collection-result projection."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from material_collector.application.sessions import (
    COLLECTION_RESULT,
    CONTROL_DIRECTORY,
    SESSION_DATABASE,
    SESSIONS_DIRECTORY,
    SessionStore,
)
from material_collector.core.errors import ContractError, SessionStateError
from material_collector.core.manifest import (
    CollectionResult,
    DiscoveryLink,
    ManifestCandidate,
    ManifestMediaUnit,
    ReviewRecord,
    WorkGroupMember,
    WorkGroupRecord,
)
from material_collector.core.media import (
    AssetRecord,
    AuthorizationDisposition,
    DisplayGeometryAssessment,
    MediaQuality,
    NetworkRoute,
    NetworkRouteEvidence,
    Platform,
    ResolvedSource,
    SearchBatch,
)
from material_collector.infrastructure.session_store import SqliteSessionStore

MAX_AUTOMATIC_DURATION_SECONDS = 20 * 60
_STABLE_MEDIA_METADATA_KEYS: dict[Platform, frozenset[str]] = {
    Platform.BILIBILI: frozenset({"bvid", "cid"}),
    Platform.DOUYIN: frozenset({"aweme_id"}),
    Platform.XIAOHONGSHU: frozenset({"note_id"}),
    Platform.YOUTUBE: frozenset({"extractor"}),
    Platform.TIKTOK: frozenset({"extractor", "username"}),
}
_ALLOWED_HOSTS: dict[Platform, tuple[str, ...]] = {
    Platform.BILIBILI: ("bilibili.com", "b23.tv"),
    Platform.DOUYIN: ("douyin.com", "iesdouyin.com"),
    Platform.XIAOHONGSHU: ("xiaohongshu.com", "xhslink.com"),
    Platform.YOUTUBE: ("youtube.com", "youtu.be"),
    Platform.TIKTOK: ("tiktok.com",),
}
def _optional_model_json(value: BaseModel | None) -> str | None:
    return None if value is None else value.model_dump_json()


def _optional_model[ModelT: BaseModel](
    value: object, model: type[ModelT]
) -> ModelT | None:
    if value is None:
        return None
    return model.model_validate_json(str(value))


class SqliteSourceManifestStore:
    """SQLite/filesystem adapter for source state and JSON projection."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._sessions = session_store or SqliteSessionStore()

    def record_search_batch(
        self,
        workspace: Path,
        session_id: str,
        batch: SearchBatch,
    ) -> CollectionResult:
        session_view = self._sessions.get_session(workspace, session_id)
        if batch.platform not in session_view.platform_scope:
            raise ContractError(
                "A search batch platform is outside the frozen platform scope.",
                details={
                    "platform": batch.platform.value,
                    "platform_scope": [
                        platform.value for platform in session_view.platform_scope
                    ],
                },
            )
        database_path, session_dir = self._locations(workspace, session_id)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _configure_write(connection)
            _ensure_manifest_schema(connection)
            with connection:
                for candidate in batch.candidates:
                    if candidate.platform != batch.platform:
                        raise ContractError(
                            "A search batch contains a candidate from another platform.",
                            details={"candidate_id": candidate.candidate_id},
                        )
                    canonical_url = _canonical_source_url(
                        candidate.platform, candidate.canonical_url
                    )
                    connection.execute(
                        """
                        INSERT INTO candidate_source (
                            candidate_id,
                            platform,
                            source_id,
                            canonical_url,
                            title,
                            author,
                            description,
                            published_at,
                            duration_seconds,
                            network_route,
                            route_evidence_json,
                            yt_dlp_version,
                            authorization_disposition,
                            geometry_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(candidate_id) DO UPDATE SET
                            canonical_url = excluded.canonical_url,
                            title = excluded.title,
                            author = COALESCE(excluded.author, candidate_source.author),
                            description = COALESCE(
                                excluded.description,
                                candidate_source.description
                            ),
                            published_at = COALESCE(
                                excluded.published_at,
                                candidate_source.published_at
                            ),
                            duration_seconds = COALESCE(
                                excluded.duration_seconds,
                                candidate_source.duration_seconds
                            ),
                            network_route = excluded.network_route,
                            route_evidence_json = COALESCE(
                                excluded.route_evidence_json,
                                candidate_source.route_evidence_json
                            ),
                            yt_dlp_version = COALESCE(
                                excluded.yt_dlp_version,
                                candidate_source.yt_dlp_version
                            ),
                            authorization_disposition = excluded.authorization_disposition,
                            geometry_json = COALESCE(
                                excluded.geometry_json,
                                candidate_source.geometry_json
                            )
                        """,
                        (
                            candidate.candidate_id,
                            candidate.platform.value,
                            candidate.source_id,
                            canonical_url,
                            candidate.title,
                            candidate.author,
                            candidate.description,
                            candidate.published_at,
                            candidate.duration_seconds,
                            candidate.network_route,
                            _optional_model_json(candidate.route_evidence),
                            candidate.yt_dlp_version,
                            candidate.authorization_disposition,
                            _optional_model_json(candidate.geometry_assessment),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO source_discovery (
                            candidate_id,
                            query_plan_id,
                            segment_id,
                            query_id,
                            round_number,
                            rank
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(
                            candidate_id,
                            query_plan_id,
                            query_id,
                            round_number
                        ) DO UPDATE SET rank = MIN(rank, excluded.rank)
                        """,
                        (
                            candidate.candidate_id,
                            candidate.query_plan_id,
                            candidate.segment_id,
                            candidate.query_id,
                            candidate.round_number,
                            candidate.rank,
                        ),
                    )
        return self._publish(workspace, session_id, database_path, session_dir)

    def record_resolved_source(
        self,
        workspace: Path,
        session_id: str,
        resolved: ResolvedSource,
    ) -> CollectionResult:
        database_path, session_dir = self._locations(workspace, session_id)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _configure_write(connection)
            _ensure_manifest_schema(connection)
            candidate = connection.execute(
                "SELECT platform, source_id FROM candidate_source WHERE candidate_id = ?",
                (resolved.candidate_id,),
            ).fetchone()
            if candidate is None:
                raise ContractError(
                    "Resolved media references an unknown candidate.",
                    details={"candidate_id": resolved.candidate_id},
                )
            with connection:
                for media_unit in resolved.media_units:
                    if media_unit.platform.value != str(
                        candidate["platform"]
                    ) or media_unit.source_id != str(candidate["source_id"]):
                        raise ContractError(
                            "A resolved media unit does not belong to its candidate.",
                            details={"media_unit_id": media_unit.stable_id},
                        )
                    status = _initial_media_status(media_unit.duration_seconds)
                    connection.execute(
                        """
                        INSERT INTO media_unit (
                            media_unit_id,
                            candidate_id,
                            title,
                            canonical_url,
                            duration_seconds,
                            part_index,
                            metadata_json,
                            network_route,
                            route_evidence_json,
                            yt_dlp_version,
                            authorization_disposition,
                            geometry_json,
                            status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(media_unit_id) DO UPDATE SET
                            title = excluded.title,
                            canonical_url = excluded.canonical_url,
                            duration_seconds = COALESCE(
                                excluded.duration_seconds,
                                media_unit.duration_seconds
                            ),
                            part_index = COALESCE(
                                excluded.part_index,
                                media_unit.part_index
                            ),
                            metadata_json = excluded.metadata_json,
                            network_route = excluded.network_route,
                            route_evidence_json = COALESCE(
                                excluded.route_evidence_json,
                                media_unit.route_evidence_json
                            ),
                            yt_dlp_version = COALESCE(
                                excluded.yt_dlp_version,
                                media_unit.yt_dlp_version
                            ),
                            authorization_disposition = excluded.authorization_disposition,
                            geometry_json = COALESCE(
                                excluded.geometry_json,
                                media_unit.geometry_json
                            ),
                            status = CASE
                                WHEN media_unit.status IN (
                                    'proxy_ready',
                                    'high_quality_ready',
                                    'rejected'
                                ) THEN media_unit.status
                                ELSE excluded.status
                            END
                        """,
                        (
                            media_unit.stable_id,
                            resolved.candidate_id,
                            media_unit.title,
                            _canonical_source_url(media_unit.platform, media_unit.canonical_url),
                            media_unit.duration_seconds,
                            media_unit.part_index,
                            _encode_metadata(
                                media_unit.platform,
                                media_unit.metadata,
                            ),
                            media_unit.network_route,
                            _optional_model_json(media_unit.route_evidence),
                            media_unit.yt_dlp_version,
                            media_unit.authorization_disposition,
                            _optional_model_json(media_unit.geometry_assessment),
                            status,
                        ),
                    )
        return self._publish(workspace, session_id, database_path, session_dir)

    def record_asset(
        self,
        workspace: Path,
        session_id: str,
        asset: AssetRecord,
    ) -> CollectionResult:
        database_path, session_dir = self._locations(workspace, session_id)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _configure_write(connection)
            _ensure_manifest_schema(connection)
            exists = connection.execute(
                "SELECT 1 FROM media_unit WHERE media_unit_id = ?",
                (asset.media_unit_id,),
            ).fetchone()
            if exists is None:
                raise ContractError(
                    "An asset references an unknown media unit.",
                    details={"media_unit_id": asset.media_unit_id},
                )
            with connection:
                connection.execute(
                    """
                    INSERT INTO media_asset_ref (
                        media_unit_id,
                        quality,
                        asset_id,
                        sha256,
                        relative_path,
                        display_relative_path,
                        size_bytes,
                        container,
                        duration_seconds,
                        width,
                        height,
                        geometry_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(media_unit_id, quality) DO UPDATE SET
                        asset_id = excluded.asset_id,
                        sha256 = excluded.sha256,
                        relative_path = excluded.relative_path,
                        display_relative_path = excluded.display_relative_path,
                        size_bytes = excluded.size_bytes,
                        container = excluded.container,
                        duration_seconds = excluded.duration_seconds,
                        width = excluded.width,
                        height = excluded.height,
                        geometry_json = excluded.geometry_json
                    """,
                    (
                        asset.media_unit_id,
                        asset.quality.value,
                        asset.asset_id,
                        asset.sha256,
                        asset.relative_path,
                        asset.display_relative_path,
                        asset.size_bytes,
                        asset.container,
                        asset.duration_seconds,
                        asset.width,
                        asset.height,
                        _optional_model_json(asset.geometry_assessment),
                    ),
                )
                status = (
                    "proxy_ready"
                    if asset.quality == MediaQuality.LOW_PROXY
                    else "high_quality_ready"
                )
                connection.execute(
                    "UPDATE media_unit SET status = ? WHERE media_unit_id = ?",
                    (status, asset.media_unit_id),
                )
        return self._publish(workspace, session_id, database_path, session_dir)

    def decide_review(
        self,
        workspace: Path,
        session_id: str,
        media_unit_id: str,
        *,
        approved: bool,
        actor: str,
    ) -> CollectionResult:
        database_path, session_dir = self._locations(workspace, session_id)
        decision = "approved" if approved else "rejected"
        decided_at = _iso_utc(self._now())
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _configure_write(connection)
            _ensure_manifest_schema(connection)
            row = connection.execute(
                "SELECT status FROM media_unit WHERE media_unit_id = ?",
                (media_unit_id,),
            ).fetchone()
            if row is None:
                raise ContractError(
                    "The review decision references an unknown media unit.",
                    details={"media_unit_id": media_unit_id},
                )
            if str(row["status"]) not in {
                "manual_review_required",
                "duration_unknown",
                "approved",
                "rejected",
            }:
                raise ContractError(
                    "The media unit is not waiting for manual review.",
                    details={
                        "media_unit_id": media_unit_id,
                        "status": str(row["status"]),
                    },
                )
            with connection:
                connection.execute(
                    """
                    UPDATE media_unit
                    SET
                        status = ?,
                        review_decision = ?,
                        review_actor = ?,
                        review_decided_at = ?
                    WHERE media_unit_id = ?
                    """,
                    (decision, decision, actor, decided_at, media_unit_id),
                )
        return self._publish(workspace, session_id, database_path, session_dir)

    def export(self, workspace: Path, session_id: str) -> CollectionResult:
        database_path, session_dir = self._locations(workspace, session_id)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            _configure_write(connection)
            _ensure_manifest_schema(connection)
        return self._publish(workspace, session_id, database_path, session_dir)

    def replace_work_groups(
        self,
        workspace: Path,
        session_id: str,
        groups: tuple[WorkGroupRecord, ...],
    ) -> CollectionResult:
        database_path, session_dir = self._locations(workspace, session_id)
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            _configure_write(connection)
            _ensure_manifest_schema(connection)
            known_units = {
                str(row["media_unit_id"]): Platform(str(row["platform"]))
                for row in connection.execute(
                    """
                    SELECT media_unit.media_unit_id, candidate_source.platform
                    FROM media_unit
                    JOIN candidate_source USING (candidate_id)
                    """
                ).fetchall()
            }
            _validate_work_groups(groups, known_units)
            with connection:
                connection.execute("DELETE FROM work_group_member")
                connection.execute("DELETE FROM work_group")
                for group in groups:
                    connection.execute(
                        """
                        INSERT INTO work_group (
                            work_group_id,
                            schema_version,
                            status,
                            primary_media_unit_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            group.work_group_id,
                            group.schema_version,
                            group.status,
                            group.primary_media_unit_id,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO work_group_member (
                            work_group_id,
                            media_unit_id,
                            schema_version,
                            platform,
                            role,
                            fallback_order
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                group.work_group_id,
                                member.media_unit_id,
                                member.schema_version,
                                member.platform.value,
                                member.role,
                                member.fallback_order,
                            )
                            for member in group.members
                        ),
                    )
        return self._publish(workspace, session_id, database_path, session_dir)

    def _locations(self, workspace: Path, session_id: str) -> tuple[Path, Path]:
        view = self._sessions.get_session(workspace, session_id)
        normalized_workspace = Path(view.workspace_path)
        session_dir = normalized_workspace / CONTROL_DIRECTORY / SESSIONS_DIRECTORY / session_id
        return session_dir / SESSION_DATABASE, session_dir

    def _publish(
        self,
        workspace: Path,
        session_id: str,
        database_path: Path,
        session_dir: Path,
    ) -> CollectionResult:
        session_view = self._sessions.get_session(workspace, session_id)
        with closing(_connect_read_only(database_path)) as connection:
            _ensure_manifest_schema_read(connection)
            candidate_rows = connection.execute(
                """
                SELECT *
                FROM candidate_source
                ORDER BY
                    CASE platform
                        WHEN 'bilibili' THEN 1
                        WHEN 'douyin' THEN 2
                        WHEN 'xiaohongshu' THEN 3
                        ELSE 4
                    END,
                    candidate_id
                """
            ).fetchall()
            allowed_platforms = {
                platform.value for platform in session_view.platform_scope
            }
            out_of_scope_platforms = sorted(
                {
                    str(row["platform"])
                    for row in candidate_rows
                    if row["platform"] not in allowed_platforms
                }
            )
            if out_of_scope_platforms:
                raise SessionStateError(
                    "Stored candidates exceed the frozen platform scope.",
                    details={
                        "out_of_scope_platforms": out_of_scope_platforms,
                        "platform_scope": sorted(allowed_platforms),
                    },
                )
            candidates = tuple(_candidate_from_row(connection, row) for row in candidate_rows)
            work_groups = _work_groups_from_connection(connection)
        result = CollectionResult(
            session_id=session_id,
            workspace_path=session_view.workspace_path,
            platform_scope=session_view.platform_scope,
            session_status=session_view.status,
            state_version=session_view.state_version,
            generated_at=_iso_utc(self._now()),
            candidates=candidates,
            work_groups=work_groups,
        )
        _atomic_write_json(session_dir / COLLECTION_RESULT, result)
        return result


def _configure_write(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA busy_timeout = 5000")


def _ensure_manifest_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_source (
            candidate_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            source_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            description TEXT,
            published_at TEXT,
            duration_seconds REAL,
            network_route TEXT NOT NULL DEFAULT 'domestic_direct',
            route_evidence_json TEXT,
            yt_dlp_version TEXT,
            authorization_disposition TEXT NOT NULL DEFAULT 'authorized',
            geometry_json TEXT,
            UNIQUE(platform, source_id)
        );

        CREATE TABLE IF NOT EXISTS source_discovery (
            candidate_id TEXT NOT NULL REFERENCES candidate_source(candidate_id),
            query_plan_id TEXT NOT NULL,
            segment_id TEXT NOT NULL,
            query_id TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY(candidate_id, query_plan_id, query_id, round_number)
        );

        CREATE TABLE IF NOT EXISTS media_unit (
            media_unit_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidate_source(candidate_id),
            title TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            duration_seconds REAL,
            part_index INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            network_route TEXT NOT NULL DEFAULT 'domestic_direct',
            route_evidence_json TEXT,
            yt_dlp_version TEXT,
            authorization_disposition TEXT NOT NULL DEFAULT 'authorized',
            geometry_json TEXT,
            status TEXT NOT NULL,
            review_decision TEXT,
            review_actor TEXT,
            review_decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS media_asset_ref (
            media_unit_id TEXT NOT NULL REFERENCES media_unit(media_unit_id),
            quality TEXT NOT NULL,
            asset_id TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            display_relative_path TEXT,
            size_bytes INTEGER NOT NULL,
            container TEXT,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            geometry_json TEXT,
            PRIMARY KEY(media_unit_id, quality)
        );

        CREATE TABLE IF NOT EXISTS work_group (
            work_group_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            status TEXT NOT NULL,
            primary_media_unit_id TEXT NOT NULL REFERENCES media_unit(media_unit_id)
        );

        CREATE TABLE IF NOT EXISTS work_group_member (
            work_group_id TEXT NOT NULL
                REFERENCES work_group(work_group_id) ON DELETE CASCADE,
            media_unit_id TEXT NOT NULL
                REFERENCES media_unit(media_unit_id),
            schema_version TEXT NOT NULL,
            platform TEXT NOT NULL,
            role TEXT NOT NULL,
            fallback_order INTEGER NOT NULL,
            PRIMARY KEY(work_group_id, media_unit_id),
            UNIQUE(media_unit_id),
            UNIQUE(work_group_id, fallback_order)
        );
        """
    )
    media_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(media_unit)").fetchall()
    }
    if "metadata_json" not in media_columns:
        connection.execute(
            "ALTER TABLE media_unit ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
    _ensure_columns(
        connection,
        "candidate_source",
        {
            "network_route": "TEXT NOT NULL DEFAULT 'domestic_direct'",
            "route_evidence_json": "TEXT",
            "yt_dlp_version": "TEXT",
            "authorization_disposition": "TEXT NOT NULL DEFAULT 'authorized'",
            "geometry_json": "TEXT",
        },
    )
    _ensure_columns(
        connection,
        "media_unit",
        {
            "network_route": "TEXT NOT NULL DEFAULT 'domestic_direct'",
            "route_evidence_json": "TEXT",
            "yt_dlp_version": "TEXT",
            "authorization_disposition": "TEXT NOT NULL DEFAULT 'authorized'",
            "geometry_json": "TEXT",
        },
    )
    _ensure_columns(connection, "media_asset_ref", {"geometry_json": "TEXT"})
    connection.commit()


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: Mapping[str, str],
) -> None:
    present = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, declaration in columns.items():
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _ensure_manifest_schema_read(connection: sqlite3.Connection) -> None:
    required = {
        "candidate_source",
        "source_discovery",
        "media_unit",
        "media_asset_ref",
        "work_group",
        "work_group_member",
    }
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    present = {str(row["name"]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise SessionStateError(
            "The source manifest schema is incomplete.",
            details={"missing_tables": missing},
        )


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _candidate_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> ManifestCandidate:
    candidate_id = str(row["candidate_id"])
    discovery_rows = connection.execute(
        """
        SELECT query_plan_id, segment_id, query_id, round_number, rank
        FROM source_discovery
        WHERE candidate_id = ?
        ORDER BY round_number, query_plan_id, query_id
        """,
        (candidate_id,),
    ).fetchall()
    media_rows = connection.execute(
        """
        SELECT *
        FROM media_unit
        WHERE candidate_id = ?
        ORDER BY part_index, media_unit_id
        """,
        (candidate_id,),
    ).fetchall()
    return ManifestCandidate(
        candidate_id=candidate_id,
        platform=Platform(str(row["platform"])),
        source_id=str(row["source_id"]),
        canonical_url=str(row["canonical_url"]),
        title=str(row["title"]),
        author=None if row["author"] is None else str(row["author"]),
        description=None if row["description"] is None else str(row["description"]),
        published_at=None if row["published_at"] is None else str(row["published_at"]),
        duration_seconds=None
        if row["duration_seconds"] is None
        else float(row["duration_seconds"]),
        network_route=NetworkRoute(str(row["network_route"])),
        route_evidence=_optional_model(
            row["route_evidence_json"], NetworkRouteEvidence
        ),
        yt_dlp_version=None
        if row["yt_dlp_version"] is None
        else str(row["yt_dlp_version"]),
        authorization_disposition=AuthorizationDisposition(
            str(row["authorization_disposition"])
        ),
        geometry_assessment=_optional_model(
            row["geometry_json"], DisplayGeometryAssessment
        ),
        discoveries=tuple(
            DiscoveryLink(
                query_plan_id=str(discovery["query_plan_id"]),
                segment_id=str(discovery["segment_id"]),
                query_id=str(discovery["query_id"]),
                round_number=int(discovery["round_number"]),
                rank=int(discovery["rank"]),
            )
            for discovery in discovery_rows
        ),
        media_units=tuple(
            _media_unit_from_row(
                connection,
                media,
                platform=Platform(str(row["platform"])),
                source_id=str(row["source_id"]),
            )
            for media in media_rows
        ),
    )


def _media_unit_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    platform: Platform,
    source_id: str,
) -> ManifestMediaUnit:
    media_unit_id = str(row["media_unit_id"])
    asset_rows = connection.execute(
        "SELECT * FROM media_asset_ref WHERE media_unit_id = ?",
        (media_unit_id,),
    ).fetchall()
    assets = {MediaQuality(str(asset["quality"])): _asset_from_row(asset) for asset in asset_rows}
    proxy_asset = assets.get(MediaQuality.LOW_PROXY)
    status = str(row["status"])
    membership = connection.execute(
        """
        SELECT work_group_id, role, fallback_order
        FROM work_group_member
        WHERE media_unit_id = ?
        """,
        (media_unit_id,),
    ).fetchone()
    review = None
    if row["review_decision"] is not None:
        decision = cast(Literal["approved", "rejected"], str(row["review_decision"]))
        review = ReviewRecord(
            decision=decision,
            actor=str(row["review_actor"]),
            decided_at=str(row["review_decided_at"]),
        )
    return ManifestMediaUnit(
        media_unit_id=media_unit_id,
        title=str(row["title"]),
        canonical_url=str(row["canonical_url"]),
        duration_seconds=None
        if row["duration_seconds"] is None
        else float(row["duration_seconds"]),
        part_index=None if row["part_index"] is None else int(row["part_index"]),
        metadata=_stable_metadata_with_legacy_fallback(
            platform,
            source_id,
            media_unit_id,
            _decode_metadata(row["metadata_json"]),
        ),
        status=status,
        review=review,
        proxy_asset=proxy_asset,
        high_quality_asset=assets.get(MediaQuality.HIGH),
        work_group_id=None if membership is None else str(membership["work_group_id"]),
        source_role=None
        if membership is None
        else cast(
            Literal["primary", "fallback"],
            str(membership["role"]),
        ),
        fallback_order=None if membership is None else int(membership["fallback_order"]),
        eligible_for_understanding=(
            proxy_asset is not None
            and status
            not in {
                "rejected",
                "manual_review_required",
                "duration_unknown",
            }
            and (membership is None or str(membership["role"]) == "primary")
        ),
        network_route=NetworkRoute(str(row["network_route"])),
        route_evidence=_optional_model(
            row["route_evidence_json"], NetworkRouteEvidence
        ),
        yt_dlp_version=None
        if row["yt_dlp_version"] is None
        else str(row["yt_dlp_version"]),
        authorization_disposition=AuthorizationDisposition(
            str(row["authorization_disposition"])
        ),
        geometry_assessment=_optional_model(
            row["geometry_json"], DisplayGeometryAssessment
        ),
    )


def _work_groups_from_connection(
    connection: sqlite3.Connection,
) -> tuple[WorkGroupRecord, ...]:
    groups: list[WorkGroupRecord] = []
    group_rows = connection.execute("SELECT * FROM work_group ORDER BY work_group_id").fetchall()
    for group in group_rows:
        member_rows = connection.execute(
            """
            SELECT *
            FROM work_group_member
            WHERE work_group_id = ?
            ORDER BY fallback_order, media_unit_id
            """,
            (str(group["work_group_id"]),),
        ).fetchall()
        groups.append(
            WorkGroupRecord(
                schema_version=cast(
                    Literal["1.0"],
                    str(group["schema_version"]),
                ),
                work_group_id=str(group["work_group_id"]),
                status=cast(
                    Literal[
                        "confirmed_duplicate",
                        "independent",
                        "fingerprint_unavailable",
                    ],
                    str(group["status"]),
                ),
                primary_media_unit_id=str(group["primary_media_unit_id"]),
                members=tuple(
                    WorkGroupMember(
                        schema_version=cast(
                            Literal["1.0"],
                            str(member["schema_version"]),
                        ),
                        media_unit_id=str(member["media_unit_id"]),
                        platform=Platform(str(member["platform"])),
                        role=cast(
                            Literal["primary", "fallback"],
                            str(member["role"]),
                        ),
                        fallback_order=int(member["fallback_order"]),
                    )
                    for member in member_rows
                ),
            )
        )
    return tuple(groups)


def _validate_work_groups(
    groups: tuple[WorkGroupRecord, ...],
    known_units: Mapping[str, Platform],
) -> None:
    group_ids: set[str] = set()
    assigned_units: set[str] = set()
    for group in groups:
        if group.work_group_id in group_ids:
            raise ContractError(
                "A work-group snapshot contains duplicate group identifiers.",
                details={"work_group_id": group.work_group_id},
            )
        group_ids.add(group.work_group_id)
        for member in group.members:
            platform = known_units.get(member.media_unit_id)
            if platform is None:
                raise ContractError(
                    "A work-group member references an unknown media unit.",
                    details={"media_unit_id": member.media_unit_id},
                )
            if platform is not member.platform:
                raise ContractError(
                    "A work-group member platform does not match its media unit.",
                    details={"media_unit_id": member.media_unit_id},
                )
            if member.media_unit_id in assigned_units:
                raise ContractError(
                    "A media unit cannot belong to multiple work groups.",
                    details={"media_unit_id": member.media_unit_id},
                )
            assigned_units.add(member.media_unit_id)


def _asset_from_row(row: sqlite3.Row) -> AssetRecord:
    return AssetRecord(
        asset_id=str(row["asset_id"]),
        sha256=str(row["sha256"]),
        relative_path=str(row["relative_path"]),
        display_relative_path=None
        if row["display_relative_path"] is None
        else str(row["display_relative_path"]),
        size_bytes=int(row["size_bytes"]),
        quality=MediaQuality(str(row["quality"])),
        media_unit_id=str(row["media_unit_id"]),
        container=None if row["container"] is None else str(row["container"]),
        duration_seconds=None
        if row["duration_seconds"] is None
        else float(row["duration_seconds"]),
        width=None if row["width"] is None else int(row["width"]),
        height=None if row["height"] is None else int(row["height"]),
        geometry_assessment=_optional_model(
            row["geometry_json"], DisplayGeometryAssessment
        ),
    )


def _initial_media_status(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return "duration_unknown"
    if duration_seconds > MAX_AUTOMATIC_DURATION_SECONDS:
        return "manual_review_required"
    return "discovered"


def _encode_metadata(
    platform: Platform,
    value: Mapping[str, Any],
) -> str:
    stable_value = {
        key: value[key] for key in _STABLE_MEDIA_METADATA_KEYS[platform] if key in value
    }
    try:
        return json.dumps(
            stable_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise ContractError(
            "Media identity metadata must be JSON serializable.",
            details={"reason": str(error)},
        ) from error


def _decode_metadata(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise SessionStateError(
            "Persisted media identity metadata is invalid JSON.",
            details={"reason": str(error)},
        ) from error
    if not isinstance(decoded, dict):
        raise SessionStateError("Persisted media identity metadata must be a JSON object.")
    return {str(key): item for key, item in decoded.items()}


def _stable_metadata_with_legacy_fallback(
    platform: Platform,
    source_id: str,
    media_unit_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Recover stable identities for manifests created before metadata storage."""

    if metadata:
        return metadata
    if platform is Platform.BILIBILI:
        return {
            "bvid": source_id,
            "cid": media_unit_id.rsplit(":", maxsplit=1)[-1],
        }
    if platform is Platform.DOUYIN:
        return {"aweme_id": source_id}
    return {"note_id": source_id}


def _canonical_source_url(platform: Platform, value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ContractError(
            "A source URL must be a public HTTPS page URL.",
            details={"platform": platform.value},
        )
    hostname = parsed.hostname.lower()
    if not any(
        hostname == allowed or hostname.endswith(f".{allowed}")
        for allowed in _ALLOWED_HOSTS[platform]
    ):
        raise ContractError(
            "A source URL host does not match its platform.",
            details={"platform": platform.value, "hostname": hostname},
        )
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))


def _atomic_write_json(path: Path, model: CollectionResult) -> None:
    payload = (
        json.dumps(
            model.model_dump(mode="json", exclude_none=False),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".collection-result-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
