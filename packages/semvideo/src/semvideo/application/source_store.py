"""Managed, content-addressed source-video copies inside one workspace."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from semvideo.application.locking import application_file_lock
from semvideo.application.workspace import WorkspacePaths
from semvideo.errors import input_error
from semvideo.infrastructure.io import (
    atomic_write_json,
    read_versioned_json,
    unlink_best_effort,
)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def register_source(workspace: WorkspacePaths, source: str | Path) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise input_error(
            "source_video_not_found",
            f"源视频不存在或不是文件：{source_path}",
            source=str(source_path),
        )
    content_hash = sha256_file(source_path)
    digest = content_hash.split(":", 1)[1]
    source_video_id = f"video_{digest[:24]}"
    source_root = workspace.sources / source_video_id
    metadata_path = source_root / "source.json"
    with application_file_lock(
        workspace.locks / f"source-{digest[:24]}.lock"
    ):
        if metadata_path.is_file():
            metadata = read_versioned_json(metadata_path)
            managed = source_root / str(metadata["managed_filename"])
            if (
                managed.is_file()
                and metadata.get("content_hash") == content_hash
                and sha256_file(managed) == content_hash
            ):
                return {**metadata, "managed_path": str(managed)}

        source_root.mkdir(parents=True, exist_ok=True)
        suffix = source_path.suffix.lower() or ".video"
        managed_filename = f"source{suffix}"
        managed_path = source_root / managed_filename
        temporary = source_root / f".{managed_filename}.{os.getpid()}.tmp"
        try:
            shutil.copy2(source_path, temporary)
            if sha256_file(temporary) != content_hash:
                raise OSError("copied source hash mismatch")
            os.replace(temporary, managed_path)
        finally:
            unlink_best_effort(temporary)
        metadata = {
            "schema_version": 1,
            "source_video_id": source_video_id,
            "content_hash": content_hash,
            "original_filename": source_path.name,
            "managed_filename": managed_filename,
            "size_bytes": managed_path.stat().st_size,
        }
        atomic_write_json(metadata_path, metadata)
        return {**metadata, "managed_path": str(managed_path)}


def resolve_source(workspace: WorkspacePaths, source_video_id: str) -> tuple[Path, dict[str, Any]]:
    root = workspace.sources / source_video_id
    metadata_path = root / "source.json"
    if not metadata_path.is_file():
        raise input_error(
            "managed_source_missing",
            f"受管源视频记录不存在：{source_video_id}",
            source_video_id=source_video_id,
        )
    metadata = read_versioned_json(metadata_path)
    path = root / str(metadata["managed_filename"])
    if not path.is_file():
        raise input_error(
            "managed_source_file_missing",
            f"受管源视频文件不存在：{path}",
            source_video_id=source_video_id,
        )
    actual_hash = sha256_file(path)
    if actual_hash != metadata.get("content_hash"):
        raise input_error(
            "managed_source_hash_mismatch",
            "受管源视频内容与登记哈希不一致；请重新提交原始视频。",
            source_video_id=source_video_id,
            expected_hash=metadata.get("content_hash"),
            actual_hash=actual_hash,
        )
    return path, metadata
