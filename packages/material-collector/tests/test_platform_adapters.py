from __future__ import annotations

import hashlib
import socket
import ssl
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import httpcore
import httpx
import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from material_collector.application.ports import (
    MediaFetcher,
    SearchProvider,
    SourceResolver,
)
from material_collector.core.media import (
    BrowserChannel,
    FetchRequest,
    MediaQuality,
    Platform,
    PlatformContext,
    SearchRequest,
)
from material_collector.infrastructure.media_inspection import (
    MediaInspectionError,
    MediaProbe,
)
from material_collector.infrastructure.platforms._shared import (
    validate_temporary_media_url,
)
from material_collector.infrastructure.platforms.bilibili import (
    BilibiliAdapter,
    parse_bilibili_search,
)
from material_collector.infrastructure.platforms.douyin import (
    DouyinAdapter,
    parse_douyin_search,
    parse_douyin_work_id,
)
from material_collector.infrastructure.platforms.errors import PlatformAdapterError
from material_collector.infrastructure.platforms.transport import (
    PlaywrightPlatformTransport,
)
from material_collector.infrastructure.platforms.xiaohongshu import (
    XiaohongshuAdapter,
    parse_xiaohongshu_note_id,
    parse_xiaohongshu_search,
)


class FakeTransport:
    def __init__(
        self,
        payloads: Mapping[str, list[Mapping[str, Any]]] | None = None,
        *,
        resolved_url: str = "",
        body: bytes = b"media",
    ) -> None:
        self.payloads = {key: list(value) for key, value in (payloads or {}).items()}
        self.resolved_url = resolved_url
        self.body = body
        self.download_urls: list[str] = []
        self.capture_urls: list[str] = []
        self.capture_fragments: list[str] = []

    async def capture_json(
        self,
        page_url: str,
        response_url_fragment: str,
        *,
        platform: Platform,
        operation: str,
        context: PlatformContext,
    ) -> Mapping[str, Any]:
        del platform, context
        self.capture_urls.append(page_url)
        self.capture_fragments.append(response_url_fragment)
        return self.payloads[operation].pop(0)

    async def resolve_url(
        self,
        url: str,
        *,
        platform: Platform,
        context: PlatformContext,
    ) -> str:
        del url, platform, context
        return self.resolved_url

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        context: PlatformContext,
        referer: str,
    ) -> None:
        del context, referer
        self.download_urls.append(url)
        destination.write_bytes(self.body)


def search_request(*, limit: int = 20) -> SearchRequest:
    return SearchRequest(
        query_plan_id="qp_segment",
        segment_id="segment",
        query_id="query",
        round_number=1,
        text="城市夜景",
        limit=limit,
    )


CONTEXT = PlatformContext(auth_profile="editing")


def inspect_720p(path: Path) -> MediaProbe:
    return MediaProbe(
        path=path,
        duration_seconds=12.5,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        width=1280,
        height=720,
        video_stream_count=1,
        audio_stream_count=1,
    )


def bilibili_search_payload() -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "numResults": 1,
            "result": [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": "<em class=\"keyword\">城市</em> 夜景",
                    "author": "摄影师",
                    "description": "延时摄影",
                    "pubdate": 1_700_000_000,
                    "duration": "01:02",
                }
            ],
        },
    }


def bilibili_detail_payload() -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "title": "城市合集",
            "pages": [
                {"cid": 101, "page": 1, "part": "日落", "duration": 61},
                {"cid": 202, "page": 2, "part": "夜景", "duration": 72},
            ],
        },
    }


def douyin_aweme(work_id: str = "7301234567890123456") -> dict[str, Any]:
    return {
        "aweme_id": work_id,
        "desc": "城市夜景",
        "create_time": 1_700_000_000,
        "author": {"nickname": "摄影师"},
        "video": {
            "duration": 15_500,
            "bit_rate": [
                {
                    "gear_name": "normal_720_0",
                    "play_addr": {
                        "url_list": [
                            (
                                "https://v3-dy-o-abtest.zjcdn.com/"
                                "video?token=temporary"
                            )
                        ]
                    },
                }
            ],
        },
    }


def xhs_note(note_id: str = "64f0123456789abcdef01234") -> dict[str, Any]:
    return {
        "note_id": note_id,
        "title": "城市夜景",
        "desc": "散步记录",
        "time": 1_700_000_000,
        "user": {"nickname": "摄影师"},
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        {
                            "width": 540,
                            "height": 960,
                            "duration": 12_000,
                            "master_url": (
                                "https://sns-video-hw.xhscdn.com/low?sign=temporary"
                            ),
                        },
                        {
                            "width": 1080,
                            "height": 1920,
                            "duration": 12_000,
                            "master_url": (
                                "https://sns-video-hw.xhscdn.com/high?sign=temporary"
                            ),
                        },
                    ]
                }
            }
        },
    }


