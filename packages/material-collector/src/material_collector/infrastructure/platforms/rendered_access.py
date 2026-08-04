"""Classify rendered login and challenge state shared by auth and search."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from playwright.async_api import Error as PlaywrightError

from material_collector.core.media import Platform


class RenderedPage(Protocol):
    async def evaluate(self, expression: str) -> object: ...

    async def title(self) -> str: ...


class RenderedAccessState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


async def inspect_rendered_access(
    page: RenderedPage,
    platform: Platform,
) -> RenderedAccessState:
    """Return only stable access state without retaining page content."""

    try:
        if platform == Platform.DOUYIN:
            title = await page.title()
            state = await page.evaluate(
                """() => {
                    const panel = document.querySelector('#login-panel-new');
                    const style = panel ? window.getComputedStyle(panel) : null;
                    const loginPanelVisible = Boolean(
                        panel &&
                        style &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden'
                    );
                    return {
                        has_user_login:
                            window.localStorage.getItem('HasUserLogin') === '1',
                        login_panel_visible: loginPanelVisible
                    };
                }"""
            )
            if "验证码中间页" in title:
                return RenderedAccessState.CHALLENGE
            if not isinstance(state, dict):
                return RenderedAccessState.UNKNOWN
            if state.get("login_panel_visible") is True:
                return RenderedAccessState.LOGGED_OUT
            if state.get("has_user_login") is True:
                return RenderedAccessState.LOGGED_IN
            return RenderedAccessState.UNKNOWN

        if platform == Platform.XIAOHONGSHU:
            state = await page.evaluate(
                """() => {
                    const visible = (element) => {
                        if (!element) {
                            return false;
                        }
                        const style = window.getComputedStyle(element);
                        return (
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                    };
                    const profileMeVisible = Array.from(
                        document.querySelectorAll("a[href*='/user/profile/'] span")
                    ).some(
                        (element) =>
                            element.textContent?.trim() === '我' &&
                            visible(element)
                    );
                    const loginContainerVisible = Array.from(
                        document.querySelectorAll('.login-container')
                    ).some(visible);
                    return {
                        profile_me_visible: profileMeVisible,
                        captcha_prompt:
                            document.body?.innerText.includes('请通过验证') ??
                            false,
                        login_container_visible: loginContainerVisible
                    };
                }"""
            )
            if not isinstance(state, dict):
                return RenderedAccessState.UNKNOWN
            if state.get("captcha_prompt") is True:
                return RenderedAccessState.CHALLENGE
            if state.get("login_container_visible") is True:
                return RenderedAccessState.LOGGED_OUT
            if state.get("profile_me_visible") is True:
                return RenderedAccessState.LOGGED_IN
    except (PlaywrightError, TypeError, ValueError):
        return RenderedAccessState.UNKNOWN
    return RenderedAccessState.UNKNOWN
