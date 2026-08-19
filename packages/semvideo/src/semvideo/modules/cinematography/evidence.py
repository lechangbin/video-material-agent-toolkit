"""Plan and extract dense, bounded frame evidence inside each detected shot."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, hypot
from pathlib import Path
from statistics import fmean, median
from typing import Any

from semvideo.adapters.ffmpeg import FfmpegAdapter

from .models import (
    CinematographyEvidence,
    GlobalMotionMeasurement,
    ShotEvidenceBundle,
    ShotEvidenceFrame,
    ShotTimeline,
    TemporalProfile,
)


@dataclass(frozen=True, slots=True)
class ShotEvidencePolicy:
    frames_per_second: float = 1.0
    minimum_frames_per_shot: int = 4
    maximum_frames_per_shot: int = 9

    def __post_init__(self) -> None:
        if self.frames_per_second <= 0:
            raise ValueError("frames_per_second must be positive")
        if self.minimum_frames_per_shot <= 0:
            raise ValueError("minimum_frames_per_shot must be positive")
        if self.maximum_frames_per_shot < self.minimum_frames_per_shot:
            raise ValueError(
                "maximum_frames_per_shot must be at least minimum_frames_per_shot"
            )


@dataclass(frozen=True, slots=True)
class ShotEvidenceTimestamp:
    shot_id: str
    frame_id: str
    timestamp_ms: int
    ordinal: int


def plan_shot_evidence(
    timeline: ShotTimeline,
    policy: ShotEvidencePolicy | None = None,
) -> list[ShotEvidenceTimestamp]:
    """Select evenly spaced internal positions without inventing boundaries."""

    policy = policy or ShotEvidencePolicy()
    output: list[ShotEvidenceTimestamp] = []
    for shot in timeline.shots:
        duration_ms = shot.end_ms - shot.start_ms
        desired = ceil(duration_ms / 1000 * policy.frames_per_second)
        count = min(
            policy.maximum_frames_per_shot,
            max(policy.minimum_frames_per_shot, desired),
        )
        used: set[int] = set()
        for index in range(count):
            timestamp_ms = shot.start_ms + round(
                (index + 0.5) * duration_ms / count
            )
            timestamp_ms = min(shot.end_ms - 1, max(shot.start_ms, timestamp_ms))
            if timestamp_ms in used:
                continue
            used.add(timestamp_ms)
            output.append(
                ShotEvidenceTimestamp(
                    shot_id=shot.shot_id,
                    frame_id=f"{shot.shot_id}_frame_{index + 1:02d}",
                    timestamp_ms=timestamp_ms,
                    ordinal=index,
                )
            )
    return output


def _gray_image(path: Path, *, width: int = 64, height: int = 36) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for cinematography motion measurement"
        ) from exc
    with Image.open(path) as image:
        return image.convert("L").resize((width, height))


def _normalize_scale(
    image: Any, scale: float, *, width: int = 64, height: int = 36
) -> Any:
    from PIL import Image

    scaled = image.resize(
        (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
    )
    canvas = Image.new("L", (width, height), 0)
    left = (width - scaled.width) // 2
    top = (height - scaled.height) // 2
    if scaled.width <= width and scaled.height <= height:
        canvas.paste(scaled, (left, top))
        return canvas
    crop_left = max(0, -left)
    crop_top = max(0, -top)
    crop = scaled.crop(
        (
            crop_left,
            crop_top,
            crop_left + width,
            crop_top + height,
        )
    )
    canvas.paste(crop, (0, 0))
    return canvas


def _difference_score(
    left: Any, right: Any, *, dx: int = 0, dy: int = 0
) -> float:
    width, height = left.size
    left_pixels = left.load()
    right_pixels = right.load()
    tile_totals: dict[tuple[int, int], int] = {}
    tile_counts: dict[tuple[int, int], int] = {}
    margin_x = 8
    margin_y = 6
    tile_width = 8
    tile_height = 6
    for y in range(margin_y, height - margin_y):
        source_y = y - dy
        if not margin_y <= source_y < height - margin_y:
            continue
        for x in range(margin_x, width - margin_x):
            source_x = x - dx
            if not margin_x <= source_x < width - margin_x:
                continue
            tile = (
                (x - margin_x) // tile_width,
                (y - margin_y) // tile_height,
            )
            tile_totals[tile] = tile_totals.get(tile, 0) + abs(
                left_pixels[source_x, source_y] - right_pixels[x, y]
            )
            tile_counts[tile] = tile_counts.get(tile, 0) + 1
    tile_scores = [
        tile_totals[tile] / tile_counts[tile]
        for tile in tile_totals
        if tile_counts[tile]
    ]
    return median(tile_scores) / 255.0 if tile_scores else 0.0


def _pair_measurement(left_path: Path, right_path: Path) -> tuple[float, ...]:
    left = _gray_image(left_path)
    right = _gray_image(right_path)
    identity = _difference_score(left, right)
    best = (identity, 0, 0, 1.0)
    for scale in (0.96, 1.0, 1.04):
        scaled = _normalize_scale(left, scale)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                score = _difference_score(scaled, right, dx=dx, dy=dy)
                if score < best[0]:
                    best = (score, dx, dy, scale)
    improvement = max(0.0, min(1.0, (identity - best[0]) / max(identity, 1e-6)))
    return (
        best[1] / left.width,
        best[2] / left.height,
        best[3] - 1.0,
        identity,
        improvement,
    )


def measure_global_motion(
    shot_id: str,
    frame_paths: list[Path],
) -> GlobalMotionMeasurement:
    pairs = [
        _pair_measurement(left, right)
        for left, right in zip(frame_paths, frame_paths[1:])
    ]
    if not pairs:
        return GlobalMotionMeasurement(
            shot_id=shot_id,
            sample_pairs=0,
            translation_x_ratio=0.0,
            translation_y_ratio=0.0,
            scale_change_ratio=0.0,
            mean_frame_change=0.0,
            fit_improvement=0.0,
            foreground_suppression="tile_median_v1",
        )
    speed_curve = [
        round(hypot(row[0], row[1]) + abs(row[2]), 6)
        for row in pairs
    ]
    direction_change_count = 0
    for previous, current in zip(pairs, pairs[1:]):
        previous_vector = previous[:3]
        current_vector = current[:3]
        previous_speed = hypot(previous[0], previous[1]) + abs(previous[2])
        current_speed = hypot(current[0], current[1]) + abs(current[2])
        dot_product = sum(
            left * right
            for left, right in zip(previous_vector, current_vector)
        )
        if (
            previous_speed >= 0.005
            and current_speed >= 0.005
            and dot_product < 0
        ):
            direction_change_count += 1
    temporal_profile_hint: TemporalProfile = "unknown"
    if len(speed_curve) >= 2:
        midpoint = max(1, len(speed_curve) // 2)
        early_speed = fmean(speed_curve[:midpoint])
        late_speed = fmean(speed_curve[midpoint:])
        change_threshold = max(0.002, median(speed_curve) * 0.2)
        if direction_change_count:
            temporal_profile_hint = "variable"
        elif late_speed - early_speed > change_threshold:
            temporal_profile_hint = "accelerating"
        elif early_speed - late_speed > change_threshold:
            temporal_profile_hint = "decelerating"
        else:
            temporal_profile_hint = "constant"
    return GlobalMotionMeasurement(
        shot_id=shot_id,
        sample_pairs=len(pairs),
        translation_x_ratio=median(row[0] for row in pairs),
        translation_y_ratio=median(row[1] for row in pairs),
        scale_change_ratio=median(row[2] for row in pairs),
        mean_frame_change=median(row[3] for row in pairs),
        fit_improvement=median(row[4] for row in pairs),
        foreground_suppression="tile_median_v1",
        speed_curve=speed_curve,
        temporal_profile_hint=temporal_profile_hint,
        direction_change_count=direction_change_count,
    )


def _create_shot_contact_sheet(
    bundle_frames: list[ShotEvidenceFrame],
    *,
    job_root: Path,
    output: Path,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for cinematography contact sheets"
        ) from exc
    cell_width, cell_height, label_height = 320, 180, 28
    canvas = Image.new(
        "RGB",
        (cell_width * 3, (cell_height + label_height) * 3),
        "#111111",
    )
    draw = ImageDraw.Draw(canvas)
    for index, frame in enumerate(bundle_frames):
        row, column = divmod(index, 3)
        x = column * cell_width
        y = row * (cell_height + label_height)
        with Image.open(job_root / frame.path) as source:
            image = ImageOps.contain(
                source.convert("RGB"),
                (cell_width, cell_height),
            )
            canvas.paste(
                image,
                (
                    x + (cell_width - image.width) // 2,
                    y + (cell_height - image.height) // 2,
                ),
            )
        draw.text(
            (x + 6, y + cell_height + 6),
            f"{frame.frame_id} · {frame.timestamp_ms / 1000:.3f}s",
            fill="white",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, format="JPEG", quality=90)


def extract_cinematography_evidence(
    source: Path,
    job_root: Path,
    timeline: ShotTimeline,
    *,
    ffmpeg: FfmpegAdapter,
    policy: ShotEvidencePolicy | None = None,
) -> CinematographyEvidence:
    """Extract bounded temporal contact sheets and a motion track per shot."""

    planned = plan_shot_evidence(timeline, policy)
    by_shot: dict[str, list[ShotEvidenceTimestamp]] = {}
    for row in planned:
        by_shot.setdefault(row.shot_id, []).append(row)
    bundles: list[ShotEvidenceBundle] = []
    for shot in timeline.shots:
        frames: list[ShotEvidenceFrame] = []
        frame_paths: list[Path] = []
        for row in by_shot[shot.shot_id]:
            relative = (
                Path("evidence")
                / "cinematography"
                / "frames"
                / f"{row.frame_id}.jpg"
            )
            absolute = job_root / relative
            ffmpeg.extract_frame(
                source,
                timestamp_ms=row.timestamp_ms,
                output=absolute,
                max_width=960,
            )
            frame_paths.append(absolute)
            frames.append(
                ShotEvidenceFrame(
                    frame_id=row.frame_id,
                    shot_id=row.shot_id,
                    timestamp_ms=row.timestamp_ms,
                    path=relative.as_posix(),
                )
            )
        frame_groups = [
            frames[offset : offset + 9]
            for offset in range(0, len(frames), 9)
        ]
        sheet_paths: list[Path] = []
        for sheet_index, frame_group in enumerate(frame_groups, start=1):
            sheet_name = (
                f"{shot.shot_id}.jpg"
                if len(frame_groups) == 1
                else f"{shot.shot_id}_{sheet_index:02d}.jpg"
            )
            sheet_relative = (
                Path("evidence")
                / "cinematography"
                / "contact-sheets"
                / sheet_name
            )
            _create_shot_contact_sheet(
                frame_group,
                job_root=job_root,
                output=job_root / sheet_relative,
            )
            sheet_paths.append(sheet_relative)
        bundles.append(
            ShotEvidenceBundle(
                shot_id=shot.shot_id,
                start_ms=shot.start_ms,
                end_ms=shot.end_ms,
                frames=frames,
                contact_sheet_path=sheet_paths[0].as_posix(),
                contact_sheet_paths=[
                    path.as_posix() for path in sheet_paths
                ],
                motion_measurement=measure_global_motion(
                    shot.shot_id,
                    frame_paths,
                ),
            )
        )
    return CinematographyEvidence(
        source_video_id=timeline.source_video_id,
        shots=bundles,
    )