def test_bilibili_search_normalizes_html_and_duration() -> None:
    batch = parse_bilibili_search(bilibili_search_payload(), search_request())

    candidate = batch.candidates[0]
    assert candidate.source_id == "BV1xx411c7mD"
    assert candidate.title == "城市 夜景"
    assert candidate.duration_seconds == 62
    assert candidate.canonical_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert batch.exhausted is True


@pytest.mark.asyncio
async def test_bilibili_resolves_each_p_and_cid_as_a_media_unit() -> None:
    adapter = BilibiliAdapter(FakeTransport({"resolve": [bilibili_detail_payload()]}))

    source = await adapter.resolve(
        "BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        CONTEXT,
    )

    assert [unit.media_unit_id for unit in source.media_units] == [
        "BV1xx411c7mD:p1:cid101",
        "BV1xx411c7mD:p2:cid202",
    ]
    assert source.media_units[1].part_index == 2
    assert source.media_units[1].metadata == {"bvid": "BV1xx411c7mD", "cid": "202"}


@pytest.mark.asyncio
async def test_bilibili_fetch_uses_fresh_playback_url_without_persisting_it(
    tmp_path: Path,
) -> None:
    playback = {
        "code": 0,
        "data": {
            "durl": [
                {
                    "url": (
                        "https://upos-sz-mirrorcos.bilivideo.com/"
                        "proxy.mp4?deadline=temporary"
                    )
                }
            ]
        },
    }
    transport = FakeTransport(
        {"resolve": [bilibili_detail_payload()], "fetch": [playback]},
        body=b"bilibili proxy",
    )
    adapter = BilibiliAdapter(transport, media_inspector=inspect_720p)
    source = await adapter.resolve("BV1xx411c7mD", "", CONTEXT)

    result = await adapter.fetch(
        FetchRequest(
            media_unit=source.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=(tmp_path / "bilibili.mp4").resolve(),
        ),
        CONTEXT,
    )

    assert result.sha256 == hashlib.sha256(b"bilibili proxy").hexdigest()
    assert result.media_unit_id == source.media_units[0].stable_id
    assert (result.width, result.height, result.duration_seconds) == (1280, 720, 12.5)
    assert transport.download_urls == [
        "https://upos-sz-mirrorcos.bilivideo.com/proxy.mp4?deadline=temporary"
    ]
    assert "deadline=temporary" not in str(source.model_dump())


@pytest.mark.asyncio
async def test_bilibili_low_proxy_requests_720p(tmp_path: Path) -> None:
    playback = {
        "code": 0,
        "data": {
            "durl": [
                {"url": "https://upos-sz-mirrorcos.bilivideo.com/proxy.mp4"}
            ]
        },
    }
    transport = FakeTransport(
        {"resolve": [bilibili_detail_payload()], "fetch": [playback]}
    )
    adapter = BilibiliAdapter(transport, media_inspector=inspect_720p)
    source = await adapter.resolve("BV1xx411c7mD", "", CONTEXT)

    await adapter.fetch(
        FetchRequest(
            media_unit=source.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=(tmp_path / "bilibili-720.mp4").resolve(),
        ),
        CONTEXT,
    )

    assert "qn=64" in transport.capture_urls[-1]


@pytest.mark.asyncio
async def test_low_proxy_rejects_download_that_ffprobe_finds_above_720p(
    tmp_path: Path,
) -> None:
    playback = {
        "code": 0,
        "data": {
            "durl": [
                {"url": "https://upos-sz-mirrorcos.bilivideo.com/proxy.mp4"}
            ]
        },
    }

    def inspect_1080p(path: Path) -> MediaProbe:
        return MediaProbe(
            path=path,
            duration_seconds=10,
            container="mp4",
            width=1920,
            height=1080,
            video_stream_count=1,
            audio_stream_count=1,
        )

    transport = FakeTransport(
        {"resolve": [bilibili_detail_payload()], "fetch": [playback]}
    )
    adapter = BilibiliAdapter(transport, media_inspector=inspect_1080p)
    source = await adapter.resolve("BV1xx411c7mD", "", CONTEXT)
    destination = (tmp_path / "too-large.mp4").resolve()

    with pytest.raises(PlatformAdapterError) as captured:
        await adapter.fetch(
            FetchRequest(
                media_unit=source.media_units[0],
                quality=MediaQuality.LOW_PROXY,
                destination=destination,
            ),
            CONTEXT,
        )

    assert captured.value.code == "media_quality_exceeded"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_fetch_does_not_silently_accept_ffprobe_failure(tmp_path: Path) -> None:
    playback = {
        "code": 0,
        "data": {
            "durl": [
                {"url": "https://upos-sz-mirrorcos.bilivideo.com/proxy.mp4"}
            ]
        },
    }

    def failed_inspection(path: Path) -> MediaProbe:
        del path
        raise MediaInspectionError("invalid media")

    transport = FakeTransport(
        {"resolve": [bilibili_detail_payload()], "fetch": [playback]}
    )
    adapter = BilibiliAdapter(transport, media_inspector=failed_inspection)
    source = await adapter.resolve("BV1xx411c7mD", "", CONTEXT)
    destination = (tmp_path / "invalid.mp4").resolve()

    with pytest.raises(PlatformAdapterError) as captured:
        await adapter.fetch(
            FetchRequest(
                media_unit=source.media_units[0],
                quality=MediaQuality.LOW_PROXY,
                destination=destination,
            ),
            CONTEXT,
        )

    assert captured.value.code == "media_inspection_failed"
    assert not destination.exists()


