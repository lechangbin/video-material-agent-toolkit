from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from semvideo.infrastructure.io import atomic_write_bytes


class TranscriptSlice(BaseModel):
    text: str = ""
    span_ids: list[str] = Field(default_factory=list)
    language: str | None = None
    source: str | None = None
    quality: str = "unavailable"
    clipped_span_ids: list[str] = Field(default_factory=list)


class ArtifactRefs(BaseModel):
    video: str | None = None
    thumbnail: str | None = None
    contact_sheets: list[str] = Field(default_factory=list)


class RetrievalProvenance(BaseModel):
    profile: str
    profile_version: int = 1
    search_text_version: str = "retrieval-text-v1"
    summary_model_run_id: str | None = None
    merge_plan_hash: str


class RetrievalSegmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    segment_id: str
    job_id: str
    source_video_id: str
    ordinal: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    title: str
    short_summary: str
    detailed_summary: str
    visual_summary: str = ""
    topics: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    transcript: TranscriptSlice = Field(default_factory=TranscriptSlice)
    search_text: str
    confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    artifacts: ArtifactRefs = Field(default_factory=ArtifactRefs)
    provenance: RetrievalProvenance

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value != 1:
            raise ValueError(
                f"unsupported schema_version {value!r}; supported version is 1"
            )
        return value

    @model_validator(mode="after")
    def validate_time_range(self) -> "RetrievalSegmentRecord":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        if self.duration_ms != self.end_ms - self.start_ms:
            raise ValueError("duration_ms must equal end_ms - start_ms")
        return self

    def compact_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "title": self.title,
            "short_summary": self.short_summary,
            "topics": self.topics,
            "keywords": self.keywords,
            "confidence": self.confidence,
            "review_required": self.review_required,
            "transcript_quality": self.transcript.quality,
        }


def _span_time_ms(span: dict[str, Any], key: str) -> int:
    millisecond_key = f"{key}_ms"
    if millisecond_key in span:
        return int(span[millisecond_key])
    return round(float(span.get(key, 0.0)) * 1000)


def transcript_for_range(
    spans: list[dict[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
) -> TranscriptSlice:
    selected: list[dict[str, Any]] = []
    clipped: list[str] = []
    for index, span in enumerate(spans):
        span_start = _span_time_ms(span, "start")
        span_end = _span_time_ms(span, "end")
        if span_end <= start_ms or span_start >= end_ms:
            continue
        span_id = str(span.get("transcript_span_id") or f"transcript_{index:04d}")
        selected.append({**span, "_id": span_id})
        if span_start < start_ms or span_end > end_ms:
            clipped.append(span_id)
    if not selected:
        return TranscriptSlice()
    source_values = {str(row.get("source") or "unknown") for row in selected}
    quality_values = {
        str(row.get("quality") or row.get("confidence_label") or "available")
        for row in selected
    }
    languages = {str(row.get("language")) for row in selected if row.get("language")}
    return TranscriptSlice(
        text=" ".join(str(row.get("text") or "").strip() for row in selected).strip(),
        span_ids=[str(row["_id"]) for row in selected],
        language=next(iter(languages)) if len(languages) == 1 else None,
        source=next(iter(source_values)) if len(source_values) == 1 else "mixed",
        quality=next(iter(quality_values)) if len(quality_values) == 1 else "mixed",
        clipped_span_ids=clipped,
    )


def _unique(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def build_record(
    *,
    job_id: str,
    source_video_id: str,
    final_segment: dict[str, Any],
    summary: dict[str, Any],
    transcript_spans: list[dict[str, Any]],
    profile: str,
    profile_version: int,
    merge_plan_hash: str,
    artifacts: dict[str, Any] | None = None,
) -> RetrievalSegmentRecord:
    start_ms = int(final_segment["start_ms"])
    end_ms = int(final_segment["end_ms"])
    transcript = transcript_for_range(
        transcript_spans,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    semantic_parts: list[str] = [
        str(summary.get("title") or ""),
        str(summary.get("short_summary") or ""),
        str(summary.get("detailed_summary") or ""),
        str(summary.get("visual_summary") or ""),
    ]
    for key in (
        "topics",
        "participants",
        "locations",
        "organizations",
        "objects",
        "actions",
        "keywords",
    ):
        semantic_parts.extend(_unique(list(summary.get(key) or [])))
    if transcript.text:
        semantic_parts.append(transcript.text)
    search_text = "\n".join(part for part in _unique(semantic_parts) if part)
    review_reasons = _unique(
        list(summary.get("review_reasons") or [])
        + list(final_segment.get("review_reasons") or [])
    )
    return RetrievalSegmentRecord(
        segment_id=str(
            final_segment.get("final_segment_id")
            or final_segment.get("segment_id")
        ),
        job_id=job_id,
        source_video_id=source_video_id,
        ordinal=int(final_segment["ordinal"]),
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=end_ms - start_ms,
        title=str(summary.get("title") or "视频片段"),
        short_summary=str(summary.get("short_summary") or ""),
        detailed_summary=str(summary.get("detailed_summary") or ""),
        visual_summary=str(summary.get("visual_summary") or ""),
        topics=_unique(list(summary.get("topics") or [])),
        participants=_unique(list(summary.get("participants") or [])),
        locations=_unique(list(summary.get("locations") or [])),
        organizations=_unique(list(summary.get("organizations") or [])),
        objects=_unique(list(summary.get("objects") or [])),
        actions=_unique(list(summary.get("actions") or [])),
        keywords=_unique(list(summary.get("keywords") or [])),
        transcript=transcript,
        search_text=search_text,
        confidence=float(summary.get("confidence") or 0.0),
        review_required=bool(
            review_reasons or final_segment.get("review_boundary_ids")
        ),
        review_reasons=review_reasons,
        artifacts=ArtifactRefs.model_validate(artifacts or {}),
        provenance=RetrievalProvenance(
            profile=profile,
            profile_version=profile_version,
            summary_model_run_id=summary.get("model_run_id"),
            merge_plan_hash=merge_plan_hash,
        ),
    )


def write_records(path: Path, records: list[RetrievalSegmentRecord]) -> None:
    content = "".join(
        record.model_dump_json(exclude_none=False) + "\n" for record in records
    )
    atomic_write_bytes(path, content.encode("utf-8"))


def read_records(path: Path) -> list[RetrievalSegmentRecord]:
    if not path.is_file():
        return []
    output: list[RetrievalSegmentRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            output.append(RetrievalSegmentRecord.model_validate_json(line))
    return output


def list_records(
    path: Path,
    *,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    records = read_records(path)
    selected = records[offset : offset + limit]
    return {
        "schema_version": 1,
        "total": len(records),
        "offset": offset,
        "limit": limit,
        "items": [record.compact_dict() for record in selected],
    }
