from __future__ import annotations

from pathlib import Path

from semvideo.adapters.ffmpeg import MediaFacts, VideoStreamFacts
from semvideo.application.analysis_media import (
    AnalysisProxyPolicy,
    prepare_analysis_media,
)
from semvideo.application.source_store import sha256_file
from semvideo.application.workspace import initialize_workspace
from semvideo.infrastructure.io import UnsupportedSchemaVersionError


def _facts(*, width: int, height: int, rotation: int = 0) -> MediaFacts:
    return MediaFacts(
        schema_version=1,
        source="source.mp4",
        duration_ms=10_000,
        format_name="mp4",
        bit_rate=1_000,
        video_stream=VideoStreamFacts(
            index=0,
            codec="h264",
            width=width,
            height=height,
            pixel_format="yuv420p",
            avg_frame_rate="25/1",
            nominal_fps=25.0,
            time_base="1/1000",
            rotation_degrees=rotation,
        ),
        audio_streams=(),
        subtitle_streams=(),
        decodable=True,
    )


def test_large_source_creates_and_reuses_shared_analysis_proxy(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"original-4k")
    source_hash = sha256_file(source)
    calls: list[Path] = []

    def create_proxy(output: Path) -> None:
        calls.append(output)
        output.write_bytes(b"analysis-720p")

    first = prepare_analysis_media(
        workspace,
        source_video_id="video_example",
        source=source,
        source_hash=source_hash,
        media_facts=_facts(width=3840, height=2160),
        policy=AnalysisProxyPolicy(),
        create_proxy=create_proxy,
    )
    second = prepare_analysis_media(
        workspace,
        source_video_id="video_example",
        source=source,
        source_hash=source_hash,
        media_facts=_facts(width=3840, height=2160),
        policy=AnalysisProxyPolicy(),
        create_proxy=create_proxy,
    )

    assert first.uses_proxy is True
    assert first.path == second.path
    assert first.path.parent == source_root / "analysis"
    assert first.content_hash == sha256_file(first.path)
    assert len(calls) == 1
    assert first.to_provenance()["relative_path"].startswith("analysis/")


def test_source_within_720p_uses_original_without_creating_proxy(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"original-720p")
    source_hash = sha256_file(source)

    result = prepare_analysis_media(
        workspace,
        source_video_id="video_example",
        source=source,
        source_hash=source_hash,
        media_facts=_facts(width=1280, height=720),
        policy=AnalysisProxyPolicy(),
        create_proxy=lambda output: (_ for _ in ()).throw(
            AssertionError(f"unexpected proxy: {output}")
        ),
    )

    assert result.uses_proxy is False
    assert result.path == source
    assert result.content_hash == source_hash
    assert result.to_provenance()["relative_path"] == "source.mp4"


def test_display_rotation_is_considered_when_deciding_proxy(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"rotated")

    result = prepare_analysis_media(
        workspace,
        source_video_id="video_example",
        source=source,
        source_hash=sha256_file(source),
        media_facts=_facts(width=720, height=1920, rotation=90),
        policy=AnalysisProxyPolicy(),
        create_proxy=lambda output: output.write_bytes(b"proxy"),
    )

    assert result.uses_proxy is True


def test_tampered_cached_proxy_is_regenerated(tmp_path: Path) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"original")
    calls = 0

    def create_proxy(output: Path) -> None:
        nonlocal calls
        calls += 1
        output.write_bytes(f"proxy-{calls}".encode())

    arguments = {
        "workspace": workspace,
        "source_video_id": "video_example",
        "source": source,
        "source_hash": sha256_file(source),
        "media_facts": _facts(width=2560, height=1440),
        "policy": AnalysisProxyPolicy(),
        "create_proxy": create_proxy,
    }
    first = prepare_analysis_media(**arguments)
    first.path.write_bytes(b"tampered")
    second = prepare_analysis_media(**arguments)

    assert calls == 2
    assert second.path.read_bytes() == b"proxy-2"


def test_proxy_with_wrong_registered_relative_path_is_regenerated(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"original")
    calls = 0

    def create_proxy(output: Path) -> None:
        nonlocal calls
        calls += 1
        output.write_bytes(f"proxy-{calls}".encode())

    arguments = {
        "workspace": workspace,
        "source_video_id": "video_example",
        "source": source,
        "source_hash": sha256_file(source),
        "media_facts": _facts(width=2560, height=1440),
        "policy": AnalysisProxyPolicy(),
        "create_proxy": create_proxy,
    }
    first = prepare_analysis_media(**arguments)
    metadata_path = first.path.with_suffix(".json")
    metadata_path.write_text(
        metadata_path.read_text(encoding="utf-8").replace(
            '"relative_path": "analysis/',
            '"relative_path": "wrong/',
        ),
        encoding="utf-8",
    )

    second = prepare_analysis_media(**arguments)

    assert calls == 2
    assert second.path.read_bytes() == b"proxy-2"


def test_newer_proxy_metadata_schema_is_rejected_without_overwrite(
    tmp_path: Path,
) -> None:
    workspace = initialize_workspace(tmp_path / "workspace")
    source_root = workspace.sources / "video_example"
    source_root.mkdir(parents=True)
    source = source_root / "source.mp4"
    source.write_bytes(b"original")
    arguments = {
        "workspace": workspace,
        "source_video_id": "video_example",
        "source": source,
        "source_hash": sha256_file(source),
        "media_facts": _facts(width=2560, height=1440),
        "policy": AnalysisProxyPolicy(),
        "create_proxy": lambda output: output.write_bytes(b"proxy"),
    }
    result = prepare_analysis_media(**arguments)
    metadata_path = result.path.with_suffix(".json")
    metadata = metadata_path.read_text(encoding="utf-8").replace(
        '"schema_version": 1',
        '"schema_version": 2',
    )
    metadata_path.write_text(metadata, encoding="utf-8")

    try:
        prepare_analysis_media(**arguments)
    except UnsupportedSchemaVersionError:
        pass
    else:
        raise AssertionError("newer proxy metadata schema was overwritten")

    assert '"schema_version": 2' in metadata_path.read_text(encoding="utf-8")
