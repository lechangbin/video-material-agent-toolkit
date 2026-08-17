from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from material_collector.adapters.cli import app as cli_module
from material_collector.adapters.cli.app import app
from material_collector.application.sessions import (
    CreateSessionRequest,
    RuntimeConstraints,
    SessionApplication,
)
from material_collector.application.workflow import (
    WorkflowAction,
    WorkflowResult,
    WorkflowSegmentSummary,
)
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
        "schema_version": "1.0",
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


def parse_single_json_line(output: str) -> dict[str, Any]:
    lines = output.strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


def test_long_running_commands_expose_progress_format_options() -> None:
    runner = CliRunner()

    run_help = runner.invoke(app, ["run", "--help"])
    resume_help = runner.invoke(app, ["resume", "--help"])

    assert run_help.exit_code == 0
    assert "--progress-format" in run_help.stdout
    assert resume_help.exit_code == 0
    assert "--progress-format" in resume_help.stdout


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

        async def run(self, workspace: Path, session_id: str) -> WorkflowResult:
            self._progress.report("search_started", {"segment_id": "seg_001"})
            return WorkflowResult(
                session_id=session_id,
                workspace_path=str(workspace.resolve()),
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

        async def run(self, workspace: Path, session_id: str) -> WorkflowResult:
            self._progress.report("search_started", {"segment_id": "seg_001"})
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

    list_result = runner.invoke(
        app,
        ["sessions", "list", "--workspace", str(workspace)],
    )
    assert list_result.exit_code == 0
    list_payload = parse_single_json_line(list_result.stdout)
    assert [item["session_id"] for item in list_payload["sessions"]] == [session_id]


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


def test_cli_process_temporarily_skips_xiaohongshu_collection(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    probe_path = tmp_path / "adapter-calls.json"
    workspace = tmp_path / "workspace"
    write_json(input_path, collection_document())
    write_json(plans_path, query_plan_document())
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
    disabled = [
        issue
        for issue in payload["issues"]
        if issue["code"] == "platform_search_temporarily_disabled"
    ]
    assert disabled == [
        {
            "stage": "search",
            "code": "platform_search_temporarily_disabled",
            "message": "Xiaohongshu collection search is temporarily disabled.",
            "details": {
                "platform": "xiaohongshu",
                "temporary": True,
                "reason": "authentication_probe_http_406",
            },
        }
    ]
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    assert probe["authentication"] == [["bilibili", "douyin"]]
    assert probe["adapters"]["bilibili"] == {
        "search": 1,
        "resolve": 1,
        "fetch": 1,
    }
    assert probe["adapters"]["douyin"] == {
        "search": 1,
        "resolve": 1,
        "fetch": 1,
    }
    assert probe["adapters"]["xiaohongshu"] == {
        "search": 0,
        "resolve": 0,
        "fetch": 0,
    }
