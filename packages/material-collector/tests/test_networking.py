from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from material_collector.core.errors import CollectorError
from material_collector.core.media import NetworkRoute, Platform
from material_collector.infrastructure.managed_yt_dlp import ManagedYtDlpRuntime
from material_collector.infrastructure.networking import (
    direct_environment,
    discover_foreign_proxy,
    foreign_environment,
    platform_route,
)


def test_platform_routes_are_capability_owned() -> None:
    assert platform_route(Platform.BILIBILI) is NetworkRoute.DOMESTIC_DIRECT
    assert platform_route(Platform.DOUYIN) is NetworkRoute.DOMESTIC_DIRECT
    assert platform_route(Platform.XIAOHONGSHU) is NetworkRoute.DOMESTIC_DIRECT
    assert platform_route(Platform.YOUTUBE) is NetworkRoute.FOREIGN_PROXY
    assert platform_route(Platform.TIKTOK) is NetworkRoute.FOREIGN_PROXY


def test_domestic_environment_suppresses_inherited_proxy() -> None:
    result = direct_environment(
        {
            "PATH": "bin",
            "HTTP_PROXY": "http://127.0.0.1:9000",
            "https_proxy": "http://127.0.0.1:9001",
        }
    )

    assert result["PATH"] == "bin"
    assert result["NO_PROXY"] == "*"
    assert "HTTP_PROXY" not in result
    assert "https_proxy" not in result


def test_foreign_environment_uses_one_proxy_and_never_direct() -> None:
    proxy = discover_foreign_proxy(
        environment={"MATERIAL_COLLECTOR_FOREIGN_PROXY": "socks5://127.0.0.1:10808"},
        validate_reachability=False,
    )

    result = foreign_environment(proxy, {"PATH": "bin", "HTTP_PROXY": "old"})

    assert result["HTTP_PROXY"] == proxy.url
    assert result["HTTPS_PROXY"] == proxy.url
    assert result["ALL_PROXY"] == proxy.url
    assert "NO_PROXY" not in result


def test_foreign_proxy_discovery_fails_closed_without_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        "material_collector.infrastructure.networking._wininet_proxy",
        lambda _environment: None,
    )
    with pytest.raises(CollectorError) as captured:
        discover_foreign_proxy(environment={}, validate_reachability=False)

    assert captured.value.code == "foreign_proxy_required"


def test_managed_runtime_freezes_active_version_and_checks_once_per_day(
    tmp_path: Path,
) -> None:
    root = tmp_path / "yt-dlp"
    runtime = root / "versions" / "2026.08.18.235959"
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    now = datetime(2026, 8, 19, tzinfo=UTC)
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "managed-yt-dlp-runtime/v1",
                "active_version": "2026.08.18.235959",
                "last_known_good_version": "2026.08.18.235959",
                "checked_at": (now - timedelta(hours=23)).isoformat(),
                "versions": {
                    "2026.08.18.235959": {"package_hash": "a" * 64}
                },
            }
        ),
        encoding="utf-8",
    )
    manager = ManagedYtDlpRuntime(root, now=lambda: now)

    frozen = manager.freeze_for_session()

    assert frozen.version == "2026.08.18.235959"
    assert frozen.package_hash == "a" * 64
    assert manager.update_due() is False
