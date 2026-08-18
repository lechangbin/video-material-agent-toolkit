"""Local persistent-browser authentication.

This adapter owns browser profile paths and their process-level exclusion.  It
never returns cookies, account identifiers, response bodies or browser paths
through the application boundary.
"""

from __future__ import annotations

import asyncio
import ctypes
import importlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from playwright.async_api import (
    APIRequestContext,
    BrowserContext,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from material_collector.application.ports import ProgressReporter
from material_collector.core.errors import CollectorError
from material_collector.core.media import (
    PLATFORM_ORDER,
    AuthenticationSelection,
    AuthProbe,
    AuthStatus,
    BrowserChannel,
    Platform,
)
from material_collector.infrastructure.platforms.rendered_access import (
    RenderedAccessState,
    inspect_rendered_access,
)

if os.name == "nt":
    import msvcrt

_POSIX_LOCK: Any | None = importlib.import_module("fcntl") if os.name != "nt" else None


_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_LOCK_WAIT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.05
_LOGIN_PROGRESS_INTERVAL_SECONDS = 10.0
_PLAYWRIGHT_CHANNEL = {
    BrowserChannel.EDGE: "msedge",
    BrowserChannel.CHROME: "chrome",
}


class AuthenticationError(CollectorError):
    """Base class for structured local-authentication failures."""


class AuthProfileBusyError(AuthenticationError):
    def __init__(self, platform: Platform, auth_profile: str) -> None:
        super().__init__(
            "auth_profile_busy",
            "The local authentication profile is already in use.",
            details={"platform": platform.value, "auth_profile": auth_profile},
        )


class AuthenticationProbeFailedError(AuthenticationError):
    def __init__(self, probe: AuthProbe) -> None:
        super().__init__(
            "auth_probe_failed",
            "The platform could not reliably determine the authentication state.",
            details={
                "platform": probe.platform.value,
                "auth_profile": probe.auth_profile,
                "reason_code": probe.reason_code,
            },
        )


class AuthenticationDesktopUnavailableError(AuthenticationError):
    def __init__(self, platform: Platform, auth_profile: str) -> None:
        super().__init__(
            "auth_desktop_unavailable",
            "A headed browser could not be opened for interactive login.",
            details={"platform": platform.value, "auth_profile": auth_profile},
        )


class AuthenticationLoginTimeoutError(AuthenticationError):
    def __init__(
        self,
        platform: Platform,
        auth_profile: str,
        wait_seconds: int,
        *,
        reason_code: str,
    ) -> None:
        super().__init__(
            "auth_login_timeout",
            "Interactive platform login did not complete before the waiting limit.",
            details={
                "platform": platform.value,
                "auth_profile": auth_profile,
                "wait_seconds": wait_seconds,
                "reason_code": reason_code,
            },
        )


class AuthenticationContractError(AuthenticationError):
    def __init__(self, message: str, *, details: Mapping[str, Any]) -> None:
        super().__init__("contract_invalid", message, details=details)


class BrowserChannelSelectionError(AuthenticationError):
    def __init__(self, attempts: list[dict[str, str]], *, automatic: bool) -> None:
        super().__init__(
            "browser_channel_exhausted" if automatic else "browser_channel_failed",
            "No requested native browser channel could complete authentication.",
            details={
                "attempts": attempts,
                "required_action": "install_or_repair_a_supported_browser",
            },
        )


class BrowserLifecycleError(RuntimeError):
    """A credential-free browser failure that may permit channel fallback."""

    def __init__(self, stage: str, reason: str, required_action: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = _safe_reason_code(reason)
        self.required_action = _safe_reason_code(required_action)


class BrowserChannelUnavailableError(BrowserLifecycleError):
    def __init__(self) -> None:
        super().__init__(
            "unavailable",
            "browser_channel_unavailable",
            "install_selected_browser",
        )


class BrowserLaunchError(BrowserLifecycleError):
    def __init__(self) -> None:
        super().__init__("launch", "browser_launch_failed", "repair_selected_browser")


class BrowserNavigationError(BrowserLifecycleError):
    def __init__(self) -> None:
        super().__init__(
            "navigation",
            "browser_navigation_failed",
            "check_network_and_retry",
        )


class BrowserDesktopUnavailableError(BrowserLifecycleError):
    """A headed browser cannot be attached to the interactive desktop."""

    def __init__(self) -> None:
        super().__init__(
            "desktop",
            "interactive_desktop_unavailable",
            "run_from_an_interactive_desktop",
        )


class BrowserWindowVerificationError(BrowserLifecycleError):
    def __init__(self) -> None:
        super().__init__(
            "window_verification",
            "browser_window_not_verified",
            "keep_the_login_window_visible",
        )


class BrowserLoginTimeoutError(RuntimeError):
    """A driver exhausted its interactive login waiting period."""

    def __init__(self, reason_code: str = "login_wait_expired") -> None:
        super().__init__(reason_code)
        self.reason_code = _safe_reason_code(reason_code)


@dataclass(frozen=True, slots=True)
class DriverProbe:
    """Credential-free result returned by an injected browser driver."""

    status: AuthStatus
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class ChromeProcessSnapshot:
    process_id: int
    session_id: int
    command_line: str
    started_at: datetime


class WindowsChromeDesktopVerifier:
    """Bind a visible Chrome window to the launched persistent profile."""

    def __init__(
        self,
        *,
        active_session_id: Callable[[], int | None] | None = None,
        current_session_id: Callable[[], int | None] | None = None,
        visible_process_ids: Callable[[int], set[int]] | None = None,
        chrome_processes: Callable[[], tuple[ChromeProcessSnapshot, ...]] | None = None,
    ) -> None:
        self._active_session_id = active_session_id or _windows_active_session_id
        self._current_session_id = current_session_id or _windows_current_session_id
        self._visible_process_ids = (
            visible_process_ids or _visible_chrome_window_process_ids
        )
        self._chrome_processes = chrome_processes or _read_chrome_processes

    def __call__(self, user_data_dir: Path, launch_started_at: datetime) -> bool:
        active_session_id = self._active_session_id()
        if active_session_id is None:
            return False
        if self._current_session_id() != active_session_id:
            return False
        visible_process_ids = self._visible_process_ids(active_session_id)
        target_profile = _normalized_windows_path(user_data_dir)
        for process in self._chrome_processes():
            if (
                process.process_id not in visible_process_ids
                or process.session_id != active_session_id
                or process.started_at < launch_started_at
                or _command_line_is_headless(process.command_line)
            ):
                continue
            profile = _command_line_profile(process.command_line)
            if profile is not None and _normalized_windows_path(profile) == target_profile:
                return True
        return False


@dataclass(slots=True)
class _LoginProgressBridge:
    progress: ProgressReporter | None
    details: dict[str, object]
    wait_seconds: int
    now: Callable[[], datetime]
    deadline: str | None = None

    def __call__(self, event: str, details: dict[str, object]) -> None:
        deadline_details: dict[str, object] = {}
        if event in {
            "authentication_login_window_opened",
            "authentication_login_waiting",
        }:
            if self.deadline is None:
                self.deadline = _iso_utc(
                    self.now() + timedelta(seconds=self.wait_seconds)
                )
            deadline_details["deadline"] = self.deadline
        _report(
            self.progress,
            event,
            {**self.details, **deadline_details, **details},
        )


class BrowserAuthenticationDriver(Protocol):
    """Minimal browser seam used by the gateway and offline test doubles."""

    async def probe(self, platform: Platform, user_data_dir: Path) -> DriverProbe: ...

    async def login(
        self,
        platform: Platform,
        user_data_dir: Path,
        wait_seconds: int,
        progress: Callable[[str, dict[str, object]], None] | None = None,
    ) -> DriverProbe: ...


class PlaywrightAuthenticationGateway:
    """AuthenticationGateway backed by persistent Playwright Chromium profiles."""

    def __init__(
        self,
        *,
        root_dir: Path | None = None,
        driver: BrowserAuthenticationDriver | None = None,
        driver_factory: Callable[[BrowserChannel], BrowserAuthenticationDriver] | None = None,
        now: Callable[[], datetime] | None = None,
        lock_wait_seconds: float = _LOCK_WAIT_SECONDS,
        native_windows: bool | None = None,
    ) -> None:
        self._root_dir = _resolve_auth_root(root_dir)
        if driver is not None and driver_factory is not None:
            raise ValueError("driver and driver_factory are mutually exclusive")
        if driver_factory is not None:
            self._driver_factory = driver_factory
        elif driver is not None:
            self._driver_factory = lambda _channel: driver
        else:
            self._driver_factory = lambda channel: PlaywrightBrowserAuthenticationDriver(
                channel=channel
            )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock_wait_seconds = lock_wait_seconds
        self._native_windows = os.name == "nt" if native_windows is None else native_windows

    async def probe(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
    ) -> AuthProbe:
        """Run one conservative, read-only probe under the profile lock."""

        profile = _validate_profile_id(auth_profile)
        channel = _explicit_channel(browser_channel)
        lock = await self._acquire(platform, profile, channel)
        try:
            return await self._probe_locked(platform, profile, channel)
        finally:
            await asyncio.to_thread(lock.release)

    async def ensure_authenticated(
        self,
        platforms: tuple[Platform, ...],
        auth_profile: str,
        wait_seconds: int,
        *,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
        progress: ProgressReporter | None = None,
    ) -> AuthenticationSelection:
        """Select one browser channel and authenticate every requested platform."""

        candidates = _browser_candidates(browser_channel, native_windows=self._native_windows)
        attempts: list[dict[str, str]] = []
        for channel in candidates:
            try:
                probes = await self._ensure_authenticated_on_channel(
                    platforms,
                    auth_profile,
                    wait_seconds,
                    browser_channel=channel,
                    progress=progress,
                )
                return AuthenticationSelection(browser_channel=channel, probes=probes)
            except BrowserLifecycleError as error:
                attempts.append(
                    {
                        "browser_channel": channel.value,
                        "stage": error.stage,
                        "reason": error.reason,
                        "required_action": error.required_action,
                    }
                )
                if browser_channel is not BrowserChannel.AUTO:
                    raise BrowserChannelSelectionError(attempts, automatic=False) from error
        raise BrowserChannelSelectionError(attempts, automatic=True)

    async def _ensure_authenticated_on_channel(
        self,
        platforms: tuple[Platform, ...],
        auth_profile: str,
        wait_seconds: int,
        *,
        browser_channel: BrowserChannel,
        progress: ProgressReporter | None = None,
    ) -> tuple[AuthProbe, ...]:
        """Verify/login requested platforms serially in the frozen platform order."""

        profile = _validate_profile_id(auth_profile)
        if wait_seconds < 1:
            raise AuthenticationContractError(
                "wait_seconds must be at least 1.",
                details={"wait_seconds": wait_seconds},
            )

        requested = set(platforms)
        ordered = tuple(platform for platform in PLATFORM_ORDER if platform in requested)
        if len(ordered) != len(requested):
            raise AuthenticationContractError(
                "platforms contains an unsupported platform.",
                details={},
            )

        results: list[AuthProbe] = []
        for platform in ordered:
            common: dict[str, object] = {
                "platform": platform.value,
                "auth_profile": profile,
                "browser_channel": browser_channel.value,
            }
            lock = await self._acquire(platform, profile, browser_channel)
            try:
                _report(progress, "authentication_probe_started", common)
                current = await self._probe_locked(platform, profile, browser_channel)
                _report(
                    progress,
                    "authentication_probe_completed",
                    {
                        **common,
                        "status": current.status.value,
                        "reason_code": current.reason_code,
                    },
                )
                if current.status is AuthStatus.PROBE_FAILED:
                    raise AuthenticationProbeFailedError(current)
                if current.status is not AuthStatus.VALID:
                    human_action_base = {
                        **common,
                        "actor": "human",
                        "wait_seconds": wait_seconds,
                        "hint": (
                            f"Check {browser_channel.value} in the taskbar and do not close "
                            "the login window."
                        ),
                    }
                    current = await self._login_locked(
                        platform,
                        profile,
                        browser_channel,
                        wait_seconds,
                        progress=_LoginProgressBridge(
                            progress=progress,
                            details=human_action_base,
                            wait_seconds=wait_seconds,
                            now=self._now,
                        ),
                    )
                results.append(current)
                _report(
                    progress,
                    "authentication_platform_completed",
                    {
                        **common,
                        "status": current.status.value,
                        "reason_code": current.reason_code,
                    },
                )
            finally:
                await asyncio.to_thread(lock.release)
        return tuple(results)

    async def logout(
        self,
        platform: Platform,
        auth_profile: str,
        confirmation: str,
        browser_channel: BrowserChannel = BrowserChannel.CHROME,
    ) -> None:
        """Remove exactly one platform profile after explicit confirmation."""

        profile = _validate_profile_id(auth_profile)
        if confirmation != platform.value:
            raise AuthenticationContractError(
                "Logout confirmation must exactly match the platform value.",
                details={"platform": platform.value},
            )

        channel = _explicit_channel(browser_channel)
        lock = await self._acquire(platform, profile, channel)
        try:
            profile_dir = self._profile_dir(profile, channel, platform)
            await asyncio.to_thread(_remove_profile_directory, profile_dir)
        finally:
            await asyncio.to_thread(lock.release)

    async def _acquire(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel,
    ) -> _ProcessFileLock:
        lock_path = (
            self._root_dir
            / ".locks"
            / f"{auth_profile}.{browser_channel.value}.{platform.value}.lock"
        )
        lock = _ProcessFileLock(lock_path)
        deadline = time.monotonic() + self._lock_wait_seconds
        while True:
            if lock.try_acquire():
                return lock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthProfileBusyError(platform, auth_profile)
            await asyncio.sleep(min(_LOCK_POLL_SECONDS, remaining))

    async def _probe_locked(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel,
    ) -> AuthProbe:
        profile_dir = self._profile_dir(auth_profile, browser_channel, platform)
        if not profile_dir.is_dir():
            result = DriverProbe(AuthStatus.INVALID, "profile_missing")
        else:
            try:
                result = await self._driver_factory(browser_channel).probe(platform, profile_dir)
            except BrowserLifecycleError:
                raise
            except (OSError, RuntimeError, ValueError) as error:
                raise BrowserLaunchError from error
        return self._public_probe(platform, auth_profile, browser_channel, result)

    async def _login_locked(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel,
        wait_seconds: int,
        *,
        progress: Callable[[str, dict[str, object]], None],
    ) -> AuthProbe:
        profile_dir = self._profile_dir(auth_profile, browser_channel, platform)
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._driver_factory(browser_channel).login(
                platform,
                profile_dir,
                wait_seconds,
                progress,
            )
        except BrowserLifecycleError:
            raise
        except BrowserLoginTimeoutError as error:
            raise AuthenticationLoginTimeoutError(
                platform,
                auth_profile,
                wait_seconds,
                reason_code=error.reason_code,
            ) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise BrowserLaunchError from error

        probe = self._public_probe(platform, auth_profile, browser_channel, result)
        if probe.status is AuthStatus.PROBE_FAILED:
            raise AuthenticationProbeFailedError(probe)
        if probe.status is not AuthStatus.VALID:
            raise AuthenticationLoginTimeoutError(
                platform,
                auth_profile,
                wait_seconds,
                reason_code=probe.reason_code or "login_not_verified",
            )
        return probe

    def _profile_dir(
        self,
        auth_profile: str,
        browser_channel: BrowserChannel,
        platform: Platform,
    ) -> Path:
        return self._root_dir / auth_profile / browser_channel.value / platform.value

    def _public_probe(
        self,
        platform: Platform,
        auth_profile: str,
        browser_channel: BrowserChannel,
        result: DriverProbe,
    ) -> AuthProbe:
        return AuthProbe(
            platform=platform,
            auth_profile=auth_profile,
            browser_channel=browser_channel,
            status=result.status,
            checked_at=_iso_utc(self._now()),
            reason_code=(
                _safe_reason_code(result.reason_code) if result.reason_code is not None else None
            ),
        )


class PlaywrightBrowserAuthenticationDriver:
    """Conservative platform probes and interactive login using Playwright."""

    _HOME_URLS: Mapping[Platform, str] = {
        Platform.BILIBILI: "https://www.bilibili.com/",
        Platform.DOUYIN: "https://www.douyin.com/",
        Platform.XIAOHONGSHU: "https://www.xiaohongshu.com/",
    }

    def __init__(
        self,
        *,
        channel: BrowserChannel = BrowserChannel.CHROME,
        desktop_verifier: Callable[[Path, datetime], bool] | None = None,
        desktop_available: Callable[[], bool] | None = None,
    ) -> None:
        self._channel = _explicit_channel(channel)
        self._desktop_verifier = (
            desktop_verifier or _headed_chrome_visible_on_active_desktop
        )
        self._desktop_available = desktop_available or (
            _interactive_desktop_available
            if desktop_verifier is None
            else lambda: True
        )

    async def probe(self, platform: Platform, user_data_dir: Path) -> DriverProbe:
        try:
            async with async_playwright() as playwright:
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        str(user_data_dir),
                        channel=_PLAYWRIGHT_CHANNEL[self._channel],
                        headless=True,
                        chromium_sandbox=True,
                        service_workers="block",
                        args=["--no-proxy-server"],
                    )
                except PlaywrightError as error:
                    raise _playwright_launch_error(error) from error
                try:
                    if platform in {Platform.DOUYIN, Platform.XIAOHONGSHU}:
                        page = await _fresh_context_page(context)
                        try:
                            await page.goto(
                                self._HOME_URLS[platform],
                                wait_until="domcontentloaded",
                                timeout=15_000,
                            )
                        except PlaywrightError as error:
                            raise BrowserNavigationError from error
                    return await self._probe_context(platform, context)
                finally:
                    await context.close()
        except BrowserLifecycleError:
            raise
        except PlaywrightError as error:
            raise BrowserLaunchError from error

    async def login(
        self,
        platform: Platform,
        user_data_dir: Path,
        wait_seconds: int,
        progress: Callable[[str, dict[str, object]], None] | None = None,
    ) -> DriverProbe:
        if not await asyncio.to_thread(self._desktop_available):
            raise BrowserDesktopUnavailableError
        try:
            async with async_playwright() as playwright:
                launch_started_at = datetime.now(UTC)
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        str(user_data_dir),
                        channel=_PLAYWRIGHT_CHANNEL[self._channel],
                        headless=False,
                        chromium_sandbox=True,
                        service_workers="block",
                        args=["--no-proxy-server"],
                    )
                except PlaywrightError as error:
                    raise _playwright_launch_error(error) from error
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    if progress is not None:
                        progress(
                            "authentication_login_window_opening",
                            {"phase": "navigation"},
                        )
                    navigation = asyncio.create_task(
                        page.goto(
                            self._HOME_URLS[platform],
                            wait_until="domcontentloaded",
                            timeout=min(wait_seconds * 1000, 30_000),
                        )
                    )
                    try:
                        while not navigation.done():
                            await asyncio.wait(
                                {navigation},
                                timeout=_LOGIN_PROGRESS_INTERVAL_SECONDS,
                            )
                            if not navigation.done() and progress is not None:
                                progress(
                                    "authentication_login_window_opening",
                                    {"phase": "navigation"},
                                )
                        try:
                            await navigation
                        except PlaywrightError as error:
                            raise BrowserNavigationError from error
                    except BaseException:
                        if not navigation.done():
                            navigation.cancel()
                            with suppress(asyncio.CancelledError):
                                await navigation
                        raise
                    await page.bring_to_front()
                    desktop_available = await asyncio.to_thread(
                        self._desktop_verifier,
                        user_data_dir,
                        launch_started_at,
                    )
                    if not desktop_available:
                        raise BrowserWindowVerificationError
                    if progress is not None:
                        progress("authentication_login_window_opened", {})
                        progress("authentication_login_waiting", {})
                    deadline = time.monotonic() + wait_seconds
                    next_waiting_progress = (
                        time.monotonic() + _LOGIN_PROGRESS_INTERVAL_SECONDS
                    )
                    last = DriverProbe(AuthStatus.INVALID, "login_not_verified")
                    while time.monotonic() < deadline:
                        _ensure_login_window_open(context, progress)
                        now = time.monotonic()
                        if progress is not None and now >= next_waiting_progress:
                            progress("authentication_login_waiting", {})
                            next_waiting_progress = (
                                now + _LOGIN_PROGRESS_INTERVAL_SECONDS
                            )
                        last = await self._probe_context(platform, context)
                        if last.status is AuthStatus.VALID:
                            return last
                        _ensure_login_window_open(context, progress)
                        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                    raise BrowserLoginTimeoutError(last.reason_code or "login_wait_expired")
                finally:
                    await context.close()
        except BrowserLoginTimeoutError:
            raise
        except BrowserLifecycleError:
            raise
        except PlaywrightError as error:
            raise BrowserLaunchError from error

    async def _probe_context(
        self,
        platform: Platform,
        context: BrowserContext,
    ) -> DriverProbe:
        if platform is Platform.BILIBILI:
            return await _probe_bilibili(context.request)
        if platform is Platform.DOUYIN:
            return await _probe_douyin(context)
        return await _probe_xiaohongshu(context)


