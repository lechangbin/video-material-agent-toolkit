"""Application-facing native evidence orchestration."""

from __future__ import annotations

from pathlib import Path

from semvideo.adapters.ffmpeg import FfmpegAdapter, MediaFacts
from semvideo.modules.media.models import CandidateTimeline
from semvideo.modules.media.subtitles import (
    TranscriptSpan,
    extract_embedded_subtitles,
)

from .asr import FasterWhisperTranscriber
from .contact_sheets import ContactSheetPolicy, create_contact_sheets
from .frames import EvidencePolicy, extract_evidence_frames
from .models import EvidenceTimeline


class NativeEvidenceAdapter:
    """Produce local visual and transcript evidence without CRV."""

    def __init__(
        self,
        *,
        ffmpeg: FfmpegAdapter | None = None,
        transcriber: FasterWhisperTranscriber | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg or FfmpegAdapter()
        self.transcriber = transcriber

    def extract(
        self,
        source: Path,
        candidate_timeline: CandidateTimeline,
        output_dir: Path,
        *,
        media_facts: MediaFacts | None = None,
        transcript_source: Path | None = None,
        transcript_media_facts: MediaFacts | None = None,
        evidence_policy: EvidencePolicy | None = None,
        contact_sheet_policy: ContactSheetPolicy | None = None,
        transcribe_if_no_subtitles: bool = True,
    ) -> EvidenceTimeline:
        output_dir.mkdir(parents=True, exist_ok=True)
        facts = media_facts or self.ffmpeg.probe(source)
        frames, _discarded = extract_evidence_frames(
            source,
            output_dir,
            candidate_timeline,
            ffmpeg=self.ffmpeg,
            policy=evidence_policy,
        )
        sheets = create_contact_sheets(
            frames,
            evidence_dir=output_dir,
            policy=contact_sheet_policy,
        )
        subtitle_source = transcript_source or source
        subtitle_facts = transcript_media_facts
        if subtitle_facts is None:
            subtitle_facts = (
                facts
                if subtitle_source == source
                else self.ffmpeg.probe(subtitle_source)
            )
        transcript_spans: list[TranscriptSpan] = extract_embedded_subtitles(
            subtitle_source,
            output_dir / "subtitles",
            ffmpeg=self.ffmpeg,
            media_facts=subtitle_facts,
        )
        if (
            not transcript_spans
            and transcribe_if_no_subtitles
            and self.transcriber is not None
        ):
            transcript_spans = self.transcriber.transcribe(subtitle_source)
        return EvidenceTimeline(
            schema_version=1,
            source_video_id=candidate_timeline.source_video_id,
            frames=tuple(frames),
            contact_sheets=tuple(sheets),
            transcript_spans=tuple(transcript_spans),
        )
