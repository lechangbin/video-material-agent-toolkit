"""Injectable browser and download transport for authenticated platforms."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urljoin

import httpcore
import httpx
from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from material_collector.core.media import BrowserChannel, Platform, PlatformContext
from material_collector.infrastructure.platforms._shared import (
    validate_temporary_media_url,
)
from material_collector.infrastructure.platforms.errors import PlatformAdapterError
from material_collector.infrastructure.platforms.rendered_access import (
    RenderedAccessState,
    inspect_rendered_access,
)

_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DIRECT_CHROMIUM_ARGS = ("--no-proxy-server",)
_PLAYWRIGHT_CHANNEL = {
    BrowserChannel.EDGE: "msedge",
    BrowserChannel.CHROME: "chrome",
}
_CAPTURE_WARMUP_URLS: Mapping[Platform, str] = {
    Platform.XIAOHONGSHU: "https://www.xiaohongshu.com/explore",
}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_MEDIA_REDIRECTS = 5
_SHORT_INITIAL_HOSTS: Mapping[Platform, frozenset[str]] = {
    Platform.BILIBILI: frozenset(),
    Platform.DOUYIN: frozenset({"v.douyin.com", "iesdouyin.com"}),
    Platform.XIAOHONGSHU: frozenset({"xhslink.com"}),
}
_SHORT_CHAIN_SUFFIXES: Mapping[Platform, tuple[str, ...]] = {
    Platform.BILIBILI: (),
    Platform.DOUYIN: ("douyin.com", "iesdouyin.com"),
    Platform.XIAOHONGSHU: ("xhslink.com", "xiaohongshu.com"),
}
_CaptureTimeoutPhase = Literal["warmup", "navigation", "response"]
_SearchContextKey = tuple[str, BrowserChannel, Platform, bool]


@dataclass(slots=True)
class _SharedSearchContext:
    manager: Any
    browser: BrowserContext
    closed_by_user: bool = False


class HostResolver(Protocol):
    async def __call__(
        self,
        host: str,
        port: int,
        *,
        type: socket.SocketKind,
    ) -> list[tuple[Any, ...]]: ...


async def _system_resolver(
    host: str,
    port: int,
    *,
    type: socket.SocketKind,
) -> list[tuple[Any, ...]]:
    result = await asyncio.get_running_loop().getaddrinfo(host, port, type=type)
    return list(result)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to only prevalidated IPs while preserving the origin for TLS/Host."""

    def __init__(self, delegate: httpcore.AsyncNetworkBackend) -> None:
        self._delegate = delegate
        self._pins: dict[str, tuple[str, ...]] = {}

    def pin(self, host: str, addresses: tuple[str, ...]) -> None:
        self._pins[host.casefold().rstrip(".")] = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = self._pins.get(host.casefold().rstrip("."))
        if not addresses:
            raise httpcore.ConnectError("The origin has no validated DNS pin.")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.UnsupportedProtocol("Unix sockets are disabled.")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend: _PinnedNetworkBackend) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(network_backend=backend)


class PlatformTransport(Protocol):
    """Small replaceable seam around browser-authenticated network activity."""

    async def capture_json(
        self,
        page_url: str,
        response_url_fragment: str,
        *,
        platform: Platform,
        operation: str,
        context: PlatformContext,
    ) -> Mapping[str, Any]: ...

    async def resolve_url(
        self,
        url: str,
        *,
        platform: Platform,
        context: PlatformContext,
    ) -> str: ...

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        context: PlatformContext,
        referer: str,
    ) -> None: ...


