"""Build one versioned editing-media-conformance request from selected ranges."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REQUEST_SCHEMA_VERSION = "editing-media-conformance-request/v1"
RANGES_SCHEMA_VERSION = "selected-source-ranges/v1"
OUTPUT_SCHEMA_VERSION = "conformance-request-bridge/v1"
PROFILE = {
    "schema_version": "editing-delivery-profile/v1",
    "container": "mp4",
    "video_codec": "h264",
    "video_encoder": "libx264",
    "video_profile": "high",
    "pixel_format": "yuv420p",
    "width": 1920,
    "height": 1080,
    "sample_aspect_ratio": "1:1",
    "frame_rate": "30/1",
    "gop_frames": 60,
    "video_track_time_scale": 90000,
    "color_primaries": "bt709",
    "color_transfer": "bt709",
    "color_space": "bt709",
    "audio_codec": "aac",
    "audio_profile": "LC",
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "audio_bit_rate": 192000,
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ConformanceRequestError(RuntimeError):
    """The selected ranges cannot become a conformance request."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConformanceRequestError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise ConformanceRequestError(f"JSON artifact must contain an object: {path}")
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ConformanceRequestError(f"{label} is not a safe identifier: {value!r}")
    return value


def _resolve_asset_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConformanceRequestError("source asset path is missing")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ConformanceRequestError("source asset path must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ConformanceRequestError(f"source asset is not a file: {resolved}")
    return resolved


def _safe_output_directory(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ConformanceRequestError("output directory must be absolute")
    resolved = candidate.resolve(strict=False)
    if resolved.parent == resolved:
        raise ConformanceRequestError("output directory must not be a filesystem root")
    return resolved


def _validate_ranges(ranges: Any) -> list[dict[str, Any]]:
    if not isinstance(ranges, list) or not ranges:
        raise ConformanceRequestError("ranges must be a non-empty list")
    seen: set[str] = set()
    for item in ranges:
        if not isinstance(item, dict):
            raise ConformanceRequestError("each range must be an object")
        _safe_identifier(item.get("clip_id"), "clip_id")
        if item["clip_id"] in seen:
            raise ConformanceRequestError(f"clip_id is duplicated: {item['clip_id']}")
        seen.add(item["clip_id"])
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        for name, value in (("start_seconds", start), ("end_seconds", end)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConformanceRequestError(f"{name} must be a number")
        if start < 0 or end <= start:
            raise ConformanceRequestError(
                "selected ranges must be half-open with end greater than start and "
                f"non-negative start: {item['clip_id']}"
            )
    return ranges


def build_request(
    *,
    workflow: dict[str, Any],
    selection_result: dict[str, Any],
    ranges_document: dict[str, Any],
    output_directory: Path,
) -> dict[str, Any]:
    if workflow.get("schema_version") != "video-material-workflow/v1":
        raise ConformanceRequestError("workflow schema is unsupported")
    if not isinstance(workflow.get("workflow_id"), str) or not workflow["workflow_id"]:
        raise ConformanceRequestError("workflow identity is missing")
    if ranges_document.get("schema_version") != RANGES_SCHEMA_VERSION:
        raise ConformanceRequestError("selected ranges schema is unsupported")
    asset_id = _safe_identifier(
        ranges_document.get("asset_id"), "asset_id"
    )
    asset_path = _resolve_asset_path(ranges_document.get("path"))
    asset_sha256 = _file_sha256(asset_path)
    declared_sha = ranges_document.get("sha256")
    if declared_sha is not None:
        if not isinstance(declared_sha, str) or not _SHA256.fullmatch(declared_sha):
            raise ConformanceRequestError("declared sha256 is invalid")
        if declared_sha != asset_sha256:
            raise ConformanceRequestError(
                "source asset bytes changed since the declared sha256"
            )
    ranges = _validate_ranges(ranges_document.get("ranges"))
    selection_id = selection_result.get("selection_id")
    if not isinstance(selection_id, str) or not selection_id:
        raise ConformanceRequestError("selection result identity is missing")
    lineage_hash = _canonical_hash(
        {
            "workflow_id": workflow["workflow_id"],
            "selection_id": selection_id,
            "asset_id": asset_id,
            "source_sha256": asset_sha256,
            "ranges": ranges,
        }
    )
    request_id = f"mc_{lineage_hash[:12]}"
    idempotency_key = f"idem_{lineage_hash[:16]}"
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "source": {
            "asset_id": asset_id,
            "sha256": asset_sha256,
            "path": str(asset_path),
        },
        "ranges": [
            {
                "clip_id": item["clip_id"],
                "start_seconds": item["start_seconds"],
                "end_seconds": item["end_seconds"],
            }
            for item in ranges
        ],
        "output_directory": str(output_directory),
        "profile": dict(PROFILE),
    }
    return request


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
    parser = argparse.ArgumentParser(prog="prepare-conformance-request")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--selection-result", required=True)
    parser.add_argument("--ranges", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--output-directory", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        workflow = _load_object(Path(arguments.workflow).resolve(strict=True))
        selection_result = _load_object(
            Path(arguments.selection_result).resolve(strict=True)
        )
        ranges_document = _load_object(Path(arguments.ranges).resolve(strict=True))
        output_directory = _safe_output_directory(arguments.output_directory)
        request = build_request(
            workflow=workflow,
            selection_result=selection_result,
            ranges_document=ranges_document,
            output_directory=output_directory,
        )
        output_path = Path(arguments.output).expanduser().resolve(strict=False)
        if output_path.exists():
            existing = _load_object(output_path)
            if existing != request:
                raise ConformanceRequestError(
                    "refusing to overwrite a conflicting conformance request"
                )
        else:
            _atomic_write_json(output_path, request)
    except (ConformanceRequestError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": OUTPUT_SCHEMA_VERSION,
                    "status": "error",
                    "code": "conformance_request_bridge_invalid",
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
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "status": "prepared",
                "request_path": str(output_path),
                "request_sha256": _canonical_hash(request),
                "request_id": request["request_id"],
                "idempotency_key": request["idempotency_key"],
                "clip_count": len(request["ranges"]),
                "command": f"media-conformance prepare --request {output_path}",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
