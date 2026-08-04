"""Load mandatory Semvideo workspace context through public CLI commands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class ContextGateError(RuntimeError):
    def __init__(self, payload: dict[str, Any], exit_code: int = 4) -> None:
        super().__init__(str(payload.get("code") or "context_gate_failed"))
        self.payload = payload
        self.exit_code = exit_code


def _json_from_output(value: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload
    for line in reversed(value.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _resolve_cli() -> dict[str, Any]:
    resolver = Path(__file__).with_name("resolve_semvideo.py")
    result = subprocess.run(
        [sys.executable, str(resolver)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    payload = _json_from_output(
        result.stdout if result.returncode == 0 else result.stderr
    )
    if result.returncode != 0 or payload is None or not payload.get("ok"):
        raise ContextGateError(
            payload
            or {
                "schema_version": 1,
                "ok": False,
                "code": "semvideo_cli_resolution_failed",
            },
            result.returncode or 4,
        )
    return payload


def _run_json(command: str, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        [command, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    payload = _json_from_output(
        result.stdout if result.returncode == 0 else result.stderr
    )
    if result.returncode != 0 or payload is None:
        raise ContextGateError(
            payload
            or {
                "schema_version": 1,
                "ok": False,
                "code": "semvideo_context_command_failed",
                "command": list(arguments[:2]),
            },
            result.returncode or 4,
        )
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semvideo-load-context")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--profile", default="default")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = str(Path(args.workspace).expanduser().resolve())
    try:
        resolved = _resolve_cli()
        command = str(resolved["command"])
        workspace_context = _run_json(
            command,
            "workspace",
            "show",
            "--workspace",
            workspace,
            "--json",
        )
        config = _run_json(
            command,
            "config",
            "show",
            "--workspace",
            workspace,
            "--json",
        )
        profile = _run_json(
            command,
            "profile",
            "validate",
            args.profile,
            "--workspace",
            workspace,
            "--json",
        )
        doctor = _run_json(
            command,
            "doctor",
            "--workspace",
            workspace,
            "--json",
        )
        jobs = _run_json(
            command,
            "job",
            "list",
            "--workspace",
            workspace,
            "--json",
        )
        admission = _run_json(
            command,
            "job",
            "admission",
            "--workspace",
            workspace,
            "--json",
        )
    except ContextGateError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False), file=sys.stderr)
        return exc.exit_code

    profile_ok = bool(profile.get("valid", True))
    doctor_ok = bool(doctor.get("ok"))
    payload = {
        "schema_version": 1,
        "ok": doctor_ok and profile_ok,
        "command": command,
        "workspace": workspace_context,
        "config": config,
        "profile": profile,
        "doctor": doctor,
        "jobs": jobs,
        "admission": admission,
    }
    stream = sys.stdout if payload["ok"] else sys.stderr
    print(json.dumps(payload, ensure_ascii=False), file=stream)
    return 0 if payload["ok"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
