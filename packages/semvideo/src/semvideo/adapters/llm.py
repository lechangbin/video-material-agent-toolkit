"""OpenAI-compatible multimodal model Adapter with bounded retries."""

from __future__ import annotations

import json
import os
import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from semvideo.adapters.profiles import require_provider_profile
from semvideo.application.evidence_requests import EvidenceRequestPlanner
from semvideo.config import LlmConfig
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import atomic_write_json, read_json
from semvideo.infrastructure.locks import (
    LockAcquisitionTimeout,
    exclusive_file_lock,
)
from semvideo.modules.semantics.models import (
    ModelCallResult,
    ModelUsage,
    SegmentationResponse,
)


def parse_json_content(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as original:
        object_start = stripped.find("{")
        object_end = stripped.rfind("}")
        if object_start >= 0 and object_end > object_start:
            try:
                return json.loads(stripped[object_start : object_end + 1])
            except json.JSONDecodeError:
                pass
        raise original


class OpenAICompatibleLlm:
    def __init__(
        self,
        config: LlmConfig,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        cooldown_path: Path | None = None,
    ) -> None:
        if not api_key:
            raise SemvideoError(
                code="model_credential_missing",
                category=ErrorCategory.CREDENTIAL,
                message=f"缺少模型凭据环境变量：{config.credential_env}",
                recovery=RecoveryAction.USER_ACTION,
                stage="analyze",
                exit_code=2,
                details={"credential_env": config.credential_env},
            )
        self.config = config
        try:
            self._profile = require_provider_profile(config.provider)
        except ValueError as exc:
            raise SemvideoError(
                code="llm_provider_unsupported",
                category=ErrorCategory.CONFIG,
                message=f"不支持的模型提供商：{config.provider}",
                recovery=RecoveryAction.CORRECT_AND_RETRY,
                stage="analyze",
                details={"provider": config.provider},
                exit_code=2,
            ) from exc
        mismatches = self._profile.configuration_mismatches(
            base_url=config.base_url,
            model=config.model,
            credential_env=config.credential_env,
            context_window_tokens=config.context_window_tokens,
            max_output_tokens=config.max_output_tokens,
            enable_thinking=config.enable_thinking,
        )
        if mismatches:
            raise SemvideoError(
                code="llm_provider_profile_mismatch",
                category=ErrorCategory.CONFIG,
                message="模型适配器配置偏离固定 Provider Profile。",
                recovery=RecoveryAction.CORRECT_AND_RETRY,
                stage="analyze",
                details={
                    "provider": config.provider,
                    "mismatched_fields": list(mismatches),
                },
                exit_code=2,
            )
        self._request_planner = EvidenceRequestPlanner(
            max_input_tokens=config.max_input_tokens,
            image_token_reserve=self._profile.image_token_reserve,
            message_overhead_tokens=self._profile.message_overhead_tokens,
            provider=config.provider,
            model=config.model,
        )
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep
        self._cooldown_path = cooldown_path

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleLlm:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def chat(self, messages: list[dict[str, Any]]) -> ModelCallResult:
        self._request_planner.require_fit(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        payload.update(
            self._profile.request_options(
                enable_thinking=self.config.enable_thinking,
            )
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        retryable_statuses = {429, 503, 504}
        last_error: Exception | None = None
        provider_attempts: list[dict[str, Any]] = []
        for attempt in range(self.config.max_retries + 1):
            self._wait_for_shared_cooldown()
            try:
                response = self._client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                provider_attempts.append(
                    self._http_attempt_record(response, attempt + 1)
                )
                if response.status_code in retryable_statuses:
                    delay = self._retry_delay(response, attempt)
                    if response.status_code == 429:
                        self._publish_shared_cooldown(delay)
                    if attempt >= self.config.max_retries:
                        raise self._provider_error(response)
                    self._sleep(delay)
                    continue
                if response.is_error:
                    raise self._provider_error(response)
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                usage = body.get("usage") or {}
                total_tokens = usage.get("total_tokens")
                if (
                    isinstance(total_tokens, int)
                    and total_tokens > self.config.context_window_tokens
                ):
                    context_error = SemvideoError(
                        code="provider_context_usage_invalid",
                        category=ErrorCategory.MODEL_RESPONSE_INVALID,
                        message="模型报告的 token 用量超过已配置上下文上限。",
                        recovery=RecoveryAction.REPORT_BUG,
                        stage="analyze",
                        details={
                            "provider": self.config.provider,
                            "model": self.config.model,
                            "reported_total_tokens": total_tokens,
                            "context_window_tokens": (
                                self.config.context_window_tokens
                            ),
                        },
                        exit_code=5,
                    )
                    setattr(
                        context_error,
                        "provider_attempts",
                        provider_attempts,
                    )
                    raise context_error
                response_headers = self._response_headers(response)
                return ModelCallResult(
                    content=str(content),
                    usage=ModelUsage(
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        total_tokens=total_tokens,
                    ),
                    trace_id=self._trace_id(response_headers),
                    response_headers=response_headers,
                    raw_response=body,
                    provider_attempts=provider_attempts,
                )
            except SemvideoError as error:
                error.provider_attempts = provider_attempts
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                provider_attempts.append(
                    {
                        "attempt": attempt + 1,
                        "outcome": "network_error",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:2000],
                    }
                )
                if attempt >= self.config.max_retries:
                    break
                delay = min(60.0, 2**attempt) + random.uniform(0.0, 0.75)
                self._sleep(delay)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                error = SemvideoError(
                    code="provider_response_shape_invalid",
                    category=ErrorCategory.MODEL_RESPONSE_INVALID,
                    message="模型提供商返回了无法识别的响应结构。",
                    retryable=False,
                    recovery=RecoveryAction.REPORT_BUG,
                    stage="analyze",
                    details={"reason": str(exc)},
                    exit_code=5,
                )
                error.provider_attempts = provider_attempts
                raise error from exc
        error = SemvideoError(
            code="provider_network_failed",
            category=ErrorCategory.PROVIDER_TRANSIENT,
            message="模型请求在有界重试后仍然失败。",
            retryable=True,
            recovery=RecoveryAction.RETRY_SAME,
            stage="analyze",
            details={"reason": str(last_error) if last_error else "unknown"},
            exit_code=5,
        )
        error.provider_attempts = provider_attempts
        raise error

    def _http_attempt_record(
        self,
        response: httpx.Response,
        attempt: int,
    ) -> dict[str, Any]:
        try:
            raw_response: Any = response.json()
        except ValueError:
            raw_response = {"text": response.text[:2000]}
        headers = self._response_headers(response)
        record: dict[str, Any] = {
            "attempt": attempt,
            "outcome": "success" if not response.is_error else "http_error",
            "status_code": response.status_code,
            "trace_id": self._trace_id(headers),
            "response_headers": headers,
            "raw_response": raw_response,
        }
        if isinstance(raw_response, dict):
            usage = raw_response.get("usage") or {}
            record["usage"] = {
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
            try:
                record["content"] = str(
                    raw_response["choices"][0]["message"]["content"]
                )
            except (KeyError, IndexError, TypeError):
                pass
        return record

    def _response_headers(self, response: httpx.Response) -> dict[str, str]:
        allowed = {*self._profile.trace_header_names, "retry-after"}
        return {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in allowed
        }

    def _trace_id(self, headers: dict[str, str]) -> str | None:
        for name in self._profile.trace_header_names:
            if headers.get(name):
                return headers[name]
        return None

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, min(300.0, float(retry_after)))
            except ValueError:
                pass
        return min(60.0, 2**attempt) + random.uniform(0.0, 0.75)

    def _wait_for_shared_cooldown(self) -> None:
        while self._cooldown_path is not None and self._cooldown_path.is_file():
            try:
                value = read_json(self._cooldown_path)
                remaining = float(value.get("cooldown_until_epoch", 0.0)) - time.time()
            except (OSError, ValueError, TypeError):
                return
            if remaining <= 0:
                return
            self._sleep(min(remaining, 300.0))

    def _publish_shared_cooldown(self, delay: float) -> None:
        if self._cooldown_path is None:
            return
        try:
            with exclusive_file_lock(self._cooldown_path.with_suffix(".lock")):
                until = time.time() + delay
                try:
                    if self._cooldown_path.is_file():
                        current = read_json(self._cooldown_path)
                        until = max(
                            until,
                            float(current.get("cooldown_until_epoch", 0.0)),
                        )
                except (OSError, ValueError, TypeError):
                    pass
                atomic_write_json(
                    self._cooldown_path,
                    {
                        "schema_version": 1,
                        "provider": self.config.provider,
                        "model": self.config.model,
                        "cooldown_until_epoch": until,
                        "written_by_pid": os.getpid(),
                    },
                )
        except LockAcquisitionTimeout as exc:
            raise SemvideoError(
                code="provider_cooldown_lock_timeout",
                category=ErrorCategory.PROVIDER_TRANSIENT,
                message="等待模型提供商共享限流锁超时。",
                retryable=True,
                recovery=RecoveryAction.RETRY_SAME,
                stage="analyze",
                details={
                    "provider": self.config.provider,
                    "model": self.config.model,
                },
                exit_code=5,
            ) from exc

    def segment(
        self, messages: list[dict[str, Any]]
    ) -> tuple[SegmentationResponse, ModelCallResult]:
        result = self.chat(messages)
        try:
            parsed = parse_json_content(result.content)
            return SegmentationResponse.model_validate(parsed), result
        except (json.JSONDecodeError, ValidationError) as exc:
            raise SemvideoError(
                code="model_segmentation_invalid",
                category=ErrorCategory.MODEL_RESPONSE_INVALID,
                message="模型分段输出未通过结构验证。",
                retryable=True,
                recovery=RecoveryAction.RETRY_SAME,
                stage="analyze",
                details={"reason": str(exc)},
                exit_code=5,
            ) from exc

    def _provider_error(self, response: httpx.Response) -> SemvideoError:
        status = response.status_code
        try:
            body = response.json()
            detail = str(body)[:2000]
        except ValueError:
            detail = response.text[:2000]
        if status in {429, 503, 504}:
            return SemvideoError(
                code=f"provider_http_{status}",
                category=ErrorCategory.PROVIDER_TRANSIENT,
                message=f"模型提供商暂时不可用或触发限流（HTTP {status}）。",
                retryable=True,
                recovery=RecoveryAction.RETRY_SAME,
                stage="analyze",
                details={
                    "status_code": status,
                    "provider_detail": detail,
                    "trace_id": self._trace_id(self._response_headers(response)),
                },
                exit_code=5,
            )
        category = (
            ErrorCategory.CREDENTIAL if status in {401, 403} else ErrorCategory.INPUT
        )
        return SemvideoError(
            code=f"provider_http_{status}",
            category=category,
            message=f"模型提供商拒绝了请求（HTTP {status}）。",
            retryable=False,
            recovery=RecoveryAction.USER_ACTION,
            stage="analyze",
            details={
                "status_code": status,
                "provider_detail": detail,
                "trace_id": self._trace_id(self._response_headers(response)),
            },
            exit_code=5,
        )
