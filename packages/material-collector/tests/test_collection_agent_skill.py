from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "collect-video-materials"
RUNNER = SKILL_ROOT / "scripts" / "invoke-collector.ps1"
RESOLVER = SKILL_ROOT / "scripts" / "resolve_material_collector.py"
BOOTSTRAP = REPOSITORY_ROOT / "scripts" / "bootstrap-agent.ps1"
POWERSHELL_HOSTS = [
    host for host in ("powershell", "pwsh") if shutil.which(host) is not None
]


def _load_resolver():
    spec = importlib.util.spec_from_file_location("material_collector_skill_resolver", RESOLVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_distinguishes_partial_search_progress_from_final_status() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())

    assert "`search_plan_settled`" in skill
    assert "as progress only, never as the final session result" in normalized
    assert "Do not issue `resume` merely because one platform" in normalized


def test_skill_exposes_complete_inputs_and_lifecycle_commands_without_probing() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    inputs = (SKILL_ROOT / "references" / "input-contracts.md").read_text(
        encoding="utf-8"
    )
    execution = (SKILL_ROOT / "references" / "cli-execution-contract.md").read_text(
        encoding="utf-8"
    )

    assert "do not discover either contract with `--help`" in " ".join(skill.split())
    assert '"full_script"' in inputs
    assert '"platform_scope"' in inputs
    assert '"target_platforms"' in inputs
    for operation in ("run", "resume", "status", "cancel"):
        assert f"--operation {operation}" in execution


def test_skill_links_versioned_schemas_examples_and_read_only_schema_command() -> None:
    inputs = (SKILL_ROOT / "references" / "input-contracts.md").read_text(
        encoding="utf-8"
    )

    assert "schemas/collection-input-1.0.schema.json" in inputs
    assert "schemas/query-plans-2.0.schema.json" in inputs
    assert "examples/collection-input-1.0.min.json" in inputs
    assert "examples/query-plans-2.0.min.json" in inputs
    assert "material-collector contracts schema" in inputs


def test_skill_resolver_returns_the_compatible_installed_cli() -> None:
    resolved_cli = shutil.which(
        "material-collector",
        path=str(Path(sys.executable).parent),
    )
    assert resolved_cli is not None
    environment = os.environ.copy()
    environment["MATERIAL_COLLECTOR_CLI"] = resolved_cli

    completed = subprocess.run(
        [sys.executable, str(RESOLVER)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["command"] == str(Path(resolved_cli).resolve())
    assert payload["cli_version"] == "0.2.0"
    assert payload["skill_protocol_version"] == 1


def test_skill_resolver_accepts_only_the_matching_cli_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolver = _load_resolver()
    candidate = tmp_path / "material-collector.exe"
    candidate.touch()
    monkeypatch.setenv("MATERIAL_COLLECTOR_CLI", str(candidate))
    monkeypatch.setattr(
        resolver.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "schema_version": "material-collector-version/v1",
                    "cli_version": "0.2.0",
                    "skill_protocol_version": 1,
                    "collection_input_schema": {"min": "1.0", "max": "1.0"},
                    "query_plans_schema": {"min": "2.0", "max": "2.0"},
                }
            ),
            "",
        ),
    )

    exit_code = resolver.main()

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["command"] == str(candidate.resolve())
    assert payload["cli_version"] == "0.2.0"
    assert payload["skill_protocol_version"] == 1


def test_skill_resolver_reports_an_incompatible_cli_without_source_probing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolver = _load_resolver()
    candidate = tmp_path / "material-collector.exe"
    candidate.touch()
    monkeypatch.setenv("MATERIAL_COLLECTOR_CLI", str(candidate))
    monkeypatch.setattr(
        resolver.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "schema_version": "material-collector-version/v1",
                    "cli_version": "0.1.3",
                    "skill_protocol_version": 1,
                    "collection_input_schema": {"min": "1.0", "max": "1.0"},
                    "query_plans_schema": {"min": "2.0", "max": "2.0"},
                }
            ),
            "",
        ),
    )

    exit_code = resolver.main()

    payload = json.loads(capsys.readouterr().err)
    assert exit_code == 4
    assert payload["code"] == "material_collector_cli_incompatible"
    assert payload["expected"]["cli_version"] == "0.2.0"
    assert payload["actual"]["cli_version"] == "0.1.3"


