"""Native, CRV-independent video evidence extraction."""

from .asr import FasterWhisperConfig, FasterWhisperTranscriber
from .contact_sheets import ContactSheetPolicy, create_contact_sheets
from .frames import EvidencePolicy, extract_evidence_frames, near_duplicate_filter
from .models import ContactSheet, EvidenceFrame, EvidenceTimeline
from .service import NativeEvidenceAdapter

__all__ = [
    "ContactSheet",
    "ContactSheetPolicy",
    "EvidenceFrame",
    "EvidencePolicy",
    "EvidenceTimeline",
    "FasterWhisperConfig",
    "FasterWhisperTranscriber",
    "NativeEvidenceAdapter",
    "create_contact_sheets",
    "extract_evidence_frames",
    "near_duplicate_filter",
]
