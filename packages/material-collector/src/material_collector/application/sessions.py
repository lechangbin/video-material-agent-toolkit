"""Application interface for creating and inspecting collection sessions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from material_collector.core.contracts import (
    CollectionInput,
    QueryPlans,
    normalize_inline_text,
)
from material_collector.core.errors import SessionStateError
from material_collector.core.media import BrowserChannel, Platform

OUTPUT_SCHEMA_VERSION = "1.0"
SESSION_SCHEMA_VERSION = 5
SUPPORTED_SESSION_SCHEMA_VERSIONS = frozenset({SESSION_SCHEMA_VERSION})
CONTROL_DIRECTORY = ".material-collector"
SESSIONS_DIRECTORY = "sessions"
WORKSPACE_MARKER = "workspace.json"
SESSION_DATABASE = "session.sqlite3"
COLLECTION_INPUT_SNAPSHOT = "input/collection-input.json"
QUERY_PLANS_SNAPSHOT = "input/query-plans.json"
COLLECTION_RESULT = "collection-result.json"

_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeConstraints(_OutputModel):
    max_rounds: int = Field(default=3, ge=1)
    max_videos: int = Field(default=18, ge=1)
    auth_profile: str = "default"
    auth_wait_seconds: int = Field(default=600, ge=1)
    request_timeout_seconds: int = Field(default=30, ge=1)
    browser_channel: BrowserChannel = BrowserChannel.AUTO

    @field_validator("auth_profile")
    @classmethod
    def validate_auth_profile(cls, value: str) -> str:
        normalized = normalize_inline_text(value)
        if not _PROFILE_ID.fullmatch(normalized):
            raise ValueError(
                "auth_profile must start with an ASCII letter or digit and contain only "
                "letters, digits, '.', '_' or '-'."
            )
        return normalized


class CreateSessionRequest(_OutputModel):
    workspace: Path
    input_path: Path
    query_plans_path: Path
    constraints: RuntimeConstraints = Field(default_factory=RuntimeConstraints)


class WarningView(_OutputModel):
    code: str
    message: str
    details: dict[str, Any]


class SessionSegmentView(_OutputModel):
    segment_id: str
    order: int
    query_plan_id: str
    status: str


class SessionView(_OutputModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    workspace_path: str
    status: str
    state_version: int
    created_at: str
    updated_at: str
    input_snapshot_path: str
    input_sha256: str
    query_plans_snapshot_path: str
    query_plans_sha256: str
    platform_scope: tuple[Platform, ...]
    constraints: RuntimeConstraints
    selected_browser_channel: BrowserChannel | None = None
    segments: tuple[SessionSegmentView, ...]
    warnings: tuple[WarningView, ...]
    result_path: str | None
    action_required: dict[str, Any] | None = None


class SessionSummary(_OutputModel):
    session_id: str
    status: str
    state_version: int
    created_at: str
    updated_at: str
    segment_count: int


class SessionListView(_OutputModel):
    schema_version: Literal["1.0"] = "1.0"
    workspace_path: str
    sessions: tuple[SessionSummary, ...]


class SessionStore(Protocol):
    """Persistence seam for collection-session lifecycle operations."""

    def create_session(self, request: CreateSessionRequest) -> SessionView: ...

    def get_session(self, workspace: Path, session_id: str) -> SessionView: ...

    def list_sessions(self, workspace: Path) -> SessionListView: ...

    def load_frozen_contracts(
        self,
        workspace: Path,
        session_id: str,
    ) -> tuple[CollectionInput, QueryPlans]: ...

    def freeze_browser_channel(
        self,
        workspace: Path,
        session_id: str,
        channel: BrowserChannel,
    ) -> SessionView: ...


class SessionApplication:
    """Interface used by CLI and workflows for session lifecycle operations."""

    def __init__(
        self,
        *,
        store: SessionStore,
    ) -> None:
        self._store = store

    def create_session(self, request: CreateSessionRequest) -> SessionView:
        return self._store.create_session(request)

    def get_session(self, workspace: Path, session_id: str) -> SessionView:
        return self._store.get_session(workspace, session_id)

    def list_sessions(self, workspace: Path) -> SessionListView:
        return self._store.list_sessions(workspace)

    def load_frozen_contracts(
        self,
        workspace: Path,
        session_id: str,
    ) -> tuple[CollectionInput, QueryPlans]:
        return self._store.load_frozen_contracts(workspace, session_id)

    def freeze_browser_channel(
        self,
        workspace: Path,
        session_id: str,
        channel: BrowserChannel,
    ) -> SessionView:
        if channel is BrowserChannel.AUTO:
            raise SessionStateError("The automatic browser channel cannot be frozen.")
        return self._store.freeze_browser_channel(workspace, session_id, channel)
