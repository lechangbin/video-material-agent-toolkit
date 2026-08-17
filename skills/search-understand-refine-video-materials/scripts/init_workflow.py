"""Create an immutable, versioned cross-tool workflow definition."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKFLOW_SCHEMA = "video-material-workflow/v1"
PLATFORMS = ("bilibili", "douyin", "xiaohongshu")


class WorkflowInitError(RuntimeError):
    """The semantic input cannot define a safe workflow."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "workflow_initialization_invalid",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowInitError(f"invalid JSON input: {path}") from error
    if not isinstance(value, dict):
        raise WorkflowInitError(f"JSON input must be an object: {path}")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize_in_process(
    raw_input: dict[str, Any],
    raw_plans: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from material_collector.core.contracts import normalize_contracts
    from material_collector.core.errors import CollectorError

    try:
        normalized = normalize_contracts(raw_input, raw_plans)
    except CollectorError as error:
        raise WorkflowInitError(
            error.message,
            code=error.code,
            details=error.details,
        ) from error
    return (
        normalized.collection_input.model_dump(mode="json", exclude_none=False),
        normalized.query_plans.model_dump(mode="json", exclude_none=False),
    )


def _normalize_with_collector(
    collector: Path,
    input_path: Path,
    query_plans_path: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        completed = subprocess.run(
            [
                str(collector),
                "contracts",
                "normalize",
                "--input",
                str(input_path),
                "--query-plans",
                str(query_plans_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkflowInitError("Collector contract normalization failed to run") from error
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorkflowInitError(
            "Collector contract normalization did not return one JSON object"
        ) from error
    if not isinstance(payload, dict):
        raise WorkflowInitError("Collector contract normalization returned invalid JSON")
    if completed.returncode != 0:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else "unknown"
        message = error.get("message") if isinstance(error, dict) else "invalid contracts"
        details = error.get("details") if isinstance(error, dict) else None
        raise WorkflowInitError(
            message,
            code=code if isinstance(code, str) else "collector_contract_rejected",
            details=details if isinstance(details, dict) else {},
        )
    if (
        payload.get("schema_version")
        != "material-collector-normalized-contracts/v1"
        or payload.get("status") != "normalized"
        or not isinstance(payload.get("collection_input"), dict)
        or not isinstance(payload.get("query_plans"), dict)
    ):
        raise WorkflowInitError("Collector normalized-contract response is unsupported")
    return payload["collection_input"], payload["query_plans"]


def _validate_orchestration_contracts(
    collection_input: dict[str, Any],
    query_plans: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    segments = collection_input.get("segments")
    plans = query_plans.get("plans")
    platform_scope = query_plans.get("platform_scope")
    if (
        not isinstance(segments, list)
        or not isinstance(plans, list)
        or not isinstance(platform_scope, list)
        or not platform_scope
        or len(platform_scope) != len(set(platform_scope))
        or not set(platform_scope).issubset(PLATFORMS)
    ):
        raise WorkflowInitError("normalized Collector contracts are malformed")
    normalized_scope = [
        platform for platform in PLATFORMS if platform in platform_scope
    ]
    segment_ids = [segment["segment_id"] for segment in segments]
    plan_refs: list[dict[str, str]] = []
    for plan in plans:
        queries = plan.get("initial_queries")
        if not isinstance(queries, list) or not queries:
            raise WorkflowInitError("normalized QueryPlan has no expressions")
        for query in queries:
            targets = query.get("target_platforms") if isinstance(query, dict) else None
            if (
                not isinstance(targets, list)
                or targets != normalized_scope
            ):
                raise WorkflowInitError(
                    "every query expression must target the complete platform scope"
                )
        plan_refs.append(
            {
                "segment_id": plan["segment_id"],
                "query_plan_id": plan["query_plan_id"],
            }
        )
    plan_refs.sort(key=lambda item: segment_ids.index(item["segment_id"]))
    return normalized_scope, plan_refs


def _write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise WorkflowInitError(
                f"refusing to overwrite conflicting workflow artifact: {path}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def initialize(
    *,
    input_path: Path,
    query_plans_path: Path,
    workflow_root: Path,
    material_workspace: Path,
    semvideo_workspace: Path,
    semvideo_profile: str,
    max_rounds: int,
    max_videos: int,
    collector_path: Path | None = None,
    normalizer_timeout_seconds: int = 30,
) -> dict[str, Any]:
    if (
        max_rounds < 1
        or max_videos < 1
        or normalizer_timeout_seconds < 1
    ):
        raise WorkflowInitError("workflow limits and timeout must be positive")
    if not semvideo_profile.strip():
        raise WorkflowInitError("Semvideo profile must not be empty")
    resolved_input = input_path.resolve(strict=True)
    resolved_plans = query_plans_path.resolve(strict=True)
    if collector_path is None:
        collection_input, query_plans = _normalize_in_process(
            _load_object(resolved_input),
            _load_object(resolved_plans),
        )
    else:
        collection_input, query_plans = _normalize_with_collector(
            collector_path.resolve(strict=True),
            resolved_input,
            resolved_plans,
            normalizer_timeout_seconds,
        )
    platform_scope, plan_refs = _validate_orchestration_contracts(
        collection_input,
        query_plans,
    )
    material_workspace = material_workspace.expanduser().resolve(strict=True)
    semvideo_workspace = semvideo_workspace.expanduser().resolve(strict=True)
    if not material_workspace.is_dir() or not semvideo_workspace.is_dir():
        raise WorkflowInitError("material and Semvideo workspaces must exist")
    input_bytes = _canonical_bytes(collection_input)
    plans_bytes = _canonical_bytes(query_plans)
    identity = {
        "collection_input_sha256": _sha256(input_bytes),
        "initial_query_plans_sha256": _sha256(plans_bytes),
        "material_workspace": str(material_workspace),
        "semvideo_workspace": str(semvideo_workspace),
        "semvideo_profile": semvideo_profile,
        "max_rounds": max_rounds,
        "max_videos": max_videos,
    }
    workflow_id = f"vmw_{_sha256(_canonical_bytes(identity))[:20]}"
    root = workflow_root.expanduser().resolve(strict=False)
    frozen_input = root / "input" / "collection-input.json"
    frozen_plans = root / "input" / "initial-query-plans.json"
    payload = {
        "schema_version": WORKFLOW_SCHEMA,
        "workflow_id": workflow_id,
        "state_version": 1,
        "status": "active",
        "platform_scope": platform_scope,
        "material_workspace": str(material_workspace),
        "semvideo_workspace": str(semvideo_workspace),
        "semvideo_profile": semvideo_profile,
        "constraints": {
            "max_rounds": max_rounds,
            "max_videos": max_videos,
        },
        "semantic_input": {
            "collection_input_path": str(frozen_input),
            "collection_input_sha256": _sha256(input_bytes),
            "initial_query_plans_path": str(frozen_plans),
            "initial_query_plans_sha256": _sha256(plans_bytes),
        },
        "plans": [
            {
                **plan_ref,
                "max_rounds": max_rounds,
                "max_videos": max_videos,
            }
            for plan_ref in plan_refs
        ],
    }
    workflow_path = root / "workflow.json"
    workflow_bytes = _canonical_bytes(payload)
    _write_once(frozen_input, input_bytes)
    _write_once(frozen_plans, plans_bytes)
    _write_once(workflow_path, workflow_bytes)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="init-video-material-workflow")
    parser.add_argument("--input", required=True)
    parser.add_argument("--query-plans", required=True)
    parser.add_argument("--workflow-root", required=True)
    parser.add_argument("--material-workspace", required=True)
    parser.add_argument("--semvideo-workspace", required=True)
    parser.add_argument("--semvideo-profile", default="default")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--max-videos", type=int, default=18)
    parser.add_argument("--collector")
    parser.add_argument("--normalizer-timeout-seconds", type=int, default=30)
    return parser


def _resolve_collector(value: str | None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve(strict=True)
    discovered = shutil.which("material-collector")
    if discovered is None:
        raise WorkflowInitError(
            "material-collector is not on PATH; pass --collector explicitly"
        )
    return Path(discovered).resolve(strict=True)


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        payload = initialize(
            input_path=Path(arguments.input),
            query_plans_path=Path(arguments.query_plans),
            workflow_root=Path(arguments.workflow_root),
            material_workspace=Path(arguments.material_workspace),
            semvideo_workspace=Path(arguments.semvideo_workspace),
            semvideo_profile=arguments.semvideo_profile,
            max_rounds=arguments.max_rounds,
            max_videos=arguments.max_videos,
            collector_path=_resolve_collector(arguments.collector),
            normalizer_timeout_seconds=arguments.normalizer_timeout_seconds,
        )
        workflow_path = (
            Path(arguments.workflow_root).expanduser().resolve(strict=False)
            / "workflow.json"
        )
    except (WorkflowInitError, OSError, ValueError) as error:
        code = (
            error.code
            if isinstance(error, WorkflowInitError)
            else "workflow_initialization_invalid"
        )
        details = error.details if isinstance(error, WorkflowInitError) else {}
        print(
            json.dumps(
                {
                    "schema_version": WORKFLOW_SCHEMA,
                    "status": "error",
                    "code": code,
                    "message": str(error),
                    "details": details,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": WORKFLOW_SCHEMA,
                "status": "ready",
                "workflow_id": payload["workflow_id"],
                "workflow_path": str(workflow_path),
                "plan_count": len(payload["plans"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