def test_douyin_search_normalizes_nested_aweme() -> None:
    payload = {"status_code": 0, "data": [{"aweme_info": douyin_aweme()}], "has_more": 0}

    batch = parse_douyin_search(payload, search_request())

    candidate = batch.candidates[0]
    assert candidate.source_id == "7301234567890123456"
    assert candidate.duration_seconds == 15.5
    assert candidate.author == "摄影师"
    assert candidate.metadata == {}
    assert batch.exhausted is True


@pytest.mark.asyncio
async def test_douyin_short_url_is_resolved_to_stable_id() -> None:
    work_id = "7301234567890123456"
    transport = FakeTransport(
        {"resolve": [{"status_code": 0, "aweme_detail": douyin_aweme(work_id)}]},
        resolved_url=f"https://www.douyin.com/video/{work_id}?previous_page=app_code_link",
    )
    adapter = DouyinAdapter(transport, media_inspector=inspect_720p)

    source = await adapter.resolve("", "https://v.douyin.com/example/", CONTEXT)

    assert source.candidate_id == f"douyin:{work_id}"
    assert source.media_units[0].canonical_url == f"https://www.douyin.com/video/{work_id}"
    assert "temporary" not in str(source.media_units[0].model_dump())


@pytest.mark.asyncio
async def test_douyin_fetch_refreshes_detail_and_keeps_signed_url_ephemeral(
    tmp_path: Path,
) -> None:
    work_id = "7301234567890123456"
    detail = {"status_code": 0, "aweme_detail": douyin_aweme(work_id)}
    transport = FakeTransport({"resolve": [detail, detail]}, body=b"douyin proxy")
    adapter = DouyinAdapter(transport, media_inspector=inspect_720p)
    source = await adapter.resolve(work_id, "", CONTEXT)

    result = await adapter.fetch(
        FetchRequest(
            media_unit=source.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=(tmp_path / "douyin.mp4").resolve(),
        ),
        CONTEXT,
    )

    assert result.sha256 == hashlib.sha256(b"douyin proxy").hexdigest()
    assert transport.download_urls == [
        "https://v3-dy-o-abtest.zjcdn.com/video?token=temporary"
    ]
    assert "token=temporary" not in str(source.model_dump())


@pytest.mark.asyncio
async def test_douyin_low_proxy_selects_highest_stream_not_exceeding_720p(
    tmp_path: Path,
) -> None:
    work_id = "7301234567890123456"
    aweme = douyin_aweme(work_id)
    video = aweme["video"]
    assert isinstance(video, dict)
    video["bit_rate"] = [
        {
            "gear_name": "normal_540_0",
            "play_addr": {
                "url_list": ["https://v3-dy-o-abtest.zjcdn.com/540.mp4"]
            },
        },
        {
            "gear_name": "normal_720_0",
            "play_addr": {
                "url_list": ["https://v3-dy-o-abtest.zjcdn.com/720.mp4"]
            },
        },
        {
            "gear_name": "normal_1080_0",
            "play_addr": {
                "url_list": ["https://v3-dy-o-abtest.zjcdn.com/1080.mp4"]
            },
        },
    ]
    detail = {"status_code": 0, "aweme_detail": aweme}
    transport = FakeTransport({"resolve": [detail, detail]})
    adapter = DouyinAdapter(transport, media_inspector=inspect_720p)
    source = await adapter.resolve(work_id, "", CONTEXT)

    await adapter.fetch(
        FetchRequest(
            media_unit=source.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=(tmp_path / "douyin-720.mp4").resolve(),
        ),
        CONTEXT,
    )

    assert transport.download_urls == [
        "https://v3-dy-o-abtest.zjcdn.com/720.mp4"
    ]


def test_xiaohongshu_search_normalizes_note_card() -> None:
    note_id = "64f0123456789abcdef01234"
    payload = {
        "success": True,
        "code": 0,
        "data": {
            "has_more": False,
            "items": [{"id": note_id, "note_card": xhs_note(note_id)}],
        },
    }

    batch = parse_xiaohongshu_search(payload, search_request())

    candidate = batch.candidates[0]
    assert candidate.source_id == note_id
    assert candidate.title == "城市夜景"
    assert candidate.author == "摄影师"
    assert candidate.metadata == {}
    assert batch.exhausted is True


@pytest.mark.asyncio
async def test_xiaohongshu_search_captures_current_v2_notes_endpoint() -> None:
    note_id = "64f0123456789abcdef01234"
    payload = {
        "success": True,
        "code": 0,
        "data": {
            "has_more": False,
            "items": [{"id": note_id, "note_card": xhs_note(note_id)}],
        },
    }
    transport = FakeTransport({"search": [payload]})

    batch = await XiaohongshuAdapter(transport).search(search_request(), CONTEXT)

    assert len(batch.candidates) == 1
    assert transport.capture_fragments == ["/api/sns/web/v2/search/notes"]


