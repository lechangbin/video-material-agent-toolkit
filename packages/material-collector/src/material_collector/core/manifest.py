"""Versioned external projection for every discovered source."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from material_collector.core.media import AssetRecord, Platform


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryLink(_ManifestModel):
    query_plan_id: str
    segment_id: str
    query_id: str
    round_number: int
    rank: int


class ReviewRecord(_ManifestModel):
    decision: Literal["approved", "rejected"]
    actor: str
    decided_at: str


class WorkGroupMember(_ManifestModel):
    """One ordered source in a locally fingerprint-confirmed work group."""

    schema_version: Literal["1.0"] = "1.0"
    media_unit_id: str
    platform: Platform
    role: Literal["primary", "fallback"]
    fallback_order: int = Field(ge=1)


class WorkGroupRecord(_ManifestModel):
    """Versioned cross-platform work-group decision."""

    schema_version: Literal["1.0"] = "1.0"
    work_group_id: str
    status: Literal[
        "confirmed_duplicate",
        "independent",
        "fingerprint_unavailable",
    ]
    primary_media_unit_id: str
    members: tuple[WorkGroupMember, ...]

    @model_validator(mode="after")
    def validate_primary(self) -> WorkGroupRecord:
        primary_members = tuple(
            member for member in self.members if member.role == "primary"
        )
        if len(primary_members) != 1:
            raise ValueError("A work group must contain exactly one primary member.")
        if primary_members[0].media_unit_id != self.primary_media_unit_id:
            raise ValueError(
                "The primary member must match primary_media_unit_id."
            )
        member_ids = [member.media_unit_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("A work group cannot contain duplicate media units.")
        orders = [member.fallback_order for member in self.members]
        if len(orders) != len(set(orders)):
            raise ValueError("Work-group fallback_order values must be unique.")
        return self


class ManifestMediaUnit(_ManifestModel):
    media_unit_id: str
    title: str
    canonical_url: str
    duration_seconds: float | None
    part_index: int | None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    review: ReviewRecord | None
    proxy_asset: AssetRecord | None
    high_quality_asset: AssetRecord | None
    work_group_id: str | None = None
    source_role: Literal["primary", "fallback"] | None = None
    fallback_order: int | None = None
    eligible_for_understanding: bool = True


class ManifestCandidate(_ManifestModel):
    candidate_id: str
    platform: Platform
    source_id: str
    canonical_url: str
    title: str
    author: str | None
    description: str | None
    published_at: str | None
    duration_seconds: float | None
    discoveries: tuple[DiscoveryLink, ...]
    media_units: tuple[ManifestMediaUnit, ...]


class CollectionResult(_ManifestModel):
    schema_version: Literal["2.0"] = "2.0"
    session_id: str
    workspace_path: str
    platform_scope: tuple[Platform, ...]
    session_status: str
    state_version: int
    generated_at: str
    candidates: tuple[ManifestCandidate, ...]
    work_groups: tuple[WorkGroupRecord, ...] = ()
