"""Multimodal semantic segmentation models and prompt construction."""

from .models import SemanticSegment, SegmentationResponse
from .proposals import (
    build_segmentation_proposal,
    response_from_proposal,
    validate_response_timeline,
)

__all__ = [
    "SemanticSegment",
    "SegmentationResponse",
    "build_segmentation_proposal",
    "response_from_proposal",
    "validate_response_timeline",
]
