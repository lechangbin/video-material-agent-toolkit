"""SQLite and filesystem adapter for isolated collection sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from material_collector.application.sessions import (
    COLLECTION_INPUT_SNAPSHOT,
    COLLECTION_RESULT,
    CONTROL_DIRECTORY,
    QUERY_PLANS_SNAPSHOT,
    SESSION_DATABASE,
    SESSION_SCHEMA_VERSION,
    SESSIONS_DIRECTORY,
    SUPPORTED_SESSION_SCHEMA_VERSIONS,
    WORKSPACE_MARKER,
    CreateSessionRequest,
    RuntimeConstraints,
    SessionListView,
    SessionSegmentView,
    SessionSummary,
    SessionView,
    WarningView,
)
from material_collector.core.contracts import (
    CollectionInput,
    ContractWarning,
    QueryPlans,
    normalize_contracts,
)
from material_collector.core.errors import (
    ContractError,
    SessionNotFoundError,
    SessionStateError,
    SessionVersionError,
    WorkspaceError,
)
from material_collector.core.media import BrowserChannel

_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9_-]{1,80}$")


class SqliteSessionStore:
    """Filesystem and SQLite implementation of the session-store interface."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[datetime], str] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._session_id_factory = session_id_factory or _new_session_id

    def create_session(self, request: CreateSessionRequest) -> SessionView:
        """Validate both input documents and atomically create one session."""

        collection_data = _load_json_document(request.input_path, "collection_input")
        query_plan_data = _load_json_document(request.query_plans_path, "query_plans")
        contracts = normalize_contracts(collection_data, query_plan_data)

        workspace = _normalize_workspace(request.workspace, create=True)
        control_root = workspace / CONTROL_DIRECTORY
        sessions_root = control_root / SESSIONS_DIRECTORY
        sessions_root.mkdir(parents=True, exist_ok=True)
        _ensure_workspace_marker(control_root, self._now())

        created_at = _iso_utc(self._now())
        session_id = self._session_id_factory(self._now())
        _validate_session_id(session_id)
        final_session_dir = sessions_root / session_id
        if final_session_dir.exists():
            raise SessionStateError(
                "The generated session identifier already exists.",
                details={"session_id": session_id},
            )

        temporary_path = Path(
            tempfile.mkdtemp(prefix=".creating-", dir=str(sessions_root))
        ).resolve()
        try:
            input_bytes = _canonical_model_bytes(contracts.collection_input)
            plans_bytes = _canonical_model_bytes(contracts.query_plans)
            input_hash = hashlib.sha256(input_bytes).hexdigest()
            plans_hash = hashlib.sha256(plans_bytes).hexdigest()

            _write_new_file(
                temporary_path / COLLECTION_INPUT_SNAPSHOT,
                input_bytes,
            )
            _write_new_file(
                temporary_path / QUERY_PLANS_SNAPSHOT,
                plans_bytes,
            )
            _initialize_database(
                temporary_path / SESSION_DATABASE,
                session_id=session_id,
                created_at=created_at,
                input_sha256=input_hash,
                query_plans_sha256=plans_hash,
                constraints=request.constraints,
                collection_input=contracts.collection_input,
                query_plans=contracts.query_plans,
                warnings=contracts.warnings,
            )
            temporary_path.replace(final_session_dir)
        except Exception:
            _remove_temporary_session(temporary_path, sessions_root.resolve())
            raise

        return self.get_session(workspace, session_id)

    def load_frozen_contracts(
        self,
        workspace: Path,
        session_id: str,
    ) -> tuple[CollectionInput, QueryPlans]:
        """Read and hash-verify the immutable semantic session inputs."""

        session = self.get_session(workspace, session_id)
        normalized_workspace = Path(session.workspace_path)
        input_bytes = (
            normalized_workspace / Path(session.input_snapshot_path)
        ).read_bytes()
        plans_bytes = (
            normalized_workspace / Path(session.query_plans_snapshot_path)
        ).read_bytes()
        if hashlib.sha256(input_bytes).hexdigest() != session.input_sha256:
            raise SessionStateError(
                "The frozen collection input failed hash validation."
            )
        if (
            hashlib.sha256(plans_bytes).hexdigest()
            != session.query_plans_sha256
        ):
            raise SessionStateError(
                "The frozen QueryPlan file failed hash validation."
            )
        return (
            CollectionInput.model_validate_json(input_bytes),
            QueryPlans.model_validate_json(plans_bytes),
        )

    def freeze_browser_channel(
        self,
        workspace: Path,
        session_id: str,
        channel: BrowserChannel,
    ) -> SessionView:
        """Persist the first successful native browser and reject later switches."""

        if channel is BrowserChannel.AUTO:
            raise SessionStateError("The automatic browser channel cannot be frozen.")
        _validate_session_id(session_id)
        normalized_workspace = _normalize_workspace(workspace, create=False)
        database_path = _session_directory(normalized_workspace, session_id) / SESSION_DATABASE
        if not database_path.is_file():
            raise SessionNotFoundError(session_id)
        try:
            with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                _verify_database(connection, session_id)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT selected_browser_channel FROM session_state WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionStateError(
                        "The session database does not contain its expected session row.",
                        details={"session_id": session_id},
                    )
                selected = row["selected_browser_channel"]
                if selected is not None and selected != channel.value:
                    raise SessionStateError(
                        "The session browser channel is already frozen.",
                        details={
                            "session_id": session_id,
                            "selected_browser_channel": selected,
                        },
                    )
                if selected is None:
                    connection.execute(
                        """
                        UPDATE session_state
                        SET selected_browser_channel = ?, state_version = state_version + 1,
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (channel.value, _iso_utc(self._now()), session_id),
                    )
                connection.commit()
        except sqlite3.Error as error:
            raise SessionStateError(
                "The session browser channel could not be frozen.",
                details={"session_id": session_id},
            ) from error
        return self.get_session(normalized_workspace, session_id)

    def get_session(self, workspace: Path, session_id: str) -> SessionView:
        """Read one session without mutating its business state."""

        _validate_session_id(session_id)
        normalized_workspace = _normalize_workspace(workspace, create=False)
        session_dir = _session_directory(normalized_workspace, session_id)
        database_path = session_dir / SESSION_DATABASE
        if not database_path.is_file():
            raise SessionNotFoundError(session_id)

        try:
            with closing(_connect_read_only(database_path)) as connection:
                _verify_database(connection, session_id)
                row = connection.execute(
                    f"""
                    SELECT
                        session_id,
                        status,
                        state_version,
                        created_at,
                        updated_at,
                        input_snapshot_path,
                        input_sha256,
                        query_plans_snapshot_path,
                        query_plans_sha256,
                        max_rounds,
                        max_videos,
                        auth_profile,
                        auth_wait_seconds,
                        request_timeout_seconds,
                        browser_channel,
                        selected_browser_channel
                    FROM session_state
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionStateError(
                        "The session database does not contain its expected session row.",
                        details={"session_id": session_id},
                    )

                segment_rows = connection.execute(
                    """
                    SELECT segment_id, order_value, query_plan_id, status
                    FROM segment_state
                    ORDER BY order_value, segment_id
                    """
                ).fetchall()
                warning_rows = connection.execute(
                    """
                    SELECT code, message, details_json
                    FROM session_warning
                    ORDER BY sequence
                    """
                ).fetchall()
        except sqlite3.Error as error:
            raise SessionStateError(
                "The session database could not be read.",
                details={"session_id": session_id, "reason": str(error)},
            ) from error

        result_file = session_dir / COLLECTION_RESULT
        plans_path = normalized_workspace / Path(str(row["query_plans_snapshot_path"]))
        try:
            plans_bytes = plans_path.read_bytes()
        except OSError as error:
            raise SessionStateError(
                "The frozen QueryPlan file could not be read.",
                details={"session_id": session_id},
            ) from error
        if hashlib.sha256(plans_bytes).hexdigest() != str(row["query_plans_sha256"]):
            raise SessionStateError(
                "The frozen QueryPlan file failed hash validation.",
                details={"session_id": session_id},
            )
        try:
            platform_scope = QueryPlans.model_validate_json(plans_bytes).platform_scope
        except ValidationError as error:
            raise SessionStateError(
                "The frozen QueryPlan contract is unsupported.",
                details={"session_id": session_id},
            ) from error
        return SessionView(
            session_id=str(row["session_id"]),
            workspace_path=str(normalized_workspace),
            status=str(row["status"]),
            state_version=int(row["state_version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            input_snapshot_path=str(row["input_snapshot_path"]),
            input_sha256=str(row["input_sha256"]),
            query_plans_snapshot_path=str(row["query_plans_snapshot_path"]),
            query_plans_sha256=str(row["query_plans_sha256"]),
            platform_scope=platform_scope,
            constraints=RuntimeConstraints(
                max_rounds=int(row["max_rounds"]),
                max_videos=int(row["max_videos"]),
                auth_profile=str(row["auth_profile"]),
                auth_wait_seconds=int(row["auth_wait_seconds"]),
                request_timeout_seconds=int(row["request_timeout_seconds"]),
                browser_channel=BrowserChannel(str(row["browser_channel"])),
            ),
            selected_browser_channel=(
                BrowserChannel(str(row["selected_browser_channel"]))
                if row["selected_browser_channel"] is not None
                else None
            ),
            segments=tuple(
                SessionSegmentView(
                    segment_id=str(segment["segment_id"]),
                    order=int(segment["order_value"]),
                    query_plan_id=str(segment["query_plan_id"]),
                    status=str(segment["status"]),
                )
                for segment in segment_rows
            ),
            warnings=tuple(
                WarningView(
                    code=str(warning["code"]),
                    message=str(warning["message"]),
                    details=_load_details_json(str(warning["details_json"])),
                )
                for warning in warning_rows
            ),
            result_path=str(result_file.resolve()) if result_file.is_file() else None,
        )

    def list_sessions(self, workspace: Path) -> SessionListView:
        """Scan one explicit material workspace for readable sessions."""

        normalized_workspace = _normalize_workspace(workspace, create=False)
        sessions_root = (
            normalized_workspace / CONTROL_DIRECTORY / SESSIONS_DIRECTORY
        )
        if not sessions_root.exists():
            return SessionListView(
                workspace_path=str(normalized_workspace),
                sessions=(),
            )
        if not sessions_root.is_dir():
            raise WorkspaceError(
                "The material workspace session root is not a directory.",
                details={"path": str(sessions_root)},
            )

        summaries: list[SessionSummary] = []
        for child in sorted(sessions_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir() or not _SESSION_ID.fullmatch(child.name):
                continue
            view = self.get_session(normalized_workspace, child.name)
            summaries.append(
                SessionSummary(
                    session_id=view.session_id,
                    status=view.status,
                    state_version=view.state_version,
                    created_at=view.created_at,
                    updated_at=view.updated_at,
                    segment_count=len(view.segments),
                )
            )

        summaries.sort(key=lambda item: (item.created_at, item.session_id), reverse=True)
        return SessionListView(
            workspace_path=str(normalized_workspace),
            sessions=tuple(summaries),
        )


def _new_session_id(now: datetime) -> str:
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ses_{timestamp}_{uuid.uuid4().hex[:12]}"


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID.fullmatch(session_id):
        raise ContractError(
            "session_id contains unsafe characters.",
            details={"session_id": session_id},
        )


def _normalize_workspace(workspace: Path, *, create: bool) -> Path:
    try:
        normalized = workspace.expanduser().resolve(strict=False)
    except OSError as error:
        raise WorkspaceError(
            "The material workspace path cannot be resolved.",
            details={"path": str(workspace), "reason": str(error)},
        ) from error

    if normalized.exists() and not normalized.is_dir():
        raise WorkspaceError(
            "The material workspace path is not a directory.",
            details={"path": str(normalized)},
        )
    if create:
        try:
            normalized.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise WorkspaceError(
                "The material workspace directory cannot be created.",
                details={"path": str(normalized), "reason": str(error)},
            ) from error
    elif not normalized.is_dir():
        raise WorkspaceError(
            "The material workspace directory does not exist.",
            details={"path": str(normalized)},
        )
    return normalized


def _session_directory(workspace: Path, session_id: str) -> Path:
    sessions_root = (workspace / CONTROL_DIRECTORY / SESSIONS_DIRECTORY).resolve()
    candidate = (sessions_root / session_id).resolve()
    if candidate.parent != sessions_root:
        raise ContractError(
            "session_id resolves outside the material workspace.",
            details={"session_id": session_id},
        )
    return candidate


def _load_json_document(path: Path, document_name: str) -> Any:
    try:
        raw = path.expanduser().resolve(strict=True).read_bytes()
    except (OSError, RuntimeError) as error:
        raise ContractError(
            f"{document_name} cannot be read.",
            details={"path": str(path), "reason": str(error)},
        ) from error
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ContractError(
            f"{document_name} must be UTF-8 JSON.",
            details={"path": str(path), "reason": str(error)},
        ) from error

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(
                    f"{document_name} contains a duplicate JSON object key.",
                    details={"path": str(path), "key": key},
                )
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except ContractError:
        raise
    except json.JSONDecodeError as error:
        raise ContractError(
            f"{document_name} is not valid JSON.",
            details={
                "path": str(path),
                "line": error.lineno,
                "column": error.colno,
                "reason": error.msg,
            },
        ) from error


def _canonical_model_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude_none=True)
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _ensure_workspace_marker(control_root: Path, now: datetime) -> None:
    control_root.mkdir(parents=True, exist_ok=True)
    marker_path = control_root / WORKSPACE_MARKER
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError(
                "The material workspace marker cannot be read.",
                details={"path": str(marker_path), "reason": str(error)},
            ) from error
        if not isinstance(marker, dict) or marker.get("schema_version") != "1.0":
            raise WorkspaceError(
                "The material workspace marker has an unsupported schema.",
                details={"path": str(marker_path)},
            )
        return

    marker = {
        "schema_version": "1.0",
        "workspace_id": f"ws_{uuid.uuid4().hex}",
        "created_at": _iso_utc(now),
    }
    marker_bytes = (
        json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        _write_new_file(marker_path, marker_bytes)
    except FileExistsError:
        # A concurrent creator won the race. The next read validates its marker.
        _ensure_workspace_marker(control_root, now)


def _initialize_database(
    database_path: Path,
    *,
    session_id: str,
    created_at: str,
    input_sha256: str,
    query_plans_sha256: str,
    constraints: RuntimeConstraints,
    collection_input: CollectionInput,
    query_plans: QueryPlans,
    warnings: tuple[ContractWarning, ...],
) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(database_path, timeout=5.0)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(
                """
                CREATE TABLE schema_info (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL
                );

                CREATE TABLE session_state (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    state_version INTEGER NOT NULL CHECK (state_version >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    input_snapshot_path TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    query_plans_snapshot_path TEXT NOT NULL,
                    query_plans_sha256 TEXT NOT NULL,
                    max_rounds INTEGER NOT NULL CHECK (max_rounds >= 1),
                    max_videos INTEGER NOT NULL CHECK (max_videos >= 1),
                    auth_profile TEXT NOT NULL,
                    auth_wait_seconds INTEGER NOT NULL CHECK (auth_wait_seconds >= 1),
                    request_timeout_seconds INTEGER NOT NULL
                        CHECK (request_timeout_seconds >= 1),
                    browser_channel TEXT NOT NULL
                        CHECK (browser_channel IN ('auto', 'edge', 'chrome')),
                    selected_browser_channel TEXT
                        CHECK (selected_browser_channel IN ('edge', 'chrome'))
                );

                CREATE TABLE segment_state (
                    segment_id TEXT PRIMARY KEY,
                    order_value INTEGER NOT NULL UNIQUE,
                    query_plan_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL
                );

                CREATE TABLE session_warning (
                    sequence INTEGER PRIMARY KEY,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE TABLE runtime_schema_info (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL
                );

                CREATE TABLE execution_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE runtime_control (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (cancel_requested IN (0, 1)),
                    cancel_requested_at TEXT,
                    action_required_json TEXT
                );

                CREATE TABLE stage_state (
                    stage_key TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 1),
                    status TEXT NOT NULL
                        CHECK (status IN ('pending', 'running', 'completed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0
                        CHECK (attempt_count >= 0),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE stage_attempt (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage_key TEXT NOT NULL REFERENCES stage_state(stage_key),
                    operation_key TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
                    status TEXT NOT NULL
                        CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT,
                    error_json TEXT
                );

                CREATE TABLE operation_record (
                    operation_key TEXT PRIMARY KEY,
                    stage_key TEXT NOT NULL REFERENCES stage_state(stage_key),
                    status TEXT NOT NULL
                        CHECK (status IN ('running', 'completed', 'interrupted', 'failed')),
                    latest_attempt_id INTEGER NOT NULL
                        REFERENCES stage_attempt(attempt_id),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO schema_info (singleton, schema_version) VALUES (1, ?)",
                (SESSION_SCHEMA_VERSION,),
            )
            connection.execute(
                "INSERT INTO runtime_schema_info (singleton, schema_version) VALUES (1, 1)"
            )
            connection.execute(
                "INSERT INTO runtime_control (singleton, cancel_requested) VALUES (1, 0)"
            )
            connection.execute(
                """
                INSERT INTO session_state (
                    session_id,
                    status,
                    state_version,
                    created_at,
                    updated_at,
                    input_snapshot_path,
                    input_sha256,
                    query_plans_snapshot_path,
                    query_plans_sha256,
                    max_rounds,
                    max_videos,
                    auth_profile,
                    auth_wait_seconds,
                    request_timeout_seconds,
                    browser_channel,
                    selected_browser_channel
                ) VALUES (?, 'initialized', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session_id,
                    created_at,
                    created_at,
                    _session_relative_path(session_id, COLLECTION_INPUT_SNAPSHOT),
                    input_sha256,
                    _session_relative_path(session_id, QUERY_PLANS_SNAPSHOT),
                    query_plans_sha256,
                    constraints.max_rounds,
                    constraints.max_videos,
                    constraints.auth_profile,
                    constraints.auth_wait_seconds,
                    constraints.request_timeout_seconds,
                    constraints.browser_channel.value,
                ),
            )
            plan_ids = {
                plan.segment_id: plan.query_plan_id for plan in query_plans.plans
            }
            connection.executemany(
                """
                INSERT INTO segment_state (
                    segment_id,
                    order_value,
                    query_plan_id,
                    status
                ) VALUES (?, ?, ?, 'planned')
                """,
                (
                    (segment.segment_id, segment.order, plan_ids[segment.segment_id])
                    for segment in collection_input.segments
                ),
            )
            connection.executemany(
                """
                INSERT INTO session_warning (
                    sequence,
                    code,
                    message,
                    details_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        sequence,
                        warning.code,
                        warning.message,
                        json.dumps(
                            warning.details,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    for sequence, warning in enumerate(warnings, start=1)
                ),
            )
            connection.execute(f"PRAGMA user_version = {SESSION_SCHEMA_VERSION}")
            connection.commit()
    except sqlite3.Error as error:
        raise SessionStateError(
            "The session database could not be initialized.",
            details={"reason": str(error)},
        ) from error


def _session_relative_path(session_id: str, child: str) -> str:
    return f"{CONTROL_DIRECTORY}/{SESSIONS_DIRECTORY}/{session_id}/{child}"


def _remove_temporary_session(temporary_path: Path, sessions_root: Path) -> None:
    try:
        resolved = temporary_path.resolve()
    except OSError:
        return
    if (
        resolved.parent == sessions_root
        and resolved.name.startswith(".creating-")
        and resolved.is_dir()
    ):
        shutil.rmtree(resolved)


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _verify_database(connection: sqlite3.Connection, session_id: str) -> int:
    integrity = connection.execute("PRAGMA quick_check").fetchone()
    if integrity is None or str(integrity[0]) != "ok":
        raise SessionStateError(
            "The session database failed its integrity check.",
            details={"session_id": session_id},
        )
    schema_row = connection.execute(
        "SELECT schema_version FROM schema_info WHERE singleton = 1"
    ).fetchone()
    if schema_row is None:
        raise SessionStateError(
            "The session database schema is unsupported.",
            details={"session_id": session_id},
        )
    schema_version = int(schema_row["schema_version"])
    if schema_version not in SUPPORTED_SESSION_SCHEMA_VERSIONS:
        raise SessionVersionError(
            session_id,
            schema_version,
            *sorted(SUPPORTED_SESSION_SCHEMA_VERSIONS),
        )
    return schema_version


def _load_details_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise SessionStateError(
            "A stored session warning contains invalid JSON.",
            details={"reason": str(error)},
        ) from error
    if not isinstance(parsed, dict):
        raise SessionStateError("A stored session warning must contain a JSON object.")
    return parsed
