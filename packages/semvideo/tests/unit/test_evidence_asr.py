from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from semvideo.modules.evidence.asr import (
    FasterWhisperConfig,
    FasterWhisperTranscriber,
)


class FakeModel:
    def transcribe(self, audio: str, **kwargs: object):
        assert Path(audio).name == "sample.mp4"
        assert kwargs["beam_size"] == 2
        return (
            [
                SimpleNamespace(
                    start=0.25,
                    end=1.75,
                    text=" 测试转写 ",
                    avg_logprob=-0.1,
                )
            ],
            SimpleNamespace(language="zh"),
        )


def test_asr_uses_injected_model_without_download(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp4"
    source.touch()
    calls: list[FasterWhisperConfig] = []
    transcriber = FasterWhisperTranscriber(
        FasterWhisperConfig(
            model_name_or_path="local-model",
            beam_size=2,
            local_files_only=True,
        ),
        model_factory=lambda config: calls.append(config) or FakeModel(),
    )

    spans = transcriber.transcribe(source)

    assert len(calls) == 1
    assert spans[0].start_ms == 250
    assert spans[0].end_ms == 1750
    assert spans[0].text == "测试转写"
    assert spans[0].source == "asr"
    assert spans[0].language == "zh"
