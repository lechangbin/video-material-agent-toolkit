"""Detached worker used internally by the collector-owned executor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from material_collector.infrastructure.executor import (
    _command_environment,
    _command_with_arguments,
    _process_start_ticks,
)


def main() -> int:
    payload_path = Path(sys.argv[1])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    stdout_path = Path(payload["stdout_path"])
    stderr_path = Path(payload["stderr_path"])
    handshake_path = Path(payload["handshake_path"])
    control_path = Path(payload["control_path"])
    command = _command_with_arguments(
        payload["collector_command"],
        payload["arguments"],
    )
    with (
        stdout_path.open("wb", buffering=0) as stdout_file,
        stderr_path.open("wb", buffering=0) as stderr_file,
    ):
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_file,
            "stderr": stderr_file,
            "close_fds": True,
            "env": _command_environment(payload["collector_command"]),
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            child = subprocess.Popen(command, **options)
        except OSError as error:
            _write_json(
                handshake_path,
                {"error": type(error).__name__, "message": str(error)},
            )
            return 30
        started_at = datetime.now(UTC).isoformat()
        _write_json(
            handshake_path,
            {
                "process_id": child.pid,
                "process_start_time": started_at,
                "process_start_ticks": _process_start_ticks(child.pid),
            },
        )
        exit_code = child.wait()
    _record_exit(control_path, exit_code)
    return exit_code


def _record_exit(control_path: Path, exit_code: int) -> None:
    for _attempt in range(20):
        try:
            control = json.loads(control_path.read_text(encoding="utf-8-sig"))
        except OSError, json.JSONDecodeError:
            time.sleep(0.05)
            continue
        if isinstance(control, dict):
            control["status"] = "exited"
            control["exit_code"] = exit_code
            control["exited_at"] = datetime.now(UTC).isoformat()
            _write_json(control_path, control)
        return


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
