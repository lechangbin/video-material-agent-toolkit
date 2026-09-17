"""Locate and validate the Semvideo CLI without relying on the host Agent PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_CLI_VERSION = "0.2.0"
EXPECTED_SKILL_PROTOCOL = 1
EXPECTED_WORKSPACE_SCHEMA_MAX = 1
EXPECTED_JOB_SCHEMA_MAX = 1


def _expected_contract() -> dict[str, object]:
    return {
        "cli_version": EXPECTED_CLI_VERSION,
        "workspace_schema_max": EXPECTED_WORKSPACE_SCHEMA_MAX,
        "job_schema_max": EXPECTED_JOB_SCHEMA_MAX,
        "skill_protocol_version": EXPECTED_SKILL_PROTOCOL,
    }


def _automatic_candidates() -> list[Path]:
    candidates: list[Path] = []
    on_path = shutil.which("semvideo")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(Path(sys.executable).with_name("semvideo.exe"))
    if os.name == "nt":
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        local = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")
        )
        candidates.extend(
            sorted(
                (roaming / "Python").glob("Python*/Scripts/semvideo.exe"),
                reverse=True,
            )
        )
        candidates.extend(
            sorted(
                (local / "Programs/Python").glob(
                    "Python*/Scripts/semvideo.exe"
                ),
                reverse=True,
            )
        )
    else:
        candidates.append(Path.home() / ".local/bin/semvideo")
    return candidates


def _candidates() -> list[Path]:
    explicit = os.environ.get("SEMVIDEO_CLI")
    raw = [Path(explicit)] if explicit else _automatic_candidates()
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in raw:
        resolved = candidate.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _probe_cli(
    candidate: Path,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    if not candidate.is_file():
        return None, {"code": "semvideo_cli_not_found"}
    try:
        result = subprocess.run(
            [str(candidate), "--version", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, {"code": "semvideo_cli_unavailable"}
    if result.returncode != 0 or not isinstance(payload, dict):
        return None, {"code": "semvideo_cli_unavailable"}
    workspace_schema = payload.get("workspace_schema")
    job_schema = payload.get("job_schema")
    actual = {
        "cli_version": payload.get("cli_version"),
        "workspace_schema_max": (
            workspace_schema.get("max")
            if isinstance(workspace_schema, dict)
            else None
        ),
        "job_schema_max": (
            job_schema.get("max") if isinstance(job_schema, dict) else None
        ),
        "skill_protocol_version": payload.get("skill_protocol_version"),
    }
    expected = _expected_contract()
    if (
        actual["cli_version"] != expected["cli_version"]
        or actual["skill_protocol_version"]
        != expected["skill_protocol_version"]
        or actual["workspace_schema_max"]
        != expected["workspace_schema_max"]
        or actual["job_schema_max"] != expected["job_schema_max"]
    ):
        return None, {
            "schema_version": 1,
            "ok": False,
            "code": "semvideo_cli_incompatible",
            "candidate": str(candidate),
            "expected": expected,
            "actual": actual,
            "recovery": {
                "action": "install_matching_package",
                "package": f"semvideo=={EXPECTED_CLI_VERSION}",
            },
        }
    return payload, {}


def main() -> int:
    failures: list[dict[str, object]] = []
    for candidate in _candidates():
        version, failure = _probe_cli(candidate)
        if version is None:
            failures.append(failure)
            continue
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": True,
                    "command": str(candidate),
                    **version,
                },
                ensure_ascii=False,
            )
        )
        return 0
    incompatible = next(
        (
            failure
            for failure in failures
            if failure.get("code") == "semvideo_cli_incompatible"
        ),
        None,
    )
    if incompatible is not None:
        print(json.dumps(incompatible, ensure_ascii=False), file=sys.stderr)
        return 4
    print(
        json.dumps(
            {
                "schema_version": 1,
                "ok": False,
                "code": "semvideo_cli_not_found",
                "expected": _expected_contract(),
                "recovery": {
                    "action": "install_matching_package",
                    "package": f"semvideo=={EXPECTED_CLI_VERSION}",
                },
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
