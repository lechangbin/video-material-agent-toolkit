from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from material_collector.core.errors import CollectorError
from material_collector.core.media import AuthStatus, Platform
from material_collector.infrastructure.authentication import (
    AuthenticationContractError,
    AuthenticationDesktopUnavailableError,
    AuthenticationLoginTimeoutError,
    AuthProfileBusyError,
    BrowserDesktopUnavailableError,
    BrowserLoginTimeoutError,
    ChromeProcessSnapshot,
    DriverProbe,
    PlaywrightAuthenticationGateway,
    PlaywrightBrowserAuthenticationDriver,
    WindowsChromeDesktopVerifier,
    _probe_douyin,
    _probe_xiaohongshu,
    _ProcessFileLock,
)

FIXED_NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class FakeDriver:
    def __init__(self) -> None:
        self.probes: dict[Platform, DriverProbe] = {}
        self.logins: dict[Platform, DriverProbe | Exception] = {}
        self.calls: list[tuple[str, Platform, int | None]] = []

    async def probe(self, platform: Platform, user_data_dir: Path) -> DriverProbe:
        assert user_data_dir.name == platform.value
        self.calls.append(("probe", platform, None))
        return self.probes.get(
            platform,
            DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out"),
        )

    async def login(
        self,
        platform: Platform,
        user_data_dir: Path,
        wait_seconds: int,
        progress: Callable[[str, dict[str, object]], None] | None = None,
    ) -> DriverProbe:
        assert user_data_dir.name == platform.value
        self.calls.append(("login", platform, wait_seconds))
        if progress is not None:
            progress("authentication_login_window_opened", {})
            progress("authentication_login_waiting", {})
        result = self.logins.get(
            platform,
            DriverProbe(AuthStatus.VALID, "platform_reports_logged_in"),
        )
        if isinstance(result, Exception):
            raise result
        return result


def gateway(tmp_path: Path, driver: FakeDriver) -> PlaywrightAuthenticationGateway:
    return PlaywrightAuthenticationGateway(
        root_dir=tmp_path / "auth",
        driver=driver,
        now=lambda: FIXED_NOW,
        lock_wait_seconds=0.1,
    )


class _Progress:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def report(self, event: str, details: dict[str, object]) -> None:
        self.events.append((event, details))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (AuthStatus.VALID, "platform_reports_logged_in"),
        (AuthStatus.INVALID, "platform_reports_logged_out"),
        (AuthStatus.CHALLENGE_REQUIRED, "platform_challenge"),
        (AuthStatus.PROBE_FAILED, "unrecognized_probe_response"),
    ],
)
async def test_probe_preserves_all_four_states_without_credentials(
    tmp_path: Path,
    status: AuthStatus,
    reason: str,
) -> None:
    driver = FakeDriver()
    profile_dir = tmp_path / "auth" / "editing" / Platform.BILIBILI.value
    profile_dir.mkdir(parents=True)
    driver.probes[Platform.BILIBILI] = DriverProbe(status, reason)

    result = await gateway(tmp_path, driver).probe(Platform.BILIBILI, "editing")

    assert result.status is status
    assert result.reason_code == reason
    assert result.checked_at == "2026-07-29T12:00:00Z"
    assert result.model_dump().keys() == {
        "schema_version",
        "platform",
        "auth_profile",
        "status",
        "checked_at",
        "reason_code",
    }


