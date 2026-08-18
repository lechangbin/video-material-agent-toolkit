"""Locate and validate Material Collector without asking an Agent to probe PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_CLI_VERSION = "0.2.0"
EXPECTED_SKILL_PROTOCOL = 1
EXPECTED_COLLECTION_INPUT_SCHEMA = {"min": "1.0", "max": "1.0"}
EXPECTED_QUERY_PLANS_SCHEMA = {"min": "2.0", "max": "2.0"}


def _expected_contract() -> dict[str, object]:
    return {
        "cli_version": EXPECTED_CLI_VERSION,
        "skill_protocol_version": EXPECTED_SKILL_PROTOCOL,
        "collection_input_schema": EXPECTED_COLLECTION_INPUT_SCHEMA,
        "query_plans_schema": EXPECTED_QUERY_PLANS_SCHEMA,
    }


def _automatic_candidates() -> list[Path]:
    candidates: list[Path] = []
    on_path = shutil.which("material-collector")
    if on_path:
        candidates.append(Path(on_path))
    executable_name = "material-collector.exe" if os.name == "nt" else "material-collector"
    candidates.append(Path(sys.executable).with_name(executable_name))
    candidates.append(Path.home() / ".local" / "bin" / executable_name)
    if os.name == "nt":
        roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        candidates.extend(
            sorted(
                (roaming / "Python").glob("Python*/Scripts/material-collector.exe"),
                reverse=True,
            )
        )
        candidates.extend(
            sorted(
                (local / "Programs/Python").glob(
                    "Python*/Scripts/material-collector.exe"
                ),
                reverse=True,
            )
        )
    return candidates


def _candidates() -> list[Path]:
    explicit = os.environ.get("MATERIAL_COLLECTOR_CLI")
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


def _probe_cli(candidate: Path) -> tuple[dict[str, object] | None, dict[str, object]]:
    if not candidate.is_file():
        return None, {"code": "material_collector_cli_not_found"}
    try:
        result = subprocess.run(
            [str(candidate), "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None, {"code": "material_collector_cli_unavailable"}
    if result.returncode != 0 or not isinstance(payload, dict):
        return None, {"code": "material_collector_cli_unavailable"}
    actual = {
        "cli_version": payload.get("cli_version"),
        "skill_protocol_version": payload.get("skill_protocol_version"),
        "collection_input_schema": payload.get("collection_input_schema"),
        "query_plans_schema": payload.get("query_plans_schema"),
    }
    expected = _expected_contract()
    if actual != expected:
        return None, {
            "schema_version": 1,
            "ok": False,
            "code": "material_collector_cli_incompatible",
            "candidate": str(candidate),
            "expected": expected,
            "actual": actual,
            "recovery": {
                "action": "install_matching_package",
                "package": f"video-material-collector=={EXPECTED_CLI_VERSION}",
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
                    **version,
                    "schema_version": 1,
                    "ok": True,
                    "command": str(candidate),
                },
                ensure_ascii=False,
            )
        )
        return 0
    incompatible = next(
        (
            failure
            for failure in failures
            if failure.get("code") == "material_collector_cli_incompatible"
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
                "code": "material_collector_cli_not_found",
                "expected": _expected_contract(),
                "recovery": {
                    "action": "install_matching_package",
                    "package": f"video-material-collector=={EXPECTED_CLI_VERSION}",
                },
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
