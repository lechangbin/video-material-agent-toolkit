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


def test_agnes_adapter_uses_openai_endpoint_without_siliconflow_options() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ("https://apihub.agnes-ai.com/v1/chat/completions")
        payload = json.loads(request.content)
        assert payload["model"] == "agnes-2.5-flash"
        assert payload["max_tokens"] == 8192
        assert "enable_thinking" not in payload
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "agnes-trace-1"},
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    config = LlmConfig(
        provider="agnes",
        base_url="https://apihub.agnes-ai.com/v1",
        model="agnes-2.5-flash",
        credential_env="AGNES_API_KEY",
        context_window_tokens=524288,
    )
    adapter = OpenAICompatibleLlm(
        config,
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = adapter.chat([{"role": "user", "content": "x"}])

    assert result.trace_id == "agnes-trace-1"
    assert result.response_headers == {"x-request-id": "agnes-trace-1"}


def test_adapter_rejects_reported_usage_above_context_window() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"total_tokens": 524289},
            },
        )

    adapter = OpenAICompatibleLlm(
        LlmConfig(
            provider="agnes",
            base_url="https://apihub.agnes-ai.com/v1",
            model="agnes-2.5-flash",
            credential_env="AGNES_API_KEY",
            context_window_tokens=524288,
        ),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SemvideoError) as raised:
        adapter.chat([{"role": "user", "content": "x"}])

    assert raised.value.payload.code == "provider_context_usage_invalid"
    assert raised.value.payload.recovery.value == "report_bug"


def test_adapter_rejects_oversized_request_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SemvideoError) as raised:
        adapter.chat([{"role": "user", "content": "x" * (256 * 1024)}])

    assert raised.value.payload.code == "model_input_budget_exceeded"
    assert raised.value.payload.details["max_input_tokens"] == 253952
    assert calls == 0


def test_image_payload_uses_fixed_token_reserve_not_base64_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    adapter = OpenAICompatibleLlm(
        LlmConfig(max_retries=0),
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = adapter.chat(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + "A" * 300_000,
                            "detail": "high",
                        },
                    }
                ],
            }
        ]
    )

    assert result.content == "{}"


def test_adapter_rejects_unknown_provider_at_adapter_boundary() -> None:
    with pytest.raises(SemvideoError) as raised:
        OpenAICompatibleLlm(
            LlmConfig(provider="custom"),
            "secret",
        )

    assert raised.value.payload.code == "llm_provider_unsupported"


def test_adapter_rejects_fixed_provider_identity_drift_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    with pytest.raises(SemvideoError) as raised:
        OpenAICompatibleLlm(
            LlmConfig(
                provider="siliconflow",
                base_url="https://example.invalid/v1",
                model="unverified-model",
            ),
            "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert raised.value.payload.code == "llm_provider_profile_mismatch"
    assert set(raised.value.payload.details["mismatched_fields"]) == {
        "base_url",
        "model",
    }
    assert calls == 0


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (LlmConfig(max_output_tokens=16384), "max_output_tokens"),
        (
            LlmConfig(
                provider="agnes",
                base_url="https://apihub.agnes-ai.com/v1",
                model="agnes-2.5-flash",
                credential_env="AGNES_API_KEY",
                context_window_tokens=524288,
                enable_thinking=True,
            ),
            "enable_thinking",
        ),
    ],
)
def test_adapter_rejects_provider_envelope_drift_before_http(
    config: LlmConfig,
    field: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    with pytest.raises(SemvideoError) as raised:
        OpenAICompatibleLlm(
            config,
            "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert raised.value.payload.code == "llm_provider_profile_mismatch"
    assert field in raised.value.payload.details["mismatched_fields"]
    assert calls == 0


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
