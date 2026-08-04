"""Resolve a reusable low-resolution visual-analysis derivative.

The derivative is deliberately source-scoped rather than job-scoped: multiple
jobs for the same content can share the transcode, while the managed original
remains the only source for audio transcription and exported clips.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from semvideo.adapters.ffmpeg import MediaFacts
from semvideo.application.locking import application_file_lock
from semvideo.application.source_store import sha256_file
from semvideo.application.workspace import WorkspacePaths
from semvideo.infrastructure.io import (
    UnsupportedSchemaVersionError,
    atomic_write_json,
    read_versioned_json,
    unlink_best_effort,
)


@dataclass(frozen=True, slots=True)
class AnalysisProxyPolicy:
    """Stable settings that determine one analysis-proxy cache identity."""

    version: int = 1
    max_width: int = 1280
    max_height: int = 720
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 23

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("analysis proxy policy version must be positive")
        if self.max_width < 2 or self.max_height < 2:
            raise ValueError("analysis proxy dimensions must be at least 2")
        if not self.video_codec or not self.preset:
            raise ValueError("analysis proxy codec and preset cannot be empty")
        if not 0 <= self.crf <= 51:
            raise ValueError("analysis proxy CRF must be between 0 and 51")

    @property
    def cache_key(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AnalysisMedia:
    """The media path visual analysis should read, plus auditable provenance."""

    path: Path
    source_root: Path
    content_hash: str
    uses_proxy: bool
    policy: AnalysisProxyPolicy

    def to_provenance(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "analysis_proxy" if self.uses_proxy else "original",
            "relative_path": self.path.relative_to(self.source_root).as_posix(),
            "content_hash": self.content_hash,
            "policy": asdict(self.policy),
        }


def _display_dimensions(facts: MediaFacts) -> tuple[int, int]:
    width = facts.video_stream.width
    height = facts.video_stream.height
    if width is None or height is None:
        raise ValueError("video dimensions could not be determined")
    if abs(facts.video_stream.rotation_degrees) % 180 == 90:
        return height, width
    return width, height


def _proxy_required(facts: MediaFacts, policy: AnalysisProxyPolicy) -> bool:
    width, height = _display_dimensions(facts)
    return width > policy.max_width or height > policy.max_height


def _cached_proxy_valid(
    metadata_path: Path,
    proxy_path: Path,
    *,
    source_root: Path,
    source_video_id: str,
    source_hash: str,
    policy: AnalysisProxyPolicy,
) -> bool:
    try:
        metadata = read_versioned_json(metadata_path)
        return (
            proxy_path.is_file()
            and proxy_path.stat().st_size > 0
            and metadata.get("source_video_id") == source_video_id
            and metadata.get("source_hash") == source_hash
            and metadata.get("relative_path")
            == proxy_path.relative_to(source_root).as_posix()
            and metadata.get("policy") == asdict(policy)
            and metadata.get("content_hash") == sha256_file(proxy_path)
        )
    except UnsupportedSchemaVersionError:
        raise
    except (OSError, ValueError, KeyError):
        return False


def prepare_analysis_media(
    workspace: WorkspacePaths,
    *,
    source_video_id: str,
    source: Path,
    source_hash: str,
    media_facts: MediaFacts,
    policy: AnalysisProxyPolicy,
    create_proxy: Callable[[Path], None],
) -> AnalysisMedia:
    """Return the original or atomically create/reuse its visual proxy."""

    source = source.resolve()
    source_root = workspace.sources / source_video_id
    if not _proxy_required(media_facts, policy):
        return AnalysisMedia(
            path=source,
            source_root=source_root,
            content_hash=source_hash,
            uses_proxy=False,
            policy=policy,
        )

    analysis_root = source_root / "analysis"
    stem = (
        f"analysis-{policy.max_width}x{policy.max_height}-"
        f"v{policy.version}-{policy.cache_key}"
    )
    proxy_path = analysis_root / f"{stem}.mp4"
    metadata_path = analysis_root / f"{stem}.json"
    lock_path = workspace.locks / (
        f"analysis-proxy-{source_video_id}-{policy.cache_key}.lock"
    )
    lock_timeout = max(60.0, media_facts.duration_ms / 1000 * 8)
    with application_file_lock(lock_path, timeout=lock_timeout):
        if not _cached_proxy_valid(
            metadata_path,
            proxy_path,
            source_root=source_root,
            source_video_id=source_video_id,
            source_hash=source_hash,
            policy=policy,
        ):
            analysis_root.mkdir(parents=True, exist_ok=True)
            temporary = analysis_root / f".{stem}.{os.getpid()}.tmp.mp4"
            try:
                unlink_best_effort(temporary)
                create_proxy(temporary)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise OSError("analysis proxy builder produced no output")
                content_hash = sha256_file(temporary)
                os.replace(temporary, proxy_path)
                atomic_write_json(
                    metadata_path,
                    {
                        "schema_version": 1,
                        "source_video_id": source_video_id,
                        "source_hash": source_hash,
                        "relative_path": proxy_path.relative_to(
                            source_root
                        ).as_posix(),
                        "content_hash": content_hash,
                        "policy": asdict(policy),
                    },
                )
            finally:
                unlink_best_effort(temporary)

        return AnalysisMedia(
            path=proxy_path,
            source_root=source_root,
            content_hash=sha256_file(proxy_path),
            uses_proxy=True,
            policy=policy,
        )
