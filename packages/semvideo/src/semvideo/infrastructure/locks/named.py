"""Cross-process exclusive locks for short workspace mutations."""

from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from .common import LockAcquisitionTimeout, try_open_lock_file

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _try_lock(stream: BinaryIO) -> bool:
    stream.seek(0)
    try:
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Hold one OS-released lock byte without relying on a lease or heartbeat."""

    if timeout < 0 or poll_interval <= 0:
        raise ValueError("invalid lock timing")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    stream: BinaryIO | None = None
    while stream is None:
        stream = try_open_lock_file(path)
        if stream is None:
            if time.monotonic() >= deadline:
                raise LockAcquisitionTimeout(
                    f"timed out waiting for lock: {path}"
                )
            time.sleep(
                poll_interval
                + random.uniform(0.0, min(poll_interval, 0.05))
            )
    try:
        while not _try_lock(stream):
            if time.monotonic() >= deadline:
                raise LockAcquisitionTimeout(
                    f"timed out waiting for lock: {path}"
                )
            time.sleep(
                poll_interval
                + random.uniform(0.0, min(poll_interval, 0.05))
            )
        try:
            yield
        finally:
            _unlock(stream)
    finally:
        stream.close()
