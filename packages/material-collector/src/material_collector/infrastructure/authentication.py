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
    AuthProbe,
    AuthStatus,
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


class BrowserDesktopUnavailableError(RuntimeError):
    """A driver could not start a headed browser."""


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
        now: Callable[[], datetime] | None = None,
        lock_wait_seconds: float = _LOCK_WAIT_SECONDS,
    ) -> None:
        self._root_dir = _resolve_auth_root(root_dir)
        self._driver = driver or PlaywrightBrowserAuthenticationDriver()
        self._now = now or (lambda: datetime.now(UTC))
        self._lock_wait_seconds = lock_wait_seconds

    async def probe(self, platform: Platform, auth_profile: str) -> AuthProbe:
        """Run one conservative, read-only probe under the profile lock."""

        profile = _validate_profile_id(auth_profile)
        lock = await self._acquire(platform, profile)
        try:
            return await self._probe_locked(platform, profile)
        finally:
            await asyncio.to_thread(lock.release)

    async def ensure_authenticated(
        self,
        platforms: tuple[Platform, ...],
        auth_profile: str,
        wait_seconds: int,
        *,
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
            }
            lock = await self._acquire(platform, profile)
            try:
                _report(progress, "authentication_probe_started", common)
                current = await self._probe_locked(platform, profile)
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
                            "Check Chrome in the taskbar and do not close "
                            "the login window."
                        ),
                    }
                    current = await self._login_locked(
                        platform,
                        profile,
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
    ) -> None:
        """Remove exactly one platform profile after explicit confirmation."""

        profile = _validate_profile_id(auth_profile)
        if confirmation != platform.value:
            raise AuthenticationContractError(
                "Logout confirmation must exactly match the platform value.",
                details={"platform": platform.value},
            )

        lock = await self._acquire(platform, profile)
        try:
            profile_dir = self._profile_dir(profile, platform)
            await asyncio.to_thread(_remove_profile_directory, profile_dir)
        finally:
            await asyncio.to_thread(lock.release)

    async def _acquire(
        self,
        platform: Platform,
        auth_profile: str,
    ) -> _ProcessFileLock:
        lock_path = self._root_dir / ".locks" / f"{auth_profile}.{platform.value}.lock"
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
    ) -> AuthProbe:
        profile_dir = self._profile_dir(auth_profile, platform)
        if not profile_dir.is_dir():
            result = DriverProbe(AuthStatus.INVALID, "profile_missing")
        else:
            try:
                result = await self._driver.probe(platform, profile_dir)
            except OSError, RuntimeError, ValueError:
                result = DriverProbe(AuthStatus.PROBE_FAILED, "probe_driver_error")
        return self._public_probe(platform, auth_profile, result)

    async def _login_locked(
        self,
        platform: Platform,
        auth_profile: str,
        wait_seconds: int,
        *,
        progress: Callable[[str, dict[str, object]], None],
    ) -> AuthProbe:
        profile_dir = self._profile_dir(auth_profile, platform)
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await self._driver.login(
                platform,
                profile_dir,
                wait_seconds,
                progress,
            )
        except BrowserDesktopUnavailableError as error:
            raise AuthenticationDesktopUnavailableError(platform, auth_profile) from error
        except BrowserLoginTimeoutError as error:
            raise AuthenticationLoginTimeoutError(
                platform,
                auth_profile,
                wait_seconds,
                reason_code=error.reason_code,
            ) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise AuthenticationDesktopUnavailableError(platform, auth_profile) from error

        probe = self._public_probe(platform, auth_profile, result)
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

    def _profile_dir(self, auth_profile: str, platform: Platform) -> Path:
        return self._root_dir / auth_profile / platform.value

    def _public_probe(
        self,
        platform: Platform,
        auth_profile: str,
        result: DriverProbe,
    ) -> AuthProbe:
        return AuthProbe(
            platform=platform,
            auth_profile=auth_profile,
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
        desktop_verifier: Callable[[Path, datetime], bool] | None = None,
    ) -> None:
        self._desktop_verifier = (
            desktop_verifier or _headed_chrome_visible_on_active_desktop
        )

    async def probe(self, platform: Platform, user_data_dir: Path) -> DriverProbe:
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(user_data_dir),
                    channel="chrome",
                    headless=True,
                    chromium_sandbox=True,
                    service_workers="block",
                    args=["--no-proxy-server"],
                )
                try:
                    if platform in {Platform.DOUYIN, Platform.XIAOHONGSHU}:
                        page = await _fresh_context_page(context)
                        await page.goto(
                            self._HOME_URLS[platform],
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                    return await self._probe_context(platform, context)
                finally:
                    await context.close()
        except PlaywrightError:
            return DriverProbe(AuthStatus.PROBE_FAILED, "browser_or_network_error")

    async def login(
        self,
        platform: Platform,
        user_data_dir: Path,
        wait_seconds: int,
        progress: Callable[[str, dict[str, object]], None] | None = None,
    ) -> DriverProbe:
        try:
            async with async_playwright() as playwright:
                launch_started_at = datetime.now(UTC)
                context = await playwright.chromium.launch_persistent_context(
                    str(user_data_dir),
                    channel="chrome",
                    headless=False,
                    chromium_sandbox=True,
                    service_workers="block",
                    args=["--no-proxy-server"],
                )
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
                        await navigation
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
                        raise BrowserDesktopUnavailableError
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
        except BrowserDesktopUnavailableError:
            raise
        except PlaywrightError as error:
            raise BrowserDesktopUnavailableError from error

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
    )


async def _probe_json_identity(
    request: APIRequestContext,
    url: str,
    *,
    identity_keys: tuple[str, ...],
    invalid_status_codes: tuple[int, ...] = (),
) -> DriverProbe:
    try:
        response = await request.get(url, timeout=15_000)
        if response.status == 401:
            return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")
        if response.status in {403, 412, 429}:
            return DriverProbe(AuthStatus.CHALLENGE_REQUIRED, "platform_challenge")
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
        "$items = @(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
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
