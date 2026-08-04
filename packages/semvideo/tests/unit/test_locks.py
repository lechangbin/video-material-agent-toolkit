from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from semvideo.infrastructure.locks import (
    FileSlotPool,
    LockAcquisitionTimeout,
    ResourceLimits,
    ResourceLockManager,
    exclusive_file_lock,
)


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = (
        source_root
        if not environment.get("PYTHONPATH")
        else source_root + os.pathsep + environment["PYTHONPATH"]
    )
    return environment


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=_child_environment(),
    )


def test_resource_limits_validate_known_positive_slots() -> None:
    assert ResourceLimits().ffmpeg_cpu == 2
    assert ResourceLimits.from_mapping({"media": 4}).media == 4
    with pytest.raises(ValueError):
        ResourceLimits(media=0)
    with pytest.raises(ValueError):
        ResourceLimits.from_mapping({"gpu": 1})


def test_slot_is_exclusive_between_processes_and_reusable_after_release(
    tmp_path: Path,
) -> None:
    pool = FileSlotPool(tmp_path, "asr", 1)
    lease = pool.acquire(timeout=0)
    child_code = """
import sys
from pathlib import Path
from semvideo.infrastructure.locks import FileSlotPool, LockAcquisitionTimeout
try:
    lease = FileSlotPool(Path(sys.argv[1]), "asr", 1).acquire(timeout=0.15)
except LockAcquisitionTimeout:
    raise SystemExit(7)
else:
    lease.release()
"""
    blocked = _run_child(child_code, str(tmp_path))
    lease.release()
    available = _run_child(child_code, str(tmp_path))

    assert blocked.returncode == 7, blocked.stderr
    assert available.returncode == 0, available.stderr


def test_process_exit_releases_slot(tmp_path: Path) -> None:
    child_code = """
import os, sys
from pathlib import Path
from semvideo.infrastructure.locks import FileSlotPool
FileSlotPool(Path(sys.argv[1]), "llm", 1).acquire(timeout=0)
os._exit(0)
"""
    child = _run_child(child_code, str(tmp_path))
    assert child.returncode == 0, child.stderr

    lease = FileSlotPool(tmp_path, "llm", 1).acquire(timeout=0.2)
    lease.release()


def test_media_and_render_use_shared_ffmpeg_budget_first(tmp_path: Path) -> None:
    limits = ResourceLimits(media=2, render=2, ffmpeg_cpu=1)
    manager = ResourceLockManager(tmp_path, limits)

    media = manager.acquire("media", timeout=0)
    assert [lease.resource for lease in media.leases] == ["ffmpeg_cpu", "media"]
    with pytest.raises(LockAcquisitionTimeout):
        manager.acquire("render", timeout=0.1, poll_interval=0.01)
    media.release()

    render = manager.acquire("render", timeout=0)
    assert [lease.resource for lease in render.leases] == ["ffmpeg_cpu", "render"]
    render.release()


def test_non_ffmpeg_stage_uses_only_classification_slot(tmp_path: Path) -> None:
    manager = ResourceLockManager(tmp_path, ResourceLimits(llm=1))

    llm = manager.acquire("llm", timeout=0)

    assert [lease.resource for lease in llm.leases] == ["llm"]
    with pytest.raises(LockAcquisitionTimeout):
        manager.acquire("llm", timeout=0.05, poll_interval=0.01)
    llm.release()


def test_exclusive_lock_retries_transient_open_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "provider-cooldown.lock"
    original_open = Path.open
    attempts = 0

    def transient_sharing_violation(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == lock_path:
            attempts += 1
            if attempts <= 2:
                raise PermissionError("sharing violation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", transient_sharing_violation)

    with exclusive_file_lock(lock_path, timeout=0.2, poll_interval=0.001):
        assert lock_path.is_file()

    assert attempts == 3


def test_exclusive_lock_maps_persistent_open_contention_to_timeout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "provider-cooldown.lock"

    def persistent_sharing_violation(path: Path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("sharing violation")
        return Path.open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", persistent_sharing_violation)

    with pytest.raises(TimeoutError, match="timed out waiting for lock"):
        with exclusive_file_lock(lock_path, timeout=0.01, poll_interval=0.001):
            pass


def test_classification_slot_retries_transient_open_permission_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    slot_path = tmp_path / "asr-00.lock"
    original_open = Path.open
    attempts = 0

    def transient_sharing_violation(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == slot_path:
            attempts += 1
            if attempts <= 2:
                raise PermissionError("sharing violation")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", transient_sharing_violation)

    lease = FileSlotPool(tmp_path, "asr", 1).acquire(
        timeout=0.2,
        poll_interval=0.001,
    )
    lease.release()

    assert attempts == 3
