"""Deterministic preflight planning for multimodal evidence requests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError


@dataclass(frozen=True, slots=True)
class EvidenceRequestEstimate:
    estimated_input_tokens: int
    text_byte_upper_bound: int
    image_count: int
    image_token_reserve: int
    message_overhead_tokens: int


class EvidenceRequestPlanner:
    """Admit a request only when its conservative envelope fits the model."""

    def __init__(
        self,
        *,
        max_input_tokens: int,
        image_token_reserve: int,
        message_overhead_tokens: int,
        provider: str,
        model: str,
    ) -> None:
        self._max_input_tokens = max_input_tokens
        self._image_token_reserve = image_token_reserve
        self._message_overhead_tokens = message_overhead_tokens
        self._provider = provider
        self._model = model

    def estimate(
        self, messages: Iterable[Mapping[str, Any]]
    ) -> EvidenceRequestEstimate:
        text_bytes = 0
        image_count = 0

        def visit(value: Any) -> None:
            nonlocal text_bytes, image_count
            if isinstance(value, Mapping):
                if value.get("type") == "image_url":
                    image_count += 1
                    return
                for child in value.values():
                    visit(child)
                return
            if isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)
                return
            if isinstance(value, str):
                text_bytes += len(value.encode("utf-8"))

        for message in messages:
            visit(message)
        estimate = (
            text_bytes
            + image_count * self._image_token_reserve
            + self._message_overhead_tokens
        )
        return EvidenceRequestEstimate(
            estimated_input_tokens=estimate,
            text_byte_upper_bound=text_bytes,
            image_count=image_count,
            image_token_reserve=self._image_token_reserve,
            message_overhead_tokens=self._message_overhead_tokens,
        )

    def require_fit(
        self, messages: Iterable[Mapping[str, Any]]
    ) -> EvidenceRequestEstimate:
        estimate = self.estimate(messages)
        if estimate.estimated_input_tokens > self._max_input_tokens:
            raise SemvideoError(
                code="model_input_budget_exceeded",
                category=ErrorCategory.INPUT,
                message="多模态证据请求超过模型的固定输入预算。",
                recovery=RecoveryAction.CORRECT_AND_RETRY,
                stage="analyze",
                details={
                    "provider": self._provider,
                    "model": self._model,
                    "max_input_tokens": self._max_input_tokens,
                    "estimated_input_tokens": estimate.estimated_input_tokens,
                    "text_byte_upper_bound": estimate.text_byte_upper_bound,
                    "image_count": estimate.image_count,
                    "image_token_reserve": estimate.image_token_reserve,
                    "message_overhead_tokens": estimate.message_overhead_tokens,
                },
                exit_code=3,
            )
        return estimate
