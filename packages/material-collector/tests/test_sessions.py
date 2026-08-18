from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from material_collector.application.sessions import (
    SESSION_SCHEMA_VERSION,
    CreateSessionRequest,
    RuntimeConstraints,
    SessionApplication,
    SessionListView,
    SessionSummary,
    SessionView,
)
from material_collector.core.contracts import CollectionInput, QueryPlans
from material_collector.core.errors import (
    ContractError,
    SessionStateError,
    SessionVersionError,
    WorkspaceError,
)
from material_collector.core.media import BrowserChannel
from material_collector.infrastructure.session_store import SqliteSessionStore

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def collection_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "full_script": "主题一主题二",
        "theme": "测试主题",
        "segments": [
            {"segment_id": "seg_a", "order": 1, "text": "主题一"},
            {"segment_id": "seg_b", "order": 2, "text": "主题二"},
        ],
    }


def query_plan_document() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
        "plans": [
            {
                "segment_id": "seg_a",
                "visual_strategy": "主题一策略",
                "required_visual_facets": [
                    {"facet_id": "facet_a", "description": "主题一画面"}
                ],
                "initial_queries": [
                    {
                        "query_id": "query_a",
                        "text": "主题一",
                        "target_platforms": [
                            "bilibili",
                            "douyin",
                            "xiaohongshu",
                        ],
                        "facet_ids": ["facet_a"],
                    }
                ],
            },
            {
                "segment_id": "seg_b",
                "visual_strategy": "主题二策略",
                "required_visual_facets": [
                    {"facet_id": "facet_b", "description": "主题二画面"}
                ],
                "initial_queries": [
                    {
                        "query_id": "query_b",
                        "text": "主题二",
                        "target_platforms": [
                            "bilibili",
                            "douyin",
                            "xiaohongshu",
                        ],
                        "facet_ids": ["facet_b"],
                    }
                ],
            },
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def deterministic_application(*session_ids: str) -> SessionApplication:
    identifiers: Iterator[str] = iter(session_ids)
    return SessionApplication(
        store=SqliteSessionStore(
            now=lambda: FIXED_NOW,
            session_id_factory=lambda _now: next(identifiers),
        )
    )


def create_request(root: Path, workspace: Path) -> CreateSessionRequest:
    input_path = root / "input.json"
    plans_path = root / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())
    return CreateSessionRequest(
        workspace=workspace,
        input_path=input_path,
        query_plans_path=plans_path,
        constraints=RuntimeConstraints(
            max_rounds=4,
            max_videos=20,
            auth_profile="editing",
            auth_wait_seconds=900,
            request_timeout_seconds=45,
        ),
    )


def test_application_accepts_a_store_adapter_without_touching_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "does-not-exist"
    request = CreateSessionRequest(
        workspace=workspace,
        input_path=tmp_path / "missing-input.json",
        query_plans_path=tmp_path / "missing-plans.json",
    )
    session = SessionView(
        session_id="ses_fake_001",
        workspace_path=str(workspace),
        status="initialized",
        state_version=1,
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
        input_snapshot_path="input/collection-input.json",
        input_sha256="a" * 64,
        query_plans_snapshot_path="input/query-plans.json",
        query_plans_sha256="b" * 64,
        platform_scope=("bilibili", "douyin", "xiaohongshu"),
        constraints=RuntimeConstraints(),
        segments=(),
        warnings=(),
        result_path=None,
    )
    listing = SessionListView(
        workspace_path=str(workspace),
        sessions=(
            SessionSummary(
                session_id=session.session_id,
                status=session.status,
                state_version=session.state_version,
                created_at=session.created_at,
                updated_at=session.updated_at,
                segment_count=0,
            ),
        ),
    )

    class InMemorySessionStore:
        def create_session(self, received: CreateSessionRequest) -> SessionView:
            assert received is request
            return session

        def get_session(self, received_workspace: Path, session_id: str) -> SessionView:
            assert received_workspace == workspace
            assert session_id == session.session_id
            return session

        def list_sessions(self, received_workspace: Path) -> SessionListView:
            assert received_workspace == workspace
            return listing

        def load_frozen_contracts(
            self,
            received_workspace: Path,
            session_id: str,
        ) -> tuple[CollectionInput, QueryPlans]:
            raise AssertionError(
                f"unexpected frozen read: {received_workspace} {session_id}"
            )

        def freeze_browser_channel(
            self,
            received_workspace: Path,
            session_id: str,
            channel: BrowserChannel,
        ) -> SessionView:
            raise AssertionError(
                f"unexpected browser freeze: {received_workspace} {session_id} {channel}"
            )

    application = SessionApplication(store=InMemorySessionStore())

    assert application.create_session(request) is session
    assert application.get_session(workspace, session.session_id) is session
    assert application.list_sessions(workspace) is listing
    assert not workspace.exists()