class _ProcessFileLock:
    """One-byte advisory lock whose ownership follows the open process handle."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any | None = None

    def acquire(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            if self.try_acquire():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_LOCK_POLL_SECONDS, remaining))

    def try_acquire(self) -> bool:
        """Attempt the process lock once without sleeping."""

        if self._handle is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - the production target is Windows
                assert _POSIX_LOCK is not None
                _POSIX_LOCK.flock(
                    handle.fileno(),
                    _POSIX_LOCK.LOCK_EX | _POSIX_LOCK.LOCK_NB,
                )
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - the production target is Windows
                assert _POSIX_LOCK is not None
                _POSIX_LOCK.flock(handle.fileno(), _POSIX_LOCK.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> Self:
        if not self.acquire(_LOCK_WAIT_SECONDS):
            raise TimeoutError("authentication lock is busy")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


async def _probe_bilibili(request: APIRequestContext) -> DriverProbe:
    try:
        response = await request.get(
            "https://api.bilibili.com/x/web-interface/nav",
            timeout=15_000,
        )
        if response.status == 401:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        if response.status in {403, 412, 429}:
            return DriverProbe(AuthStatus.CHALLENGE_REQUIRED, "platform_challenge")
        if not response.ok:
            return DriverProbe(AuthStatus.PROBE_FAILED, "unexpected_http_status")
        payload = await response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        logged_in = data.get("isLogin") if isinstance(data, dict) else None
        if logged_in is True:
            return DriverProbe(AuthStatus.VALID, "platform_reports_logged_in")
        if logged_in is False:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
    except PlaywrightError, ValueError, TypeError:
        pass
    return DriverProbe(AuthStatus.PROBE_FAILED, "unrecognized_probe_response")


async def _probe_douyin(context: BrowserContext) -> DriverProbe:
    for page in context.pages:
        state = await inspect_rendered_access(page, Platform.DOUYIN)
        if state == RenderedAccessState.CHALLENGE:
            return DriverProbe(AuthStatus.CHALLENGE_REQUIRED, "platform_challenge")
        if state == RenderedAccessState.LOGGED_OUT:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        if state == RenderedAccessState.LOGGED_IN:
            return DriverProbe(AuthStatus.VALID, "platform_reports_logged_in")
    return await _probe_json_identity(
        context.request,
        "https://www.douyin.com/aweme/v1/web/user/profile/self/",
        identity_keys=("uid", "sec_uid", "short_id"),
        invalid_status_codes=(8,),
    )


async def _probe_xiaohongshu(context: BrowserContext) -> DriverProbe:
    for page in context.pages:
        state = await inspect_rendered_access(page, Platform.XIAOHONGSHU)
        if state == RenderedAccessState.CHALLENGE:
            return DriverProbe(AuthStatus.CHALLENGE_REQUIRED, "platform_challenge")
        if state == RenderedAccessState.LOGGED_OUT:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        if state == RenderedAccessState.LOGGED_IN:
            return DriverProbe(AuthStatus.VALID, "platform_reports_logged_in")
    return await _probe_json_identity(
        context.request,
        "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo",
        identity_keys=("user_id", "userid", "red_id"),
        interactive_status_codes=(406,),
    )


async def _probe_json_identity(
    request: APIRequestContext,
    url: str,
    *,
    identity_keys: tuple[str, ...],
    invalid_status_codes: tuple[int, ...] = (),
    interactive_status_codes: tuple[int, ...] = (),
) -> DriverProbe:
    try:
        response = await request.get(url, timeout=15_000)
        if response.status == 401:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        if response.status in {403, 412, 429}:
            return DriverProbe(AuthStatus.CHALLENGE_REQUIRED, "platform_challenge")
        if response.status in interactive_status_codes:
            return DriverProbe(AuthStatus.INVALID, "interactive_login_required")
        if not response.ok:
            return DriverProbe(AuthStatus.PROBE_FAILED, "unexpected_http_status")
        payload = await response.json()
        if not isinstance(payload, dict):
            return DriverProbe(AuthStatus.PROBE_FAILED, "unrecognized_probe_response")
        if payload.get("status_code") in invalid_status_codes:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        candidates = [
            payload,
            payload.get("data"),
            payload.get("user"),
            payload.get("user_info"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict) and any(candidate.get(key) for key in identity_keys):
                return DriverProbe(AuthStatus.VALID, "platform_reports_logged_in")
    except PlaywrightError, ValueError, TypeError:
        pass
    return DriverProbe(AuthStatus.PROBE_FAILED, "unrecognized_probe_response")


def _resolve_auth_root(root_dir: Path | None) -> Path:
    if root_dir is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise AuthenticationContractError(
                "LOCALAPPDATA is unavailable; an authentication root cannot be selected.",
                details={},
            )
        root_dir = Path(local_app_data) / "material-collector" / "auth"
    try:
        return root_dir.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise AuthenticationContractError(
            "The authentication root path cannot be resolved.",
            details={},
        ) from error


def _validate_profile_id(value: str) -> str:
    if not _PROFILE_ID.fullmatch(value):
        raise AuthenticationContractError(
            "auth_profile contains unsafe characters.",
            details={"auth_profile": value},
        )
    return value


def _explicit_channel(value: BrowserChannel) -> BrowserChannel:
    if value is BrowserChannel.AUTO:
        raise AuthenticationContractError(
            "This operation requires an explicit browser channel.",
            details={"browser_channel": value.value},
        )
    return value


def _browser_candidates(
    value: BrowserChannel,
    *,
    native_windows: bool,
) -> tuple[BrowserChannel, ...]:
    if value is not BrowserChannel.AUTO:
        return (_explicit_channel(value),)
    if native_windows:
        return (BrowserChannel.EDGE, BrowserChannel.CHROME)
    return (BrowserChannel.CHROME,)


def _playwright_launch_error(error: PlaywrightError) -> BrowserLifecycleError:
    message = str(error).lower()
    unavailable_markers = (
        "executable doesn't exist",
        "executable does not exist",
        "distribution 'chrome' is not found",
        "distribution 'msedge' is not found",
        "browser was not found",
    )
    if any(marker in message for marker in unavailable_markers):
        return BrowserChannelUnavailableError()
    return BrowserLaunchError()


def _safe_reason_code(value: str) -> str:
    return value if _REASON_CODE.fullmatch(value) else "unspecified_auth_result"


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _report(
    progress: ProgressReporter | None,
    event: str,
    details: dict[str, object],
) -> None:
    if progress is not None:
        progress.report(event, details)


def _ensure_login_window_open(
    context: BrowserContext,
    progress: Callable[[str, dict[str, object]], None] | None,
) -> None:
    if context.pages:
        return
    if progress is not None:
        progress(
            "authentication_login_window_closed",
            {"reason_code": "login_window_closed"},
        )
    raise BrowserLoginTimeoutError("login_window_closed")


async def _fresh_context_page(context: BrowserContext) -> Any:
    """Create one page and discard pages restored from a previous browser run."""

    fresh_page = await context.new_page()
    for page in tuple(context.pages):
        if page is not fresh_page:
            await page.close()
    return fresh_page


def _headed_chrome_visible_on_active_desktop(
    user_data_dir: Path,
    launch_started_at: datetime,
) -> bool:
    """Verify this profile's visible headed Chrome in the active desktop session."""

    if os.name != "nt":
        return True
    return WindowsChromeDesktopVerifier()(user_data_dir, launch_started_at)


