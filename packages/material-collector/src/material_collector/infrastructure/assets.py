"""Content-addressed media assets owned by a material workspace."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from material_collector.core.errors import WorkspaceError
from material_collector.core.media import AssetRecord, FetchResult

ASSET_DIRECTORY = "assets"
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_SAFE_SESSION_ID = re.compile(r"^ses_[A-Za-z0-9_-]{1,80}$")


class WorkspaceAssetStore:
    """Import verified media once and return portable workspace-relative records."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve(strict=False)
        if self.workspace.exists() and not self.workspace.is_dir():
            raise WorkspaceError(
                "The material workspace path is not a directory.",
                details={"path": str(self.workspace)},
            )
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.asset_root = self.workspace / ASSET_DIRECTORY / "sha256"

    def import_fetch(self, fetched: FetchResult) -> AssetRecord:
        """Hash and atomically import a fetched file without deleting the source."""

        source = fetched.path.expanduser().resolve(strict=True)
        if not source.is_file():
            raise WorkspaceError(
                "The fetched media path is not a regular file.",
                details={"path": str(source)},
            )

        digest, size_bytes = _hash_file(source)
        if fetched.sha256 and fetched.sha256.lower() != digest:
            raise WorkspaceError(
                "The fetched media hash does not match its declared SHA-256.",
                details={
                    "media_unit_id": fetched.media_unit_id,
                    "declared_sha256": fetched.sha256.lower(),
                    "actual_sha256": digest,
                },
            )
        if fetched.size_bytes != size_bytes:
            raise WorkspaceError(
                "The fetched media size does not match its declared size.",
                details={
                    "media_unit_id": fetched.media_unit_id,
                    "declared_size": fetched.size_bytes,
                    "actual_size": size_bytes,
                },
            )

        suffix = source.suffix.lower()
        if not _SAFE_SUFFIX.fullmatch(suffix):
            suffix = ".bin"
        target_directory = self.asset_root / digest[:2]
        target_directory.mkdir(parents=True, exist_ok=True)
        target = target_directory / f"{digest}{suffix}"
        if target.exists():
            existing_digest, existing_size = _hash_file(target)
            if existing_digest != digest or existing_size != size_bytes:
                raise WorkspaceError(
                    "A content-addressed asset path contains conflicting bytes.",
                    details={"path": str(target), "sha256": digest},
                )
        else:
            _copy_atomically(source, target, target_directory)

        relative_path = target.relative_to(self.workspace).as_posix()
        return AssetRecord(
            asset_id=f"sha256:{digest}",
            sha256=digest,
            relative_path=relative_path,
            size_bytes=size_bytes,
            quality=fetched.quality,
            media_unit_id=fetched.media_unit_id,
            container=fetched.container,
            duration_seconds=fetched.duration_seconds,
            width=fetched.width,
            height=fetched.height,
        )

    def resolve(self, asset: AssetRecord) -> Path:
        """Resolve a stored record while preventing workspace escape."""

        candidate = (self.workspace / Path(asset.relative_path)).resolve(strict=False)
        asset_root = self.asset_root.resolve(strict=False)
        if asset_root not in candidate.parents:
            raise WorkspaceError(
                "The asset record resolves outside the workspace asset store.",
                details={"relative_path": asset.relative_path},
            )
        if not candidate.is_file():
            raise WorkspaceError(
                "The asset record does not resolve to a regular file.",
                details={"relative_path": asset.relative_path},
            )
        digest, size_bytes = _hash_file(candidate)
        if digest != asset.sha256 or size_bytes != asset.size_bytes:
            raise WorkspaceError(
                "The workspace asset failed integrity validation.",
                details={"asset_id": asset.asset_id},
            )
        return candidate

    def allocate_staging_path(
        self,
        session_id: str,
        media_unit_id: str,
        quality: str,
    ) -> Path:
        """Allocate a unique caller-owned path for one external fetch attempt."""

        if _SAFE_SESSION_ID.fullmatch(session_id) is None:
            raise WorkspaceError(
                "The staging session identifier is invalid.",
                details={"session_id": session_id},
            )
        del media_unit_id, quality
        staging_root = (
            self.workspace
            / ".material-collector"
            / "sessions"
            / session_id
            / "downloads"
        )
        staging_root.mkdir(parents=True, exist_ok=True)
        return staging_root / f"{uuid.uuid4().hex}.mp4"

    def discard_staging(self, path: Path) -> None:
        """Remove only a staging file allocated inside this workspace."""

        candidate = path.expanduser().resolve(strict=False)
        control_root = (
            self.workspace / ".material-collector" / "sessions"
        ).resolve(strict=False)
        if control_root not in candidate.parents:
            raise WorkspaceError(
                "The staging path resolves outside session control storage.",
                details={"path": str(candidate)},
            )
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            raise WorkspaceError(
                "The staging file could not be discarded.",
                details={
                    "path": str(candidate),
                    "reason": str(error),
                },
            ) from error


class WorkspaceAssetStoreFactory:
    """Production adapter selecting a content-addressed store per workspace."""

    def for_workspace(self, workspace: Path) -> WorkspaceAssetStore:
        return WorkspaceAssetStore(workspace)


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _copy_atomically(source: Path, target: Path, target_directory: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".import-",
        suffix=".tmp",
        dir=target_directory,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, target)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