class PlaywrightPlatformTransport:
    """Use a persistent authenticated browser profile for one short operation."""

    def __init__(
        self,
        auth_root: Path | None = None,
        *,
        headless: bool = True,
        resolver: HostResolver | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._auth_root = auth_root
        self._headless = headless
        self._resolver = resolver or _system_resolver
        self._http_transport = http_transport
        self._network_backend = network_backend or httpcore.AnyIOBackend()
        self._search_contexts: dict[_SearchContextKey, _SharedSearchContext] = {}
        self._search_locks: dict[_SearchContextKey, asyncio.Lock] = {}
        self._active_search_keys: set[_SearchContextKey] = set()

    def _operation_transport(
        self,
    ) -> tuple[httpx.AsyncBaseTransport, _PinnedNetworkBackend | None]:
        if self._http_transport is not None:
            return self._http_transport, None
        backend = _PinnedNetworkBackend(self._network_backend)
        return _PinnedAsyncHTTPTransport(backend), backend

    def _profile_path(
        self,
        auth_profile: str,
        browser_channel: BrowserChannel,
        platform: Platform,
    ) -> Path:
        if _PROFILE_ID.fullmatch(auth_profile) is None:
            raise PlatformAdapterError(
                "auth_profile_invalid",
                "The authentication profile identifier is invalid.",
                platform=platform,
                operation="open_browser",
                retryable=False,
            )
        auth_root = self._auth_root
        if auth_root is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise PlatformAdapterError(
                    "auth_root_unavailable",
                    "LOCALAPPDATA is required to locate browser authentication profiles.",
                    platform=platform,
                    operation="open_browser",
                    retryable=False,
                )
            auth_root = Path(local_app_data) / "material-collector" / "auth"
        return auth_root / auth_profile / browser_channel.value / platform.value

    @asynccontextmanager
    async def _open_context(
        self,
        platform: Platform,
        context: PlatformContext,
        *,
        visible: bool = False,
    ) -> AsyncIterator[BrowserContext]:
        profile_path = self._profile_path(
            context.auth_profile,
            context.browser_channel,
            platform,
        )
        profile_path.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                channel=_PLAYWRIGHT_CHANNEL[context.browser_channel],
                headless=self._headless and not visible,
                chromium_sandbox=True,
                args=list(_DIRECT_CHROMIUM_ARGS),
            )
            try:
                yield browser
            finally:
                await browser.close()

    @asynccontextmanager
    async def search_execution(
        self,
        platforms: tuple[Platform, ...],
        context: PlatformContext,
    ) -> AsyncIterator[None]:
        keys = (
            {
                self._search_context_key(platform, context)
                for platform in platforms
            }
            if context.show_search_browser
            else set()
        )
        self._active_search_keys.update(keys)
        try:
            yield
        finally:
            for platform in platforms:
                await self.reset_search_platform(platform, context)
            self._active_search_keys.difference_update(keys)

    async def reset_search_platform(
        self,
        platform: Platform,
        context: PlatformContext,
    ) -> None:
        key = self._search_context_key(platform, context)
        lock = self._search_locks.setdefault(key, asyncio.Lock())
        async with lock:
            shared = self._search_contexts.pop(key, None)
            if shared is not None:
                await shared.manager.__aexit__(None, None, None)

    def _search_context_key(
        self,
        platform: Platform,
        context: PlatformContext,
    ) -> _SearchContextKey:
        return (
            context.auth_profile,
            context.browser_channel,
            platform,
            context.show_search_browser,
        )

    @asynccontextmanager
    async def _open_search_context(
        self,
        platform: Platform,
        context: PlatformContext,
    ) -> AsyncIterator[_SharedSearchContext]:
        key = self._search_context_key(platform, context)
        lock = self._search_locks.setdefault(key, asyncio.Lock())
        async with lock:
            shared = self._search_contexts.get(key)
            if shared is None:
                manager = (
                    self._open_context(platform, context, visible=True)
                    if context.show_search_browser
                    else self._open_context(platform, context)
                )
                browser = await manager.__aenter__()
                shared = _SharedSearchContext(manager=manager, browser=browser)
                on_event = getattr(browser, "on", None)
                if callable(on_event):
                    on_event(
                        "close",
                        lambda *_args: setattr(shared, "closed_by_user", True),
                    )
                self._search_contexts[key] = shared
        yield shared

    async def capture_json(
        self,
        page_url: str,
        response_url_fragment: str,
        *,
        platform: Platform,
        operation: str,
        context: PlatformContext,
    ) -> Mapping[str, Any]:
        timeout_ms = context.request_timeout_seconds * 1000
        timeout_phase: _CaptureTimeoutPhase = "navigation"
        page: Any | None = None
        shared_search_context: _SharedSearchContext | None = None
        search_browser_closed = False
        visible = operation == "search" and context.show_search_browser
        try:
            search_key = self._search_context_key(platform, context)
            browser_context: Any
            if operation == "search" and search_key in self._active_search_keys:
                browser_context = self._open_search_context(
                    platform,
                    context,
                )
            else:
                browser_context = (
                    self._open_context(platform, context, visible=True)
                    if visible
                    else self._open_context(platform, context)
                )
            async with browser_context as opened_context:
                if operation == "search" and search_key in self._active_search_keys:
                    shared_search_context = cast(_SharedSearchContext, opened_context)
                    if shared_search_context.closed_by_user:
                        raise PlatformAdapterError(
                            "search_browser_closed",
                            "The visible search browser was closed.",
                            platform=platform,
                            operation=operation,
                            retryable=True,
                        )
                    browser = shared_search_context.browser
                else:
                    browser = opened_context
                page = await browser.new_page()
                if visible:
                    await _identify_visible_search_page(page, platform)

                    def mark_search_browser_closed(_page: Any) -> None:
                        nonlocal search_browser_closed
                        search_browser_closed = True
                        if shared_search_context is not None:
                            shared_search_context.closed_by_user = True

                    page.on("close", mark_search_browser_closed)
                try:
                    warmup_url = _CAPTURE_WARMUP_URLS.get(platform)
                    if warmup_url is not None:
                        timeout_phase = "warmup"
                        await page.goto(
                            warmup_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                    timeout_phase = "navigation"
                    async with page.expect_response(
                        lambda response: response_url_fragment in response.url,
                        timeout=timeout_ms,
                    ) as pending:
                        await page.goto(
                            page_url,
                            wait_until="domcontentloaded",
                            timeout=timeout_ms,
                        )
                        timeout_phase = "response"
                    response = await pending.value
                except PlaywrightTimeoutError as exc:
                    raise await _capture_timeout_error(
                        page,
                        timeout_phase=timeout_phase,
                        platform=platform,
                        operation=operation,
                    ) from exc
                if response.status in {401, 403}:
                    raise PlatformAdapterError(
                        "authentication_lost",
                        "The platform rejected the current authenticated session.",
                        platform=platform,
                        operation=operation,
                        retryable=True,
                        details={"http_status": response.status},
                    )
                if response.status >= 400:
                    raise PlatformAdapterError(
                        "platform_http_error",
                        "The platform returned an unsuccessful HTTP response.",
                        platform=platform,
                        operation=operation,
                        retryable=True,
                        details={"http_status": response.status},
                    )
                payload: Any = await response.json()
        except PlatformAdapterError:
            raise
        except PlaywrightTimeoutError as exc:
            raise PlatformAdapterError(
                "platform_navigation_timeout",
                "Timed out while opening the platform browser page.",
                platform=platform,
                operation=operation,
                retryable=True,
            ) from exc
        except Exception as exc:
            if visible and page is not None and (
                search_browser_closed or page.is_closed()
            ):
                raise PlatformAdapterError(
                    "search_browser_closed",
                    "The visible search browser was closed.",
                    platform=platform,
                    operation=operation,
                    retryable=True,
                ) from exc
            if visible and shared_search_context is not None and (
                shared_search_context.closed_by_user
            ):
                raise PlatformAdapterError(
                    "search_browser_closed",
                    "The visible search browser was closed.",
                    platform=platform,
                    operation=operation,
                    retryable=True,
                ) from exc
            raise PlatformAdapterError(
                "platform_transport_failed",
                "The authenticated browser operation failed.",
                platform=platform,
                operation=operation,
                retryable=True,
                details={"exception_type": type(exc).__name__},
            ) from exc
        if not isinstance(payload, Mapping):
            raise PlatformAdapterError(
                "platform_schema_changed",
                "The platform response is not a JSON object.",
                platform=platform,
                operation=operation,
                retryable=False,
                details={"field": "$"},
            )
        return payload
    async def resolve_url(
        self,
        url: str,
        *,
        platform: Platform,
        context: PlatformContext,
    ) -> str:
        http_transport, pinned_backend = self._operation_transport()
        try:
            _validate_short_url(platform, url, initial=True)
            timeout = httpx.Timeout(context.request_timeout_seconds)
            async with httpx.AsyncClient(
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout,
                transport=http_transport,
                trust_env=False,
            ) as client:
                current_url = url
                for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
                    _validate_short_url(
                        platform,
                        current_url,
                        initial=redirect_count == 0,
                    )
                    host, addresses = await _validated_public_origin(
                        platform,
                        current_url,
                        self._resolver,
                        error_code="short_url_invalid",
                    )
                    if pinned_backend is not None:
                        pinned_backend.pin(host, addresses)
                    async with client.stream("GET", current_url) as response:
                        if response.status_code not in _REDIRECT_STATUSES:
                            response.raise_for_status()
                            return current_url
                        location = response.headers.get("location")
                        if location is None or redirect_count >= _MAX_MEDIA_REDIRECTS:
                            raise PlatformAdapterError(
                                "short_url_invalid",
                                "The shared URL returned an invalid redirect chain.",
                                platform=platform,
                                operation="resolve_url",
                                retryable=False,
                            )
                        current_url = urljoin(current_url, location)
            raise AssertionError("redirect loop must return or raise")
        except PlatformAdapterError:
            raise
        except Exception as exc:
            raise PlatformAdapterError(
                "platform_transport_failed",
                "The shared platform URL could not be resolved.",
                platform=platform,
                operation="resolve_url",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            ) from exc

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        context: PlatformContext,
        referer: str,
    ) -> None:
        platform = _platform_for_url(referer)
        http_transport, pinned_backend = self._operation_transport()
        try:
            async with self._open_context(platform, context) as browser:
                cookies = {
                    str(cookie["name"]): str(cookie["value"])
                    for cookie in await browser.cookies()
                }
                headers = {"Referer": referer, "User-Agent": await _user_agent(browser)}
                timeout = httpx.Timeout(context.request_timeout_seconds)
                async with httpx.AsyncClient(
                    cookies=cookies,
                    follow_redirects=False,
                    headers=headers,
                    timeout=timeout,
                    transport=http_transport,
                    trust_env=False,
                ) as client:
                    current_url = url
                    for redirect_count in range(_MAX_MEDIA_REDIRECTS + 1):
                        host, addresses = await _validate_resolved_media_url(
                            platform,
                            current_url,
                            self._resolver,
                        )
                        if pinned_backend is not None:
                            pinned_backend.pin(host, addresses)
                        async with client.stream("GET", current_url) as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                location = response.headers.get("location")
                                if location is None:
                                    raise PlatformAdapterError(
                                        "media_redirect_invalid",
                                        "The platform media redirect has no target.",
                                        platform=platform,
                                        operation="fetch",
                                        retryable=True,
                                    )
                                if redirect_count >= _MAX_MEDIA_REDIRECTS:
                                    raise PlatformAdapterError(
                                        "media_redirect_limit",
                                        "The platform media URL redirected too many times.",
                                        platform=platform,
                                        operation="fetch",
                                        retryable=True,
                                    )
                                current_url = urljoin(current_url, location)
                                continue
                            response.raise_for_status()
                            with destination.open("xb") as media_file:
                                async for chunk in response.aiter_bytes():
                                    media_file.write(chunk)
                            break
        except Exception as exc:
            if isinstance(exc, PlatformAdapterError):
                raise
            raise PlatformAdapterError(
                "download_failed",
                "The platform media download failed.",
                platform=platform,
                operation="fetch",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            ) from exc