async def test_missing_profile_is_explicitly_invalid_without_starting_browser(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()

    result = await gateway(tmp_path, driver).probe(Platform.DOUYIN, "default")

    assert result.status is AuthStatus.INVALID
    assert result.reason_code == "profile_missing"
    assert driver.calls == []


async def test_ensure_authenticated_uses_frozen_serial_platform_order(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    auth_root = tmp_path / "auth" / "editing"
    for platform in Platform:
        (auth_root / platform.value).mkdir(parents=True)
        driver.probes[platform] = DriverProbe(
            AuthStatus.INVALID,
            "platform_reports_logged_out",
        )

    result = await gateway(tmp_path, driver).ensure_authenticated(
        (
            Platform.XIAOHONGSHU,
            Platform.BILIBILI,
            Platform.DOUYIN,
            Platform.BILIBILI,
        ),
        "editing",
        321,
    )

    assert [item.platform for item in result] == [
        Platform.BILIBILI,
        Platform.DOUYIN,
        Platform.XIAOHONGSHU,
    ]
    assert driver.calls == [
        ("probe", Platform.BILIBILI, None),
        ("login", Platform.BILIBILI, 321),
        ("probe", Platform.DOUYIN, None),
        ("login", Platform.DOUYIN, 321),
        ("probe", Platform.XIAOHONGSHU, None),
        ("login", Platform.XIAOHONGSHU, 321),
    ]


async def test_ensure_authenticated_reports_one_platform_login_lifecycle(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    progress = _Progress()

    result = await gateway(tmp_path, driver).ensure_authenticated(
        (Platform.BILIBILI,),
        "editing",
        321,
        progress=progress,
    )

    assert result[0].status is AuthStatus.VALID
    assert [event for event, _details in progress.events] == [
        "authentication_probe_started",
        "authentication_probe_completed",
        "authentication_login_window_opened",
        "authentication_login_waiting",
        "authentication_platform_completed",
    ]
    common = {"platform": "bilibili", "auth_profile": "editing"}
    assert progress.events[0][1] == common
    assert progress.events[1][1] == {
        **common,
        "status": "invalid",
        "reason_code": "profile_missing",
    }
    assert progress.events[2][1] == {
        **common,
        "actor": "human",
        "deadline": "2026-07-29T12:05:21Z",
        "wait_seconds": 321,
        "hint": "Check Chrome in the taskbar and do not close the login window.",
    }
    assert progress.events[3][1] == progress.events[2][1]
    assert progress.events[4][1] == {
        **common,
        "status": "valid",
        "reason_code": "platform_reports_logged_in",
    }


async def test_valid_probe_skips_headed_login(tmp_path: Path) -> None:
    driver = FakeDriver()
    profile_dir = tmp_path / "auth" / "default" / Platform.BILIBILI.value
    profile_dir.mkdir(parents=True)
    driver.probes[Platform.BILIBILI] = DriverProbe(
        AuthStatus.VALID,
        "platform_reports_logged_in",
    )

    result = await gateway(tmp_path, driver).ensure_authenticated(
        (Platform.BILIBILI,),
        "default",
        600,
    )

    assert result[0].status is AuthStatus.VALID
    assert driver.calls == [("probe", Platform.BILIBILI, None)]


@pytest.mark.parametrize(
    ("driver_error", "expected_error", "code"),
    [
        (
            BrowserDesktopUnavailableError(),
            AuthenticationDesktopUnavailableError,
            "auth_desktop_unavailable",
        ),
        (
            BrowserLoginTimeoutError(),
            AuthenticationLoginTimeoutError,
            "auth_login_timeout",
        ),
    ],
)
async def test_headed_login_failures_are_structured_and_credential_free(
    tmp_path: Path,
    driver_error: Exception,
    expected_error: type[CollectorError],
    code: str,
) -> None:
    driver = FakeDriver()
    driver.logins[Platform.BILIBILI] = driver_error

    with pytest.raises(expected_error) as captured:
        await gateway(tmp_path, driver).ensure_authenticated(
            (Platform.BILIBILI,),
            "default",
            2,
        )

    assert captured.value.code == code
    assert captured.value.details["platform"] == "bilibili"
    assert "cookie" not in str(captured.value.details).lower()


async def test_profile_lock_is_held_for_driver_lifetime(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    lock = _ProcessFileLock(root / ".locks" / "editing.bilibili.lock")
    assert lock.acquire(0.1)
    driver = FakeDriver()
    profile_dir = root / "editing" / Platform.BILIBILI.value
    profile_dir.mkdir(parents=True)

    try:
        with pytest.raises(AuthProfileBusyError) as captured:
            await PlaywrightAuthenticationGateway(
                root_dir=root,
                driver=driver,
                lock_wait_seconds=0.02,
            ).probe(Platform.BILIBILI, "editing")
    finally:
        lock.release()

    assert captured.value.code == "auth_profile_busy"
    assert driver.calls == []


def test_profile_lock_excludes_a_separate_process(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    lock_path = root / ".locks" / "editing.bilibili.lock"
    child_code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from material_collector.infrastructure.authentication import _ProcessFileLock\n"
        "lock = _ProcessFileLock(Path(sys.argv[1]))\n"
        "assert lock.acquire(1)\n"
        "print('ready', flush=True)\n"
        "input()\n"
        "lock.release()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(AuthProfileBusyError):
            asyncio.run(
                PlaywrightAuthenticationGateway(
                    root_dir=root,
                    driver=FakeDriver(),
                    lock_wait_seconds=0.02,
                ).probe(Platform.BILIBILI, "editing")
            )
    finally:
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=5)

    assert process.returncode == 0


def test_cancelled_lock_wait_does_not_delay_cli_process_exit(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    held = _ProcessFileLock(root / ".locks" / "default.bilibili.lock")
    assert held.acquire(0.1)
    child_code = (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from material_collector.core.media import Platform\n"
        "from material_collector.infrastructure.authentication import "
        "PlaywrightAuthenticationGateway\n"
        "async def main():\n"
        "    gateway = PlaywrightAuthenticationGateway("
        "root_dir=Path(sys.argv[1]), lock_wait_seconds=1.2)\n"
        "    task = asyncio.create_task(gateway.probe(Platform.BILIBILI, 'default'))\n"
        "    await asyncio.sleep(0.05)\n"
        "    task.cancel()\n"
        "    try:\n"
        "        await task\n"
        "    except asyncio.CancelledError:\n"
        "        pass\n"
        "asyncio.run(main())\n"
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", child_code, str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    finally:
        held.release()

    elapsed = time.monotonic() - started
    assert completed.returncode == 0, completed.stderr
    assert elapsed < 0.5


async def test_lock_release_after_probe_allows_next_gateway(tmp_path: Path) -> None:
    driver = FakeDriver()
    profile_dir = tmp_path / "auth" / "default" / Platform.BILIBILI.value
    profile_dir.mkdir(parents=True)
    first = gateway(tmp_path, driver)
    second = gateway(tmp_path, driver)

    await first.probe(Platform.BILIBILI, "default")
    await second.probe(Platform.BILIBILI, "default")

    assert len(driver.calls) == 2


async def test_logout_requires_exact_confirmation_and_deletes_one_platform_only(
    tmp_path: Path,
) -> None:
    driver = FakeDriver()
    root = tmp_path / "auth" / "editing"
    bili_cookie = root / Platform.BILIBILI.value / "profile.bin"
    douyin_cookie = root / Platform.DOUYIN.value / "profile.bin"
    bili_cookie.parent.mkdir(parents=True)
    douyin_cookie.parent.mkdir(parents=True)
    bili_cookie.write_bytes(b"private")
    douyin_cookie.write_bytes(b"private")
    auth = gateway(tmp_path, driver)

    with pytest.raises(AuthenticationContractError):
        await auth.logout(Platform.BILIBILI, "editing", "douyin")
    assert bili_cookie.exists()

    await auth.logout(Platform.BILIBILI, "editing", "bilibili")

    assert not bili_cookie.parent.exists()
    assert douyin_cookie.exists()


@pytest.mark.parametrize("profile", ["../escape", "bad/profile", "", ".hidden"])
async def test_profile_identifier_cannot_escape_auth_root(
    tmp_path: Path,
    profile: str,
) -> None:
    with pytest.raises(AuthenticationContractError):
        await gateway(tmp_path, FakeDriver()).probe(Platform.BILIBILI, profile)


async def test_event_loop_remains_responsive_while_waiting_for_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    held = _ProcessFileLock(root / ".locks" / "default.bilibili.lock")
    assert held.acquire(0.1)
    ticked = False

    async def tick() -> None:
        nonlocal ticked
        await asyncio.sleep(0)
        ticked = True

    try:
        probe_task = asyncio.create_task(
            PlaywrightAuthenticationGateway(
                root_dir=root,
                driver=FakeDriver(),
                lock_wait_seconds=0.02,
            ).probe(Platform.BILIBILI, "default")
        )
        await tick()
        with pytest.raises(AuthProfileBusyError):
            await probe_task
    finally:
        held.release()

    assert ticked


async def test_authentication_browser_explicitly_bypasses_system_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launches: list[dict[str, object]] = []

    class FakeContext:
        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir
            launches.append(kwargs)
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True
    )
    monkeypatch.setattr(
        driver,
        "_probe_context",
        AsyncMock(return_value=DriverProbe(AuthStatus.VALID, "logged_in")),
    )

    result = await driver.probe(Platform.BILIBILI, tmp_path)

    assert result.status is AuthStatus.VALID
    assert launches[0]["args"] == ["--no-proxy-server"]
    assert launches[0]["chromium_sandbox"] is True
    assert launches[0]["service_workers"] == "block"


@pytest.mark.parametrize(
    ("platform", "expected_url"),
    [
        (Platform.DOUYIN, "https://www.douyin.com/"),
        (Platform.XIAOHONGSHU, "https://www.xiaohongshu.com/"),
    ],
)
async def test_platform_probe_discards_restored_challenge_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: Platform,
    expected_url: str,
) -> None:
    navigated: list[str] = []

    class FakePage:
        def __init__(self, context: FakeContext, *, stale: bool) -> None:
            self._context = context
            self.stale = stale

        async def close(self) -> None:
            self._context.pages.remove(self)

        async def goto(self, url: str, **kwargs: object) -> None:
            del kwargs
            navigated.append(url)

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []
            self.pages.append(FakePage(self, stale=True))

        async def new_page(self) -> FakePage:
            page = FakePage(self, stale=False)
            self.pages.append(page)
            return page

        async def close(self) -> None:
            return None

    context = FakeContext()

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return context

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    async def assert_fresh_context(
        _platform: Platform,
        received: FakeContext,
    ) -> DriverProbe:
        assert received.pages
        assert all(not page.stale for page in received.pages)
        return DriverProbe(AuthStatus.VALID, "platform_reports_logged_in")

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True
    )
    monkeypatch.setattr(driver, "_probe_context", assert_fresh_context)

    result = await driver.probe(platform, tmp_path)

    assert result.status is AuthStatus.VALID
    assert navigated == [expected_url]


async def test_headed_browser_reports_opened_and_waiting_after_navigation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    brought_to_front = False

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def bring_to_front(self) -> None:
            nonlocal brought_to_front
            brought_to_front = True

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.asyncio.sleep",
        AsyncMock(),
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True
    )
    monkeypatch.setattr(
        driver,
        "_probe_context",
        AsyncMock(
            side_effect=[
                DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out"),
                DriverProbe(AuthStatus.VALID, "platform_reports_logged_in"),
            ]
        ),
    )

    result = await driver.login(
        Platform.BILIBILI,
        tmp_path,
        30,
        lambda event, details: events.append((event, details)),
    )

    assert result.status is AuthStatus.VALID
    assert events == [
        ("authentication_login_window_opening", {"phase": "navigation"}),
        ("authentication_login_window_opened", {}),
        ("authentication_login_waiting", {}),
    ]
    assert brought_to_front is True


async def test_headed_browser_reports_progress_during_slow_navigation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    navigation_started = asyncio.Event()
    release_navigation = asyncio.Event()

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            navigation_started.set()
            await release_navigation.wait()

        async def bring_to_front(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    monkeypatch.setattr(
        "material_collector.infrastructure.authentication._LOGIN_PROGRESS_INTERVAL_SECONDS",
        0.01,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True,
    )
    monkeypatch.setattr(
        driver,
        "_probe_context",
        AsyncMock(return_value=DriverProbe(AuthStatus.VALID, "logged_in")),
    )

    login = asyncio.create_task(
        driver.login(
            Platform.BILIBILI,
            tmp_path,
            30,
            lambda event, _details: events.append(event),
        )
    )
    await asyncio.wait_for(navigation_started.wait(), timeout=1)
    await asyncio.sleep(0.035)

    assert events.count("authentication_login_window_opening") >= 2

    release_navigation.set()
    result = await asyncio.wait_for(login, timeout=1)
    assert result.status is AuthStatus.VALID


async def test_headed_browser_repeats_waiting_progress_within_fifteen_seconds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    clock = 0.0

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def bring_to_front(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    async def advance(seconds: float) -> None:
        nonlocal clock
        clock += seconds

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.time.monotonic",
        lambda: clock,
    )
    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.asyncio.sleep",
        advance,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True
    )
    monkeypatch.setattr(
        driver,
        "_probe_context",
        AsyncMock(
            return_value=DriverProbe(
                AuthStatus.INVALID,
                "platform_reports_logged_out",
            )
        ),
    )

    with pytest.raises(BrowserLoginTimeoutError):
        await driver.login(
            Platform.BILIBILI,
            tmp_path,
            31,
            lambda event, _details: events.append(event),
        )

    assert events.count("authentication_login_waiting") == 4


async def test_douyin_status_code_8_is_explicitly_logged_out() -> None:
    class FakeResponse:
        status = 200
        ok = True

        async def json(self) -> dict[str, object]:
            return {
                "status_code": 8,
                "status_msg": "用户未登录",
                "user": None,
            }

    class FakeContext:
        request: FakeContext

        def __init__(self) -> None:
            self.pages: list[object] = []
            self.request = self

        async def get(self, url: str, *, timeout: int) -> FakeResponse:
            assert url.endswith("/aweme/v1/web/user/profile/self/")
            assert timeout == 15_000
            return FakeResponse()

    result = await _probe_douyin(FakeContext())  # type: ignore[arg-type]

    assert result == DriverProbe(
        AuthStatus.INVALID,
        "platform_reports_logged_out",
    )


async def test_douyin_uses_rendered_platform_login_state_over_bare_endpoint() -> None:
    class FakeResponse:
        status = 200
        ok = True

        async def json(self) -> dict[str, object]:
            return {"status_code": 8, "status_msg": "用户未登录", "user": None}

    class FakePage:
        async def title(self) -> str:
            return "【抖音】记录美好生活-Douyin.com"

        async def evaluate(self, expression: str) -> dict[str, bool]:
            assert "HasUserLogin" in expression
            return {
                "has_user_login": True,
                "login_panel_visible": False,
            }

    class FakeContext:
        request: FakeContext

        def __init__(self) -> None:
            self.pages = [FakePage()]
            self.request = self

        async def get(self, _url: str, *, timeout: int) -> FakeResponse:
            assert timeout == 15_000
            return FakeResponse()

    result = await _probe_douyin(FakeContext())  # type: ignore[arg-type]

    assert result == DriverProbe(
        AuthStatus.VALID,
        "platform_reports_logged_in",
    )


async def test_douyin_challenge_page_is_not_accepted_as_logged_in() -> None:
    class FakePage:
        async def title(self) -> str:
            return "验证码中间页"

        async def evaluate(self, expression: str) -> dict[str, bool]:
            assert "HasUserLogin" in expression
            return {
                "has_user_login": True,
                "login_panel_visible": False,
            }

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

    result = await _probe_douyin(FakeContext())  # type: ignore[arg-type]

    assert result == DriverProbe(
        AuthStatus.CHALLENGE_REQUIRED,
        "platform_challenge",
    )


async def test_xiaohongshu_uses_visible_profile_link_over_bare_endpoint() -> None:
    class FakeResponse:
        status = 406
        ok = False

        async def json(self) -> dict[str, object]:
            return {"code": -1, "success": False}

    class FakePage:
        async def evaluate(self, expression: str) -> dict[str, bool]:
            assert "/user/profile/" in expression
            return {
                "profile_me_visible": True,
                "captcha_prompt": False,
                "login_container_visible": False,
            }

    class FakeContext:
        request: FakeContext

        def __init__(self) -> None:
            self.pages = [FakePage()]
            self.request = self

        async def get(self, _url: str, *, timeout: int) -> FakeResponse:
            assert timeout == 15_000
            return FakeResponse()

    result = await _probe_xiaohongshu(FakeContext())  # type: ignore[arg-type]

    assert result == DriverProbe(
        AuthStatus.VALID,
        "platform_reports_logged_in",
    )


async def test_xiaohongshu_captcha_is_not_accepted_as_logged_in() -> None:
    class FakePage:
        async def evaluate(self, expression: str) -> dict[str, bool]:
            assert "请通过验证" in expression
            return {
                "profile_me_visible": True,
                "captcha_prompt": True,
                "login_container_visible": False,
            }

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

    result = await _probe_xiaohongshu(FakeContext())  # type: ignore[arg-type]

    assert result == DriverProbe(
        AuthStatus.CHALLENGE_REQUIRED,
        "platform_challenge",
    )


async def test_headed_browser_stops_when_user_closes_login_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def bring_to_front(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    context = FakeContext()

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return context

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    async def close_during_probe(
        _platform: Platform,
        _context: FakeContext,
    ) -> DriverProbe:
        context.pages.clear()
        return DriverProbe(AuthStatus.INVALID, "platform_reports_logged_out")

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.asyncio.sleep",
        AsyncMock(return_value=None),
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: True
    )
    monkeypatch.setattr(driver, "_probe_context", close_during_probe)

    with pytest.raises(BrowserLoginTimeoutError) as captured:
        await driver.login(
            Platform.DOUYIN,
            tmp_path,
            1,
            lambda event, _details: events.append(event),
        )

    assert captured.value.reason_code == "login_window_closed"
    assert events[-1] == "authentication_login_window_closed"


async def test_headed_browser_rejects_inactive_desktop_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def bring_to_front(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=lambda _path, _started_at: False
    )

    with pytest.raises(BrowserDesktopUnavailableError):
        await driver.login(
            Platform.BILIBILI,
            tmp_path,
            30,
            lambda event, _details: events.append(event),
        )

    assert events == ["authentication_login_window_opening"]


async def test_headed_browser_verifies_the_launched_profile_not_any_chrome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified_profiles: list[tuple[Path, datetime]] = []

    class FakePage:
        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def bring_to_front(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: str,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir, kwargs
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *args: object) -> None:
            del args

    def verify_profile(profile_dir: Path, launched_at: datetime) -> bool:
        verified_profiles.append((profile_dir, launched_at))
        return False

    monkeypatch.setattr(
        "material_collector.infrastructure.authentication.async_playwright",
        FakePlaywrightManager,
    )
    driver = PlaywrightBrowserAuthenticationDriver(
        desktop_verifier=verify_profile,
    )

    with pytest.raises(BrowserDesktopUnavailableError):
        await driver.login(Platform.BILIBILI, tmp_path, 30)

    assert len(verified_profiles) == 1
    assert verified_profiles[0][0] == tmp_path
    assert verified_profiles[0][1].tzinfo is UTC


def test_desktop_verifier_requires_visible_non_headless_target_profile(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target profile"
    other = tmp_path / "other profile"
    launch_started_at = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
    processes = (
        ChromeProcessSnapshot(
            process_id=10,
            session_id=7,
            command_line=f'chrome.exe "--user-data-dir={other}"',
            started_at=launch_started_at,
        ),
        ChromeProcessSnapshot(
            process_id=11,
            session_id=7,
            command_line=f'chrome.exe "--user-data-dir={target}" --headless=new',
            started_at=launch_started_at,
        ),
        ChromeProcessSnapshot(
            process_id=12,
            session_id=7,
            command_line=f'chrome.exe "--user-data-dir={target}"',
            started_at=launch_started_at.replace(hour=5, minute=59),
        ),
        ChromeProcessSnapshot(
            process_id=13,
            session_id=7,
            command_line=f'chrome.exe "--user-data-dir={target}"',
            started_at=launch_started_at.replace(minute=1),
        ),
    )
    visible_process_ids = {10, 11, 12}
    verifier = WindowsChromeDesktopVerifier(
        active_session_id=lambda: 7,
        current_session_id=lambda: 7,
        visible_process_ids=lambda _session_id: visible_process_ids,
        chrome_processes=lambda: processes,
    )

    assert verifier(target, launch_started_at) is False

    visible_process_ids.add(13)
    assert verifier(target, launch_started_at) is True
