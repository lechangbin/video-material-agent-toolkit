"""Narrow application bridge to one frozen managed yt-dlp runtime."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from material_collector.core.errors import CollectorError
from material_collector.core.media import MediaQuality, Platform
from material_collector.infrastructure.managed_yt_dlp import FrozenYtDlpRuntime
from material_collector.infrastructure.networking import ForeignProxy, foreign_environment


class YtDlpBridge:
    def __init__(
        self,
        runtime: FrozenYtDlpRuntime,
        proxy: ForeignProxy,
        *,
        edge_profiles: dict[Platform, Path],
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runtime = runtime
        self.proxy = proxy
        self.edge_profiles = dict(edge_profiles)
        self._runner = runner
        self._worker = Path(__file__).with_name("yt_dlp_worker.py")

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        request["proxy"] = self.proxy.url
        profile = self.edge_profiles.get(Platform(request["platform"]))
        if profile is not None:
            request["edge_profile"] = str(profile)
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

    def search_youtube(self, query: str, *, limit: int, timeout_seconds: int = 30) -> list[dict[str, Any]]:
        result = self._invoke(
            {
                "operation": "search",
                "platform": Platform.YOUTUBE,
                "query": query,
                "limit": limit,
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
                "timeout_seconds": timeout_seconds,
            }
        )
        return result
