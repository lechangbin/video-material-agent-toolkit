"""Crash-safe local concurrency slots backed by operating-system file locks."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from .common import LockAcquisitionTimeout, try_open_lock_file

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_RESOURCE_NAMES: Final = ("media", "asr", "llm", "render", "ffmpeg_cpu")
_FFMPEG_RESOURCES: Final = frozenset({"media", "render"})


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


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    media: int = 2
    asr: int = 1
    llm: int = 2
    render: int = 1
    ffmpeg_cpu: int = 2

    def __post_init__(self) -> None:
        for resource in _RESOURCE_NAMES:
            value = getattr(self, resource)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{resource} limit must be a positive integer")

    @classmethod
    def from_mapping(cls, values: Mapping[str, int]) -> ResourceLimits:
        unknown = set(values).difference(_RESOURCE_NAMES)
        if unknown:
            raise ValueError(f"unknown resource limits: {sorted(unknown)}")
        return cls(**dict(values))

    def for_resource(self, resource: str) -> int:
        if resource not in _RESOURCE_NAMES:
            raise ValueError(f"unknown resource: {resource}")
        return getattr(self, resource)


class SlotLease(AbstractContextManager["SlotLease"]):
    """One held slot. Closing the file releases it even after process failure."""

    def __init__(self, resource: str, slot: int, path: Path, stream: BinaryIO) -> None:
        self.resource = resource
        self.slot = slot
        self.path = path
        self._stream = stream
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        try:
            _unlock(self._stream)
        finally:
            self._stream.close()
            self._released = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class CompositeLease(AbstractContextManager["CompositeLease"]):
    """A lease over resources acquired in a fixed order."""

    def __init__(self, leases: list[SlotLease]) -> None:
        self.leases = tuple(leases)
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        for lease in reversed(self.leases):
            lease.release()
        self._released = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


class FileSlotPool:
    """A named group of lock files with a fixed number of slots."""

    def __init__(self, locks_root: Path, resource: str, slots: int) -> None:
        if resource not in _RESOURCE_NAMES:
            raise ValueError(f"unknown resource: {resource}")
        if slots < 1:
            raise ValueError("slots must be positive")
        self.resource = resource
        self.slots = slots
        self.root = Path(locks_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _slot_path(self, slot: int) -> Path:
        return self.root / f"{self.resource}-{slot:02d}.lock"

    def try_acquire(self) -> SlotLease | None:
        for slot in range(self.slots):
            path = self._slot_path(slot)
            stream = try_open_lock_file(path)
            if stream is None:
                continue
            if _try_lock(stream):
                return SlotLease(self.resource, slot, path, stream)
            stream.close()
        return None

    def acquire(
        self,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.05,
    ) -> SlotLease:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            lease = self.try_acquire()
            if lease is not None:
                return lease
            if deadline is not None and time.monotonic() >= deadline:
                raise LockAcquisitionTimeout(
                    f"timed out waiting for {self.resource} slot"
                )
            time.sleep(poll_interval)


class ResourceLockManager:
    """Acquire stage resources, including the shared FFmpeg CPU budget."""

    def __init__(self, locks_root: Path, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()
        self.pools = {
            resource: FileSlotPool(
                locks_root,
                resource,
                self.limits.for_resource(resource),
            )
            for resource in _RESOURCE_NAMES
        }

    def acquire(
        self,
        resource: str,
        *,
        timeout: float | None = None,
        poll_interval: float = 0.05,
    ) -> CompositeLease:
        """Acquire a stage budget.

        Media and software rendering always attempt ``ffmpeg_cpu`` before the
        classification slot. If the second resource is busy, the shared lease
        is released before retrying, preventing a waiter from reserving all
        shared capacity while blocked on its category.
        """

        if resource not in {"media", "asr", "llm", "render"}:
            raise ValueError(f"unknown stage resource: {resource}")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            leases: list[SlotLease] = []
            if resource in _FFMPEG_RESOURCES:
                shared = self.pools["ffmpeg_cpu"].try_acquire()
                if shared is not None:
                    leases.append(shared)
                    classified = self.pools[resource].try_acquire()
                    if classified is not None:
                        leases.append(classified)
                        return CompositeLease(leases)
                    shared.release()
            else:
                classified = self.pools[resource].try_acquire()
                if classified is not None:
                    return CompositeLease([classified])

            if deadline is not None and time.monotonic() >= deadline:
                raise LockAcquisitionTimeout(
                    f"timed out waiting for {resource} resources"
                )
            time.sleep(poll_interval)
