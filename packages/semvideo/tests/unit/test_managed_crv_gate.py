"""Acceptance tests for the managed CRV hard-dependency gate (issue #20).

The v0.3 subagent evidence path must treat CRV as a hard formal-workflow
dependency: when no tested CRV runtime is installed, the runtime raises a
structured ``crv_runtime_unavailable`` error instead of returning a null
runtime that would let the processor silently fall back to native global
evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from semvideo.errors import SemvideoError
from semvideo.infrastructure.managed_crv import ManagedCrvRuntime


def _empty_manager(tmp_path: Path) -> ManagedCrvRuntime:
    return ManagedCrvRuntime(tmp_path / "crv-runtime")


def test_active_raises_when_no_runtime_is_installed(tmp_path: Path) -> None:
    manager = _empty_manager(tmp_path)

    with pytest.raises(SemvideoError) as captured:
        manager.active()

    assert captured.value.payload.code == "crv_runtime_unavailable"


def test_freeze_raises_when_no_runtime_can_be_frozen(tmp_path: Path) -> None:
    manager = _empty_manager(tmp_path)

    with pytest.raises(SemvideoError) as captured:
        manager.freeze()

    assert captured.value.payload.code == "crv_runtime_unavailable"


def test_freeze_raises_for_unknown_version(tmp_path: Path) -> None:
    manager = _empty_manager(tmp_path)

    with pytest.raises(SemvideoError) as captured:
        manager.freeze("9.9.9-not-installed")

    assert captured.value.payload.code == "crv_runtime_unavailable"


def test_update_due_is_true_when_never_checked(tmp_path: Path) -> None:
    manager = _empty_manager(tmp_path)

    assert manager.update_due() is True


def test_state_with_unsupported_schema_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "crv-runtime" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"schema_version": "managed-crv-runtime/v0"}', encoding="utf-8")
    manager = _empty_manager(tmp_path)

    with pytest.raises(SemvideoError) as captured:
        manager.active()

    assert captured.value.payload.code == "crv_runtime_state_invalid"
