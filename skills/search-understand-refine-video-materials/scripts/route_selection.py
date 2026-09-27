"""Freeze the selection route after an explicit host capability check."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROUTE_SCHEMA = "video-material-selection-route/v1"
CAPABILITY_SCHEMA = "selection-subagent-capability/v1"
CAPABILITY_STATES = {"available", "unavailable", "launch_denied", "no_slot"}


class RouteError(RuntimeError):
    """The host has not established an allowed selection route."""


def route(workflow: dict[str, Any], capability: dict[str, Any] | None) -> dict[str, Any]:
    if workflow.get("schema_version") != "video-material-workflow/v1":
        raise RouteError("workflow schema is unsupported")
    selection = workflow.get("selection")
    mode = selection.get("mode") if isinstance(selection, dict) else None
    if mode not in {"auto", "subagent_only", "embedding_only"}:
        raise RouteError("frozen workflow selection mode is invalid")
    capability_status: str = "not_checked"
    if mode == "embedding_only":
        chosen = "embedding"
        reason = "explicit_embedding_only"
    else:
        if (
            not isinstance(capability, dict)
            or capability.get("schema_version") != CAPABILITY_SCHEMA
        ):
            raise RouteError("explicit subagent capability report is required")
        reported_status = capability.get("status")
        if reported_status not in CAPABILITY_STATES:
            raise RouteError("subagent capability status is invalid")
        capability_status = str(reported_status)
        if capability_status == "available":
            chosen = "subagent"
            reason = "host_subagent_available"
        elif mode == "auto":
            chosen = "embedding"
            reason = f"host_subagent_{capability_status}"
        else:
            raise RouteError(f"subagent_only route unavailable: {capability_status}")
    return {
        "schema_version": ROUTE_SCHEMA,
        "workflow_id": workflow["workflow_id"],
        "requested_mode": mode,
        "route": chosen,
        "reason": reason,
        "capability_status": capability_status,
    }


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if path.exists():
        if path.read_bytes() != content:
            raise RouteError("refusing to change a frozen selection route")
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="route-video-segment-selection")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--capability")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    try:
        workflow = json.loads(Path(arguments.workflow).read_text(encoding="utf-8"))
        capability = (
            None
            if arguments.capability is None
            else json.loads(Path(arguments.capability).read_text(encoding="utf-8"))
        )
        result = route(workflow, capability)
        output = Path(arguments.output).expanduser().resolve(strict=False)
        _write_once(output, result)
    except (RouteError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "schema_version": ROUTE_SCHEMA,
                    "status": "error",
                    "code": "selection_route_invalid",
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": ROUTE_SCHEMA,
                "status": "ready",
                "route": result["route"],
                "reason": result["reason"],
                "output_path": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
