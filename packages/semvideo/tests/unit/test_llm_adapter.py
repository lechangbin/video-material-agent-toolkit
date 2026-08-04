from __future__ import annotations

import json

import httpx
import pytest

from semvideo.adapters.llm import OpenAICompatibleLlm, parse_json_content
from semvideo.config import LlmConfig
from semvideo.errors import ErrorCategory, SemvideoError
from semvideo.infrastructure.io import read_json
from semvideo.infrastructure.locks import LockAcquisitionTimeout


def test_parse_json_content_accepts_fenced_json() -> None:
    assert parse_json_content('```json\n{"segments":[]}\n```') == {"segments": []}


def test_llm_adapter_parses_structured_segmentation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            request=request,
            headers={"x-siliconcloud-trace-id": "trace-1"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"segments":[{"title":"事件","short_summary":"摘要",'
                                '"detailed_summary":"详细摘要","event":"连续事件",'
                                '"start_anchor_id":"anchor_0000",'
                                '"end_anchor_id":"anchor_0001","reason":"事件结束",'
                                '"confidence":0.9}]}'
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=client,
    )

    response, result = adapter.segment([{"role": "user", "content": "x"}])

    assert response.segments[0].start_anchor_id == "anchor_0000"
    assert result.usage.total_tokens == 30
    assert result.trace_id == "trace-1"
    assert result.provider_attempts[0]["usage"]["total_tokens"] == 30


def test_llm_adapter_classifies_rate_limit_without_sleeping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, json={"message": "rate limited"})

    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )

    with pytest.raises(SemvideoError) as raised:
        adapter.chat([{"role": "user", "content": "x"}])

    assert raised.value.payload.category == ErrorCategory.PROVIDER_TRANSIENT
    assert raised.value.payload.retryable is True
    attempts = raised.value.provider_attempts
    assert len(attempts) == 1
    assert attempts[0]["status_code"] == 429
    assert attempts[0]["raw_response"] == {"message": "rate limited"}
    assert "authorization" not in json.dumps(attempts).lower()


def test_rate_limit_publishes_shared_provider_cooldown(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            headers={"retry-after": "12"},
            json={"message": "rate limited"},
        )

    cooldown = tmp_path / "provider.json"
    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        cooldown_path=cooldown,
    )

    with pytest.raises(SemvideoError):
        adapter.chat([{"role": "user", "content": "x"}])

    state = read_json(cooldown)
    assert state["provider"] == "siliconflow"
    assert state["model"] == "Qwen/Qwen3.6-35B-A3B"
    assert state["cooldown_until_epoch"] > 0


def test_cooldown_lock_timeout_is_retryable_provider_failure(
    tmp_path,
    monkeypatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, json={"message": "rate limited"})

    monkeypatch.setattr(
        "semvideo.adapters.llm.exclusive_file_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LockAcquisitionTimeout("timed out waiting for lock")
        ),
    )
    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
        cooldown_path=tmp_path / "provider.json",
    )

    with pytest.raises(SemvideoError) as raised:
        adapter.chat([{"role": "user", "content": "x"}])

    assert raised.value.payload.code == "provider_cooldown_lock_timeout"
    assert raised.value.payload.category == ErrorCategory.PROVIDER_TRANSIENT
    assert raised.value.payload.retryable is True
    assert raised.value.payload.recovery.value == "retry_same"
    assert raised.value.provider_attempts[0]["status_code"] == 429
