from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).parents[3]
    / "skills/search-understand-refine-video-materials/scripts/route_selection.py"
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("route_selection", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("mode", "capability", "expected"),
    [
        ("auto", "available", "subagent"),
        ("auto", "unavailable", "embedding"),
        ("auto", "launch_denied", "embedding"),
        ("auto", "no_slot", "embedding"),
        ("embedding_only", None, "embedding"),
        ("subagent_only", "available", "subagent"),
    ],
)
def test_route_selection(mode: str, capability: str | None, expected: str) -> None:
    module = _module()
    workflow = {
        "schema_version": "video-material-workflow/v1",
        "workflow_id": "vmw_1",
        "selection": {"mode": mode},
    }
    report = (
        None
        if capability is None
        else {"schema_version": "selection-subagent-capability/v1", "status": capability}
    )
    assert module.route(workflow, report)["route"] == expected


def test_auto_requires_explicit_capability_report() -> None:
    module = _module()
    workflow = {
        "schema_version": "video-material-workflow/v1",
        "workflow_id": "vmw_1",
        "selection": {"mode": "auto"},
    }
    with pytest.raises(module.RouteError, match="capability"):
        module.route(workflow, None)


def test_forced_subagent_does_not_fallback() -> None:
    module = _module()
    workflow = {
        "schema_version": "video-material-workflow/v1",
        "workflow_id": "vmw_1",
        "selection": {"mode": "subagent_only"},
    }
    with pytest.raises(module.RouteError, match="unavailable"):
        module.route(
            workflow, {"schema_version": "selection-subagent-capability/v1", "status": "no_slot"}
        )


def test_started_subagent_route_cannot_be_replaced_after_failure(tmp_path: Path) -> None:
    module = _module()
    workflow = {
        "schema_version": "video-material-workflow/v1",
        "workflow_id": "vmw_1",
        "selection": {"mode": "auto"},
    }
    available = {"schema_version": "selection-subagent-capability/v1", "status": "available"}
    denied = {"schema_version": "selection-subagent-capability/v1", "status": "launch_denied"}
    path = tmp_path / "selection-route.json"
    module._write_once(path, module.route(workflow, available))
    with pytest.raises(module.RouteError, match="frozen"):
        module._write_once(path, module.route(workflow, denied))
