"""Versioned public contracts for generic multimodal subagent handoff."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextTier(StrEnum):
    K128 = "128k"
    K256 = "256k"
    K512 = "512k"
    M1 = "1m"

    @classmethod
    def from_tokens(cls, tokens: int) -> ContextTier:
        if tokens < 128_000:
            raise ValueError("subagent_context_below_minimum")
        if tokens < 256_000:
            return cls.K128
        if tokens < 512_000:
            return cls.K256
        if tokens < 1_000_000:
            return cls.K512
        return cls.M1


class EvidenceReference(_Contract):
    evidence_id: str
    kind: Literal["frame", "contact_sheet", "transcript", "analysis_proxy"]
    relative_path: str | None = None
    local_path: str | None = None
    sha256: str | None = None
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class CinematographyReference(_Contract):
    annotation_id: str
    shot_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    relative_path: str
    sha256: str

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class TimelineAnchorReference(_Contract):
    anchor_id: str
    timestamp_seconds: float = Field(ge=0)


class CrvProvenance(_Contract):
    runtime_version: str
    evidence_profile: str
    package_hash: str
    evidence_hash: str

    @field_validator("package_hash", "evidence_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value


class VideoUnderstandingAgentRequest(_Contract):
    schema_version: Literal["video-understanding-agent-request/v1"] = (
        "video-understanding-agent-request/v1"
    )
    request_id: str
    job_id: str
    source_video_id: str
    context_root: str
    source_sha256: str
    duration_seconds: float = Field(gt=0)
    context_tier: ContextTier
    context_hash: str
    crv: CrvProvenance
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    cinematography: tuple[CinematographyReference, ...] = ()
    anchors: tuple[TimelineAnchorReference, ...] = Field(min_length=2)
    observer_window_count: int = Field(ge=1)
    agent_attempt_id: str
    prior_attempt_id: str | None = None

    @field_validator("source_sha256", "context_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("context_root")
    @classmethod
    def validate_context_root(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("context_root must be an absolute local path")
        return value

    @model_validator(mode="after")
    def validate_unique_references(self) -> VideoUnderstandingAgentRequest:
        ids = [item.evidence_id for item in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence references must be unique")
        return self

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class AgentSemanticSegment(_Contract):
    segment_id: str
    start_anchor_id: str
    end_anchor_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    summary: str = Field(min_length=1)
    entities: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    ambiguities: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_range(self) -> AgentSemanticSegment:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("segment end must be greater than start")
        return self


class VideoUnderstandingAgentResult(_Contract):
    schema_version: Literal["video-understanding-agent-result/v1"] = (
        "video-understanding-agent-result/v1"
    )
    request_id: str
    request_hash: str
    context_hash: str
    agent_attempt_id: str
    segments: tuple[AgentSemanticSegment, ...] = Field(min_length=1)

    @field_validator("request_hash", "context_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value


def validate_result_against_request(
    request: VideoUnderstandingAgentRequest,
    result: VideoUnderstandingAgentResult,
    *,
    tolerance_seconds: float = 0.05,
) -> tuple[str, ...]:
    """Return deterministic validation errors without mutating either artifact."""

    errors: list[str] = []
    if result.request_id != request.request_id:
        errors.append("request_id_mismatch")
    if result.request_hash != request.content_hash():
        errors.append("request_hash_mismatch")
    if result.context_hash != request.context_hash:
        errors.append("context_hash_mismatch")
    if result.agent_attempt_id != request.agent_attempt_id:
        errors.append("agent_attempt_id_mismatch")
    allowed_refs = {item.evidence_id for item in request.evidence} | {
        item.annotation_id for item in request.cinematography
    }
    anchor_by_id = {item.anchor_id: item.timestamp_seconds for item in request.anchors}
    previous_end = 0.0
    seen_ids: set[str] = set()
    for segment in result.segments:
        if segment.segment_id in seen_ids:
            errors.append(f"duplicate_segment_id:{segment.segment_id}")
        seen_ids.add(segment.segment_id)
        if abs(segment.start_seconds - previous_end) > tolerance_seconds:
            errors.append(f"timeline_gap_or_overlap:{segment.segment_id}")
        if segment.start_anchor_id not in anchor_by_id:
            errors.append(f"unknown_start_anchor:{segment.segment_id}")
        elif abs(anchor_by_id[segment.start_anchor_id] - segment.start_seconds) > tolerance_seconds:
            errors.append(f"start_anchor_time_mismatch:{segment.segment_id}")
        if segment.end_anchor_id not in anchor_by_id:
            errors.append(f"unknown_end_anchor:{segment.segment_id}")
        elif abs(anchor_by_id[segment.end_anchor_id] - segment.end_seconds) > tolerance_seconds:
            errors.append(f"end_anchor_time_mismatch:{segment.segment_id}")
        unknown = sorted(set(segment.evidence_refs) - allowed_refs)
        if unknown:
            errors.append(f"unknown_evidence_ref:{segment.segment_id}:{','.join(unknown)}")
        previous_end = segment.end_seconds
    if abs(previous_end - request.duration_seconds) > tolerance_seconds:
        errors.append("timeline_duration_mismatch")
    return tuple(errors)