async def _identify_visible_search_page(page: Any, platform: Platform) -> None:
    label = f"Material Collector · {platform.value} · "
    await page.add_init_script(
        f"""
        (() => {{
          const label = {label!r};
          window.name = `material-collector-search-{platform.value}`;
          window.addEventListener('DOMContentLoaded', () => {{
            const update = () => {{
              if (!document.title.startsWith(label)) document.title = label + document.title;
            }};
            update();
            const title = document.querySelector('title');
            if (title) new MutationObserver(update).observe(title, {{childList: true}});
          }});
        }})();
        """
    )


async def _capture_timeout_error(
    page: Any,
    *,
    timeout_phase: _CaptureTimeoutPhase,
    platform: Platform,
    operation: str,
) -> PlatformAdapterError:
    if timeout_phase == "response":
        access_state = await inspect_rendered_access(page, platform)
        if access_state == RenderedAccessState.CHALLENGE:
            return PlatformAdapterError(
                "challenge_required",
                "The rendered platform page requires an interactive challenge.",
                platform=platform,
                operation=operation,
                retryable=True,
                details={"reason": "rendered_platform_challenge"},
            )
        if access_state == RenderedAccessState.LOGGED_OUT:
            return PlatformAdapterError(
                "authentication_lost",
                "The rendered platform page requires authentication.",
                platform=platform,
                operation=operation,
                retryable=True,
                details={"reason": "rendered_platform_logged_out"},
            )
        return PlatformAdapterError(
            "platform_response_timeout",
            "Timed out while waiting for the platform response.",
            platform=platform,
            operation=operation,
            retryable=True,
        )
    if timeout_phase == "warmup":
        return PlatformAdapterError(
            "platform_navigation_timeout",
            "Timed out while warming the authenticated platform page.",
            platform=platform,
            operation=operation,
            retryable=True,
            details={"navigation_phase": "warmup"},
        )
    return PlatformAdapterError(
        "platform_navigation_timeout",
        "Timed out while navigating to the platform page.",
        platform=platform,
        operation=operation,
        retryable=True,
    )


