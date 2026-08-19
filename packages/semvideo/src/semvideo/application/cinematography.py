"""Application use cases for audited cinematography model calls."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from semvideo.adapters.llm import OpenAICompatibleLlm, parse_json_content
from semvideo.application.model_runs import aggregate_usage
from semvideo.config import WorkspaceConfig
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.modules.cinematography.models import (
    CinematographyResponse,
    ShotTimeline,
)
from semvideo.modules.cinematography.validate import (
    validate_cinematography_response,
)


def call_cinematography_model(
    config: WorkspaceConfig,
    messages: list[dict[str, Any]],
    *,
    timeline: ShotTimeline,
    evidence_frame_ids: Mapping[str, Sequence[str]],
    required_shot_ids: Sequence[str],
    cooldown_path: Path | None = None,
) -> tuple[CinematographyResponse, dict[str, Any]]:
    """Call, repair once, validate, and retain every provider response."""

    key = os.environ.get(config.llm.credential_env, "")
    attempts: list[dict[str, Any]] = []
    repair_attempted = False

    def retain(result: Any) -> None:
        provider_attempts = list(result.provider_attempts)
        if provider_attempts:
            attempts.extend(provider_attempts)
        else:
            attempts.append(
                result.model_dump(
                    mode="json",
                    exclude={"provider_attempts"},
                )
            )

    def parse_and_validate(content: str) -> CinematographyResponse:
        response = CinematographyResponse.model_validate(
            parse_json_content(content)
        )
        validate_cinematography_response(
            response,
            timeline,
            evidence_frame_ids=evidence_frame_ids,
            required_shot_ids=required_shot_ids,
        )
        return response

    try:
        with OpenAICompatibleLlm(
            config.llm,
            key,
            cooldown_path=cooldown_path,
        ) as client:
            result = client.chat(messages)
            retain(result)
            try:
                response = parse_and_validate(result.content)
            except (json.JSONDecodeError, ValidationError, ValueError) as first_error:
                repair_attempted = True
                repaired = client.chat(
                    [
                        *messages,
                        {"role": "assistant", "content": result.content},
                        {
                            "role": "user",
                            "content": (
                                "上一条响应未通过镜头语言 JSON Schema 验证。"
                                "只修正格式、字段、镜头覆盖、时间范围和证据引用；"
                                "不要改变基于视频证据的判断，只返回严格 JSON。"
                                f"\n校验错误：{str(first_error)[:1500]}"
                            ),
                        },
                    ]
                )
                retain(repaired)
                try:
                    response = parse_and_validate(repaired.content)
                except (
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as second_error:
                    error = SemvideoError(
                        code="model_cinematography_invalid_after_repair",
                        category=ErrorCategory.MODEL_RESPONSE_INVALID,
                        message="镜头语言输出经一次格式修复后仍未通过结构验证。",
                        retryable=True,
                        recovery=RecoveryAction.RETRY_SAME,
                        stage="cinematography",
                        details={"reason": str(second_error)},
                        exit_code=5,
                    )
                    error.model_attempts = attempts
                    error.repair_attempted = True
                    raise error from second_error
                result = repaired
    except SemvideoError as error:
        error.with_context(stage="cinematography")
        if not error.model_attempts:
            error.model_attempts = [
                *attempts,
                *list(getattr(error, "provider_attempts", [])),
            ]
        error.repair_attempted = error.repair_attempted or repair_attempted
        raise
    return response, {
        **result.model_dump(mode="json", exclude={"provider_attempts"}),
        "usage": aggregate_usage(attempts),
        "attempts": attempts,
        "repair_attempted": repair_attempted,
    }
