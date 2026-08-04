"""Persist one theme segment's recoverable orchestration outcome."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKFLOW_SCHEMA = "video-material-workflow/v1"
STATE_SCHEMA = "video-material-workflow-state/v1"
SEGMENT_STATUSES = {
    "pending",
    "sufficient",
    "stopped_with_gaps",
    "understanding_failed",
    "human_action_required",
    "cancelled",
    "failed",
}
FINAL_SEGMENT_STATUSES = SEGMENT_STATUSES - {"pending", "human_action_required"}


class SegmentStateError(RuntimeError):
    """A segment outcome conflicts with the durable workflow lineage."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SegmentStateError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise SegmentStateError(f"JSON artifact must contain an object: {path}")
    return value


def _artifact_ref(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise SegmentStateError(f"result artifact is not a file: {resolved}")
    content = resolved.read_bytes()
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _new_state(workflow: dict[str, Any]) -> dict[str, Any]:
    plans = workflow.get("plans")
    if not isinstance(plans, list) or not plans:
        raise SegmentStateError("workflow plans are invalid")
    segments: list[dict[str, Any]] = []
    for plan in plans:
        if (
            not isinstance(plan, dict)
            or not isinstance(plan.get("segment_id"), str)
            or not isinstance(plan.get("query_plan_id"), str)
        ):
            raise SegmentStateError("workflow plan identity is invalid")
        segments.append(
            {
                "segment_id": plan["segment_id"],
                "query_plan_id": plan["query_plan_id"],
                "status": "pending",
            }
        )
    return {
        "schema_version": STATE_SCHEMA,
        "workflow_id": workflow["workflow_id"],
        "workflow_definition_version": workflow["state_version"],
        "revision": 0,
        "status": "active",
        "segments": segments,
    }


def _overall_status(segments: list[dict[str, Any]]) -> str:
    statuses = {item["status"] for item in segments}
    if "human_action_required" in statuses:
        return "action_required"
    if "pending" in statuses:
        return "active"
    if "failed" in statuses or "understanding_failed" in statuses:
        return "failed"
    if statuses == {"cancelled"}:
        return "cancelled"
    if "stopped_with_gaps" in statuses or "cancelled" in statuses:
        return "completed_with_gaps"
    return "completed"


def record_state(
    *,
    workflow: dict[str, Any],
    state: dict[str, Any] | None,
    segment_id: str,
    status: str,
    round_number: int,
    result_artifact: Path,
    collection_session_ids: list[str],
    semvideo_job_ids: list[str],
) -> dict[str, Any]:
    if workflow.get("schema_version") != WORKFLOW_SCHEMA:
        raise SegmentStateError("workflow schema is unsupported")
    if status not in SEGMENT_STATUSES - {"pending"}:
        raise SegmentStateError("segment status is unsupported")
    if round_number < 1:
        raise SegmentStateError("round_number must be positive")
    if not all(isinstance(value, str) and value for value in collection_session_ids):
        raise SegmentStateError("collection session identities are invalid")
    if not all(isinstance(value, str) and value for value in semvideo_job_ids):
        raise SegmentStateError("Semvideo job identities are invalid")
    payload = _new_state(workflow) if state is None else json.loads(json.dumps(state))
    if (
        payload.get("schema_version") != STATE_SCHEMA
        or payload.get("workflow_id") != workflow.get("workflow_id")
        or payload.get("workflow_definition_version")
        != workflow.get("state_version")
        or not isinstance(payload.get("revision"), int)
        or not isinstance(payload.get("segments"), list)
    ):
        raise SegmentStateError("workflow state lineage is invalid")
    segment = next(
        (
            item
            for item in payload["segments"]
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    if segment is None:
        raise SegmentStateError(f"workflow does not contain segment: {segment_id}")
    record = {
        "segment_id": segment_id,
        "query_plan_id": segment["query_plan_id"],
        "status": status,
        "round_number": round_number,
        "result_artifact": _artifact_ref(result_artifact),
        "collection_session_ids": sorted(set(collection_session_ids)),
        "semvideo_job_ids": sorted(set(semvideo_job_ids)),
    }
    current_status = segment.get("status")
    if current_status in FINAL_SEGMENT_STATUSES:
        if segment != record:
            raise SegmentStateError(
                f"refusing to replace terminal segment state: {segment_id}"
            )
        return payload
    if segment != record:
        payload["segments"][payload["segments"].index(segment)] = record
        payload["revision"] += 1
    payload["status"] = _overall_status(payload["segments"])
    return payload


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="record-video-material-segment-state")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--status", required=True, choices=sorted(SEGMENT_STATUSES - {"pending"}))
    parser.add_argument("--round-number", required=True, type=int)
    parser.add_argument("--result-artifact", required=True)
    parser.add_argument("--collection-session-id", action="append", default=[])
    parser.add_argument("--semvideo-job-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        workflow = _load_object(Path(arguments.workflow).resolve(strict=True))
        state_path = Path(arguments.state).expanduser().resolve(strict=False)
        payload = record_state(
            workflow=workflow,
            state=_load_object(state_path) if state_path.exists() else None,
            segment_id=arguments.segment_id,
            status=arguments.status,
            round_number=arguments.round_number,
            result_artifact=Path(arguments.result_artifact),
            collection_session_ids=arguments.collection_session_id,
            semvideo_job_ids=arguments.semvideo_job_id,
        )
        _atomic_write_json(state_path, payload)
    except (SegmentStateError, OSError, ValueError, KeyError) as error:
        print(
            json.dumps(
                {
                    "schema_version": STATE_SCHEMA,
                    "status": "error",
                    "code": "workflow_segment_state_invalid",
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
                "schema_version": STATE_SCHEMA,
                "status": payload["status"],
                "revision": payload["revision"],
                "state_path": str(state_path),
                "segment_id": arguments.segment_id,
                "segment_status": arguments.status,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
