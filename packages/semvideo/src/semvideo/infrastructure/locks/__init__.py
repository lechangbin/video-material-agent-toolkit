"""Operating-system-held concurrency slots."""

from .common import LockAcquisitionTimeout
from .named import exclusive_file_lock
from .slots import (
    CompositeLease,
    FileSlotPool,
    ResourceLimits,
    ResourceLockManager,
    SlotLease,
)

__all__ = [
    "CompositeLease",
    "FileSlotPool",
    "LockAcquisitionTimeout",
    "ResourceLimits",
    "ResourceLockManager",
    "SlotLease",
    "exclusive_file_lock",
]
