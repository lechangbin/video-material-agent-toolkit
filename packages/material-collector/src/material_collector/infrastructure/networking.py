"""Capability-scoped network routing with fail-closed foreign proxy discovery."""

from __future__ import annotations

import hashlib
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from material_collector.core.errors import CollectorError
from material_collector.core.media import (
    PLATFORM_NETWORK_ROUTES,
    NetworkRoute,
    NetworkRouteEvidence,
    Platform,
)

_PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


@dataclass(frozen=True)
class ForeignProxy:
    """Validated proxy endpoint; its URL must never be serialized into reports."""

    url: str
    kind: Literal["http_connect", "socks5"]
    discovery_source: str

    @property
    def safe_fingerprint(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def evidence(self) -> NetworkRouteEvidence:
        return NetworkRouteEvidence(
            route=NetworkRoute.FOREIGN_PROXY,
            discovery_source=self.discovery_source,
            proxy_kind=self.kind,
            validated_at=datetime.now(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
        )


def platform_route(platform: Platform) -> NetworkRoute:
    return PLATFORM_NETWORK_ROUTES[platform]


def _normalize_proxy(value: str, source: str) -> ForeignProxy:
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        raise CollectorError(
            "foreign_proxy_invalid",
            "The configured foreign proxy must use HTTP CONNECT or SOCKS5.",
            details={"discovery_source": source},
        )
    if not parsed.hostname or parsed.port is None:
        raise CollectorError(
            "foreign_proxy_invalid",
            "The configured foreign proxy must include a host and port.",
            details={"discovery_source": source},
        )
    if parsed.username is not None or parsed.password is not None:
        raise CollectorError(
            "foreign_proxy_invalid",
            "Credentialed proxy URLs are not supported by visible browser login.",
            details={"discovery_source": source},
        )
    kind: Literal["http_connect", "socks5"] = (
        "socks5" if scheme.startswith("socks5") else "http_connect"
    )
    return ForeignProxy(url=value.strip(), kind=kind, discovery_source=source)


def _wininet_proxy(environment: Mapping[str, str]) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            if not enabled:
                return None
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not raw:
        return None
    if "=" in raw:
        pairs = dict(
            item.split("=", 1) for item in raw.split(";") if "=" in item
        )
        raw = pairs.get("https") or pairs.get("http") or pairs.get("socks") or ""
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw


def discover_foreign_proxy(
    *,
    configured_proxy: str | None = None,
    environment: Mapping[str, str] | None = None,
    validate_reachability: bool = True,
    connect_timeout_seconds: float = 2.0,
) -> ForeignProxy:
    """Discover one proxy without scanning ports and prove its endpoint is reachable."""

    env = environment if environment is not None else os.environ
    candidates = (
        (configured_proxy, "toolkit_config"),
        (env.get("MATERIAL_COLLECTOR_FOREIGN_PROXY"), "toolkit_environment"),
        (_wininet_proxy(env), "windows_wininet"),
        (
            env.get("HTTPS_PROXY")
            or env.get("https_proxy")
            or env.get("ALL_PROXY")
            or env.get("all_proxy")
            or env.get("HTTP_PROXY")
            or env.get("http_proxy"),
            "standard_environment",
        ),
    )
    selected = next(((value, source) for value, source in candidates if value), None)
    if selected is None:
        raise CollectorError(
            "foreign_proxy_required",
            "Foreign collection requires a configured local proxy and will not connect directly.",
        )
    proxy = _normalize_proxy(str(selected[0]), selected[1])
    if validate_reachability:
        parsed = urlsplit(proxy.url)
        try:
            with socket.create_connection(
                (str(parsed.hostname), int(parsed.port or 0)),
                timeout=connect_timeout_seconds,
            ):
                pass
        except OSError as error:
            raise CollectorError(
                "foreign_proxy_invalid",
                "The configured foreign proxy endpoint is not reachable.",
                details={
                    "discovery_source": proxy.discovery_source,
                    "proxy_fingerprint": proxy.safe_fingerprint,
                },
            ) from error
    return proxy


def direct_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment that cannot inherit a proxy."""

    result = dict(base or os.environ)
    for name in _PROXY_ENV_NAMES:
        result.pop(name, None)
    result["NO_PROXY"] = "*"
    result["no_proxy"] = "*"
    return result


def foreign_environment(
    proxy: ForeignProxy,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a subprocess environment with one explicit foreign route."""

    result = direct_environment(base)
    result.pop("NO_PROXY", None)
    result.pop("no_proxy", None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        result[name] = proxy.url
    return result


def browser_proxy(proxy: ForeignProxy) -> dict[str, str]:
    return {"server": proxy.url}


def http_client_for_route(
    route: NetworkRoute,
    *,
    proxy: ForeignProxy | None = None,
    timeout_seconds: float = 30,
) -> httpx.AsyncClient:
    if route is NetworkRoute.DOMESTIC_DIRECT:
        return httpx.AsyncClient(trust_env=False, timeout=timeout_seconds)
    if proxy is None:
        raise CollectorError(
            "foreign_proxy_required",
            "Foreign HTTP access cannot start without a validated proxy.",
        )
    return httpx.AsyncClient(proxy=proxy.url, trust_env=False, timeout=timeout_seconds)


def ensure_path_outside_workspace(path: Path, workspace: Path) -> Path:
    resolved = path.resolve()
    workspace_resolved = workspace.resolve()
    if resolved == workspace_resolved or workspace_resolved in resolved.parents:
        raise CollectorError(
            "authentication_profile_location_invalid",
            "Authentication profiles must live outside the material workspace.",
        )
    return resolved
