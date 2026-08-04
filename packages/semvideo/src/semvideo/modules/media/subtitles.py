"""Embedded text subtitle extraction and SRT parsing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from semvideo.adapters.ffmpeg import FfmpegAdapter, MediaFacts, SubtitleStreamFacts


@dataclass(frozen=True, slots=True)
class TranscriptSpan:
    transcript_span_id: str
    start_ms: int
    end_ms: int
    text: str
    source: str
    confidence: float | None = None
    language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_TIMING_RE = re.compile(
    r"(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{3})"
)
_TAG_RE = re.compile(r"<[^>]+>")


def _timestamp_ms(value: str) -> int:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return round(
        (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
    )


def parse_srt(
    text: str,
    *,
    source: str = "embedded_subtitle",
    language: str | None = None,
) -> list[TranscriptSpan]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    spans: list[TranscriptSpan] = []
    for block in re.split(r"\n{2,}", normalized):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if _TIMING_RE.search(line)),
            None,
        )
        if timing_index is None:
            continue
        match = _TIMING_RE.search(lines[timing_index])
        assert match is not None
        content = " ".join(lines[timing_index + 1 :]).strip()
        content = _TAG_RE.sub("", content)
        if not content:
            continue
        spans.append(
            TranscriptSpan(
                transcript_span_id=f"transcript_{len(spans) + 1:04d}",
                start_ms=_timestamp_ms(match.group("start")),
                end_ms=_timestamp_ms(match.group("end")),
                text=content,
                source=source,
                confidence=None,
                language=language,
            )
        )
    return spans


def choose_text_subtitle(
    streams: Iterable[SubtitleStreamFacts],
    *,
    preferred_languages: tuple[str, ...] = ("zh", "zho", "chi", "en", "eng"),
) -> SubtitleStreamFacts | None:
    candidates = [stream for stream in streams if stream.is_text]
    if not candidates:
        return None
    language_rank = {
        language.lower(): index for index, language in enumerate(preferred_languages)
    }
    return min(
        candidates,
        key=lambda stream: (
            language_rank.get((stream.language or "").lower(), len(language_rank)),
            stream.index,
        ),
    )


def extract_embedded_subtitles(
    source: Path,
    output_dir: Path,
    *,
    ffmpeg: FfmpegAdapter | None = None,
    media_facts: MediaFacts | None = None,
    preferred_languages: tuple[str, ...] = ("zh", "zho", "chi", "en", "eng"),
) -> list[TranscriptSpan]:
    adapter = ffmpeg or FfmpegAdapter()
    facts = media_facts or adapter.probe(source)
    selected = choose_text_subtitle(
        facts.subtitle_streams,
        preferred_languages=preferred_languages,
    )
    if selected is None:
        return []
    subtitle_path = output_dir / f"embedded_{selected.index:02d}.srt"
    adapter.extract_subtitle(
        source,
        stream_index=selected.index,
        output=subtitle_path,
    )
    return parse_srt(
        subtitle_path.read_text(encoding="utf-8-sig"),
        language=selected.language,
    )
