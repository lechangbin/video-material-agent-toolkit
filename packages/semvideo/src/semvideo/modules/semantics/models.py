from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SemanticSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    short_summary: str = Field(min_length=1)
    detailed_summary: str = Field(min_length=1)
    visual_summary: str = ""
    event: str = Field(min_length=1)
    start_anchor_id: str
    end_anchor_id: str
    reason: str = Field(min_length=1)
    topics: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator(
        "topics",
        "participants",
        "locations",
        "organizations",
        "objects",
        "actions",
        "keywords",
    )
    @classmethod
    def clean_strings(cls, values: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw).strip()
            if value and value not in seen:
                seen.add(value)
                output.append(value)
        return output


class SegmentationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[SemanticSegment] = Field(min_length=1)


class ModelUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelCallResult(BaseModel):
    content: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    trace_id: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    raw_response: dict[str, Any]
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list)
