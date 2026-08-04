"""Evidence timeline values kept independent from model-provider payloads."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from semvideo.modules.media.subtitles import TranscriptSpan


@dataclass(frozen=True, slots=True)
class DedupDecision:
    decision: str
    reason: str
    distance: int | None = None
    compared_frame_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidenceFrame:
    evidence_frame_id: str
    timestamp_ms: int
    artifact_id: str
    relative_path: str
    extraction_reasons: tuple[str, ...]
    scene_score: float | None
    dedup: DedupDecision

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["extraction_reasons"] = list(self.extraction_reasons)
        payload["dedup"] = self.dedup.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ContactSheet:
    contact_sheet_id: str
    artifact_id: str
    relative_path: str
    rows: int
    columns: int
    cell_frame_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact_sheet_id": self.contact_sheet_id,
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "layout": {"rows": self.rows, "columns": self.columns},
            "cell_frame_ids": list(self.cell_frame_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceTimeline:
    schema_version: int
    source_video_id: str
    frames: tuple[EvidenceFrame, ...]
    contact_sheets: tuple[ContactSheet, ...]
    transcript_spans: tuple[TranscriptSpan, ...]
    ocr_observations: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        frame_ids = {frame.evidence_frame_id for frame in self.frames}
        timestamps = [frame.timestamp_ms for frame in self.frames]
        if timestamps != sorted(timestamps):
            raise ValueError("evidence frames must be in timestamp order")
        for sheet in self.contact_sheets:
            if any(frame_id not in frame_ids for frame_id in sheet.cell_frame_ids):
                raise ValueError("contact sheet references an unknown evidence frame")
            sheet_timestamps = [
                next(
                    frame.timestamp_ms
                    for frame in self.frames
                    if frame.evidence_frame_id == frame_id
                )
                for frame_id in sheet.cell_frame_ids
            ]
            if sheet_timestamps != sorted(sheet_timestamps):
                raise ValueError("contact sheet cells must be in timestamp order")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_video_id": self.source_video_id,
            "frames": [frame.to_dict() for frame in self.frames],
            "contact_sheets": [sheet.to_dict() for sheet in self.contact_sheets],
            "transcript_spans": [span.to_dict() for span in self.transcript_spans],
            "ocr_observations": list(self.ocr_observations),
        }