@pytest.mark.asyncio
async def test_xiaohongshu_fetch_resolves_fresh_url_and_hashes_destination(
    tmp_path: Path,
) -> None:
    note_id = "64f0123456789abcdef01234"
    detail = {
        "success": True,
        "code": 0,
        "data": {"items": [{"note_card": xhs_note(note_id)}]},
    }
    transport = FakeTransport({"resolve": [detail, detail]}, body=b"offline video bytes")
    adapter = XiaohongshuAdapter(transport, media_inspector=inspect_720p)
    resolved = await adapter.resolve(note_id, "", CONTEXT)
    destination = (tmp_path / "proxy.mp4").resolve()

    result = await adapter.fetch(
        FetchRequest(
            media_unit=resolved.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=destination,
        ),
        CONTEXT,
    )

    assert result.path == destination
    assert result.size_bytes == len(b"offline video bytes")
    assert result.sha256 == hashlib.sha256(b"offline video bytes").hexdigest()
    assert transport.download_urls == [
        "https://sns-video-hw.xhscdn.com/low?sign=temporary"
    ]
    assert "sign=temporary" not in str(resolved.media_units[0].model_dump())


@pytest.mark.asyncio
async def test_xiaohongshu_low_proxy_selects_highest_stream_not_exceeding_720p(
    tmp_path: Path,
) -> None:
    note_id = "64f0123456789abcdef01234"
    note = xhs_note(note_id)
    streams = note["video"]["media"]["stream"]["h264"]
    assert isinstance(streams, list)
    streams.insert(
        1,
        {
            "width": 720,
            "height": 1280,
            "duration": 12_000,
            "master_url": "https://sns-video-hw.xhscdn.com/720.mp4",
        },
    )
    detail = {
        "success": True,
        "code": 0,
        "data": {"items": [{"note_card": note}]},
    }
    transport = FakeTransport({"resolve": [detail, detail]})
    adapter = XiaohongshuAdapter(transport, media_inspector=inspect_720p)
    resolved = await adapter.resolve(note_id, "", CONTEXT)

    await adapter.fetch(
        FetchRequest(
            media_unit=resolved.media_units[0],
            quality=MediaQuality.LOW_PROXY,
            destination=(tmp_path / "xhs-720.mp4").resolve(),
        ),
        CONTEXT,
    )

    assert transport.download_urls == [
        "https://sns-video-hw.xhscdn.com/720.mp4"
    ]


class _FakeBrowser:
    async def cookies(self) -> list[dict[str, str]]:
        return []

    async def new_page(self) -> Any:
        class _Page:
            async def evaluate(self, script: str) -> str:
                del script
                return "test-agent"

            async def close(self) -> None:
                return None

        return _Page()


class _FakeResponseExpectation:
    def __init__(
        self,
        *,
        response_timeout: bool,
        payload: Mapping[str, object] | None,
    ) -> None:
        self._response_timeout = response_timeout
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    @property
    def value(self) -> Any:
        async def wait() -> Any:
            if self._response_timeout:
                raise PlaywrightTimeoutError("response timeout")

            class Response:
                status = 200

                async def json(response_self) -> Mapping[str, object]:
                    del response_self
                    if self._payload is None:
                        raise AssertionError("A response was not configured for this test.")
                    return self._payload

            return Response()

        return wait()


