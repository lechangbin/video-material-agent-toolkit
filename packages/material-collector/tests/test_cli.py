from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from material_collector.adapters.cli import app as cli_module
from material_collector.adapters.cli.app import app
from material_collector.application.sessions import (
    SESSION_SCHEMA_VERSION,
    CreateSessionRequest,
    RuntimeConstraints,
    SessionApplication,
)
from material_collector.application.workflow import (
    WorkflowAction,
    WorkflowResult,
    WorkflowSegmentSummary,
)
from material_collector.core.errors import CollectorError
from material_collector.core.media import (
    AuthenticationSelection,
    AuthProbe,
    AuthStatus,
    BrowserChannel,
    Platform,
)
from material_collector.infrastructure import executor as executor_module
from material_collector.infrastructure.executor import CollectionExecutor
from material_collector.infrastructure.session_runtime_store import SqliteSessionRuntime
from material_collector.infrastructure.session_store import SqliteSessionStore


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def collection_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "full_script": "一段文案",
        "segments": [{"segment_id": "seg_001", "text": "一段文案"}],
    }


def query_plan_document() -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
        "plans": [
            {
                "segment_id": "seg_001",
                "visual_strategy": "展示主题主体",
                "required_visual_facets": [
                    {"facet_id": "facet_001", "description": "主题主体"}
                ],
                "initial_queries": [
                    {
                        "query_id": "query_001",
                        "text": "主题 视频",
                        "target_platforms": [
                            "bilibili",
                            "douyin",
                            "xiaohongshu",
                        ],
                        "facet_ids": ["facet_001"],
                    }
                ],
            }
        ],
    }


def scoped_query_plan_document(
    *platform_scope: str,
) -> dict[str, Any]:
    plans = query_plan_document()
    plans["schema_version"] = "2.0"
    plans["platform_scope"] = list(platform_scope)
    for plan in plans["plans"]:
        for query in plan["initial_queries"]:
            query["target_platforms"] = list(platform_scope)
    return plans


def parse_single_json_line(output: str) -> dict[str, Any]:
    lines = output.strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def terminate_execution(payload: dict[str, Any]) -> None:
    process_ids = [payload.get("process_id"), payload.get("collector_process_id")]
    if sys.platform == "win32":
        for process_id in process_ids:
            if isinstance(process_id, int):
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
        return
    for process_id in process_ids:
        if isinstance(process_id, int):
            try:
                os.kill(process_id, 15)
            except ProcessLookupError:
                pass


