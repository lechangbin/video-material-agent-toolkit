"""Real FFmpeg mixed-media regression for editing-media conformance.

These tests exercise the full conformance runtime against locally generated
source clips that span the acceptance matrix in issue #26: source codecs
(H.264/H.265/AV1), frame-rate and VFR/CFR variants, 720p/1080p/4K
resolutions, SDR/HDR, absent/multiple/mismatched audio, odd paths, and the
negative eligibility/hash/corruption gates. They require a working
``ffmpeg``/``ffprobe`` and the source codecs; the suite skips cleanly when
those are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from media_conformance.contracts import (
    EditingMediaConformanceRequest,
    ImmutableSourceAsset,
    SelectedRange,
)
from media_conformance.errors import ConformanceError
from media_conformance.runtime import ConformanceRuntime

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


def _encoders() -> set[str]:
    assert _FFMPEG is not None
    completed = subprocess.run(
        [_FFMPEG, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return {
        line.split(maxsplit=2)[1]
        for line in completed.stdout.splitlines()
        if line.startswith((" V", " A"))
    } if completed.returncode == 0 else set()


_AVAILABLE = _encoders() if _FFMPEG else set()
_REQUIRE = {"libx264", "aac"}
_X265 = "libx265" in _AVAILABLE
_AV1 = any(name in _AVAILABLE for name in ("libaom-av1", "libsvtav1"))


requires_ffmpeg = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE and _REQUIRE.issubset(_AVAILABLE)),
    reason="ffmpeg, ffprobe, libx264, and aac are required for real-FFmpeg regression",
)
requires_x265 = pytest.mark.skipif(not _X265, reason="libx265 is required for H.265 fixtures")
requires_av1 = pytest.mark.skipif(not _AV1, reason="an AV1 software encoder is required")
requires_4k = pytest.mark.skipif(not _X265, reason="4K fixtures use H.265 to keep generation fast")


def _run(args: list[str]) -> None:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, (
        f"command failed: {' '.join(args[:3])}...\n"
        f"stdout: {completed.stdout[-800:]}\nstderr: {completed.stderr[-1200:]}"
    )


def _generate_source(
    out: Path,
    *,
    codec: str = "libx264",
    width: int = 1280,
    height: int = 720,
    fps: str = "30",
    duration: float = 2.0,
    audio: str = "sine",
    color: dict[str, str] | None = None,
    extra_vf: str | None = None,
) -> Path:
    """Generate one small 16:9 source clip with the requested properties."""
    source_filters = [f"scale={width}:{height}:force_original_aspect_ratio=decrease"]
    source_filters.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    source_filters.append(f"fps={fps}")
    source_filters.append("setsar=1")
    if color:
        source_filters.append(f"format={color.get('pix_fmt', 'yuv420p')}")
    vf = ",".join(source_filters)
    if extra_vf:
        vf = f"{extra_vf},{vf}"
    cmd = [_FFMPEG, "-hide_banner", "-nostdin", "-y"]
    cmd.extend(["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}"])
    if audio == "sine":
        cmd.extend(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}"])
    elif audio == "multi":
        cmd.extend([
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration}",
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={duration}",
        ])
    elif audio == "mono_44100":
        cmd.extend(["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration}"])
    elif audio == "none":
        pass
    else:
        raise AssertionError(f"unknown audio fixture: {audio}")
    cmd.extend(["-map", "0:v:0"])
    if audio != "none":
        cmd.extend(["-map", "1:a:0"] + (["-map", "2:a:0"] if audio == "multi" else []))
    cmd.extend(["-vf", vf, "-c:v", codec, "-pix_fmt", "yuv420p"])
    if color:
        for key in ("color_primaries", "color_trc", "colorspace"):
            if key in color:
                cmd.extend([f"-{key}", color[key]])
    cmd.extend(["-c:a", "aac", "-ar", "44100", "-ac", "2" if audio in {"sine", "multi"} else "1"])
    cmd.extend(["-t", f"{duration}", str(out)])
    _run(cmd)
    return out


def _probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [_FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, f"ffprobe failed on {path}: {completed.stderr}"
    return json.loads(completed.stdout)


def _build_request(
    source: Path,
    *,
    output_directory: Path,
    asset_id: str = "asset_001",
    declared_hash: str | None = None,
    ranges: tuple[tuple[str, float, float], ...] = (
        ("clip_a", 0.3, 1.2),
        ("clip_b", 0.5, 1.0),
    ),
) -> EditingMediaConformanceRequest:
    sha256 = declared_hash or hashlib.sha256(source.read_bytes()).hexdigest()
    lineage = hashlib.sha256(
        json.dumps(
            {"sha256": sha256, "ranges": list(ranges)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return EditingMediaConformanceRequest(
        request_id=f"mc_{lineage[:12]}",
        idempotency_key=f"idem_{lineage[:16]}",
        source=ImmutableSourceAsset(asset_id=asset_id, sha256=sha256, path=source),
        ranges=tuple(
            SelectedRange(clip_id=cid, start_seconds=start, end_seconds=end)
            for cid, start, end in ranges
        ),
        output_directory=output_directory,
    )


def _assert_clip_matches_profile(clip: Any) -> None:
    v = clip.video
    assert v.codec_name == "h264"
    assert v.profile.casefold() == "high"
    assert v.pixel_format == "yuv420p"
    assert (v.width, v.height) == (1920, 1080)
    assert v.sample_aspect_ratio == "1:1"
    assert v.frame_rate == "30/1"
    assert v.time_base == "1/90000"
    assert v.color_primaries == "bt709"
    assert v.color_transfer == "bt709"
    assert v.color_space == "bt709"
    a = clip.audio
    assert a.codec_name == "aac"
    assert a.sample_rate == 48000
    assert a.channels == 2
    assert clip.profile_valid is True


def _concat_stream_copy(
    clips: list[Path],
    out: Path,
) -> None:
    list_file = out.parent / "concat-list.txt"
    list_file.write_text(
        "\n".join(f"file '{clip.resolve().as_posix()}'" for clip in clips) + "\n",
        encoding="utf-8",
    )
    _run([
        _FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-movflags", "+faststart", str(out),
    ])


@requires_ffmpeg
def test_h264_sdr_720p_30fps_conforms(tmp_path: Path) -> None:
    source = _generate_source(tmp_path / "h264_720.mp4", codec="libx264")
    before = source.read_bytes()
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)
    out_dir = tmp_path / "conformed"

    result = runtime.prepare(_build_request(source, output_directory=out_dir))

    assert result.status == "completed"
    assert len(result.clips) == 2
    assert result.assembly_set is not None
    assert result.assembly_set.compatible is True
    assert result.assembly_set.packet_concat_verified is True
    for clip in result.clips:
        _assert_clip_matches_profile(clip)
    assert source.read_bytes() == before
    clip_paths = [out_dir / clip.relative_path for clip in result.clips]
    concat_out = tmp_path / "concat.mp4"
    _concat_stream_copy(clip_paths, concat_out)
    assert concat_out.exists() and concat_out.stat().st_size > 0
    concat_probe = _probe(concat_out)
    concat_video = next(
        stream for stream in concat_probe["streams"] if stream.get("codec_type") == "video"
    )
    assert concat_video["codec_name"] == "h264"


@requires_ffmpeg
@requires_x265
def test_h265_sdr_1080p_24fps_conforms(tmp_path: Path) -> None:
    source = _generate_source(
        tmp_path / "h265_1080.mp4",
        codec="libx265",
        width=1920,
        height=1080,
        fps="24",
    )
    before = source.read_bytes()
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    _assert_clip_matches_profile(result.clips[0])
    assert source.read_bytes() == before


@requires_ffmpeg
@requires_av1
def test_av1_sdr_720p_30fps_conforms(tmp_path: Path) -> None:
    av1_encoder = "libsvtav1" if "libsvtav1" in _AVAILABLE else "libaom-av1"
    source = _generate_source(tmp_path / "av1_720.mp4", codec=av1_encoder)
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    _assert_clip_matches_profile(result.clips[0])


@requires_ffmpeg
@pytest.mark.parametrize("fps", ["25", "29.97", "60"])
def test_cfr_variants_conform(tmp_path: Path, fps: str) -> None:
    source = _generate_source(tmp_path / f"h264_{fps}.mp4", codec="libx264", fps=fps)
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    assert result.clips[0].video.frame_rate == "30/1"


@requires_ffmpeg
@requires_4k
def test_4k_source_scales_to_delivery_profile(tmp_path: Path) -> None:
    source = _generate_source(
        tmp_path / "h265_4k.mp4",
        codec="libx265",
        width=3840,
        height=2160,
        fps="30",
        duration=1.5,
    )
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    assert (result.clips[0].video.width, result.clips[0].video.height) == (1920, 1080)


@requires_ffmpeg
def test_vfr_source_conforms(tmp_path: Path) -> None:
    segment_a = _generate_source(tmp_path / "vfr_a.mp4", codec="libx264", fps="30", duration=1.0, audio="none")
    segment_b = _generate_source(tmp_path / "vfr_b.mp4", codec="libx264", fps="60", duration=1.0, audio="none")
    list_file = tmp_path / "vfr_list.txt"
    list_file.write_text(
        f"file '{segment_a.resolve().as_posix()}'\nfile '{segment_b.resolve().as_posix()}'\n",
        encoding="utf-8",
    )
    vfr_source = tmp_path / "vfr_source.mp4"
    _run([
        _FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(vfr_source),
    ])
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(vfr_source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    _assert_clip_matches_profile(result.clips[0])


@requires_ffmpeg
def test_hdr_source_tonemaps_to_bt709_sdr(tmp_path: Path) -> None:
    source = _generate_source(
        tmp_path / "hdr.mp4",
        codec="libx264",
        color={
            "pix_fmt": "yuv420p10le",
            "color_primaries": "bt2020",
            "color_trc": "smpte2084",
            "colorspace": "bt2020nc",
        },
    )
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    assert result.clips[0].video.color_transfer == "bt709"
    assert result.clips[0].video.color_space == "bt709"


@requires_ffmpeg
@pytest.mark.parametrize(
    "audio",
    ["none", "mono_44100", "multi"],
    ids=["absent-audio", "mono-44100", "multiple-audio"],
)
def test_audio_variants_conform(tmp_path: Path, audio: str) -> None:
    source = _generate_source(tmp_path / f"audio_{audio}.mp4", codec="libx264", audio=audio)
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    _assert_clip_matches_profile(result.clips[0])


@requires_ffmpeg
def test_odd_path_source_conforms(tmp_path: Path) -> None:
    odd_dir = tmp_path / "奇特 路径 (带空格)"
    odd_dir.mkdir()
    source = _generate_source(odd_dir / "源 媒体.mp4", codec="libx264")
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert result.status == "completed"
    _assert_clip_matches_profile(result.clips[0])


@requires_ffmpeg
def test_idempotent_rerun_returns_completed_result(tmp_path: Path) -> None:
    source = _generate_source(tmp_path / "h264.mp4", codec="libx264")
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)
    request = _build_request(source, output_directory=tmp_path / "c")

    first = runtime.prepare(request)
    assert first.status == "completed"
    before_bytes = source.read_bytes()
    first_clip_sha = first.clips[0].sha256

    second = runtime.prepare(request)

    assert second.status == "completed"
    assert [clip.sha256 for clip in second.clips] == [first_clip_sha, first.clips[1].sha256]
    assert source.read_bytes() == before_bytes


@requires_ffmpeg
def test_non_16_9_source_is_rejected(tmp_path: Path) -> None:
    source = _generate_source(
        tmp_path / "square.mp4",
        codec="libx264",
        width=640,
        height=480,
        audio="none",
    )
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)
    before = source.read_bytes()

    with pytest.raises(ConformanceError) as captured:
        runtime.prepare(_build_request(source, output_directory=tmp_path / "c"))

    assert captured.value.code == "source_geometry_rejected"
    assert source.read_bytes() == before


@requires_ffmpeg
def test_declared_hash_mismatch_fails_before_render(tmp_path: Path) -> None:
    source = _generate_source(tmp_path / "h264.mp4", codec="libx264")
    before = source.read_bytes()
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)

    result = runtime.prepare(
        _build_request(source, output_directory=tmp_path / "c", declared_hash="0" * 64)
    )

    assert result.status == "failed"
    assert result.error["code"] == "source_hash_mismatch"
    assert source.read_bytes() == before
    assert not list((tmp_path / "c").rglob("*.partial.mp4"))


@requires_ffmpeg
def test_corrupt_source_returns_structured_failure(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 256)
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)
    request = _build_request(
        corrupt,
        output_directory=tmp_path / "c",
        declared_hash=hashlib.sha256(corrupt.read_bytes()).hexdigest(),
    )

    with pytest.raises(ConformanceError) as captured:
        runtime.prepare(request)

    assert captured.value.code in {"media_probe_failed", "video_stream_missing"}


@requires_ffmpeg
def test_repeated_prepare_after_external_clip_removal_re_renders(
    tmp_path: Path,
) -> None:
    source = _generate_source(tmp_path / "h264.mp4", codec="libx264")
    runtime = ConformanceRuntime(ffmpeg=_FFMPEG, ffprobe=_FFPROBE)
    request = _build_request(source, output_directory=tmp_path / "c")

    first = runtime.prepare(request)
    assert first.status == "completed"
    clip_path = tmp_path / "c" / first.clips[0].relative_path
    clip_sha = first.clips[0].sha256
    clip_path.unlink()

    second = runtime.prepare(request)

    assert second.status == "completed"
    assert second.clips[0].sha256 == clip_sha
