"""Declared capabilities for supported multimodal model providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class LlmProviderProfile:
    """One selectable, non-secret model-provider contract."""

    provider: str
    base_url: str
    model: str
    credential_env: str
    context_window_tokens: int
    default_max_output_tokens: int
    provider_max_output_tokens: int
    max_concurrency: int
    supports_enable_thinking: bool = False
    fixed_identity: bool = True
    selectable: bool = True
    image_token_reserve: int = 8192
    message_overhead_tokens: int = 1024
    trace_header_names: tuple[str, ...] = (
        "x-request-id",
        "x-trace-id",
        "request-id",
    )

    @property
    def input_budget_tokens(self) -> int:
        return self.context_window_tokens - self.default_max_output_tokens

    def request_options(self, *, enable_thinking: bool) -> dict[str, Any]:
        if not self.supports_enable_thinking:
            return {}
        return {"enable_thinking": enable_thinking}

    def configuration_mismatches(
        self,
        *,
        base_url: str,
        model: str,
        credential_env: str,
        context_window_tokens: int,
        max_output_tokens: int,
        enable_thinking: bool,
    ) -> tuple[str, ...]:
        mismatches: list[str] = []
        if self.fixed_identity:
            expected = {
                "base_url": self.base_url,
                "model": self.model,
                "credential_env": self.credential_env,
                "context_window_tokens": self.context_window_tokens,
            }
            actual = {
                "base_url": base_url,
                "model": model,
                "credential_env": credential_env,
                "context_window_tokens": context_window_tokens,
            }
            mismatches.extend(
                name
                for name, expected_value in expected.items()
                if actual[name] != expected_value
            )
        if max_output_tokens > self.provider_max_output_tokens:
            mismatches.append("max_output_tokens")
        if enable_thinking and not self.supports_enable_thinking:
            mismatches.append("enable_thinking")
        return tuple(mismatches)


AGNES_PROFILE: Final = LlmProviderProfile(
    provider="agnes",
    base_url="https://apihub.agnes-ai.com/v1",
    model="agnes-2.5-flash",
    credential_env="AGNES_API_KEY",
    context_window_tokens=512 * 1024,
    default_max_output_tokens=8 * 1024,
    provider_max_output_tokens=64 * 1024,
    max_concurrency=2,
)

SILICONFLOW_PROFILE: Final = LlmProviderProfile(
    provider="siliconflow",
    base_url="https://api.siliconflow.cn/v1",
    model="Qwen/Qwen3.6-35B-A3B",
    credential_env="SEMVIDEO_API_KEY",
    context_window_tokens=256 * 1024,
    default_max_output_tokens=8 * 1024,
    provider_max_output_tokens=8 * 1024,
    max_concurrency=2,
    supports_enable_thinking=True,
    trace_header_names=(
        "x-siliconcloud-trace-id",
        "x-request-id",
        "x-trace-id",
        "request-id",
    ),
)

OPENAI_COMPATIBLE_PROFILE: Final = LlmProviderProfile(
    provider="openai-compatible",
    base_url="",
    model="",
    credential_env="SEMVIDEO_API_KEY",
    context_window_tokens=256 * 1024,
    default_max_output_tokens=8 * 1024,
    provider_max_output_tokens=64 * 1024,
    max_concurrency=2,
    fixed_identity=False,
    selectable=False,
)

PROVIDER_PROFILES: Final = {
    AGNES_PROFILE.provider: AGNES_PROFILE,
    SILICONFLOW_PROFILE.provider: SILICONFLOW_PROFILE,
    OPENAI_COMPATIBLE_PROFILE.provider: OPENAI_COMPATIBLE_PROFILE,
}


def get_provider_profile(provider: str) -> LlmProviderProfile | None:
    return PROVIDER_PROFILES.get(provider.casefold())


def require_provider_profile(provider: str) -> LlmProviderProfile:
    profile = get_provider_profile(provider)
    if profile is None:
        raise ValueError(f"unsupported model provider: {provider}")
    return profile
