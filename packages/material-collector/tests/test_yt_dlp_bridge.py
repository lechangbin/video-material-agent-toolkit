from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from material_collector.core.media import BrowserChannel, Platform
from material_collector.infrastructure.managed_yt_dlp import FrozenYtDlpRuntime
from material_collector.infrastructure.networking import ForeignProxy
from material_collector.infrastructure.yt_dlp_bridge import YtDlpBridge
from material_collector.infrastructure.yt_dlp_worker import _base_options


def test_bridge_passes_frozen_browser_profile_over_stdin_only(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "operation": "auth_probe",
                        "platform": "youtube",
                        "authenticated": True,
                    },
                }
            ),
            stderr="",
        )

    profile = tmp_path / "auth" / "editing" / "edge" / "youtube"
    bridge = YtDlpBridge(
        FrozenYtDlpRuntime("2026.08.18", tmp_path / "python.exe", "a" * 64),
        ForeignProxy("http://127.0.0.1:7890", "http_connect", "test"),
        browser_profiles={(BrowserChannel.EDGE, Platform.YOUTUBE): profile},
        runner=runner,
    )

    assert bridge.probe_auth(Platform.YOUTUBE, BrowserChannel.EDGE) is True
    request = json.loads(captured["input"])
    assert request["browser_channel"] == "edge"
    assert request["browser_profile"] == str(profile)
    assert str(profile) not in " ".join(captured["command"])


def test_worker_uses_the_frozen_channel_and_profile() -> None:
    options = _base_options(
        {
            "platform": "youtube",
            "proxy": "http://127.0.0.1:7890",
            "browser_channel": "chrome",
            "browser_profile": "C:/profiles/youtube",
        }
    )

    assert options["cookiesfrombrowser"] == (
        "chrome",
        "C:/profiles/youtube",
        None,
        None,
    )