async def _validate_resolved_media_url(
    platform: Platform,
    url: str,
    resolver: HostResolver,
) -> tuple[str, tuple[str, ...]]:
    """Validate an allowed CDN URL and every IP currently returned by DNS."""

    validate_temporary_media_url(platform, url)
    return await _validated_public_origin(
        platform,
        url,
        resolver,
        error_code="media_url_invalid",
    )


async def _validated_public_origin(
    platform: Platform,
    url: str,
    resolver: HostResolver,
    *,
    error_code: str,
) -> tuple[str, tuple[str, ...]]:
    parsed = httpx.URL(url)
    host = parsed.host
    if not host:
        raise PlatformAdapterError(
            error_code,
            "The platform URL has no hostname.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    if literal is not None:
        addresses = (literal,)
    else:
        try:
            records = await resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PlatformAdapterError(
                "media_url_unresolved",
                "The platform media hostname could not be resolved.",
                platform=platform,
                operation="fetch",
                retryable=True,
            ) from exc
        addresses = tuple(
            ipaddress.ip_address(record[4][0])
            for record in records
            if len(record) >= 5 and record[4]
        )
    if not addresses or any(not address.is_global for address in addresses):
        raise PlatformAdapterError(
            error_code,
            "The platform hostname resolved to a non-public address.",
            platform=platform,
            operation="fetch",
            retryable=False,
        )
    return host.casefold().rstrip("."), tuple(str(address) for address in addresses)


def _validate_short_url(platform: Platform, url: str, *, initial: bool) -> None:
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise PlatformAdapterError(
            "short_url_invalid",
            "The shared platform URL is invalid.",
            platform=platform,
            operation="resolve_url",
            retryable=False,
        ) from exc
    host = parsed.host.casefold().rstrip(".") if parsed.host else ""
    allowed = (
        host in _SHORT_INITIAL_HOSTS[platform]
        if initial
        else any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in _SHORT_CHAIN_SUFFIXES[platform]
        )
    )
    if (
        parsed.scheme != "https"
        or parsed.userinfo
        or parsed.port not in {None, 443}
        or not allowed
    ):
        raise PlatformAdapterError(
            "short_url_invalid",
            "The shared URL left the approved HTTPS platform origins.",
            platform=platform,
            operation="resolve_url",
            retryable=False,
        )


async def _user_agent(browser: BrowserContext) -> str:
    page = await browser.new_page()
    try:
        value: Any = await page.evaluate("navigator.userAgent")
        return value if isinstance(value, str) else "Mozilla/5.0"
    finally:
        await page.close()


def _platform_for_url(url: str) -> Platform:
    hostname = httpx.URL(url).host.casefold()
    if hostname.endswith(("bilibili.com", "bilivideo.com")):
        return Platform.BILIBILI
    if hostname.endswith("douyin.com"):
        return Platform.DOUYIN
    if hostname.endswith(("xiaohongshu.com", "xhscdn.com")):
        return Platform.XIAOHONGSHU
    raise PlatformAdapterError(
        "download_origin_invalid",
        "The media referer is not a supported platform origin.",
        platform=Platform.BILIBILI,
        operation="fetch",
        retryable=False,
    )
