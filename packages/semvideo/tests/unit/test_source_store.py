from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil

import pytest

from semvideo.application.source_store import register_source, resolve_source
from semvideo.application.workspace import initialize_workspace
from semvideo.infrastructure.io import UnsupportedSchemaVersionError
from semvideo.infrastructure.locks import LockAcquisitionTimeout
from semvideo.errors import SemvideoError


def test_register_source_copies_and_deduplicates_within_workspace(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video-bytes")

    first = register_source(workspace, source)
    second = register_source(workspace, source)
    managed, metadata = resolve_source(workspace, first["source_video_id"])

    assert first["source_video_id"] == second["source_video_id"]
    assert managed.read_bytes() == b"video-bytes"
    assert metadata["original_filename"] == "input.mp4"


def test_resolve_source_rejects_managed_file_tampering(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "input.mp4"
    source.write_bytes(b"original")
    registered = register_source(workspace, source)
    managed = Path(registered["managed_path"])
    managed.write_bytes(b"tampered")

    with pytest.raises(SemvideoError) as raised:
        resolve_source(workspace, registered["source_video_id"])

    assert raised.value.payload.code == "managed_source_hash_mismatch"


def test_resolve_source_rejects_future_metadata_schema(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    registered = register_source(workspace, source)
    metadata_path = (
        workspace.sources / registered["source_video_id"] / "source.json"
    )
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8").replace(
            '"schema_version": 1',
            '"schema_version": 2',
        ),
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedSchemaVersionError):
        resolve_source(workspace, registered["source_video_id"])


def test_register_source_preserves_copy_error_when_cleanup_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    original_unlink = Path.unlink

    monkeypatch.setattr(
        shutil,
        "copy2",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("copy failed")
        ),
    )

    def reject_temp_delete(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(".source") and path.name.endswith(".tmp"):
            raise OSError("safe-delete unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_temp_delete)

    with pytest.raises(OSError, match="copy failed"):
        register_source(workspace, source)


def test_register_source_maps_lock_timeout_at_application_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")

    @contextmanager
    def unavailable_lock(*args, **kwargs):
        raise LockAcquisitionTimeout("timed out waiting for lock")
        yield

    monkeypatch.setattr(
        "semvideo.application.locking.exclusive_file_lock",
        unavailable_lock,
    )

    with pytest.raises(SemvideoError) as raised:
        register_source(workspace, source)

    assert raised.value.payload.code == "lock_acquisition_timeout"
    assert raised.value.payload.category.value == "resource_transient"
    assert raised.value.payload.retryable is True
    assert raised.value.payload.recovery.value == "retry_same"