def test_executor_cli_starts_collection_without_powershell(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())
    captured_args = tmp_path / "captured-args.json"
    fake_collector = tmp_path / "fake_collector.py"
    fake_collector.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_ARGS_PATH']).write_text("
        "json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "print(json.dumps({'event': 'session_started', "
        "'session_id': 'ses_cross_platform'}), file=sys.stderr, flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "material_collector.adapters.cli.app",
            "executor",
            "invoke",
            "--operation",
            "run",
            "--collector-path",
            str(fake_collector),
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
            "--browser-channel",
            "chrome",
            "--show-search-browsers",
            "--control-directory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "FAKE_ARGS_PATH": str(captured_args)},
    )

    assert completed.returncode == 0, completed.stdout
    payload = parse_single_json_line(completed.stdout)
    try:
        assert completed.stderr == ""
        assert payload["status"] == "started"
        assert payload["operation"] == "run"
        assert payload["session_id"] == "ses_cross_platform"
        assert payload["process_id"] != payload["collector_process_id"]
        deadline = time.monotonic() + 3
        while not captured_args.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(captured_args.read_text(encoding="utf-8")) == [
            "run",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
            "--request-timeout-seconds",
            "30",
            "--max-rounds",
            "3",
            "--max-videos",
            "18",
            "--browser-channel",
            "chrome",
            "--show-search-browsers",
            "--progress-format",
            "jsonl",
        ]
    finally:
        terminate_execution(payload)


def test_executor_ignores_reused_pid_with_mismatched_start_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    control_root.mkdir()
    write_json(
        control_root / "stale.control.json",
        {
            "session_id": "ses_reused",
            "host_id": socket.gethostname(),
            "process_id": 101,
            "process_start_ticks": 111,
            "collector_process_id": None,
            "collector_process_start_ticks": None,
        },
    )
    monkeypatch.setattr(executor_module, "_process_alive", lambda _process_id: True)
    monkeypatch.setattr(executor_module, "_process_start_ticks", lambda _process_id: 222)

    assert CollectionExecutor()._live_execution(control_root, "ses_reused") is None


def test_executor_reads_current_process_start_identity() -> None:
    assert executor_module._process_start_ticks(os.getpid()) is not None


def test_executor_cli_recovers_past_dead_control_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, scoped_query_plan_document("bilibili"))
    session = SessionApplication(store=SqliteSessionStore()).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
        )
    )
    control_root = tmp_path / "control"
    control_root.mkdir()
    write_json(
        control_root / "interrupted.control.json",
        {
            "schema_version": "1.0",
            "session_id": session.session_id,
            "host_id": socket.gethostname(),
            "process_id": 999_999_991,
            "process_start_ticks": 1,
            "collector_process_id": 999_999_992,
            "collector_process_start_ticks": 2,
        },
    )
    fake_collector = tmp_path / "fake_collector.py"
    fake_collector.write_text(
        "import json, sys, time\n"
        "if sys.argv[1] == 'status':\n"
        "    print(json.dumps({'runtime': {'state': 'idle', 'lease_expired': True}}))\n"
        "else:\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "material_collector.adapters.cli.app",
            "executor",
            "invoke",
            "--operation",
            "resume",
            "--collector-path",
            str(fake_collector),
            "--workspace",
            str(workspace),
            "--session-id",
            session.session_id,
            "--control-directory",
            str(control_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stdout
    payload = parse_single_json_line(completed.stdout)
    try:
        assert payload["status"] == "started"
        assert payload["session_id"] == session.session_id
        assert payload["control_path"] != str(control_root / "interrupted.control.json")
    finally:
        terminate_execution(payload)


def test_executor_cli_reports_structured_browser_channel_failure() -> None:
    result = CliRunner().invoke(
        app,
        [
            "executor",
            "invoke",
            "--operation",
            "run",
            "--browser-channel",
            "firefox",
        ],
    )

    assert result.exit_code == 40
    payload = parse_single_json_line(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "browser_channel_invalid"


def test_long_running_commands_expose_progress_format_options() -> None:
    runner = CliRunner()

    run_help = runner.invoke(app, ["run", "--help"])
    resume_help = runner.invoke(app, ["resume", "--help"])

    assert run_help.exit_code == 0
    assert "--progress-format" in run_help.stdout
    assert "--browser-channel" in run_help.stdout
    assert "--show-search-browsers" in run_help.stdout
    assert resume_help.exit_code == 0
    assert "--progress-format" in resume_help.stdout
    assert "--show-search-browsers" in resume_help.stdout


def test_resume_forwards_execution_scoped_visible_search_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    seen: list[bool] = []

    class FakeWorkflow:
        async def run(
            self,
            requested_workspace: Path,
            session_id: str,
            *,
            show_search_browsers: bool = False,
        ) -> WorkflowResult:
            seen.append(show_search_browsers)
            return WorkflowResult(
                session_id=session_id,
                workspace_path=str(requested_workspace),
                platform_scope=(Platform.BILIBILI,),
                status="completed",
                result_path=str(requested_workspace / "collection-result.json"),
                candidates_found=1,
                media_units_found=1,
                proxies_ready=1,
                issues=(),
                action_required=None,
            )

    monkeypatch.setattr(cli_module, "_workflow", lambda *, progress: FakeWorkflow())
    result = CliRunner().invoke(
        app,
        [
            "resume",
            "--workspace",
            str(workspace),
            "--session-id",
            "ses_visible_resume",
            "--show-search-browsers",
        ],
    )

    assert result.exit_code == 0
    assert seen == [True]


def test_resume_reports_search_browser_closed_at_public_cli_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWorkflow:
        async def run(
            self,
            workspace: Path,
            session_id: str,
            *,
            show_search_browsers: bool = False,
        ) -> WorkflowResult:
            del workspace, session_id
            assert show_search_browsers is True
            raise CollectorError(
                "search_browser_closed",
                "The visible search browser was closed.",
                details={
                    "platform": "bilibili",
                    "operation": "search",
                    "retryable": True,
                },
            )

    monkeypatch.setattr(cli_module, "_workflow", lambda *, progress: FakeWorkflow())
    result = CliRunner().invoke(
        app,
        [
            "resume",
            "--workspace",
            str(tmp_path / "workspace"),
            "--session-id",
            "ses_closed_window",
            "--show-search-browsers",
        ],
    )

    assert result.exit_code == 30
    payload = parse_single_json_line(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "search_browser_closed"
    assert payload["error"]["details"]["retryable"] is True


def test_contracts_normalize_uses_the_session_creation_contracts(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    collection = collection_document()
    collection["segments"][0].pop("segment_id")
    plans = query_plan_document()
    write_json(input_path, collection)
    write_json(plans_path, plans)

    result = runner.invoke(
        app,
        [
            "contracts",
            "normalize",
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert payload["schema_version"] == "material-collector-normalized-contracts/v1"
    assert payload["status"] == "normalized"
    assert payload["collection_input"]["segments"][0]["segment_id"] == "seg_001"
    assert payload["query_plans"]["plans"][0]["query_plan_id"] == "qp_seg_001"


def test_contracts_schema_exposes_authoritative_read_only_contracts() -> None:
    result = CliRunner().invoke(app, ["contracts", "schema"])

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert payload["schema_version"] == "material-collector-contract-schemas/v1"
    assert payload["status"] == "available"
    assert payload["contracts"]["collection_input"]["properties"]["schema_version"][
        "const"
    ] == "1.0"
    assert payload["contracts"]["query_plans"]["properties"]["schema_version"][
        "const"
    ] == "2.0"


def test_version_exposes_cli_and_agent_protocol_compatibility() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert payload == {
        "schema_version": "material-collector-version/v1",
        "cli_version": "0.2.1",
        "skill_protocol_version": 1,
        "collection_input_schema": {"min": "1.0", "max": "1.0"},
        "query_plans_schema": {"min": "2.0", "max": "2.0"},
    }


def test_contracts_normalize_freezes_a_bilibili_only_platform_scope(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    plans = scoped_query_plan_document("bilibili")
    write_json(input_path, collection_document())
    write_json(plans_path, plans)

    result = runner.invoke(
        app,
        [
            "contracts",
            "normalize",
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert payload["query_plans"]["schema_version"] == "2.0"
    assert payload["query_plans"]["platform_scope"] == ["bilibili"]
    assert payload["query_plans"]["plans"][0]["initial_queries"][0][
        "target_platforms"
    ] == ["bilibili"]


def test_contracts_normalize_canonicalizes_a_supported_platform_subset(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(
        plans_path,
        scoped_query_plan_document("xiaohongshu", "bilibili"),
    )

    result = runner.invoke(
        app,
        [
            "contracts",
            "normalize",
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert payload["query_plans"]["platform_scope"] == [
        "bilibili",
        "xiaohongshu",
    ]
    assert payload["query_plans"]["plans"][0]["initial_queries"][0][
        "target_platforms"
    ] == ["bilibili", "xiaohongshu"]


def test_run_and_status_preserve_a_bilibili_only_platform_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, scoped_query_plan_document("bilibili"))

    class FakeWorkflow:
        async def run(
            self,
            workspace: Path,
            session_id: str,
            *,
            show_search_browsers: bool = False,
        ) -> WorkflowResult:
            assert show_search_browsers is False
            return WorkflowResult(
                session_id=session_id,
                workspace_path=str(workspace.resolve()),
                platform_scope=(Platform.BILIBILI,),
                status="completed",
                result_path=str(workspace / "collection-result.json"),
                candidates_found=0,
                media_units_found=0,
                proxies_ready=0,
                issues=(),
                action_required=None,
            )

    monkeypatch.setattr(cli_module, "_workflow", lambda **_kwargs: FakeWorkflow())
    run_result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert run_result.exit_code == 0, run_result.stdout
    run_payload = parse_single_json_line(run_result.stdout)
    assert run_payload["platform_scope"] == ["bilibili"]

    status_result = runner.invoke(
        app,
        [
            "status",
            "--workspace",
            str(workspace),
            "--session-id",
            run_payload["session_id"],
        ],
    )

    assert status_result.exit_code == 0, status_result.stdout
    status_payload = parse_single_json_line(status_result.stdout)
    assert status_payload["platform_scope"] == ["bilibili"]
    frozen_plans = json.loads(
        (workspace / status_payload["query_plans_snapshot_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert frozen_plans["platform_scope"] == ["bilibili"]


def test_status_rejects_an_old_session_without_mutating_it(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, scoped_query_plan_document("bilibili"))
    session = SessionApplication(store=SqliteSessionStore()).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
        )
    )
    database = (
        workspace
        / ".material-collector"
        / "sessions"
        / session.session_id
        / "session.sqlite3"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE schema_info SET schema_version = 2 WHERE singleton = 1"
        )
        connection.execute("PRAGMA user_version = 2")

    result = runner.invoke(
        app,
        [
            "status",
            "--workspace",
            str(workspace),
            "--session-id",
            session.session_id,
        ],
    )

    assert result.exit_code == 50
    payload = parse_single_json_line(result.stdout)
    assert payload["error"] == {
        "code": "session_version_unsupported",
        "message": "The collection session schema version is unsupported.",
        "details": {
            "session_id": session.session_id,
            "received_version": 2,
            "supported_versions": [SESSION_SCHEMA_VERSION],
        },
    }
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT schema_version FROM schema_info WHERE singleton = 1"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_contracts_normalize_rejects_queryplans_v1_as_unsupported(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    plans = query_plan_document()
    plans["schema_version"] = "1.0"
    plans.pop("platform_scope")
    write_json(plans_path, plans)

    result = runner.invoke(
        app,
        [
            "contracts",
            "normalize",
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 40
    payload = parse_single_json_line(result.stdout)
    assert payload["error"] == {
        "code": "contract_version_unsupported",
        "message": "query_plans schema version is unsupported.",
        "details": {
            "document": "query_plans",
            "received_version": "1.0",
            "supported_versions": ["2.0"],
        },
    }


def test_contracts_normalize_rejects_invalid_json_with_machine_protocol(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    input_path.write_text("not json", encoding="utf-8")
    write_json(plans_path, query_plan_document())

    result = runner.invoke(
        app,
        [
            "contracts",
            "normalize",
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 40
    payload = parse_single_json_line(result.stdout)
    assert payload["error"]["code"] == "contract_invalid"


def test_run_can_render_human_readable_progress_without_polluting_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())

    class FakeWorkflow:
        def __init__(self, progress: Any) -> None:
            self._progress = progress

        async def run(
            self,
            workspace: Path,
            session_id: str,
            *,
            show_search_browsers: bool = False,
        ) -> WorkflowResult:
            assert show_search_browsers is True
            self._progress.report("search_started", {"segment_id": "seg_001"})
            return WorkflowResult(
                session_id=session_id,
                workspace_path=str(workspace.resolve()),
                platform_scope=tuple(Platform),
                status="completed",
                result_path=str(workspace / "collection-result.json"),
                candidates_found=1,
                media_units_found=1,
                proxies_ready=1,
                issues=(),
                action_required=None,
            )

    def fake_workflow(*, progress: Any) -> FakeWorkflow:
        return FakeWorkflow(progress)

    monkeypatch.setattr(cli_module, "_workflow", fake_workflow)
    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
            "--progress-format",
            "text",
            "--show-search-browsers",
        ],
    )

    assert result.exit_code == 0
    assert parse_single_json_line(result.stdout)["status"] == "completed"
    assert result.stderr == "search_started: segment_id=seg_001\n"


def test_run_status_and_sessions_list_share_the_application_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())

    class FakeWorkflow:
        def __init__(self, *, progress: Any) -> None:
            self._progress = progress

        async def run(
            self,
            workspace: Path,
            session_id: str,
            *,
            show_search_browsers: bool = False,
        ) -> WorkflowResult:
            assert show_search_browsers is False
            self._progress.report("search_started", {"segment_id": "seg_001"})
            SessionApplication(store=SqliteSessionStore()).freeze_browser_channel(
                workspace,
                session_id,
                BrowserChannel.EDGE,
            )
            result_path = (
                workspace
                / ".material-collector"
                / "sessions"
                / session_id
                / "collection-result.json"
            )
            return WorkflowResult(
                session_id=session_id,
                workspace_path=str(workspace.resolve()),
                platform_scope=tuple(Platform),
                status="integration_required",
                result_path=str(result_path),
                candidates_found=3,
                media_units_found=3,
                proxies_ready=2,
                segments=(
                    WorkflowSegmentSummary(
                        segment_id="seg_001",
                        status="integration_required",
                        candidates_found=3,
                        media_units_found=3,
                        proxies_ready=2,
                    ),
                ),
                issues=(),
                action_required=WorkflowAction(
                    actor="agent",
                    type="integration_required",
                    reason="test checkpoint",
                    target="seg_001",
                    requested_artifacts=("collection-result.json",),
                ),
            )

    monkeypatch.setattr(cli_module, "_workflow", FakeWorkflow)
    run_result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
            "--max-rounds",
            "4",
            "--request-timeout-seconds",
            "45",
            "--browser-channel",
            "edge",
        ],
    )

    assert run_result.exit_code == 20, run_result.stdout
    run_payload = parse_single_json_line(run_result.stdout)
    assert run_payload["schema_version"] == "1.0"
    assert run_payload["status"] == "integration_required"
    assert run_payload["segments"] == [
        {
            "segment_id": "seg_001",
            "status": "integration_required",
            "candidates_found": 3,
            "media_units_found": 3,
            "proxies_ready": 2,
        }
    ]
    assert run_payload["action_required"]["target"] == "seg_001"
    assert run_payload["action_required"]["requested_artifacts"] == [
        "collection-result.json"
    ]
    progress_payload = parse_single_json_line(run_result.stderr)
    assert progress_payload["event"] == "search_started"
    session_id = str(run_payload["session_id"])

    status_result = runner.invoke(
        app,
        [
            "status",
            "--workspace",
            str(workspace),
            "--session-id",
            session_id,
        ],
    )
    assert status_result.exit_code == 0
    status_payload = parse_single_json_line(status_result.stdout)
    assert status_payload["session_id"] == session_id
    assert status_payload["constraints"]["max_rounds"] == 4
    assert status_payload["constraints"]["request_timeout_seconds"] == 45
    assert status_payload["constraints"]["browser_channel"] == "edge"
    assert status_payload["selected_browser_channel"] == "edge"

    list_result = runner.invoke(
        app,
        ["sessions", "list", "--workspace", str(workspace)],
    )
    assert list_result.exit_code == 0
    list_payload = parse_single_json_line(list_result.stdout)
    assert [item["session_id"] for item in list_payload["sessions"]] == [session_id]


def test_auth_login_reports_the_selected_native_browser_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[BrowserChannel] = []

    class FakeAuthentication:
        async def ensure_authenticated(
            self,
            platforms: tuple[Platform, ...],
            auth_profile: str,
            wait_seconds: int,
            *,
            browser_channel: BrowserChannel,
            progress: Any | None = None,
        ) -> AuthenticationSelection:
            del wait_seconds, progress
            requested.append(browser_channel)
            return AuthenticationSelection(
                browser_channel=BrowserChannel.EDGE,
                probes=(
                    AuthProbe(
                        platform=platforms[0],
                        auth_profile=auth_profile,
                        browser_channel=BrowserChannel.EDGE,
                        status=AuthStatus.VALID,
                        checked_at="2026-08-18T00:00:00Z",
                    ),
                ),
            )

    monkeypatch.setattr(cli_module, "_authentication", FakeAuthentication)

    result = CliRunner().invoke(
        app,
        [
            "auth",
            "login",
            "--platform",
            "bilibili",
            "--browser-channel",
            "auto",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = parse_single_json_line(result.stdout)
    assert requested == [BrowserChannel.AUTO]
    assert payload["browser_channel"] == "edge"
    assert payload["platforms"][0]["browser_channel"] == "edge"


def test_cancel_and_status_report_pending_execution_lease(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())
    session = SessionApplication(store=SqliteSessionStore()).create_session(
        CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=plans_path,
            constraints=RuntimeConstraints(),
        )
    )
    runtime = SqliteSessionRuntime(lease_ttl_seconds=60)
    lease = runtime.begin_execution(
        workspace,
        session.session_id,
        owner_id="worker-test",
        stages=("authenticate",),
    )

    cancelled = runner.invoke(
        app,
        [
            "cancel",
            "--workspace",
            str(workspace),
            "--session-id",
            session.session_id,
        ],
    )

    assert cancelled.exit_code == 0
    cancel_payload = parse_single_json_line(cancelled.stdout)
    assert cancel_payload["status"] == "running"
    assert cancel_payload["runtime"] == {
        "state": "cancelling",
        "cancel_requested": True,
        "lease_owner_id": "worker-test",
        "lease_expires_at": lease.expires_at,
        "lease_expired": False,
        "next_stage": "authenticate",
        "completed_stages": [],
    }
    assert cancel_payload["suggested_action"] == "wait_for_executor_or_lease_expiry"

    inspected = runner.invoke(
        app,
        [
            "status",
            "--workspace",
            str(workspace),
            "--session-id",
            session.session_id,
        ],
    )

    assert inspected.exit_code == 0
    status_payload = parse_single_json_line(inspected.stdout)
    assert status_payload["runtime"] == cancel_payload["runtime"]


def test_contract_failure_returns_one_json_result_and_exit_40(tmp_path: Path) -> None:
    runner = CliRunner()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    write_json(input_path, collection_document())
    plans = query_plan_document()
    plans["plans"][0]["max_videos"] = 99
    write_json(plans_path, plans)

    result = runner.invoke(
        app,
        [
            "run",
            "--workspace",
            str(tmp_path / "workspace"),
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
        ],
    )

    assert result.exit_code == 40
    payload = parse_single_json_line(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "contract_invalid"
    assert not (tmp_path / "workspace").exists()


def test_console_entry_point_wraps_usage_errors_as_json() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "material_collector.adapters.cli.app",
            "run",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 40
    payload = parse_single_json_line(completed.stdout)
    assert payload["error"]["code"] == "cli_usage_error"


def test_cli_process_never_contacts_adapters_outside_bilibili_scope(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    probe_path = tmp_path / "adapter-calls.json"
    workspace = tmp_path / "workspace"
    write_json(input_path, collection_document())
    write_json(plans_path, scoped_query_plan_document("bilibili"))
    process_script = r'''
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tests"))
import test_workflow as support
from material_collector.adapters.cli import app as cli
from material_collector.core.media import PLATFORM_ORDER

probe_path, workspace, input_path, plans_path = map(Path, sys.argv[1:])
authentication = support._Authentication()
adapters = {platform: support._Platform(platform) for platform in PLATFORM_ORDER}

def workflow_factory(*, progress):
    return support._make_workflow(
        authentication,
        adapters,
        support.SessionRuntime(lease_ttl_seconds=900),
        progress=progress,
    )

cli._workflow = workflow_factory
sys.argv = [
    "material-collector",
    "run",
    "--workspace",
    str(workspace),
    "--input",
    str(input_path),
    "--query-plans",
    str(plans_path),
]
return_code = cli.main()
probe_path.write_text(
    json.dumps(
        {
            "authentication": [
                [platform.value for platform in call]
                for call in authentication.ensure_calls
            ],
            "adapters": {
                platform.value: {
                    "search": adapter.search_count,
                    "resolve": adapter.resolve_count,
                    "fetch": adapter.fetch_count,
                }
                for platform, adapter in adapters.items()
            },
        }
    ),
    encoding="utf-8",
)
raise SystemExit(return_code)
'''

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            process_script,
            str(probe_path),
            str(workspace),
            str(input_path),
            str(plans_path),
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 20, completed.stderr
    payload = parse_single_json_line(completed.stdout)
    assert payload["platform_scope"] == ["bilibili"]
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    assert probe["authentication"] == [["bilibili"]]
    assert probe["adapters"]["bilibili"] == {
        "search": 1,
        "resolve": 1,
        "fetch": 1,
    }
    assert probe["adapters"]["douyin"] == {
        "search": 0,
        "resolve": 0,
        "fetch": 0,
    }
    assert probe["adapters"]["xiaohongshu"] == {
        "search": 0,
        "resolve": 0,
        "fetch": 0,
    }