def _interactive_desktop_available() -> bool:
    if os.name != "nt":
        return True
    active_session_id = _windows_active_session_id()
    return active_session_id is not None and _windows_current_session_id() == active_session_id


def _windows_active_session_id() -> int | None:
    session_id = int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
    return None if session_id == 0xFFFFFFFF else session_id


def _windows_current_session_id() -> int | None:
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    current_session_id = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(
        os.getpid(),
        ctypes.byref(current_session_id),
    ):
        return None
    return int(current_session_id.value)


def _visible_chrome_window_process_ids(active_session_id: int) -> set[int]:
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    found: set[int] = set()
    enum_callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    def inspect_window_impl(window: int, _parameter: int) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(window):
            return True
        class_name = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(window, class_name, len(class_name)):
            return True
        if class_name.value != "Chrome_WidgetWin_1":
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        process_session_id = wintypes.DWORD()
        if kernel32.ProcessIdToSessionId(
            process_id.value,
            ctypes.byref(process_session_id),
        ) and process_session_id.value == active_session_id:
            found.add(int(process_id.value))
        return True

    inspect_window = enum_callback_type(inspect_window_impl)
    user32.EnumWindows(inspect_window, 0)
    return found


def _read_chrome_processes() -> tuple[ChromeProcessSnapshot, ...]:
    command = (
        "$items = @(Get-CimInstance Win32_Process "
        "-Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
        "ForEach-Object { "
        "$started = $null; "
        "try { $started = (Get-Process -Id $_.ProcessId -ErrorAction Stop)."
        "StartTime.ToUniversalTime().ToString('o') } catch {}; "
        "[pscustomobject]@{ ProcessId=$_.ProcessId; SessionId=$_.SessionId; "
        "CommandLine=$_.CommandLine; StartedAt=$started } "
        "}); ConvertTo-Json -InputObject $items -Compress"
    )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return ()
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    snapshots: list[ChromeProcessSnapshot] = []
    for item in payload:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("CommandLine"), str)
            or not isinstance(item.get("StartedAt"), str)
        ):
            continue
        try:
            started_at = datetime.fromisoformat(item["StartedAt"])
            if started_at.tzinfo is None:
                continue
            snapshots.append(
                ChromeProcessSnapshot(
                    process_id=int(item["ProcessId"]),
                    session_id=int(item["SessionId"]),
                    command_line=item["CommandLine"],
                    started_at=started_at.astimezone(UTC),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(snapshots)


_PROFILE_ARGUMENT_PATTERNS = (
    re.compile(r'"--user-data-dir=([^"]+)"', re.IGNORECASE),
    re.compile(r'--user-data-dir="([^"]+)"', re.IGNORECASE),
    re.compile(r'--user-data-dir=([^\s"]+)', re.IGNORECASE),
)


def _command_line_profile(command_line: str) -> Path | None:
    for pattern in _PROFILE_ARGUMENT_PATTERNS:
        match = pattern.search(command_line)
        if match is not None:
            return Path(match.group(1))
    return None


def _command_line_is_headless(command_line: str) -> bool:
    return re.search(
        r'(?:^|[\s"])--headless(?:=|[\s"]|$)',
        command_line,
        re.IGNORECASE,
    ) is not None


def _normalized_windows_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _remove_profile_directory(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
