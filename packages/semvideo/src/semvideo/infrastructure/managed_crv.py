"""Side-by-side, conformance-gated stable CRV runtime management."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import atomic_write_json

_CHECK_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class FrozenCrvRuntime:
    version: str
    executable: Path
    package_hash: str


class ManagedCrvRuntime:
    """Activate only official stable PyPI candidates that pass the public CLI seam."""

    def __init__(
        self,
        root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.root = root
        self.versions = root / "versions"
        self.state_path = root / "state.json"
        self._runner = runner
        self._now = now

    def _state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"schema_version": "managed-crv-runtime/v1"}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "managed-crv-runtime/v1":
            raise _error("crv_runtime_state_invalid", "CRV runtime state is unsupported.")
        return cast(dict[str, Any], value)

    @staticmethod
    def _executable(root: Path) -> Path:
        return root / ("Scripts/crv.exe" if os.name == "nt" else "bin/crv")

    def _record(self, version: str, state: dict[str, Any]) -> FrozenCrvRuntime:
        record = dict(state.get("versions", {})).get(version)
        executable = self._executable(self.versions / version)
        if not isinstance(record, dict) or not executable.is_file():
            raise _error("crv_runtime_unavailable", "The active CRV runtime is incomplete.")
        return FrozenCrvRuntime(
            version=version,
            executable=executable,
            package_hash=str(record["package_hash"]),
        )

    def active(self) -> FrozenCrvRuntime:
        state = self._state()
        version = state.get("active_version")
        if not isinstance(version, str):
            raise _error("crv_runtime_unavailable", "No tested CRV runtime is active.")
        return self._record(version, state)

    def freeze(self, version: str | None = None) -> FrozenCrvRuntime:
        state = self._state()
        selected = version or state.get("active_version")
        if not isinstance(selected, str):
            raise _error("crv_runtime_unavailable", "No CRV runtime can be frozen.")
        return self._record(selected, state)

    def update_due(self) -> bool:
        checked = self._state().get("checked_at")
        if not isinstance(checked, str):
            return True
        try:
            return self._now() - datetime.fromisoformat(checked) >= _CHECK_INTERVAL
        except ValueError:
            return True

    def install_candidate(self) -> FrozenCrvRuntime:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.root))
        try:
            self._run([sys.executable, "-m", "venv", str(staging)])
            python = staging / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            report = staging / "install-report.json"
            self._run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "--report",
                    str(report),
                    "claude-real-video[whisper]",
                ],
                timeout=1800,
            )
            version = self._run(
                [
                    str(python),
                    "-c",
                    "import importlib.metadata as m; print(m.version('claude-real-video'))",
                ]
            )
            if not version or any(character in version for character in "\\/:"):
                raise _error("crv_runtime_update_failed", "CRV returned an unsafe version.")
            executable = self._executable(staging)
            help_text = self._run([str(executable), "--help"])
            required = {"--grid", "--max-frames", "--no-transcribe", "--lang"}
            missing = sorted(flag for flag in required if flag not in help_text)
            if missing:
                raise _error(
                    "crv_runtime_contract_changed",
                    "The CRV candidate does not expose the required bounded interface.",
                )
            package_hash = self._package_hash(report)
            self.versions.mkdir(parents=True, exist_ok=True)
            destination = self.versions / version
            if destination.exists():
                self._remove_staging(staging)
            else:
                os.replace(staging, destination)
            state = self._state()
            versions = dict(state.get("versions", {}))
            previous = state.get("active_version")
            versions[version] = {
                "package_hash": package_hash,
                "channel": "official_pypi_stable",
                "activated_at": self._timestamp(),
            }
            atomic_write_json(
                self.state_path,
                {
                    "schema_version": "managed-crv-runtime/v1",
                    "active_version": version,
                    "last_known_good_version": previous or version,
                    "checked_at": self._timestamp(),
                    "versions": versions,
                },
            )
            return self.active()
        except SemvideoError:
            state = self._state()
            state.update(
                {
                    "schema_version": "managed-crv-runtime/v1",
                    "checked_at": self._timestamp(),
                    "last_update_status": "failed",
                }
            )
            atomic_write_json(self.state_path, state)
            raise
        finally:
            if staging.exists():
                self._remove_staging(staging)

    def rollback(self) -> FrozenCrvRuntime:
        state = self._state()
        version = state.get("last_known_good_version")
        if not isinstance(version, str):
            raise _error("crv_runtime_unavailable", "No last-known-good CRV exists.")
        state["active_version"] = version
        state["last_update_status"] = "rolled_back"
        atomic_write_json(self.state_path, state)
        return self._record(version, state)

    def _run(self, arguments: Sequence[str], *, timeout: int = 300) -> str:
        completed = self._runner(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise _error("crv_runtime_update_failed", "A CRV runtime command failed.")
        return completed.stdout.strip()

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds").replace("+00:00", "Z")

    def _remove_staging(self, staging: Path) -> None:
        resolved = staging.resolve()
        if resolved.parent != self.root.resolve() or not resolved.name.startswith("candidate-"):
            raise _error("crv_runtime_path_invalid", "Refusing unsafe runtime cleanup.")
        shutil.rmtree(resolved, ignore_errors=True)

    @staticmethod
    def _package_hash(report: Path) -> str:
        value = json.loads(report.read_text(encoding="utf-8"))
        for installed in value.get("install", []):
            metadata = installed.get("metadata", {})
            if metadata.get("name", "").lower().replace("_", "-") != "claude-real-video":
                continue
            sha256 = (
                installed.get("download_info", {})
                .get("archive_info", {})
                .get("hashes", {})
                .get("sha256")
            )
            if isinstance(sha256, str) and len(sha256) == 64:
                return sha256
        raise _error("crv_runtime_update_failed", "CRV package hash is unavailable.")


def _error(code: str, message: str) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.DEPENDENCY,
        message=message,
        recovery=RecoveryAction.REPORT_BUG,
        exit_code=4,
    )
