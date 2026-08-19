from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parents[4] / "skills" / "semvideo"


def _load_resolver():
    path = SKILL_ROOT / "scripts" / "resolve_semvideo.py"
    spec = importlib.util.spec_from_file_location("semvideo_skill_resolver", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_context_gate():
    path = SKILL_ROOT / "scripts" / "load_context.py"
    spec = importlib.util.spec_from_file_location("semvideo_skill_context", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_resolver_returns_compatible_absolute_cli_path() -> None:
    resolver = SKILL_ROOT / "scripts" / "resolve_semvideo.py"
    resolved_cli = shutil.which("semvideo")
    assert resolved_cli is not None
    cli = Path(resolved_cli)
    environment = os.environ.copy()
    environment["SEMVIDEO_CLI"] = str(cli)

    result = subprocess.run(
        [sys.executable, str(resolver)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["command"] == str(cli.resolve())
    assert payload["cli_version"] == "0.2.0"
    assert payload["skill_protocol_version"] == 1


def test_skill_resolver_reports_explicit_incompatible_cli(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    resolver = _load_resolver()
    candidate = tmp_path / "semvideo.exe"
    candidate.touch()
    monkeypatch.setenv("SEMVIDEO_CLI", str(candidate))
    monkeypatch.setattr(
        resolver.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "cli_version": "0.1.0",
                    "workspace_schema": {"min": 1, "max": 1},
                    "job_schema": {"min": 1, "max": 1},
                    "skill_protocol_version": 1,
                }
            ),
            "",
        ),
    )

    exit_code = resolver.main()

    payload = json.loads(capsys.readouterr().err)
    assert exit_code == 4
    assert payload["code"] == "semvideo_cli_incompatible"
    assert payload["expected"]["cli_version"] == "0.2.0"
    assert payload["actual"]["cli_version"] == "0.1.0"
    assert payload["recovery"] == {
        "action": "install_matching_package",
        "package": "semvideo==0.2.0",
    }


def test_context_gate_loads_required_context_and_caps_batch_submission(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    gate = _load_context_gate()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        gate,
        "_resolve_cli",
        lambda: {
            "ok": True,
            "command": "C:/tools/semvideo.exe",
            "cli_version": "0.2.0",
        },
    )

    def fake_run(command: str, *arguments: str):
        calls.append(arguments)
        if arguments[:2] == ("workspace", "show"):
            return {"schema_version": 1, "root": str(workspace)}
        if arguments[:2] == ("config", "show"):
            return {
                "schema_version": 1,
                "profile": "default",
                "concurrency": {
                    "media": 2,
                    "asr": 1,
                    "llm": 2,
                    "render": 1,
                    "ffmpeg_cpu": 2,
                },
            }
        if arguments[:2] == ("profile", "validate"):
            return {"schema_version": 1, "name": "default", "valid": True}
        if arguments[0] == "doctor":
            return {"schema_version": 1, "ok": True}
        if arguments[:2] == ("job", "list"):
            return {
                "schema_version": 1,
                "items": [
                    {"job_id": "job_running", "state": "analyzing"},
                    {"job_id": "job_done", "state": "completed"},
                ],
            }
        if arguments[:2] == ("job", "admission"):
            return {
                "schema_version": 1,
                "configured_limit": 2,
                "active_count": 1,
                "available_submission_slots": 1,
                "active_job_ids": ["job_running"],
                "active_jobs": [
                    {"job_id": "job_running", "state": "analyzing"}
                ],
            }
        raise AssertionError(arguments)

    monkeypatch.setattr(gate, "_run_json", fake_run)

    exit_code = gate.main(
        ["--workspace", str(workspace), "--profile", "default"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["admission"]["configured_limit"] == 2
    assert payload["admission"]["active_count"] == 1
    assert payload["admission"]["available_submission_slots"] == 1
    assert payload["admission"]["active_job_ids"] == ["job_running"]
    assert [call[:2] for call in calls] == [
        ("workspace", "show"),
        ("config", "show"),
        ("profile", "validate"),
        ("doctor", "--workspace"),
        ("job", "list"),
        ("job", "admission"),
    ]


def test_context_gate_accepts_pretty_printed_cli_json(monkeypatch) -> None:
    gate = _load_context_gate()
    payload = {
        "schema_version": 1,
        "root": "C:/workspace",
    }
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(payload, ensure_ascii=False, indent=2),
            "",
        ),
    )

    assert gate._run_json(
        "C:/tools/semvideo.exe",
        "workspace",
        "show",
        "--json",
    ) == payload


def test_skill_requires_context_gate_before_mutating_commands() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    gate = (SKILL_ROOT / "scripts" / "load_context.py").read_text(
        encoding="utf-8"
    )

    assert "NON-NEGOTIABLE CONTEXT GATE" in skill
    assert "scripts/load_context.py" in skill
    assert "available_submission_slots" in skill
    assert "Never fan out a directory" in skill
    assert '"admission"' in gate
    assert "TERMINAL_STATES" not in gate
    assert "configured_limit -" not in gate
