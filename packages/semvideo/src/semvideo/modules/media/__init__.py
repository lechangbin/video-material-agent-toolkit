"""Media probing, candidate segmentation, and embedded subtitle support."""

from .candidates import CandidatePolicy, build_candidate_timeline
from .models import CandidateBoundary, CandidateSegment, CandidateTimeline
from .service import MediaAnalyzer
from .subtitles import TranscriptSpan, extract_embedded_subtitles, parse_srt

__all__ = [
    "CandidateBoundary",
    "CandidatePolicy",
    "CandidateSegment",
    "CandidateTimeline",
    "MediaAnalyzer",
    "TranscriptSpan",
    "build_candidate_timeline",
    "extract_embedded_subtitles",
    "parse_srt",
]
