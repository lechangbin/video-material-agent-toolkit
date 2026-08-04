"""Stable domain values for shot facts and cinematography language."""

from __future__ import annotations

from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Viewpoint = Literal[
    "aerial",
    "ground",
    "interior",
    "overhead",
    "low_angle",
    "high_angle",
    "eye_level",
    "unknown",
]
ShotScaleValue = Literal[
    "extreme_wide",
    "wide",
    "medium",
    "close_up",
    "extreme_close_up",
    "unknown",
]
MotionType = Literal[
    "static",
    "pan",
    "tilt",
    "push_in",
    "pull_out",
    "rise",
    "fall",
    "tracking",
    "orbit",
    "handheld",
    "compound",
    "unknown",
]
MotionDirection = Literal[
    "left",
    "right",
    "up",
    "down",
    "forward",
    "backward",
    "clockwise",
    "counterclockwise",
    "mixed",
    "none",
    "unknown",
]
MotionSpeed = Literal["still", "slow", "moderate", "fast", "variable", "unknown"]
TemporalProfile = Literal[
    "constant",
    "gradual",
    "accelerating",
    "decelerating",
    "variable",
    "unknown",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShotBoundary(StrictModel):
    timestamp_ms: int = Field(gt=0)
    kind: Literal["hard_cut_candidate", "gradual_transition_candidate", "unknown"]
    score: float = Field(ge=0.0, le=1.0)
    detector: str = Field(min_length=1)
    source_candidate_boundary_id: str = Field(min_length=1)


class ShotRecord(StrictModel):
    shot_id: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    right_boundary: ShotBoundary | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "ShotRecord":
        if self.end_ms <= self.start_ms:
            raise ValueError("shot end_ms must be greater than start_ms")
        if (
            self.right_boundary is not None
            and self.right_boundary.timestamp_ms != self.end_ms
        ):
            raise ValueError("shot right boundary must equal shot end_ms")
        return self


class ShotTimeline(StrictModel):
    schema_version: Literal[1] = 1
    source_video_id: str = Field(min_length=1)
    duration_ms: int = Field(gt=0)
    detector: dict[str, object]
    shots: list[ShotRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "ShotTimeline":
        if self.shots[0].start_ms != 0:
            raise ValueError("shot timeline must start at zero")
        if self.shots[-1].end_ms != self.duration_ms:
            raise ValueError("shot timeline must end at duration_ms")
        if len({shot.shot_id for shot in self.shots}) != len(self.shots):
            raise ValueError("shot IDs must be unique")
        for index, shot in enumerate(self.shots):
            if shot.ordinal != index:
                raise ValueError("shot ordinals must be contiguous")
            if index and self.shots[index - 1].end_ms != shot.start_ms:
                raise ValueError("shots must continuously cover the timeline")
        return self


class ShotScale(StrictModel):
    start: ShotScaleValue
    end: ShotScaleValue


class CameraMotion(StrictModel):
    type: MotionType
    direction: MotionDirection = "unknown"
    speed: MotionSpeed = "unknown"
    temporal_profile: TemporalProfile = "unknown"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_range(self) -> "CameraMotion":
        if self.end_ms <= self.start_ms:
            raise ValueError("camera motion end_ms must be greater than start_ms")
        return self


class CinematographyAnnotation(StrictModel):
    shot_id: str = Field(min_length=1)
    viewpoints: list[Viewpoint] = Field(min_length=1)
    shot_scale: ShotScale
    camera_motions: list[CameraMotion] = Field(min_length=1)
    cinematography_summary: str = Field(min_length=1)
    cinematography_keywords: list[str] = Field(min_length=1)
    evidence_frame_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class CinematographyResponse(StrictModel):
    annotations: list[CinematographyAnnotation] = Field(min_length=1)


class ShotEvidenceFrame(StrictModel):
    frame_id: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    timestamp_ms: int = Field(ge=0)
    path: str = Field(min_length=1)


class GlobalMotionMeasurement(StrictModel):
    shot_id: str = Field(min_length=1)
    sample_pairs: int = Field(ge=0)
    translation_x_ratio: float
    translation_y_ratio: float
    scale_change_ratio: float
    mean_frame_change: float = Field(ge=0.0, le=1.0)
    fit_improvement: float = Field(ge=0.0, le=1.0)
    foreground_suppression: Literal[
        "none_v0",
        "tile_median_v1",
    ] = "none_v0"
    speed_curve: list[float] = Field(default_factory=list)
    temporal_profile_hint: TemporalProfile = "unknown"
    direction_change_count: int = Field(default=0, ge=0)


class ShotEvidenceBundle(StrictModel):
    shot_id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    frames: list[ShotEvidenceFrame] = Field(min_length=1)
    contact_sheet_path: str = Field(min_length=1)
    contact_sheet_paths: list[str] = Field(default_factory=list)
    motion_measurement: GlobalMotionMeasurement

    @model_validator(mode="after")
    def validate_contact_sheets(self) -> "ShotEvidenceBundle":
        if not self.contact_sheet_paths:
            self.contact_sheet_paths = [self.contact_sheet_path]
        if self.contact_sheet_paths[0] != self.contact_sheet_path:
            raise ValueError(
                "contact_sheet_path must be the first contact_sheet_paths entry"
            )
        if len(set(self.contact_sheet_paths)) != len(self.contact_sheet_paths):
            raise ValueError("contact sheet paths must be unique")
        expected_sheet_count = ceil(len(self.frames) / 9)
        if len(self.contact_sheet_paths) != expected_sheet_count:
            raise ValueError(
                "contact sheet paths must cover every group of up to nine frames"
            )
        return self


class CinematographyEvidence(StrictModel):
    schema_version: Literal[1] = 1
    source_video_id: str = Field(min_length=1)
    shots: list[ShotEvidenceBundle] = Field(min_length=1)