class _FakeCapturePage:
    def __init__(
        self,
        *,
        navigation_timeout: bool = False,
        response_timeout: bool = False,
        rendered_state: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        self._navigation_timeout = navigation_timeout
        self._response_timeout = response_timeout
        self._rendered_state = rendered_state or {}
        self._payload = payload
        self.goto_urls: list[str] = []

    def expect_response(self, *args: object, **kwargs: object) -> _FakeResponseExpectation:
        del args, kwargs
        return _FakeResponseExpectation(
            response_timeout=self._response_timeout,
            payload=self._payload,
        )

    async def goto(self, *args: object, **kwargs: object) -> None:
        self.goto_urls.append(str(args[0]))
        del kwargs
        if self._navigation_timeout:
            raise PlaywrightTimeoutError("navigation timeout")

    async def evaluate(self, script: str) -> Mapping[str, object]:
        del script
        return self._rendered_state

    async def title(self) -> str:
        return ""


def _capture_browser_context(page: _FakeCapturePage) -> Any:
    @asynccontextmanager
    async def context() -> Any:
        class Browser:
            async def new_page(self) -> _FakeCapturePage:
                return page

        yield Browser()

    return context()


def _browser_context() -> Any:
    @asynccontextmanager
    async def context() -> Any:
        yield _FakeBrowser()

    return context()


@pytest.mark.asyncio
async def test_capture_json_reports_navigation_timeout_separately(
    tmp_path: Path,
) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(navigation_timeout=True)
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.capture_json(
            "https://search.bilibili.com/all?keyword=city",
            "/x/web-interface/wbi/search/type",
            platform=Platform.BILIBILI,
            operation="search",
            context=CONTEXT,
        )

    assert captured.value.code == "platform_navigation_timeout"
    assert captured.value.details == {
        "platform": "bilibili",
        "operation": "search",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_xiaohongshu_capture_warms_authenticated_page_before_search(
    tmp_path: Path,
) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(payload={"success": True, "data": {"items": []}})
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]
    search_url = "https://www.xiaohongshu.com/search_result?keyword=city"

    payload = await transport.capture_json(
        search_url,
        "/api/sns/web/v2/search/notes",
        platform=Platform.XIAOHONGSHU,
        operation="search",
        context=CONTEXT,
    )

    assert payload["success"] is True
    assert page.goto_urls == [
        "https://www.xiaohongshu.com/explore",
        search_url,
    ]


@pytest.mark.asyncio
async def test_xiaohongshu_capture_identifies_warmup_timeout(tmp_path: Path) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(navigation_timeout=True)
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.capture_json(
            "https://www.xiaohongshu.com/search_result?keyword=city",
            "/api/sns/web/v2/search/notes",
            platform=Platform.XIAOHONGSHU,
            operation="search",
            context=CONTEXT,
        )

    assert captured.value.code == "platform_navigation_timeout"
    assert captured.value.details["navigation_phase"] == "warmup"


@pytest.mark.asyncio
async def test_capture_json_reports_response_timeout_separately(
    tmp_path: Path,
) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(response_timeout=True)
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.capture_json(
            "https://www.xiaohongshu.com/search_result?keyword=city",
            "/api/sns/web/v1/search/notes",
            platform=Platform.XIAOHONGSHU,
            operation="search",
            context=CONTEXT,
        )

    assert captured.value.code == "platform_response_timeout"
    assert captured.value.details == {
        "platform": "xiaohongshu",
        "operation": "search",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_xiaohongshu_response_timeout_reports_rendered_challenge(
    tmp_path: Path,
) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(
        response_timeout=True,
        rendered_state={
            "profile_me_visible": False,
            "captcha_prompt": True,
            "login_container_visible": False,
        },
    )
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.capture_json(
            "https://www.xiaohongshu.com/search_result?keyword=city",
            "/api/sns/web/v1/search/notes",
            platform=Platform.XIAOHONGSHU,
            operation="search",
            context=CONTEXT,
        )

    assert captured.value.code == "challenge_required"
    assert captured.value.details == {
        "platform": "xiaohongshu",
        "operation": "search",
        "retryable": True,
        "reason": "rendered_platform_challenge",
    }


@pytest.mark.asyncio
async def test_xiaohongshu_response_timeout_reports_rendered_logout(
    tmp_path: Path,
) -> None:
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    page = _FakeCapturePage(
        response_timeout=True,
        rendered_state={
            "profile_me_visible": False,
            "captcha_prompt": False,
            "login_container_visible": True,
        },
    )
    transport._open_context = lambda platform, context: _capture_browser_context(page)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.capture_json(
            "https://www.xiaohongshu.com/search_result?keyword=city",
            "/api/sns/web/v1/search/notes",
            platform=Platform.XIAOHONGSHU,
            operation="search",
            context=CONTEXT,
        )

    assert captured.value.code == "authentication_lost"
    assert captured.value.details == {
        "platform": "xiaohongshu",
        "operation": "search",
        "retryable": True,
        "reason": "rendered_platform_logged_out",
    }


class _RecordingNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self._response = b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\ndirect"
        self.server_hostnames: list[str | None] = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        response, self._response = self._response, b""
        return response

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        del buffer, timeout

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del ssl_context, timeout
        self.server_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        del info
        return None


class _RecordingNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected_hosts: list[str] = []
        self.stream = _RecordingNetworkStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        del port, timeout, local_address, socket_options
        self.connected_hosts.append(host)
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("Unix sockets are not used for platform media.")

    async def sleep(self, seconds: float) -> None:
        del seconds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visible", "expected_headless"),
    [(False, True), (True, False)],
)
async def test_platform_browser_visibility_is_execution_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    visible: bool,
    expected_headless: bool,
) -> None:
    launches: list[tuple[Path, dict[str, object]]] = []

    class FakeContext(_FakeBrowser):
        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: Path,
            **kwargs: object,
        ) -> FakeContext:
            launches.append((user_data_dir, kwargs))
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> Any:
            return type("FakePlaywright", (), {"chromium": FakeChromium()})()

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.platforms.transport.async_playwright",
        FakePlaywrightManager,
    )
    transport = PlaywrightPlatformTransport(auth_root=tmp_path)

    context = PlatformContext(
        auth_profile="editing",
        browser_channel=BrowserChannel.EDGE,
        show_search_browser=visible,
    )
    async with transport._open_context(Platform.BILIBILI, context, visible=visible):
        pass

    profile_path, launch = launches[0]
    assert profile_path == tmp_path / "editing" / "edge" / "bilibili"
    assert launch["channel"] == "msedge"
    assert launch["args"] == ["--no-proxy-server"]
    assert launch["headless"] is expected_headless
    assert launch["chromium_sandbox"] is True


