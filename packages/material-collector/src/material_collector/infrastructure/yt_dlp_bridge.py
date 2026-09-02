"""Narrow application bridge to one frozen managed yt-dlp runtime."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from material_collector.core.errors import CollectorError
from material_collector.core.media import BrowserChannel, MediaQuality, Platform
from material_collector.infrastructure.managed_yt_dlp import FrozenYtDlpRuntime
from material_collector.infrastructure.networking import ForeignProxy, foreign_environment


class YtDlpBridge:
    def __init__(
        self,
        runtime: FrozenYtDlpRuntime,
        proxy: ForeignProxy,
        *,
        browser_profiles: dict[tuple[BrowserChannel, Platform], Path],
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runtime = runtime
        self.proxy = proxy
        self.browser_profiles = dict(browser_profiles)
        self._runner = runner
        self._worker = Path(__file__).with_name("yt_dlp_worker.py")

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        request["proxy"] = self.proxy.url
        channel_value = request.get("browser_channel")
        if channel_value is not None:
            channel = BrowserChannel(str(channel_value))
            profile = self.browser_profiles.get(
                (channel, Platform(request["platform"]))
            )
            if profile is not None:
                request["browser_profile"] = str(profile)
        completed = self._runner(
            [str(self.runtime.python_executable), str(self._worker)],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=foreign_environment(self.proxy),
            check=False,
            timeout=int(request.get("timeout_seconds", 30)) + 30,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise CollectorError(
                "yt_dlp_operation_failed",
                "The managed yt-dlp bridge returned an invalid response.",
                details={"runtime_version": self.runtime.version},
            ) from error
        if completed.returncode != 0 or not payload.get("ok"):
            code = str(payload.get("error", {}).get("code", "yt_dlp_operation_failed"))
            raise CollectorError(
                code,
                "The managed yt-dlp operation failed.",
                details={"runtime_version": self.runtime.version},
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CollectorError(
                "yt_dlp_operation_failed",
                "The managed yt-dlp result is malformed.",
                details={"runtime_version": self.runtime.version},
            )
        return result

    def probe_auth(
        self,
        platform: Platform,
        browser_channel: BrowserChannel,
        *,
        timeout_seconds: int = 30,
    ) -> bool:
        result = self._invoke(
            {
                "operation": "auth_probe",
                "platform": platform,
                "browser_channel": browser_channel,
                "timeout_seconds": timeout_seconds,
            }
        )
        return result.get("authenticated") is True

    def search_youtube(
        self,
        query: str,
        *,
        limit: int,
        browser_channel: BrowserChannel,
        timeout_seconds: int = 30,
    ) -> list[dict[str, Any]]:
        result = self._invoke(
            {
                "operation": "search",
                "platform": Platform.YOUTUBE,
                "query": query,
                "limit": limit,
                "browser_channel": browser_channel,
                "timeout_seconds": timeout_seconds,
            }
        )
        entries = result.get("entries", [])
        return [item for item in entries if isinstance(item, dict)]

    def resolve(
        self,
        platform: Platform,
        url: str,
        *,
        browser_channel: BrowserChannel,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if platform not in {Platform.YOUTUBE, Platform.TIKTOK}:
            raise CollectorError(
                "foreign_platform_unsupported",
                "The managed yt-dlp bridge accepts only YouTube and TikTok.",
            )
        result = self._invoke(
            {
                "operation": "resolve",
                "platform": platform,
                "url": url,
                "browser_channel": browser_channel,
                "timeout_seconds": timeout_seconds,
            }
        )
        info = result.get("info")
        if not isinstance(info, dict):
            raise CollectorError("yt_dlp_operation_failed", "yt-dlp metadata is malformed.")
        return info

    def download(
        self,
        platform: Platform,
        url: str,
        destination: Path,
        quality: MediaQuality,
        *,
        browser_channel: BrowserChannel,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        selector = (
            "bv*[height<=720]+ba/b[height<=720]/b"
            if quality is MediaQuality.LOW_PROXY
            else "bv*+ba/b"
        )
        result = self._invoke(
            {
                "operation": "download",
                "platform": platform,
                "url": url,
                "destination": str(destination),
                "format_selector": selector,
                "browser_channel": browser_channel,
                "timeout_seconds": timeout_seconds,
            }
        )
        return result
