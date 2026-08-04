"""Deterministic validation of model-produced cinematography annotations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .models import CinematographyResponse, ShotTimeline


def validate_cinematography_response(
    response: CinematographyResponse,
    timeline: ShotTimeline,
    *,
    evidence_frame_ids: Mapping[str, Sequence[str]],
    required_shot_ids: Sequence[str] | None = None,
) -> None:
    shot_by_id = {shot.shot_id: shot for shot in timeline.shots}
    annotation_by_id = {
        annotation.shot_id: annotation for annotation in response.annotations
    }
    if len(annotation_by_id) != len(response.annotations):
        raise ValueError("cinematography annotations must have unique shot IDs")
    required = (
        set(required_shot_ids)
        if required_shot_ids is not None
        else set(shot_by_id)
    )
    if not required.issubset(shot_by_id):
        raise ValueError("required shot IDs must exist in the shot timeline")
    if set(annotation_by_id) != required:
        missing = sorted(required - set(annotation_by_id))
        unknown = sorted(set(annotation_by_id) - required)
        raise ValueError(
            f"cinematography annotations must cover every shot; "
            f"missing={missing}, unknown={unknown}"
        )
    for shot_id, annotation in annotation_by_id.items():
        shot = shot_by_id[shot_id]
        allowed_frames = set(evidence_frame_ids.get(shot_id) or ())
        if not set(annotation.evidence_frame_ids).issubset(allowed_frames):
            raise ValueError(
                f"cinematography annotation {shot_id} references unknown evidence"
            )
        for motion in annotation.camera_motions:
            if motion.start_ms < shot.start_ms or motion.end_ms > shot.end_ms:
                raise ValueError(
                    f"camera motion for {shot_id} exceeds the shot range"
                )
