"""Build one v2 selection request from cumulative understanding artifacts."""

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
CATALOGS_SCHEMA = "video-material-understanding-catalogs/v1"
REQUEST_SCHEMA = "segment-selection-request/v2"


class SelectionRequestError(RuntimeError):
    """Selection lineage or completeness is invalid."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SelectionRequestError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise SelectionRequestError(f"JSON artifact must contain an object: {path}")
    return value


def _canonical_hash(value: dict[str, Any]) -> str:
    content = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise SelectionRequestError(f"candidate artifact cannot be read: {path}") from error
    return digest.hexdigest()


def _count_jsonl(path: Path) -> int:
    count = 0
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise SelectionRequestError(
                        "candidate JSONL contains a non-object record"
                    )
                count += 1
    except (OSError, json.JSONDecodeError) as error:
        raise SelectionRequestError(f"candidate JSONL is invalid: {path}") from error
    return count


def build_request(
    *,
    workflow: dict[str, Any],
    segment_id: str,
    round_number: int,
    catalogs: dict[str, Any],
    candidate_path: Path,
    narration_duration_ms: int | None,
) -> dict[str, Any]:
    if workflow.get("schema_version") != WORKFLOW_SCHEMA:
        raise SelectionRequestError("workflow schema is unsupported")
    if catalogs.get("schema_version") != CATALOGS_SCHEMA:
        raise SelectionRequestError("understanding catalogs schema is unsupported")
    if (
        catalogs.get("workflow_id") != workflow.get("workflow_id")
        or catalogs.get("workflow_state_version") != workflow.get("state_version")
        or catalogs.get("segment_id") != segment_id
    ):
        raise SelectionRequestError(
            "understanding catalogs do not match workflow segment lineage"
        )
    if round_number < 1:
        raise SelectionRequestError("round_number must be positive")
    semantic = workflow.get("semantic_input")
    if not isinstance(semantic, dict):
        raise SelectionRequestError("workflow semantic input is invalid")
    input_document = _load_object(
        Path(semantic["collection_input_path"]).resolve(strict=True)
    )
    plans_document = _load_object(
        Path(semantic["initial_query_plans_path"]).resolve(strict=True)
    )
    if (
        _canonical_hash(input_document) != semantic.get("collection_input_sha256")
        or _canonical_hash(plans_document)
        != semantic.get("initial_query_plans_sha256")
    ):
        raise SelectionRequestError("frozen semantic input hash mismatch")
    segment = next(
        (
            item
            for item in input_document.get("segments", [])
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    plan = next(
        (
            item
            for item in plans_document.get("plans", [])
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    if segment is None or plan is None:
        raise SelectionRequestError("segment is missing from frozen semantic input")
    if catalogs.get("query_plan_id") != plan.get("query_plan_id"):
        raise SelectionRequestError(
            "understanding catalogs do not match workflow query plan"
        )
    catalog_items = catalogs.get("catalogs")
    if not isinstance(catalog_items, list):
        raise SelectionRequestError("understanding catalogs are invalid")
    complete = [
        item
        for item in catalog_items
        if isinstance(item, dict) and item.get("status") == "complete"
    ]
    if not complete:
        raise SelectionRequestError(
            "no complete understanding catalog is available for selection"
        )
    candidate_path = candidate_path.expanduser().resolve(strict=True)
    candidate_count = _count_jsonl(candidate_path)
    if candidate_count < 1:
        raise SelectionRequestError("candidate artifact is empty")
    declared_candidate = catalogs.get("candidate_artifact")
    if not isinstance(declared_candidate, dict):
        raise SelectionRequestError(
            "understanding catalogs are missing candidate artifact lineage"
        )
    candidate_hash = _hash_file(candidate_path)
    declared_path = declared_candidate.get("path")
    declared_hash = declared_candidate.get("sha256")
    declared_count = declared_candidate.get("candidate_count")
    if (
        not isinstance(declared_path, str)
        or Path(declared_path).expanduser().resolve(strict=True) != candidate_path
        or declared_hash != candidate_hash
        or declared_count != candidate_count
    ):
        raise SelectionRequestError(
            "candidate artifact does not match understanding catalogs"
        )
    facets = plan.get("required_visual_facets")
    if not isinstance(facets, list) or not facets:
        raise SelectionRequestError("query plan has no required visual facets")
    max_k = min(12, 2 * len(facets) + 2)
    selection_seed = (
        f"{workflow['workflow_id']}:{segment_id}:{round_number}:{candidate_hash}"
    )
    collection_session_ids = sorted(
        {
            session_id
            for catalog in complete
            for session_id in catalog.get("collection_session_ids", [])
            if isinstance(session_id, str)
        }
    )
    return {
        "schema_version": REQUEST_SCHEMA,
        "selection_id": (
            f"sel_{hashlib.sha256(selection_seed.encode('utf-8')).hexdigest()[:20]}"
        ),
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "query_plan_id": plan["query_plan_id"],
        "segment_id": segment_id,
        "round_number": round_number,
        "theme_segment": {
            "id": segment_id,
            "text": segment["text"],
            "intent": plan["visual_strategy"],
            "narration_duration_ms": narration_duration_ms,
        },
        "required_visual_facets": [
            {
                **facet,
                "priority": index,
            }
            for index, facet in enumerate(facets, start=1)
        ],
        "max_k": max_k,
        "max_k_source": "derived",
        "selection_targets": {
            "independent_candidates_per_required_facet": 2,
            "candidate_duration_to_narration_ratio": 3.0,
        },
        "collection_session_ids": collection_session_ids,
        "understanding_catalogs": complete,
        "candidate_artifact": {
            "path": str(candidate_path),
            "format": "jsonl",
            "sha256": candidate_hash,
            "candidate_count": candidate_count,
        },
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise SelectionRequestError(
                f"refusing to overwrite conflicting artifact: {path}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="build-selection-request")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--round-number", type=int, required=True)
    parser.add_argument("--catalogs", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--narration-duration-ms", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if (
            arguments.narration_duration_ms is not None
            and arguments.narration_duration_ms < 1
        ):
            raise SelectionRequestError("narration duration must be positive")
        payload = build_request(
            workflow=_load_object(Path(arguments.workflow).resolve(strict=True)),
            segment_id=arguments.segment_id,
            round_number=arguments.round_number,
            catalogs=_load_object(Path(arguments.catalogs).resolve(strict=True)),
            candidate_path=Path(arguments.candidates),
            narration_duration_ms=arguments.narration_duration_ms,
        )
        output = Path(arguments.output).expanduser().resolve(strict=False)
        _atomic_write_json(output, payload)
    except (SelectionRequestError, OSError, ValueError, KeyError) as error:
        print(
            json.dumps(
                {
                    "schema_version": REQUEST_SCHEMA,
                    "status": "error",
                    "code": "selection_request_invalid",
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
                "schema_version": REQUEST_SCHEMA,
                "status": "ready",
                "selection_id": payload["selection_id"],
                "output_path": str(output),
                "candidate_count": payload["candidate_artifact"][
                    "candidate_count"
                ],
                "max_k": payload["max_k"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
