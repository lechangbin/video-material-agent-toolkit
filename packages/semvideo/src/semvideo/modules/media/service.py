"""Application-facing native media analysis facade."""

from __future__ import annotations

from pathlib import Path

from semvideo.adapters.ffmpeg import FfmpegAdapter, MediaFacts

from .candidates import CandidatePolicy, build_candidate_timeline
from .models import CandidateTimeline


class MediaAnalyzer:
    def __init__(self, ffmpeg: FfmpegAdapter | None = None) -> None:
        self.ffmpeg = ffmpeg or FfmpegAdapter()

    def probe(self, source: Path) -> MediaFacts:
        return self.ffmpeg.probe(source)

    def segment(
        self,
        source: Path,
        *,
        source_video_id: str,
        media_facts: MediaFacts | None = None,
        policy: CandidatePolicy | None = None,
    ) -> CandidateTimeline:
        policy = policy or CandidatePolicy()
        facts = media_facts or self.probe(source)
        scenes = self.ffmpeg.detect_scenes(source, threshold=policy.scene_threshold)
        return build_candidate_timeline(
            source_video_id=source_video_id,
            duration_ms=facts.duration_ms,
            scene_points=scenes,
            policy=policy,
        )
