"""Declared capabilities for supported multimodal model providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


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
    fixed_capabilities: bool = True

    @property
    def input_budget_tokens(self) -> int:
        return self.context_window_tokens - self.default_max_output_tokens


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
    fixed_capabilities=False,
)

PROVIDER_PROFILES: Final = {
    AGNES_PROFILE.provider: AGNES_PROFILE,
    SILICONFLOW_PROFILE.provider: SILICONFLOW_PROFILE,
}


def get_provider_profile(provider: str) -> LlmProviderProfile | None:
    return PROVIDER_PROFILES.get(provider.casefold())
