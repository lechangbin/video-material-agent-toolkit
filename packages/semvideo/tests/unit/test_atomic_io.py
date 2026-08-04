from __future__ import annotations

import os
from pathlib import Path

import pytest

from semvideo.infrastructure.io import atomic_write_bytes, read_json


def test_atomic_write_preserves_replace_error_when_cleanup_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "state.json"
    original_unlink = Path.unlink

    monkeypatch.setattr(
        os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("replace failed")
        ),
    )

    def reject_temp_delete(path: Path, *, missing_ok: bool = False) -> None:
        if path.parent == tmp_path and path.name.endswith(".tmp"):
            raise OSError("safe-delete unavailable")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_temp_delete)

    with pytest.raises(OSError, match="replace failed"):
        atomic_write_bytes(output, b"content")


def test_json_read_retries_one_transient_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "state.json"
    output.write_text('{"schema_version": 1}', encoding="utf-8")
    original_read_text = Path.read_text
    calls = 0

    def transient_read(path: Path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("sharing violation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_read)

    assert read_json(output) == {"schema_version": 1}
    assert calls == 2
