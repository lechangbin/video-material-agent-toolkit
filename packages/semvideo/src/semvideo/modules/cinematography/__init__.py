"""Shot facts and structured cinematography annotations."""

from .evidence import (
    ShotEvidencePolicy,
    ShotEvidenceTimestamp,
    extract_cinematography_evidence,
    measure_global_motion,
    plan_shot_evidence,
)
from .models import (
    CameraMotion,
    CinematographyAnnotation,
    CinematographyEvidence,
    CinematographyResponse,
    GlobalMotionMeasurement,
    ShotBoundary,
    ShotEvidenceBundle,
    ShotEvidenceFrame,
    ShotRecord,
    ShotScale,
    ShotTimeline,
)
from .timeline import build_shot_timeline
from .validate import validate_cinematography_response
from .prompt import PROMPT_VERSION, cinematography_messages

__all__ = [
    "CameraMotion",
    "CinematographyAnnotation",
    "CinematographyEvidence",
    "CinematographyResponse",
    "GlobalMotionMeasurement",
    "ShotBoundary",
    "ShotEvidencePolicy",
    "ShotEvidenceTimestamp",
    "ShotEvidenceBundle",
    "ShotEvidenceFrame",
    "ShotRecord",
    "ShotScale",
    "ShotTimeline",
    "build_shot_timeline",
    "cinematography_messages",
    "extract_cinematography_evidence",
    "measure_global_motion",
    "plan_shot_evidence",
    "validate_cinematography_response",
    "PROMPT_VERSION",
]
