"""Shared primitives for Windows-safe lock-file acquisition."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class LockAcquisitionTimeout(TimeoutError):
    """Raised when a lock cannot be acquired before its deadline."""


def try_open_lock_file(path: Path) -> BinaryIO | None:
    """Open and initialize a lock file, treating sharing violations as busy."""

    try:
        stream = path.open("a+b")
    except PermissionError:
        return None
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
    except PermissionError:
        stream.close()
        return None
    except BaseException:
        stream.close()
        raise
    return stream
