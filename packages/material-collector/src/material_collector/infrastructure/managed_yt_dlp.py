"""Side-by-side, conformance-gated yt-dlp nightly runtime manager."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from material_collector.core.errors import CollectorError

_CHECK_INTERVAL = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


@dataclass(frozen=True)
class FrozenYtDlpRuntime:
    version: str
    python_executable: Path
    package_hash: str


class ManagedYtDlpRuntime:
    """Own install/activate/freeze behavior; callers only receive a tested runtime."""

    def __init__(
        self,
        root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], datetime] = _utc_now,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.root = root
        self._runner = runner
        self._now = now
        self._environment = dict(environment) if environment is not None else None
        self.versions = root / "versions"
        self.state_path = root / "state.json"

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"schema_version": "managed-yt-dlp-runtime/v1"}
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        if value.get("schema_version") != "managed-yt-dlp-runtime/v1":
            raise CollectorError(
                "managed_runtime_unavailable",
                "The managed yt-dlp runtime state has an unsupported schema.",
            )
        return cast(dict[str, Any], value)

    @staticmethod
    def _python(runtime: Path) -> Path:
        return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _run(self, arguments: Sequence[str], *, timeout: int = 300) -> str:
        completed = self._runner(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=self._environment,
        )
        if completed.returncode != 0:
            raise CollectorError(
                "managed_runtime_update_failed",
                "A managed yt-dlp runtime command failed.",
                details={"returncode": completed.returncode},
            )
        return completed.stdout.strip()

    def _runtime_record(self, version: str, state: dict[str, Any]) -> FrozenYtDlpRuntime:
        record = dict(state.get("versions", {})).get(version)
        runtime = self.versions / version
        python = self._python(runtime)
        if not isinstance(record, dict) or not python.is_file():
            raise CollectorError(
                "managed_runtime_unavailable",
                "The activated yt-dlp runtime is missing or incomplete.",
                details={"version": version},
            )
        return FrozenYtDlpRuntime(
            version=version,
            python_executable=python,
            package_hash=str(record["package_hash"]),
        )

    def active(self) -> FrozenYtDlpRuntime:
        state = self._read_state()
        version = state.get("active_version")
        if not isinstance(version, str) or not version:
            raise CollectorError(
                "managed_runtime_unavailable",
                "No conformance-tested yt-dlp runtime is active.",
            )
        return self._runtime_record(version, state)

    def freeze_for_session(self, requested_version: str | None = None) -> FrozenYtDlpRuntime:
        state = self._read_state()
        version = requested_version or state.get("active_version")
        if not isinstance(version, str) or not version:
            raise CollectorError(
                "managed_runtime_unavailable",
                "No yt-dlp runtime can be frozen for this session.",
            )
        return self._runtime_record(version, state)

    def update_due(self) -> bool:
        checked = self._read_state().get("checked_at")
        if not isinstance(checked, str):
            return True
        try:
            checked_at = datetime.fromisoformat(checked)
        except ValueError:
            return True
        return self._now() - checked_at >= _CHECK_INTERVAL

    def install_candidate(self) -> FrozenYtDlpRuntime:
        """Install official PyPI nightly plus default/EJS dependencies and activate on pass."""

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="candidate-", dir=self.root))
        try:
            self._run([sys.executable, "-m", "venv", str(staging)])
            python = self._python(staging)
            report = staging / "install-report.json"
            self._run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--pre",
                    "--upgrade",
                    "--report",
                    str(report),
                    "yt-dlp[default]",
                ],
                timeout=600,
            )
            version = self._run([str(python), "-m", "yt_dlp", "--version"])
            if not version or any(character in version for character in "\\/:"):
                raise CollectorError(
                    "managed_runtime_update_failed",
                    "The candidate yt-dlp version is not a safe runtime identifier.",
                )
            package_hash = self._yt_dlp_hash(report)
            self._conformance_test(python)
            destination = self.versions / version
            self.versions.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                self._remove_staging(staging)
            else:
                os.replace(staging, destination)
            state = self._read_state()
            versions = dict(state.get("versions", {}))
            versions[version] = {
                "package_hash": package_hash,
                "activated_at": self._timestamp(),
                "channel": "official_pypi_nightly",
            }
            previous = state.get("active_version")
            _atomic_json(
                self.state_path,
                {
                    "schema_version": "managed-yt-dlp-runtime/v1",
                    "active_version": version,
                    "last_known_good_version": previous or version,
                    "checked_at": self._timestamp(),
                    "versions": versions,
                },
            )
            return self.active()
        except CollectorError:
            self._record_failed_check()
            raise
        finally:
            if staging.exists():
                self._remove_staging(staging)

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds").replace("+00:00", "Z")

    def _remove_staging(self, staging: Path) -> None:
        resolved = staging.resolve()
        if resolved.parent != self.root.resolve() or not resolved.name.startswith("candidate-"):
            raise CollectorError(
                "managed_runtime_path_invalid",
                "Refusing unsafe yt-dlp runtime cleanup.",
            )
        shutil.rmtree(resolved, ignore_errors=True)

    def _record_failed_check(self) -> None:
        state = self._read_state()
        state["schema_version"] = "managed-yt-dlp-runtime/v1"
        state["checked_at"] = self._timestamp()
        state["last_update_status"] = "failed"
        _atomic_json(self.state_path, state)

    @staticmethod
    def _yt_dlp_hash(report: Path) -> str:
        value = json.loads(report.read_text(encoding="utf-8"))
        for installed in value.get("install", []):
            metadata = installed.get("metadata", {})
            if metadata.get("name", "").lower().replace("_", "-") != "yt-dlp":
                continue
            hashes = installed.get("download_info", {}).get("archive_info", {}).get(
                "hashes", {}
            )
            sha256 = hashes.get("sha256")
            if isinstance(sha256, str) and len(sha256) == 64:
                return sha256
        raise CollectorError(
            "managed_runtime_update_failed",
            "The yt-dlp wheel hash was absent from pip's installation report.",
        )

    def _conformance_test(self, python: Path) -> None:
        extractors = self._run([str(python), "-m", "yt_dlp", "--list-extractors"])
        required = {"youtube", "tiktok"}
        available = {line.strip().casefold() for line in extractors.splitlines()}
        if not required.issubset(available):
            raise CollectorError(
                "managed_runtime_update_failed",
                "The candidate yt-dlp runtime lacks a required named extractor.",
                details={"missing_extractors": sorted(required - available)},
            )

    def rollback(self) -> FrozenYtDlpRuntime:
        state = self._read_state()
        version = state.get("last_known_good_version")
        if not isinstance(version, str):
            raise CollectorError(
                "managed_runtime_unavailable",
                "No last-known-good yt-dlp runtime is available.",
            )
        state["active_version"] = version
        state["last_update_status"] = "rolled_back"
        _atomic_json(self.state_path, state)
        return self._runtime_record(version, state)
