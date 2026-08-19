"""Normalized, versioned CRV evidence package contract."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CrvEvidenceFrame(_Contract):
    frame_id: str
    timestamp_seconds: float = Field(ge=0)
    relative_path: str
    sha256: str
    width: int = Field(ge=1)
    height: int = Field(ge=1)

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class CrvTranscriptSpan(_Contract):
    span_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> CrvTranscriptSpan:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("transcript end must be greater than start")
        return self


class GlobalUnderstandingEvidencePackage(_Contract):
    schema_version: Literal["global-understanding-evidence/v1"] = (
        "global-understanding-evidence/v1"
    )
    source_video_id: str
    source_sha256: str
    analysis_proxy_sha256: str
    crv_version: str
    evidence_profile: str
    runtime_package_hash: str
    frames: tuple[CrvEvidenceFrame, ...] = Field(min_length=1)
    transcript: tuple[CrvTranscriptSpan, ...] = ()
    evidence_hash: str

    @field_validator(
        "source_sha256",
        "analysis_proxy_sha256",
        "runtime_package_hash",
        "evidence_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("hash must be 64 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def validate_unique_ordered_frames(self) -> GlobalUnderstandingEvidencePackage:
        ids = [frame.frame_id for frame in self.frames]
        if len(ids) != len(set(ids)):
            raise ValueError("CRV frame identifiers must be unique")
        timestamps = [frame.timestamp_seconds for frame in self.frames]
        if timestamps != sorted(timestamps):
            raise ValueError("CRV frames must be ordered by timestamp")
        return self
