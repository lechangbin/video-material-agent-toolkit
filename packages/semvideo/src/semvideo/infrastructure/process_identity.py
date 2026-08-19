"""Portable-enough process identity checks for detached local workers."""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from datetime import UTC, datetime
from typing import cast

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WINDOWS_EPOCH_OFFSET_SECONDS = 11_644_473_600


class ProcessLookupFailure(LookupError):
    """Raised when a process no longer exists or cannot be inspected."""


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _windows_process_started_at(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    get_process_times.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise ProcessLookupFailure(f"cannot inspect process {pid}")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ProcessLookupFailure(f"cannot read creation time for process {pid}")
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        timestamp = ticks / 10_000_000 - _WINDOWS_EPOCH_OFFSET_SECONDS
        return _iso_utc(datetime.fromtimestamp(timestamp, UTC))
    finally:
        close_handle(handle)


def _linux_process_started_at(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stream:
            stat_fields = stream.read().split()
        if stat_fields[2] == "Z":
            raise ProcessLookupFailure(f"process {pid} is a zombie")
        start_ticks = int(stat_fields[21])
        sysconf = cast(Callable[[str], int], os.__dict__["sysconf"])
        clock_ticks = sysconf("SC_CLK_TCK")
        with open("/proc/stat", encoding="ascii") as stream:
            boot_line = next(line for line in stream if line.startswith("btime "))
        boot_time = int(boot_line.split()[1])
    except ProcessLookupFailure:
        raise
    except (OSError, StopIteration, ValueError, IndexError) as error:
        raise ProcessLookupFailure(f"cannot inspect process {pid}") from error
    return _iso_utc(datetime.fromtimestamp(boot_time + start_ticks / clock_ticks, UTC))


def process_started_at(pid: int) -> str:
    """Return a stable UTC creation timestamp for *pid*."""

    if pid <= 0:
        raise ValueError("pid must be positive")
    if os.name == "nt":
        return _windows_process_started_at(pid)
    if os.path.isdir("/proc"):
        return _linux_process_started_at(pid)
    raise ProcessLookupFailure("process identity inspection is unsupported")


def current_process_identity() -> tuple[int, str]:
    """Return the current PID and its operating-system creation time."""

    pid = os.getpid()
    return pid, process_started_at(pid)


def process_matches(pid: int, expected_started_at: str) -> bool:
    """Check both PID existence and creation time to reject PID reuse."""

    try:
        return process_started_at(pid) == expected_started_at
    except (ProcessLookupFailure, PermissionError):
        return False
