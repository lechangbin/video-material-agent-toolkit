"""Project completed Semvideo jobs into a traceable global candidate catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BATCH_SCHEMA = "video-material-understanding-batch/v2"
JOBS_SCHEMA = "video-material-understanding-jobs/v1"
CATALOGS_SCHEMA = "video-material-understanding-catalogs/v1"
CANDIDATE_SCHEMA = "video-material-candidate-segment/v1"


class CatalogError(RuntimeError):
    """Semvideo output cannot safely cross the selection boundary."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise CatalogError(f"JSON artifact must contain an object: {path}")
    return value


def _run_json(command: Path, arguments: list[str], timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [str(command), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CatalogError(f"Semvideo command failed to execute: {arguments[:2]}") from error
    stream = completed.stdout if completed.returncode == 0 else completed.stderr
    try:
        payload = json.loads(stream)
    except json.JSONDecodeError as error:
        raise CatalogError(
            f"Semvideo did not return one JSON object: {arguments[:2]}"
        ) from error
    if not isinstance(payload, dict):
        raise CatalogError(f"Semvideo returned a non-object: {arguments[:2]}")
    if completed.returncode != 0:
        code = payload.get("code", "semvideo_command_failed")
        raise CatalogError(f"Semvideo command returned {code}: {arguments[:2]}")
    return payload


def _job_map(
    jobs: dict[str, Any],
    batch_items: dict[str, dict[str, Any]],
) -> dict[str, str]:
    if jobs.get("schema_version") != JOBS_SCHEMA:
        raise CatalogError("understanding jobs artifact has an unsupported schema")
    items = jobs.get("items")
    if not isinstance(items, list):
        raise CatalogError("understanding jobs artifact must contain items")
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise CatalogError("understanding job entries must be objects")
        item_id = item.get("item_id")
        job_id = item.get("job_id")
        if not isinstance(item_id, str) or not isinstance(job_id, str):
            raise CatalogError("understanding job entry is missing item_id or job_id")
        batch_item = batch_items.get(item_id)
        if batch_item is None:
            continue
        if (
            item.get("asset_sha256") != batch_item.get("asset_sha256")
            or item.get("semvideo_profile")
            != batch_item.get("semvideo_profile")
            or item.get("semvideo_idempotency_key")
            != batch_item.get("semvideo_idempotency_key")
        ):
            raise CatalogError(
                f"understanding job mapping does not match batch item: {item_id}"
            )
        if item_id in result and result[item_id] != job_id:
            raise CatalogError(f"item has conflicting Semvideo jobs: {item_id}")
        result[item_id] = job_id
    return result


def _candidate_record(
    *,
    batch_item: dict[str, Any],
    job_id: str,
    segment: dict[str, Any],
) -> dict[str, Any]:
    segment_id = segment.get("segment_id")
    start_ms = segment.get("start_ms")
    end_ms = segment.get("end_ms")
    if (
        not isinstance(segment_id, str)
        or not isinstance(start_ms, int)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms <= start_ms
    ):
        raise CatalogError(f"Semvideo segment has an invalid identity or range: {job_id}")
    sources = batch_item["sources"]
    return {
        "schema_version": CANDIDATE_SCHEMA,
        "candidate_segment_id": f"{job_id}:{segment_id}",
        "semvideo_segment_id": segment_id,
        "understanding_job_id": job_id,
        "item_id": batch_item["item_id"],
        "asset_sha256": batch_item["asset_sha256"],
        "media_unit_ids": batch_item["media_unit_ids"],
        "collection_session_ids": sorted(
            {source["collection_session_id"] for source in sources}
        ),
        "work_group_ids": sorted(
            {
                source["work_group_id"]
                for source in sources
                if isinstance(source.get("work_group_id"), str)
            }
        ),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
        "title": segment.get("title", ""),
        "short_summary": segment.get("short_summary", ""),
        "detailed_summary": segment.get("detailed_summary", ""),
        "visual_summary": segment.get("visual_summary", ""),
        "topics": segment.get("topics", []),
        "participants": segment.get("participants", []),
        "locations": segment.get("locations", []),
        "organizations": segment.get("organizations", []),
        "objects": segment.get("objects", []),
        "actions": segment.get("actions", []),
        "keywords": segment.get("keywords", []),
        "transcript": segment.get("transcript", {}),
        "confidence": segment.get("confidence"),
        "review_required": bool(segment.get("review_required", False)),
        "review_reasons": segment.get("review_reasons", []),
        "provider_evidence": {
            "artifacts": segment.get("artifacts", {}),
            "provenance": segment.get("provenance", {}),
        },
        "lineage": {
            "provider": "semvideo",
            "understanding_schema_version": segment.get("schema_version"),
            "understanding_job_id": job_id,
            "asset_sha256": batch_item["asset_sha256"],
        },
    }


def _completed_job_segments(
    command: Path,
    workspace: Path,
    job_id: str,
    *,
    page_size: int,
    timeout: int,
) -> tuple[list[dict[str, Any]], int]:
    segments: list[dict[str, Any]] = []
    offset = 0
    page_count = 0
    total: int | None = None
    while total is None or offset < total:
        page = _run_json(
            command,
            [
                "segment",
                "list",
                job_id,
                "--workspace",
                str(workspace),
                "--offset",
                str(offset),
                "--limit",
                str(page_size),
                "--json",
            ],
            timeout,
        )
        page_total = page.get("total")
        items = page.get("items")
        if not isinstance(page_total, int) or not isinstance(items, list):
            raise CatalogError(f"Semvideo segment list is malformed: {job_id}")
        if total is None:
            total = page_total
        elif total != page_total:
            raise CatalogError(f"Semvideo segment total changed while paging: {job_id}")
        page_count += 1
        for compact in items:
            if not isinstance(compact, dict) or not isinstance(
                compact.get("segment_id"), str
            ):
                raise CatalogError(f"Semvideo compact segment is malformed: {job_id}")
            segments.append(
                _run_json(
                    command,
                    [
                        "segment",
                        "show",
                        job_id,
                        compact["segment_id"],
                        "--workspace",
                        str(workspace),
                        "--json",
                    ],
                    timeout,
                )
            )
        if not items:
            break
        offset += len(items)
    if total is None or len(segments) != total:
        raise CatalogError(f"Semvideo segment catalog is incomplete: {job_id}")
    return segments, page_count


def collect_catalog(
    *,
    batch: dict[str, Any],
    jobs: dict[str, Any],
    command: Path,
    workspace: Path,
    page_size: int = 50,
    timeout: int = 60,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if batch.get("schema_version") != BATCH_SCHEMA:
        raise CatalogError("understanding batch has an unsupported schema")
    expected_workspace = batch.get("semvideo_workspace")
    if (
        not isinstance(expected_workspace, str)
        or Path(expected_workspace).expanduser().resolve(strict=True)
        != workspace.expanduser().resolve(strict=True)
    ):
        raise CatalogError(
            "Semvideo workspace does not match understanding batch lineage"
        )
    batch_items = batch.get("items")
    if not isinstance(batch_items, list):
        raise CatalogError("understanding batch must contain items")
    batch_item_map = {
        item["item_id"]: item
        for item in batch_items
        if isinstance(item, dict) and isinstance(item.get("item_id"), str)
    }
    if len(batch_item_map) != len(batch_items):
        raise CatalogError("understanding batch contains invalid or duplicate items")
    mapped_jobs = _job_map(jobs, batch_item_map)
    catalogs: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []

    for item in batch_items:
        if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
            raise CatalogError("understanding batch contains an invalid item")
        item_id = item["item_id"]
        collection_session_ids = sorted(
            {
                source["collection_session_id"]
                for source in item.get("sources", [])
                if isinstance(source, dict)
                and isinstance(source.get("collection_session_id"), str)
            }
        )
        job_id = mapped_jobs.get(item_id)
        if job_id is None:
            catalogs.append(
                {
                    "item_id": item_id,
                    "status": "not_submitted",
                    "media_unit_ids": item.get("media_unit_ids", []),
                    "collection_session_ids": collection_session_ids,
                }
            )
            continue
        status = _run_json(
            command,
            ["job", "status", job_id, "--workspace", str(workspace), "--json"],
            timeout,
        )
        state = status.get("state")
        if state != "completed":
            catalogs.append(
                {
                    "item_id": item_id,
                    "understanding_job_id": job_id,
                    "status": state if isinstance(state, str) else "unknown",
                    "media_unit_ids": item.get("media_unit_ids", []),
                    "collection_session_ids": collection_session_ids,
                    "failure": status.get("failure"),
                }
            )
            continue
        segments, page_count = _completed_job_segments(
            command,
            workspace,
            job_id,
            page_size=page_size,
            timeout=timeout,
        )
        if not segments:
            raise CatalogError(f"completed Semvideo job returned no segments: {job_id}")
        ordered = sorted(segments, key=lambda value: value.get("ordinal", 0))
        expected_start = 0
        for segment in ordered:
            if segment.get("start_ms") != expected_start:
                raise CatalogError(f"Semvideo catalog has a timeline gap: {job_id}")
            end_ms = segment.get("end_ms")
            if not isinstance(end_ms, int) or end_ms <= expected_start:
                raise CatalogError(f"Semvideo catalog has an invalid range: {job_id}")
            expected_start = end_ms
            candidates.append(
                _candidate_record(
                    batch_item=item,
                    job_id=job_id,
                    segment=segment,
                )
            )
        catalogs.append(
            {
                "item_id": item_id,
                "understanding_job_id": job_id,
                "status": "complete",
                "media_unit_ids": item["media_unit_ids"],
                "collection_session_ids": collection_session_ids,
                "covered_ranges_ms": [[0, expected_start]],
                "unprocessed_ranges_ms": [],
                "page_count": page_count,
                "segment_count": len(ordered),
            }
        )

    candidates.sort(
        key=lambda item: (
            item["understanding_job_id"],
            item["start_ms"],
            item["candidate_segment_id"],
        )
    )
    complete_count = sum(catalog["status"] == "complete" for catalog in catalogs)
    catalog_payload = {
        "schema_version": CATALOGS_SCHEMA,
        "workflow_id": batch.get("workflow_id"),
        "workflow_state_version": batch.get("workflow_state_version"),
        "segment_id": batch.get("segment_id"),
        "query_plan_id": batch.get("query_plan_id"),
        "material_workspace": batch.get("material_workspace"),
        "semvideo_workspace": batch.get("semvideo_workspace"),
        "status": "complete" if complete_count == len(batch_items) else "partial",
        "batch_item_count": len(batch_items),
        "complete_catalog_count": complete_count,
        "candidate_count": len(candidates),
        "catalogs": catalogs,
    }
    return catalog_payload, candidates


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _write_once(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    content = "".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in values
    )
    _write_once(path, content)


def _write_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise CatalogError(f"refusing to overwrite conflicting artifact: {path}")
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
    parser = argparse.ArgumentParser(prog="collect-semvideo-catalog")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--semvideo", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--catalogs-output", required=True)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.page_size < 1 or arguments.timeout_seconds < 1:
            raise CatalogError("page size and timeout must be positive")
        command = Path(arguments.semvideo).expanduser().resolve(strict=True)
        workspace = Path(arguments.workspace).expanduser().resolve(strict=True)
        if not command.is_file() or not workspace.is_dir():
            raise CatalogError("Semvideo command or workspace is invalid")
        catalogs, candidates = collect_catalog(
            batch=_load_object(Path(arguments.batch).expanduser().resolve(strict=True)),
            jobs=_load_object(Path(arguments.jobs).expanduser().resolve(strict=True)),
            command=command,
            workspace=workspace,
            page_size=arguments.page_size,
            timeout=arguments.timeout_seconds,
        )
        output = Path(arguments.output).expanduser().resolve(strict=False)
        catalogs_output = Path(arguments.catalogs_output).expanduser().resolve(
            strict=False
        )
        _atomic_write_jsonl(output, candidates)
        candidate_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        catalogs["candidate_artifact"] = {
            "path": str(output),
            "sha256": candidate_hash,
            "candidate_count": len(candidates),
        }
        _atomic_write_json(catalogs_output, catalogs)
    except (CatalogError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": CATALOGS_SCHEMA,
                    "status": "error",
                    "code": "understanding_catalog_invalid",
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
                "schema_version": CATALOGS_SCHEMA,
                "status": catalogs["status"],
                "catalogs_path": str(catalogs_output),
                "candidate_path": str(output),
                "complete_catalog_count": catalogs["complete_catalog_count"],
                "candidate_count": len(candidates),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
