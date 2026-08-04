"""Core domain types for the Semvideo timeline and merge plan."""

from .decisions import (
    BoundaryDecision,
    BoundaryDecisionKind,
    BoundaryRelationship,
)
from .timeline import (
    CandidateBoundary,
    CandidateSegment,
    CandidateTimeline,
    DomainValidationError,
    Segment,
)

__all__ = [
    "BoundaryDecision",
    "BoundaryDecisionKind",
    "BoundaryRelationship",
    "CandidateBoundary",
    "CandidateSegment",
    "CandidateTimeline",
    "DomainValidationError",
    "Segment",
]
