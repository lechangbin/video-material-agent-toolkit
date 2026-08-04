"""Stable manifest records for files inside a task package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from semvideo.application.source_store import sha256_file


def artifact_record(
    job_root: Path,
    path: Path,
    kind: str,
    *,
    schema_version: int = 1,
) -> dict[str, Any]:
    content_hash = sha256_file(path)
    identity = json.dumps(
        {"kind": kind, "content_hash": content_hash},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    opaque = hashlib.sha256(identity).hexdigest()[:24]
    return {
        "artifact_id": f"artifact_{opaque}",
        "kind": kind,
        "path": path.relative_to(job_root).as_posix(),
        "content_hash": content_hash,
        "schema_version": schema_version,
    }
