"""Explicit initialization and discovery of a Semvideo workspace."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from semvideo.config import DEFAULT_CONFIG_TOML
from semvideo.infrastructure.io import atomic_write_bytes, atomic_write_json, read_json

DATA_DIRECTORY_NAME: Final = "semvideo-data"
WORKSPACE_MARKER_NAME: Final = "workspace.json"
CONFIG_NAME: Final = "config.toml"
WORKSPACE_SCHEMA_VERSION: Final = 1

_INITIAL_DIRECTORIES = (
    "sources",
    "jobs",
    "runtime/locks",
    "runtime/provider-cooldowns",
)

class WorkspaceError(RuntimeError):
    """Base class for workspace errors."""


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when no initialized workspace can be found."""


class InvalidWorkspaceError(WorkspaceError):
    """Raised when a marker exists but is invalid."""


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Resolved paths belonging to one initialized Semvideo workspace."""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / DATA_DIRECTORY_NAME

    @property
    def marker(self) -> Path:
        return self.data / WORKSPACE_MARKER_NAME

    @property
    def config(self) -> Path:
        return self.data / CONFIG_NAME

    @property
    def sources(self) -> Path:
        return self.data / "sources"

    @property
    def jobs(self) -> Path:
        return self.data / "jobs"

    @property
    def runtime(self) -> Path:
        return self.data / "runtime"

    @property
    def locks(self) -> Path:
        return self.runtime / "locks"

    @property
    def provider_cooldowns(self) -> Path:
        return self.runtime / "provider-cooldowns"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate(paths: WorkspacePaths) -> WorkspacePaths:
    if not paths.marker.is_file():
        raise WorkspaceNotFoundError(
            f"{paths.root} is not an initialized Semvideo workspace"
        )
    try:
        marker = read_json(paths.marker)
    except (OSError, ValueError) as error:
        raise InvalidWorkspaceError(f"cannot read {paths.marker}: {error}") from error
    if marker.get("schema_version") != WORKSPACE_SCHEMA_VERSION:
        raise InvalidWorkspaceError(
            f"unsupported workspace schema in {paths.marker}: "
            f"{marker.get('schema_version')!r}"
        )
    if not isinstance(marker.get("workspace_id"), str) or not marker["workspace_id"]:
        raise InvalidWorkspaceError(f"missing workspace_id in {paths.marker}")
    return paths


def initialize_workspace(root: str | Path) -> WorkspacePaths:
    """Explicitly initialize *root*, idempotently preserving its identity."""

    resolved_root = Path(root).expanduser().resolve()
    if resolved_root.exists() and not resolved_root.is_dir():
        raise WorkspaceError(f"workspace root is not a directory: {resolved_root}")
    paths = WorkspacePaths(resolved_root)
    if paths.marker.exists():
        _validate(paths)

    paths.data.mkdir(parents=True, exist_ok=True)
    for relative in _INITIAL_DIRECTORIES:
        (paths.data / relative).mkdir(parents=True, exist_ok=True)

    if not paths.config.exists():
        atomic_write_bytes(paths.config, DEFAULT_CONFIG_TOML.encode("utf-8"))
    if not paths.marker.exists():
        marker = {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "workspace_id": f"workspace_{secrets.token_hex(12)}",
            "created_at": _utc_now(),
        }
        # The marker is written last: its presence means the layout is usable.
        atomic_write_json(paths.marker, marker)
    return _validate(paths)


def discover_workspace(
    start: str | Path | None = None,
    *,
    explicit: str | Path | None = None,
) -> WorkspacePaths:
    """Resolve an explicit root or search from *start* toward filesystem root."""

    if explicit is not None:
        explicit_root = Path(explicit).expanduser().resolve()
        if explicit_root.name == DATA_DIRECTORY_NAME:
            explicit_root = explicit_root.parent
        return _validate(WorkspacePaths(explicit_root))

    current = Path.cwd() if start is None else Path(start).expanduser()
    current = current.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        paths = WorkspacePaths(candidate)
        if paths.marker.is_file():
            return _validate(paths)
    raise WorkspaceNotFoundError(
        f"no {DATA_DIRECTORY_NAME}/{WORKSPACE_MARKER_NAME} found from {current}; "
        "run 'semvideo init' first or pass --workspace"
    )


def read_workspace_marker(workspace: WorkspacePaths) -> dict[str, object]:
    """Read and validate the workspace marker."""

    _validate(workspace)
    return read_json(workspace.marker)