@pytest.mark.asyncio
async def test_visible_search_queries_share_one_platform_browser_context(
    tmp_path: Path,
) -> None:
    opened = 0
    closed = 0

    class SharedPage(_FakeCapturePage):
        def __init__(self) -> None:
            super().__init__(payload={"code": 0, "data": {"result": []}})
            self._closed = False
            self._close_callbacks: list[Any] = []

        async def add_init_script(self, script: str) -> None:
            del script

        def on(self, event: str, callback: Any) -> None:
            assert event == "close"
            self._close_callbacks.append(callback)

        def is_closed(self) -> bool:
            return self._closed

        def close_by_user(self) -> None:
            self._closed = True
            for callback in self._close_callbacks:
                callback(self)

    class SharedBrowser:
        def __init__(self) -> None:
            self.pages: list[SharedPage] = []

        async def new_page(self) -> SharedPage:
            page = SharedPage()
            self.pages.append(page)
            return page

    browser = SharedBrowser()

    @asynccontextmanager
    async def shared_context() -> Any:
        nonlocal opened, closed
        opened += 1
        try:
            yield browser
        finally:
            closed += 1

    transport = PlaywrightPlatformTransport(auth_root=tmp_path)
    transport._open_context = lambda platform, context, visible=False: shared_context()  # type: ignore[method-assign, misc]
    context = PlatformContext(
        auth_profile="editing",
        browser_channel=BrowserChannel.EDGE,
        show_search_browser=True,
    )

    async with transport.search_execution((Platform.BILIBILI,), context):
        await transport.capture_json(
            "https://search.bilibili.com/all?keyword=city",
            "/x/web-interface/wbi/search/type",
            platform=Platform.BILIBILI,
            operation="search",
            context=context,
        )
        await transport.capture_json(
            "https://search.bilibili.com/all?keyword=space",
            "/x/web-interface/wbi/search/type",
            platform=Platform.BILIBILI,
            operation="search",
            context=context,
        )

        assert opened == 1
        assert closed == 0

        browser.pages[-1].close_by_user()
        with pytest.raises(PlatformAdapterError) as captured:
            await transport.capture_json(
                "https://search.bilibili.com/all?keyword=closed",
                "/x/web-interface/wbi/search/type",
                platform=Platform.BILIBILI,
                operation="search",
                context=context,
            )
        assert captured.value.code == "search_browser_closed"

    assert opened == 1
    assert closed == 1


@pytest.mark.asyncio
async def test_visible_search_window_close_is_structured_and_never_reopens_headless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launches: list[dict[str, object]] = []
    scripts: list[str] = []

    class FakePending:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        @property
        async def value(self) -> object:
            raise AssertionError("a closed page cannot produce a response")

    class ClosedPage:
        def __init__(self) -> None:
            self._closed = False
            self._close_callbacks: list[Any] = []

        async def add_init_script(self, script: str) -> None:
            scripts.append(script)

        def on(self, event: str, callback: Any) -> None:
            assert event == "close"
            self._close_callbacks.append(callback)

        def expect_response(self, *args: object, **kwargs: object) -> FakePending:
            del args, kwargs
            return FakePending()

        async def goto(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            self._closed = True
            for callback in self._close_callbacks:
                callback(self)
            raise RuntimeError("private browser path must not escape")

        def is_closed(self) -> bool:
            return self._closed

    class FakeContext:
        async def new_page(self) -> ClosedPage:
            return ClosedPage()

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch_persistent_context(
            self,
            user_data_dir: Path,
            **kwargs: object,
        ) -> FakeContext:
            del user_data_dir
            launches.append(kwargs)
            return FakeContext()

    class FakePlaywrightManager:
        async def __aenter__(self) -> Any:
            return type("FakePlaywright", (), {"chromium": FakeChromium()})()

        async def __aexit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "material_collector.infrastructure.platforms.transport.async_playwright",
        FakePlaywrightManager,
    )
    context = PlatformContext(
        auth_profile="editing",
        browser_channel=BrowserChannel.EDGE,
        show_search_browser=True,
    )

    with pytest.raises(PlatformAdapterError) as captured:
        await PlaywrightPlatformTransport(auth_root=tmp_path).capture_json(
            "https://www.bilibili.com/search",
            "/api/search",
            platform=Platform.BILIBILI,
            operation="search",
            context=context,
        )

    assert captured.value.code == "search_browser_closed"
    assert captured.value.details == {
        "platform": "bilibili",
        "operation": "search",
        "retryable": True,
    }
    assert launches == [
        {
            "channel": "msedge",
            "headless": False,
            "chromium_sandbox": True,
            "args": ["--no-proxy-server"],
        }
    ]
    assert len(scripts) == 1
    assert "Material Collector" in scripts[0]
    assert "bilibili" in scripts[0]
    assert "private browser path" not in str(captured.value.details)


