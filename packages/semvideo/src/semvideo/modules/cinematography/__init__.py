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
from .prompt import PROMPT_VERSION, cinematography_messages
from .timeline import build_shot_timeline
from .validate import validate_cinematography_response

__all__ = [
    "PROMPT_VERSION",
    "CameraMotion",
    "CinematographyAnnotation",
    "CinematographyEvidence",
    "CinematographyResponse",
    "GlobalMotionMeasurement",
    "ShotBoundary",
    "ShotEvidenceBundle",
    "ShotEvidenceFrame",
    "ShotEvidencePolicy",
    "ShotEvidenceTimestamp",
    "ShotRecord",
    "ShotScale",
    "ShotTimeline",
    "build_shot_timeline",
    "cinematography_messages",
    "extract_cinematography_evidence",
    "measure_global_motion",
    "plan_shot_evidence",
    "validate_cinematography_response",
]
