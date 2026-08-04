from __future__ import annotations

import json
from pathlib import Path

import pytest

from semvideo.application.workspace import (
    InvalidWorkspaceError,
    WorkspaceNotFoundError,
    discover_workspace,
    initialize_workspace,
)
from semvideo.config import load_workspace_config


def test_initialize_workspace_creates_contract_layout_and_valid_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"

    workspace = initialize_workspace(root)

    assert workspace.root == root.resolve()
    assert workspace.marker.is_file()
    assert workspace.config.is_file()
    assert workspace.sources.is_dir()
    assert workspace.jobs.is_dir()
    assert workspace.locks.is_dir()
    assert workspace.provider_cooldowns.is_dir()
    marker = json.loads(workspace.marker.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 1
    assert marker["workspace_id"].startswith("workspace_")
    config = load_workspace_config(workspace.data)
    assert config.concurrency.media == 2
    assert config.concurrency.ffmpeg_cpu == 2
    assert config.llm.credential_env == "SEMVIDEO_API_KEY"


def test_initialize_workspace_is_idempotent_and_preserves_local_config(
    tmp_path: Path,
) -> None:
    first = initialize_workspace(tmp_path)
    marker_before = first.marker.read_bytes()
    first.config.write_text("custom = true\n", encoding="utf-8")

    second = initialize_workspace(tmp_path)

    assert second == first
    assert second.marker.read_bytes() == marker_before
    assert second.config.read_text(encoding="utf-8") == "custom = true\n"


def test_reinitialization_repairs_missing_standard_directories(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path)
    workspace.locks.rmdir()

    initialize_workspace(tmp_path)

    assert workspace.locks.is_dir()


def test_discover_workspace_searches_upward_and_explicit_root_wins(
    tmp_path: Path,
) -> None:
    outer = initialize_workspace(tmp_path / "outer")
    inner = initialize_workspace(outer.root / "nested" / "inner")
    deep = inner.root / "a" / "b"
    deep.mkdir(parents=True)

    assert discover_workspace(deep) == inner
    assert discover_workspace(deep, explicit=outer.root) == outer
    assert discover_workspace(deep, explicit=outer.data) == outer


def test_discovery_never_initializes_implicitly(tmp_path: Path) -> None:
    start = tmp_path / "not-initialized" / "child"
    start.mkdir(parents=True)

    with pytest.raises(WorkspaceNotFoundError):
        discover_workspace(start)

    assert not (tmp_path / "semvideo-data").exists()
    assert not (start / "semvideo-data").exists()


def test_invalid_marker_schema_is_rejected(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path)
    workspace.marker.write_text(
        '{"schema_version":999,"workspace_id":"workspace_old"}\n',
        encoding="utf-8",
    )

    with pytest.raises(InvalidWorkspaceError):
        discover_workspace(tmp_path)
