from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from semvideo.infrastructure.process_identity import process_matches, process_started_at


@pytest.mark.skipif(
    os.name == "nt" or not Path("/proc").is_dir(),
    reason="Linux /proc process states are required",
)
def test_process_matches_treats_zombie_as_stopped() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
    )
    try:
        started_at = process_started_at(child.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat_fields = Path(f"/proc/{child.pid}/stat").read_text(encoding="ascii").split()
            if stat_fields[2] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("child process did not enter the zombie state")

        assert process_matches(child.pid, started_at) is False
    finally:
        child.wait(timeout=5)
