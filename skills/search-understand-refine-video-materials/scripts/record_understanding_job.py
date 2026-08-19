"""Atomically bind one Semvideo process response to an understanding batch item."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BATCH_SCHEMA = "video-material-understanding-batch/v2"
JOBS_SCHEMA = "video-material-understanding-jobs/v1"


class JobRecordError(RuntimeError):
    """A Semvideo job response does not match its batch item."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise JobRecordError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise JobRecordError(f"JSON artifact must contain an object: {path}")
    return value


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


def merge_jobs(
    *,
    batch: dict[str, Any],
    job_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    if batch.get("schema_version") != BATCH_SCHEMA:
        raise JobRecordError("understanding batch schema is unsupported")
    batch_items = batch.get("items")
    if not isinstance(batch_items, list):
        raise JobRecordError("understanding batch must contain items")
    batch_by_id = {
        item["item_id"]: item
        for item in batch_items
        if isinstance(item, dict) and isinstance(item.get("item_id"), str)
    }
    if len(batch_by_id) != len(batch_items):
        raise JobRecordError("understanding batch contains invalid or duplicate items")
    merged: dict[str, dict[str, Any]] = {}
    for artifact in job_artifacts:
        if artifact.get("schema_version") != JOBS_SCHEMA or not isinstance(
            artifact.get("items"), list
        ):
            raise JobRecordError("understanding jobs artifact is invalid")
        for record in artifact["items"]:
            if not isinstance(record, dict) or not isinstance(
                record.get("item_id"), str
            ):
                raise JobRecordError("understanding job record is invalid")
            item_id = record["item_id"]
            batch_item = batch_by_id.get(item_id)
            if batch_item is None:
                continue
            if (
                record.get("asset_sha256") != batch_item.get("asset_sha256")
                or record.get("semvideo_profile")
                != batch_item.get("semvideo_profile")
                or record.get("semvideo_idempotency_key")
                != batch_item.get("semvideo_idempotency_key")
            ):
                raise JobRecordError(
                    f"understanding job does not match batch item: {item_id}"
                )
            existing = merged.get(item_id)
            if existing is not None and existing != record:
                raise JobRecordError(
                    f"batch item is bound to conflicting jobs: {item_id}"
                )
            merged[item_id] = dict(record)
    return {
        "schema_version": JOBS_SCHEMA,
        "items": [merged[item_id] for item_id in sorted(merged)],
    }


def record_job(
    *,
    batch: dict[str, Any],
    jobs: dict[str, Any] | None,
    item_id: str,
    response: dict[str, Any],
    response_hash: str,
    lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if batch.get("schema_version") != BATCH_SCHEMA:
        raise JobRecordError("understanding batch schema is unsupported")
    items = batch.get("items")
    if not isinstance(items, list):
        raise JobRecordError("understanding batch must contain items")
    batch_item = next(
        (
            item
            for item in items
            if isinstance(item, dict) and item.get("item_id") == item_id
        ),
        None,
    )
    if batch_item is None:
        raise JobRecordError(f"batch item does not exist: {item_id}")
    job_id = response.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise JobRecordError("Semvideo process response is missing job_id")
    if jobs is None:
        payload: dict[str, Any] = {
            "schema_version": JOBS_SCHEMA,
            "items": [],
        }
    else:
        if jobs.get("schema_version") != JOBS_SCHEMA or not isinstance(
            jobs.get("items"), list
        ):
            raise JobRecordError("understanding jobs artifact is invalid")
        payload = jobs
    existing = next(
        (
            item
            for item in payload["items"]
            if isinstance(item, dict) and item.get("item_id") == item_id
        ),
        None,
    )
    record: dict[str, Any] = {
        "item_id": item_id,
        "asset_sha256": batch_item["asset_sha256"],
        "semvideo_profile": batch_item["semvideo_profile"],
        "semvideo_idempotency_key": batch_item["semvideo_idempotency_key"],
        "job_id": job_id,
        "submission_state": response.get("state"),
        "process_response_sha256": response_hash,
    }
    if lineage is not None:
        if not isinstance(lineage, dict):
            raise JobRecordError("understanding lineage must be an object")
        normalized = {key: value for key, value in lineage.items() if key and value is not None}
        if normalized:
            record["lineage"] = dict(normalized)
    if existing is not None and existing != record:
        raise JobRecordError(f"batch item is already bound to another job: {item_id}")
    if existing is None:
        payload["items"].append(record)
        payload["items"].sort(key=lambda item: item["item_id"])
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="record-understanding-job")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--previous-jobs", action="append", default=[])
    parser.add_argument("--item-id")
    parser.add_argument("--process-response")
    parser.add_argument(
        "--lineage",
        help=(
            "Optional JSON object preserving CRV, context-tier, attempt, "
            "result, validation, and Semvideo import hashes for this item."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        batch_path = Path(arguments.batch).expanduser().resolve(strict=True)
        jobs_path = Path(arguments.jobs).expanduser().resolve(strict=False)
        if (arguments.item_id is None) != (arguments.process_response is None):
            raise JobRecordError(
                "item-id and process-response must be supplied together"
            )
        prior_paths = [
            Path(value).expanduser().resolve(strict=True)
            for value in arguments.previous_jobs
        ]
        if jobs_path.exists():
            prior_paths.append(jobs_path.resolve(strict=True))
        batch = _load_object(batch_path)
        payload = merge_jobs(
            batch=batch,
            job_artifacts=[_load_object(path) for path in prior_paths],
        )
        if arguments.item_id is not None:
            response_path = (
                Path(arguments.process_response).expanduser().resolve(strict=True)
            )
            response_bytes = response_path.read_bytes()
            lineage = (
                _load_object(Path(arguments.lineage).expanduser().resolve(strict=True))
                if arguments.lineage
                else None
            )
            payload = record_job(
                batch=batch,
                jobs=payload,
                item_id=arguments.item_id,
                response=_load_object(response_path),
                response_hash=hashlib.sha256(response_bytes).hexdigest(),
                lineage=lineage,
            )
        _atomic_write_json(jobs_path, payload)
    except (JobRecordError, OSError, ValueError, KeyError) as error:
        print(
            json.dumps(
                {
                    "schema_version": JOBS_SCHEMA,
                    "status": "error",
                    "code": "understanding_job_record_invalid",
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
                "schema_version": JOBS_SCHEMA,
                "status": "recorded",
                "jobs_path": str(jobs_path),
                "item_id": arguments.item_id,
                "job_id": (
                    None
                    if arguments.item_id is None
                    else next(
                        item["job_id"]
                        for item in payload["items"]
                        if item["item_id"] == arguments.item_id
                    )
                ),
                "mapped_job_count": len(payload["items"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
