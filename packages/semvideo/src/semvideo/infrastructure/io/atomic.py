"""Small, durable file operations used by the file-backed task store."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class UnsupportedSchemaVersionError(ValueError):
    """A durable artifact is newer than this executable can safely read."""


def unlink_best_effort(path: Path) -> OSError | None:
    """Try to remove a temporary file without masking the primary operation."""

    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        return exc
    return None


def validate_schema_version(
    value: Mapping[str, Any],
    path: Path,
    *,
    supported_versions: tuple[int, ...] = (1,),
) -> None:
    version = value.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in supported_versions
    ):
        raise UnsupportedSchemaVersionError(
            f"unsupported schema_version {version!r} in {path}; "
            f"supported versions: {supported_versions}"
        )


def _flush_directory(path: Path) -> None:
    """Best-effort directory flush.

    Windows does not let regular Python code open directories for ``fsync``.
    ``os.replace`` still gives atomic name replacement there, while POSIX gets
    the extra durability guarantee when supported.
    """

    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Replace *path* with *content* without exposing a partial file."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        _flush_directory(path.parent)
    except BaseException:
        unlink_best_effort(temporary_path)
        raise


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Serialize a JSON object and atomically replace *path*."""

    content = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, content)


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, rejecting non-object top-level values."""

    value = json.loads(_read_text(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def read_versioned_json(
    path: Path,
    *,
    supported_versions: tuple[int, ...] = (1,),
) -> dict[str, Any]:
    value = read_json(path)
    validate_schema_version(
        value,
        Path(path),
        supported_versions=supported_versions,
    )
    return value


def read_versioned_json_lines(
    path: Path,
    *,
    supported_versions: tuple[int, ...] = (1,),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(
                f"expected a JSON object in {path} at line {line_number}"
            )
        validate_schema_version(
            value,
            Path(f"{path}:{line_number}"),
            supported_versions=supported_versions,
        )
        rows.append(value)
    return rows


def _read_text(path: Path, *, attempts: int = 5) -> str:
    """Tolerate brief Windows sharing violations during atomic replacement."""

    target = Path(path)
    for attempt in range(attempts):
        try:
            return target.read_text(encoding="utf-8")
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.01 * (attempt + 1))
    raise AssertionError("unreachable")


def append_json_line(path: Path, value: Mapping[str, Any]) -> None:
    """Append one complete, compact JSON line and flush it to disk."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        written = os.write(descriptor, line)
        if written != len(line):
            raise OSError(f"short append to {path}: {written} of {len(line)} bytes")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
