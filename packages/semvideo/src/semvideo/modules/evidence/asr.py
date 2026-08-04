"""Optional faster-whisper adapter with explicit, testable configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from semvideo.modules.media.subtitles import TranscriptSpan


class WhisperModelLike(Protocol):
    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Any, Any]: ...


@dataclass(frozen=True, slots=True)
class FasterWhisperConfig:
    model_name_or_path: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 4
    num_workers: int = 1
    beam_size: int = 5
    language: str | None = None
    vad_filter: bool = True
    download_root: Path | None = None
    local_files_only: bool = False

    def __post_init__(self) -> None:
        if not self.model_name_or_path:
            raise ValueError("model_name_or_path is required")
        if self.cpu_threads < 0 or self.num_workers <= 0 or self.beam_size <= 0:
            raise ValueError("ASR worker and beam values must be positive")


ModelFactory = Callable[[FasterWhisperConfig], WhisperModelLike]


def _default_model_factory(config: FasterWhisperConfig) -> WhisperModelLike:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is required when ASR fallback is enabled"
        ) from exc
    return WhisperModel(
        config.model_name_or_path,
        device=config.device,
        compute_type=config.compute_type,
        cpu_threads=config.cpu_threads,
        num_workers=config.num_workers,
        download_root=str(config.download_root) if config.download_root else None,
        local_files_only=config.local_files_only,
    )


class FasterWhisperTranscriber:
    def __init__(
        self,
        config: FasterWhisperConfig | None = None,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.config = config or FasterWhisperConfig()
        self._model_factory = model_factory or _default_model_factory
        self._model: WhisperModelLike | None = None

    def transcribe(self, source: Path) -> list[TranscriptSpan]:
        if self._model is None:
            self._model = self._model_factory(self.config)
        segments, info = self._model.transcribe(
            str(source.resolve()),
            language=self.config.language,
            beam_size=self.config.beam_size,
            vad_filter=self.config.vad_filter,
        )
        detected_language = self.config.language or getattr(info, "language", None)
        spans: list[TranscriptSpan] = []
        for item in segments:
            text = str(getattr(item, "text", "")).strip()
            if not text:
                continue
            spans.append(
                TranscriptSpan(
                    transcript_span_id=f"transcript_{len(spans) + 1:04d}",
                    start_ms=round(float(getattr(item, "start")) * 1000),
                    end_ms=round(float(getattr(item, "end")) * 1000),
                    text=text,
                    source="asr",
                    # faster-whisper exposes average log probability, not a
                    # calibrated span confidence. Do not mislabel it.
                    confidence=None,
                    language=detected_language,
                )
            )
        return spans