@pytest.mark.asyncio
async def test_download_connects_to_validated_ip_without_second_dns_lookup(
    tmp_path: Path,
) -> None:
    resolver_calls = 0

    async def rebinding_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        nonlocal resolver_calls
        del host, port, type
        resolver_calls += 1
        address = "8.8.8.8" if resolver_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    backend = _RecordingNetworkBackend()
    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=rebinding_resolver,
        network_backend=backend,
    )
    transport._open_context = lambda platform, context: _browser_context()  # type: ignore[method-assign, misc, assignment]

    destination = (tmp_path / "pinned.mp4").resolve()
    await transport.download(
        "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
        destination,
        context=CONTEXT,
        referer="https://www.bilibili.com/video/BV1xx411c7mD",
    )

    assert destination.read_bytes() == b"direct"
    assert resolver_calls == 1
    assert backend.connected_hosts == ["8.8.8.8"]
    assert backend.stream.server_hostnames == [
        "upos-sz-mirrorcos.bilivideo.com"
    ]


@pytest.mark.asyncio
async def test_download_rejects_allowed_cdn_name_resolving_to_private_address(
    tmp_path: Path,
) -> None:
    async def private_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        del host, port, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=private_resolver,
    )
    transport._open_context = lambda platform, context: _browser_context()  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.download(
            "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
            (tmp_path / "private.mp4").resolve(),
            context=CONTEXT,
            referer="https://www.bilibili.com/video/BV1xx411c7mD",
        )

    assert captured.value.code == "media_url_invalid"
    assert not (tmp_path / "private.mp4").exists()


def test_media_url_rejects_non_platform_origin_before_network_access() -> None:
    with pytest.raises(PlatformAdapterError) as captured:
        validate_temporary_media_url(
            Platform.BILIBILI,
            "https://attacker.example/video.mp4",
        )

    assert captured.value.code == "media_url_invalid"


@pytest.mark.parametrize(
    "url",
    (
        "https://8.8.8.8/video.mp4",
        "https://upos-sz-mirrorcos.bilivideo.com:8443/video.mp4",
    ),
)
def test_media_url_rejects_literal_ips_and_non_https_ports(url: str) -> None:
    with pytest.raises(PlatformAdapterError) as captured:
        validate_temporary_media_url(Platform.BILIBILI, url)

    assert captured.value.code == "media_url_invalid"


@pytest.mark.asyncio
async def test_download_revalidates_each_redirect_and_rejects_private_target(
    tmp_path: Path,
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    async def public_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        del host, port, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=public_resolver,
        http_transport=httpx.MockTransport(handler),
    )
    transport._open_context = lambda platform, context: _browser_context()  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.download(
            "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
            (tmp_path / "redirect.mp4").resolve(),
            context=CONTEXT,
            referer="https://www.bilibili.com/video/BV1xx411c7mD",
        )

    assert captured.value.code == "media_url_invalid"
    assert requested == ["https://upos-sz-mirrorcos.bilivideo.com/video.mp4"]


@pytest.mark.asyncio
async def test_download_does_not_use_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:10808")
    observed_proxy_headers: list[str | None] = []
    observed_client_options: dict[str, Any] = {}
    real_async_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        observed_client_options.update(kwargs)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        observed_proxy_headers.append(request.headers.get("proxy-authorization"))
        return httpx.Response(200, content=b"direct")

    async def public_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        del host, port, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=public_resolver,
        http_transport=httpx.MockTransport(handler),
    )
    transport._open_context = lambda platform, context: _browser_context()  # type: ignore[method-assign, misc, assignment]

    destination = (tmp_path / "direct.mp4").resolve()
    await transport.download(
        "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
        destination,
        context=CONTEXT,
        referer="https://www.bilibili.com/video/BV1xx411c7mD",
    )

    assert destination.read_bytes() == b"direct"
    assert observed_proxy_headers == [None]
    assert observed_client_options["trust_env"] is False
    assert observed_client_options["follow_redirects"] is False


@pytest.mark.parametrize(
    ("adapter", "spoofed_url"),
    [
        (
            DouyinAdapter(
                FakeTransport(
                    resolved_url=(
                        "https://www.douyin.com/video/7301234567890123456"
                    )
                )
            ),
            "https://attacker.example/?next=v.douyin.com",
        ),
        (
            XiaohongshuAdapter(
                FakeTransport(
                    resolved_url=(
                        "https://www.xiaohongshu.com/explore/"
                        "64f0123456789abcdef01234"
                    )
                )
            ),
            "https://attacker.example/?next=xhslink.com",
        ),
    ],
)
@pytest.mark.asyncio
async def test_short_link_detection_rejects_hostname_substring_spoofing(
    adapter: DouyinAdapter | XiaohongshuAdapter,
    spoofed_url: str,
) -> None:
    with pytest.raises(PlatformAdapterError) as captured:
        await adapter.resolve("", spoofed_url, CONTEXT)

    assert captured.value.code == "source_id_invalid"