def test_create_session_freezes_documents_and_initializes_sqlite(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "materials"
    request = create_request(tmp_path, workspace)
    application = deterministic_application("ses_fixed_001")

    created = application.create_session(request)

    session_dir = workspace / ".material-collector" / "sessions" / "ses_fixed_001"
    input_snapshot = workspace / Path(created.input_snapshot_path)
    plans_snapshot = workspace / Path(created.query_plans_snapshot_path)
    assert created.status == "initialized"
    assert created.state_version == 1
    assert created.workspace_path == str(workspace.resolve())
    assert created.constraints.max_rounds == 4
    assert created.constraints.auth_profile == "editing"
    assert created.constraints.request_timeout_seconds == 45
    assert created.constraints.browser_channel is BrowserChannel.AUTO
    assert created.selected_browser_channel is None
    assert [segment.status for segment in created.segments] == ["planned", "planned"]
    assert input_snapshot.is_file()
    assert plans_snapshot.is_file()
    assert (session_dir / "session.sqlite3").is_file()
    assert created.input_sha256 == hashlib.sha256(input_snapshot.read_bytes()).hexdigest()
    assert created.query_plans_sha256 == hashlib.sha256(
        plans_snapshot.read_bytes()
    ).hexdigest()

    write_json(request.input_path, {"schema_version": "changed"})
    loaded = application.get_session(workspace, created.session_id)
    assert loaded.input_sha256 == created.input_sha256
    assert loaded.constraints.request_timeout_seconds == 45
    assert json.loads(input_snapshot.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    with sqlite3.connect(session_dir / "session.sqlite3") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == SESSION_SCHEMA_VERSION
        )


def test_session_freezes_the_first_successful_browser_channel(tmp_path: Path) -> None:
    workspace = tmp_path / "materials"
    application = deterministic_application("ses_browser_001")
    created = application.create_session(create_request(tmp_path, workspace))

    frozen = application.freeze_browser_channel(
        workspace,
        created.session_id,
        BrowserChannel.EDGE,
    )

    assert frozen.selected_browser_channel is BrowserChannel.EDGE
    assert (
        application.freeze_browser_channel(
            workspace,
            created.session_id,
            BrowserChannel.EDGE,
        ).selected_browser_channel
        is BrowserChannel.EDGE
    )
    with pytest.raises(SessionStateError):
        application.freeze_browser_channel(
            workspace,
            created.session_id,
            BrowserChannel.CHROME,
        )

def test_invalid_input_does_not_create_workspace_or_session(tmp_path: Path) -> None:
    workspace = tmp_path / "materials"
    request = create_request(tmp_path, workspace)
    write_json(request.input_path, {"schema_version": "1.0", "segments": []})
    application = deterministic_application("ses_fixed_002")

    with pytest.raises(ContractError):
        application.create_session(request)

    assert not workspace.exists()


def test_duplicate_json_keys_are_rejected_before_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "materials"
    input_path = tmp_path / "duplicate.json"
    plans_path = tmp_path / "plans.json"
    input_path.write_text(
        '{"schema_version":"1.0","schema_version":"1.0"}',
        encoding="utf-8",
    )
    write_json(plans_path, query_plan_document())
    request = CreateSessionRequest(
        workspace=workspace,
        input_path=input_path,
        query_plans_path=plans_path,
    )

    with pytest.raises(ContractError) as captured:
        deterministic_application("ses_fixed_003").create_session(request)

    assert "duplicate JSON object key" in captured.value.message
    assert not workspace.exists()


def test_sessions_are_isolated_and_listed_only_in_explicit_workspace(
    tmp_path: Path,
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    application = deterministic_application(
        "ses_fixed_010",
        "ses_fixed_011",
        "ses_fixed_020",
    )

    first = application.create_session(
        create_request(tmp_path / "one", first_workspace)
    )
    second = application.create_session(
        create_request(tmp_path / "two", first_workspace)
    )
    other = application.create_session(
        create_request(tmp_path / "three", second_workspace)
    )

    first_list = application.list_sessions(first_workspace)
    second_list = application.list_sessions(second_workspace)
    assert {item.session_id for item in first_list.sessions} == {
        first.session_id,
        second.session_id,
    }
    assert [item.session_id for item in second_list.sessions] == [other.session_id]


def test_version_one_session_is_rejected_without_migration(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "materials"
    application = deterministic_application("ses_legacy_001")
    created = application.create_session(create_request(tmp_path, workspace))
    database = (
        workspace
        / ".material-collector"
        / "sessions"
        / created.session_id
        / "session.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE session_state DROP COLUMN request_timeout_seconds"
        )
        connection.execute(
            "UPDATE schema_info SET schema_version = 1 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(SessionVersionError) as captured:
        application.get_session(workspace, created.session_id)

    assert captured.value.details == {
        "session_id": created.session_id,
        "received_version": 1,
        "supported_versions": [SESSION_SCHEMA_VERSION],
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton = 1"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_unsafe_session_id_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "materials"
    workspace.mkdir()

    with pytest.raises(ContractError):
        SessionApplication(store=SqliteSessionStore()).get_session(
            workspace, "../outside"
        )


def test_corrupt_session_is_not_silently_hidden_from_list(tmp_path: Path) -> None:
    workspace = tmp_path / "materials"
    request = create_request(tmp_path, workspace)
    application = deterministic_application("ses_corrupt_001")
    created = application.create_session(request)
    database = (
        workspace
        / ".material-collector"
        / "sessions"
        / created.session_id
        / "session.sqlite3"
    )
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(SessionStateError):
        application.list_sessions(workspace)


def test_list_requires_an_existing_workspace(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError):
        SessionApplication(store=SqliteSessionStore()).list_sessions(
            tmp_path / "missing"
        )
