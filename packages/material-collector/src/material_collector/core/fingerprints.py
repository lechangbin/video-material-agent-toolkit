"""Stable results returned by local cross-platform fingerprint comparison."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FingerprintMatch(BaseModel):
    """Conservative pairwise evidence; only reliable matches may be merged."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    same_work: bool
    reliable: bool
    audio_similarity: float | None = Field(default=None, ge=0, le=1)
    video_similarity: float | None = Field(default=None, ge=0, le=1)
    duration_difference_seconds: float | None = Field(default=None, ge=0)
    reason: str