class _FakeResolvePage:
    def __init__(self, final_url: str) -> None:
        self.url = final_url

    async def goto(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def _resolve_browser_context(final_url: str) -> Any:
    @asynccontextmanager
    async def context() -> Any:
        class Browser:
            async def new_page(self) -> _FakeResolvePage:
                return _FakeResolvePage(final_url)

        yield Browser()

    return context()


@pytest.mark.asyncio
async def test_short_link_rejects_private_dns_before_request(tmp_path: Path) -> None:
    async def private_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        del host, port, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    url = "https://v.douyin.com/example/"
    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=private_resolver,
    )
    transport._open_context = lambda platform, context: _resolve_browser_context(url)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.resolve_url(url, platform=Platform.DOUYIN, context=CONTEXT)

    assert captured.value.code == "short_url_invalid"


@pytest.mark.asyncio
async def test_short_link_rejects_cross_platform_redirect(tmp_path: Path) -> None:
    target = (
        "https://www.xiaohongshu.com/explore/64f0123456789abcdef01234"
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    async def public_resolver(
        host: str, port: int, *, type: socket.SocketKind
    ) -> list[tuple[Any, ...]]:
        del host, port, type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    initial = "https://v.douyin.com/example/"
    transport = PlaywrightPlatformTransport(
        auth_root=tmp_path,
        resolver=public_resolver,
        http_transport=httpx.MockTransport(handler),
    )
    transport._open_context = lambda platform, context: _resolve_browser_context(target)  # type: ignore[method-assign, misc, assignment]

    with pytest.raises(PlatformAdapterError) as captured:
        await transport.resolve_url(
            initial,
            platform=Platform.DOUYIN,
            context=CONTEXT,
        )

    assert captured.value.code == "short_url_invalid"
    assert requested == [initial]


def test_stable_url_parsers_reject_unrelated_numbers_and_share_tokens() -> None:
    assert (
        parse_douyin_work_id("https://www.douyin.com/video/7301234567890123456?foo=1")
        == "7301234567890123456"
    )
    assert (
        parse_xiaohongshu_note_id(
            "https://www.xiaohongshu.com/explore/64F0123456789ABCDEF01234?xsec_token=secret"
        )
        == "64f0123456789abcdef01234"
    )
    with pytest.raises(PlatformAdapterError):
        parse_douyin_work_id("published at 20260729")


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_bilibili_search, {"code": 0, "data": {"unexpected": []}}),
        (parse_douyin_search, {"status_code": 0, "unexpected": []}),
        (
            parse_xiaohongshu_search,
            {"success": True, "code": 0, "data": {"unexpected": []}},
        ),
    ],
)
def test_platform_schema_drift_is_structured_not_an_empty_result(
    parser: Any,
    payload: Mapping[str, Any],
) -> None:
    with pytest.raises(PlatformAdapterError) as captured:
        parser(payload, search_request())

    assert captured.value.code == "platform_schema_changed"
    assert captured.value.details["retryable"] is False


def test_authentication_failure_is_not_reported_as_empty_search() -> None:
    with pytest.raises(PlatformAdapterError) as captured:
        parse_bilibili_search({"code": -101}, search_request())

    assert captured.value.code == "authentication_lost"
    assert captured.value.details["platform"] == "bilibili"


@pytest.mark.asyncio
async def test_fetch_refuses_existing_destination(tmp_path: Path) -> None:
    note_id = "64f0123456789abcdef01234"
    detail = {
        "success": True,
        "code": 0,
        "data": {"items": [{"note_card": xhs_note(note_id)}]},
    }
    adapter = XiaohongshuAdapter(FakeTransport({"resolve": [detail]}))
    resolved = await adapter.resolve(note_id, "", CONTEXT)
    destination = (tmp_path / "existing.mp4").resolve()
    destination.write_bytes(b"keep")

    with pytest.raises(PlatformAdapterError) as captured:
        await adapter.fetch(
            FetchRequest(
                media_unit=resolved.media_units[0],
                quality=MediaQuality.HIGH,
                destination=destination,
            ),
            CONTEXT,
        )

    assert captured.value.code == "destination_exists"
    assert destination.read_bytes() == b"keep"


def test_adapters_match_the_frozen_protocol_shapes() -> None:
    transport = FakeTransport()
    search_providers: tuple[SearchProvider, ...] = (
        BilibiliAdapter(transport),
        DouyinAdapter(transport),
        XiaohongshuAdapter(transport),
    )
    source_resolvers: tuple[SourceResolver, ...] = (
        BilibiliAdapter(transport),
        DouyinAdapter(transport),
        XiaohongshuAdapter(transport),
    )
    media_fetchers: tuple[MediaFetcher, ...] = (
        BilibiliAdapter(transport),
        DouyinAdapter(transport),
        XiaohongshuAdapter(transport),
    )

    assert [provider.platform for provider in search_providers] == [
        Platform.BILIBILI,
        Platform.DOUYIN,
        Platform.XIAOHONGSHU,
    ]
    assert len(source_resolvers) == len(media_fetchers) == 3