@pytest.mark.parametrize("powershell_host", POWERSHELL_HOSTS)
def test_skill_runner_rejects_model_authored_operations(
    powershell_host: str,
) -> None:
    completed = subprocess.run(
        [
            powershell_host,
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "shell",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 40
    payload = json.loads(completed.stdout)
    assert payload == {
        "schema_version": "1.0",
        "status": "error",
        "error": {
            "code": "operation_invalid",
            "message": "Operation must be run, resume, status, or cancel.",
        },
    }


def test_skill_runner_delegates_lifecycle_to_collector_executor() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "$arguments.Add('executor')" in source
    assert "Start-Process" not in source
    assert "Get-Process" not in source
    assert "process_start_ticks" not in source


def test_native_bootstrap_exposes_edge_first_browser_selection() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert "[string]$BrowserChannel = 'auto'" in source
    assert "@('auto', 'edge', 'chrome') -notcontains $BrowserChannel" in source
    assert "foreach ($candidate in @('edge', 'chrome'))" in source
    assert "-BrowserChannel $BrowserChannel" in source
    assert "Test-ChromeAvailable" not in source


@pytest.mark.parametrize(
    ("extra_arguments", "expected_code"),
    [
        (["-RequestTimeoutSeconds", "0"], "request_timeout_invalid"),
        (["-MaxRounds", "0"], "max_rounds_invalid"),
        (["-MaxVideos", "0"], "max_videos_invalid"),
        (["-ProgressFormat", "xml"], "progress_format_invalid"),
        (["-CollectorPath", ""], "collector_path_invalid"),
        (["-BrowserChannel", "firefox"], "browser_channel_invalid"),
        (["-Bogus", "value"], "arguments_invalid"),
    ],
)
def test_skill_runner_returns_json_for_invalid_script_parameters(
    tmp_path: Path,
    extra_arguments: list[str],
    expected_code: str,
) -> None:
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "run",
            "-Workspace",
            str(tmp_path / "workspace"),
            "-InputPath",
            str(tmp_path / "input.json"),
            "-QueryPlansPath",
            str(tmp_path / "plans.json"),
            *extra_arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 40
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "error"
    assert payload["error"]["code"] == expected_code


@pytest.mark.parametrize("powershell_host", POWERSHELL_HOSTS)
def test_native_bootstrap_returns_json_for_invalid_browser_channel(
    powershell_host: str,
) -> None:
    completed = subprocess.run(
        [
            powershell_host,
            "-NoProfile",
            "-File",
            str(BOOTSTRAP),
            "-CheckOnly",
            "-BrowserChannel",
            "firefox",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["status"] == "error"


def test_skill_runner_reports_missing_collector_as_structured_error(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "status",
            "-CollectorPath",
            str(tmp_path / "missing.exe"),
            "-Workspace",
            str(tmp_path / "workspace"),
            "-SessionId",
            "ses_missing",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "collector_not_found"


@pytest.mark.parametrize("powershell_host", POWERSHELL_HOSTS)
def test_skill_runner_starts_run_with_preserved_argument_boundaries(
    tmp_path: Path,
    powershell_host: str,
) -> None:
    workspace = tmp_path / "素材 workspace"
    workspace.mkdir()
    input_path = tmp_path / "input script.json"
    plans_path = tmp_path / "query plans.json"
    input_path.write_text("{}", encoding="utf-8")
    plans_path.write_text("{}", encoding="utf-8")
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        ":capture\n"
        'if "%~1"=="" goto captured\n'
        '>>"%FAKE_ARGS_PATH%" echo(%~1\n'
        "shift\n"
        "goto capture\n"
        ":captured\n"
        "echo authentication_started: session_id=ses_test 1>&2\n"
        "ping -n 3 127.0.0.1 >nul",
        encoding="utf-8",
    )
    captured_args = tmp_path / "captured-args.txt"
    environment = {**os.environ, "FAKE_ARGS_PATH": str(captured_args)}

    completed = subprocess.run(
        [
            powershell_host,
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "run",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-InputPath",
            str(input_path),
            "-QueryPlansPath",
            str(plans_path),
            "-MaxRounds",
            "2",
            "-MaxVideos",
            "7",
            "-BrowserChannel",
            "chrome",
            "-ShowSearchBrowsers",
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "started"
    assert payload["operation"] == "run"
    assert payload["session_id"] == "ses_test"
    assert isinstance(payload["process_id"], int)
    assert isinstance(payload["collector_process_id"], int)
    assert payload["collector_process_id"] != payload["process_id"]
    assert Path(payload["stdout_path"]).is_file()
    assert Path(payload["stderr_path"]).is_file()
    assert Path(payload["control_path"]).is_file()

    deadline = time.monotonic() + 3
    captured_lines: list[str] = []
    while time.monotonic() < deadline:
        if captured_args.exists():
            captured_lines = captured_args.read_text(encoding="utf-8").splitlines()
            if len(captured_lines) == 19:
                break
        time.sleep(0.05)
    assert captured_lines == [
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
        "2",
        "--max-videos",
        "7",
        "--browser-channel",
        "chrome",
        "--show-search-browsers",
        "--progress-format",
        "jsonl",
    ]


def test_skill_runner_observes_flushed_session_event_before_child_exits(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    input_path.write_text("{}", encoding="utf-8")
    plans_path.write_text("{}", encoding="utf-8")
    fake_cli = tmp_path / "fake-collector.ps1"
    fake_cli.write_text(
        "[Console]::Error.WriteLine("
        "'{\"event\":\"session_started\",\"session_id\":\"ses_streamed\"}'"
        ")\n"
        "[Console]::Error.Flush()\n"
        "Start-Sleep -Seconds 5\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "run",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-InputPath",
            str(input_path),
            "-QueryPlansPath",
            str(plans_path),
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "started"
    assert payload["session_id"] == "ses_streamed"
    assert "ses_streamed" in Path(payload["stderr_path"]).read_text(encoding="utf-8")
    subprocess.run(
        ["taskkill", "/PID", str(payload["process_id"]), "/T", "/F"],
        check=False,
        capture_output=True,
    )


def test_skill_runner_preserves_immediate_cli_validation_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    input_path.write_text("{}", encoding="utf-8")
    plans_path.write_text("{}", encoding="utf-8")
    fake_cli = tmp_path / "fake-collector.ps1"
    fake_cli.write_text(
        "[Console]::Out.WriteLine("
        "'{\"schema_version\":\"1.0\",\"status\":\"error\","
        "\"error\":{\"code\":\"contract_invalid\"}}'"
        ")\n"
        "exit 40\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "run",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-InputPath",
            str(input_path),
            "-QueryPlansPath",
            str(plans_path),
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "collector_exited_during_start"
    execution = payload["execution"]
    assert execution["status"] == "exited"
    assert Path(execution["control_path"]).is_file()
    cli_payload = json.loads(
        Path(execution["stdout_path"]).read_text(encoding="utf-8")
    )
    assert cli_payload["error"]["code"] == "contract_invalid"


def test_skill_runner_waits_for_resume_child_startup_handshake(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" (\n'
        '  echo {"schema_version":"1.0","runtime":{"state":"idle"}}\n'
        "  exit /b 0\n"
        ")\n"
        'powershell -NoProfile -Command "Start-Sleep -Milliseconds 250"\n'
        'echo {"schema_version":"1.0","status":"error",'
        '"error":{"code":"resume_invalid"}}\n'
        "exit /b 40\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "resume",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-SessionId",
            "ses_resume",
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "collector_exited_during_start"
    assert payload["execution"]["status"] == "exited"
    cli_payload = json.loads(
        Path(payload["execution"]["stdout_path"]).read_text(encoding="utf-8")
    )
    assert cli_payload["error"]["code"] == "resume_invalid"


def test_skill_runner_rejects_second_executor_for_same_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" (\n'
        '  echo {"schema_version":"1.0","runtime":{"state":"idle"}}\n'
        "  exit /b 0\n"
        ")\n"
        "ping -n 6 127.0.0.1 >nul\n",
        encoding="utf-8",
    )
    control = tmp_path / "control"
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(RUNNER),
        "-Operation",
        "resume",
        "-CollectorPath",
        str(fake_cli),
        "-Workspace",
        str(workspace),
        "-SessionId",
        "ses_test",
        "-ControlDirectory",
        str(control),
    ]

    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    try:
        second = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert second.returncode == 30
        payload = json.loads(second.stdout)
        assert payload["error"]["code"] == "execution_already_running"
        assert payload["existing"]["process_id"] == first_payload["process_id"]
        assert payload["existing"]["session_id"] == "ses_test"
    finally:
        subprocess.run(
            [
                "taskkill",
                "/PID",
                str(first_payload["process_id"]),
                "/T",
                "/F",
            ],
            check=False,
            capture_output=True,
        )


def test_skill_runner_rejects_resume_when_only_collector_child_survives(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" (\n'
        '  echo {"schema_version":"1.0","runtime":{"state":"idle"}}\n'
        "  exit /b 0\n"
        ")\n"
        "ping -n 20 127.0.0.1 >nul\n",
        encoding="utf-8",
    )
    control = tmp_path / "control"
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(RUNNER),
        "-Operation",
        "resume",
        "-CollectorPath",
        str(fake_cli),
        "-Workspace",
        str(workspace),
        "-SessionId",
        "ses_orphan_child",
        "-ControlDirectory",
        str(control),
    ]
    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert first.returncode == 0, first.stderr
    first_payload = json.loads(first.stdout)
    wrapper_pid = int(first_payload["process_id"])
    collector_pid = int(first_payload["collector_process_id"])
    try:
        subprocess.run(
            ["taskkill", "/PID", str(wrapper_pid), "/F"],
            check=False,
            capture_output=True,
        )
        time.sleep(0.1)

        second = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        assert second.returncode == 30
        payload = json.loads(second.stdout)
        assert payload["error"]["code"] == "execution_already_running"
        assert payload["existing"]["collector_process_id"] == collector_pid
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(collector_pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )


def test_skill_runner_serializes_concurrent_launches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" (\n'
        '  echo {"schema_version":"1.0","runtime":{"state":"idle"}}\n'
        "  exit /b 0\n"
        ")\n"
        "ping -n 6 127.0.0.1 >nul\n",
        encoding="utf-8",
    )
    command = [
        "pwsh",
        "-NoProfile",
        "-File",
        str(RUNNER),
        "-Operation",
        "resume",
        "-CollectorPath",
        str(fake_cli),
        "-Workspace",
        str(workspace),
        "-SessionId",
        "ses_race",
        "-ControlDirectory",
        str(tmp_path / "control"),
    ]

    launches = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=15) for process in launches]
    payloads = [json.loads(stdout) for stdout, _stderr in results]
    return_codes = [process.returncode for process in launches]

    assert sorted(return_codes) == [0, 30]
    started = next(payload for payload in payloads if payload["status"] == "started")
    rejected = next(payload for payload in payloads if payload["status"] == "error")
    assert rejected["error"]["code"] == "execution_already_running"
    try:
        assert rejected["existing"]["process_id"] == started["process_id"]
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(started["process_id"]), "/T", "/F"],
            check=False,
            capture_output=True,
        )


def test_skill_runner_allows_independent_runs_in_one_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.ps1"
    fake_cli.write_text(
        "[Console]::Error.WriteLine("
        "'{\"event\":\"session_started\",\"session_id\":\"ses_' + $PID + '\"}'"
        ")\n"
        "[Console]::Error.Flush()\n"
        "Start-Sleep -Seconds 5\n",
        encoding="utf-8",
    )
    control = tmp_path / "control"
    started: list[dict[str, object]] = []
    try:
        for index in range(2):
            input_path = tmp_path / f"input-{index}.json"
            plans_path = tmp_path / f"plans-{index}.json"
            input_path.write_text("{}", encoding="utf-8")
            plans_path.write_text("{}", encoding="utf-8")
            completed = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(RUNNER),
                    "-Operation",
                    "run",
                    "-CollectorPath",
                    str(fake_cli),
                    "-Workspace",
                    str(workspace),
                    "-InputPath",
                    str(input_path),
                    "-QueryPlansPath",
                    str(plans_path),
                    "-ControlDirectory",
                    str(control),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert completed.returncode == 0, completed.stdout
            started.append(json.loads(completed.stdout))

        assert started[0]["session_id"] != started[1]["session_id"]
    finally:
        for payload in started:
            subprocess.run(
                ["taskkill", "/PID", str(payload["process_id"]), "/T", "/F"],
                check=False,
                capture_output=True,
            )


def test_skill_runner_preserves_control_when_session_id_is_not_observed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    input_path.write_text("{}", encoding="utf-8")
    plans_path.write_text("{}", encoding="utf-8")
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\nping -n 8 127.0.0.1 >nul\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "run",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-InputPath",
            str(input_path),
            "-QueryPlansPath",
            str(plans_path),
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "session_id_not_observed"
    execution = payload["execution"]
    assert execution["status"] == "starting"
    assert Path(execution["control_path"]).is_file()
    assert json.loads(Path(execution["control_path"]).read_text(encoding="utf-8"))[
        "process_id"
    ] == execution["process_id"]
    subprocess.run(
        ["taskkill", "/PID", str(execution["process_id"]), "/T", "/F"],
        check=False,
        capture_output=True,
    )


def test_skill_runner_checks_cli_runtime_before_resume(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "resume-started.txt"
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" (\n'
        '  echo {"schema_version":"1.0","runtime":{"state":"executing"}}\n'
        "  exit /b 0\n"
        ")\n"
        f'echo started>"{marker}"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "resume",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-SessionId",
            "ses_live",
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "execution_already_running"
    assert payload["existing"]["runtime"]["state"] == "executing"
    assert not marker.exists()


def test_skill_runner_refuses_resume_when_cli_status_is_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "resume-started.txt"
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'if "%~1"=="status" exit /b 40\n'
        f'echo started>"{marker}"\n',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Operation",
            "resume",
            "-CollectorPath",
            str(fake_cli),
            "-Workspace",
            str(workspace),
            "-SessionId",
            "ses_unknown",
            "-ControlDirectory",
            str(tmp_path / "control"),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 30
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "session_status_unavailable"
    assert not marker.exists()


def test_skill_runner_relays_status_and_cancel_with_exact_arguments(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "素材 workspace"
    workspace.mkdir()
    fake_cli = tmp_path / "fake-collector.cmd"
    fake_cli.write_text(
        "@echo off\n"
        'echo {"schema_version":"1.0","operation":"%~1"}\n'
        ':capture\n'
        'if "%~1"=="" exit /b 0\n'
        '>>"%FAKE_ARGS_PATH%" echo(%~1\n'
        "shift\n"
        "goto capture\n",
        encoding="utf-8",
    )

    for operation in ("status", "cancel"):
        captured_args = tmp_path / f"{operation}-args.txt"
        completed = subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-File",
                str(RUNNER),
                "-Operation",
                operation,
                "-CollectorPath",
                str(fake_cli),
                "-Workspace",
                str(workspace),
                "-SessionId",
                "ses_test",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "FAKE_ARGS_PATH": str(captured_args)},
        )

        assert completed.returncode == 0
        assert json.loads(completed.stdout)["operation"] == operation
        assert captured_args.read_text(encoding="utf-8").splitlines() == [
            operation,
            "--workspace",
            str(workspace),
            "--session-id",
            "ses_test",
        ]
