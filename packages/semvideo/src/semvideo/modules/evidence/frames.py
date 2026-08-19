"""Timestamp selection, frame extraction, and perceptual near-deduplication."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from semvideo.adapters.ffmpeg import FfmpegAdapter
from semvideo.modules.media.models import CandidateTimeline

from .models import DedupDecision, EvidenceFrame


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    frames_per_candidate: int = 2
    maximum_interval_ms: int = 10_000
    max_frames: int = 80
    max_width: int = 960
    dedup_hamming_threshold: int = 4
    dedup_lookback: int = 3

    def __post_init__(self) -> None:
        if self.frames_per_candidate <= 0:
            raise ValueError("frames_per_candidate must be positive")
        if self.maximum_interval_ms <= 0:
            raise ValueError("maximum_interval_ms must be positive")
        if self.max_frames <= 0 or self.max_width <= 0:
            raise ValueError("max_frames and max_width must be positive")
        if not 0 <= self.dedup_hamming_threshold <= 64:
            raise ValueError("dedup_hamming_threshold must be between 0 and 64")
        if self.dedup_lookback <= 0:
            raise ValueError("dedup_lookback must be positive")


def select_evidence_timestamps(
    timeline: CandidateTimeline,
    policy: EvidencePolicy,
) -> list[tuple[int, set[str], float | None]]:
    """Select evidence positions; these never become candidate boundaries."""

    selected: dict[int, tuple[set[str], float | None]] = {}
    for segment in timeline.segments:
        span = segment.end_ms - segment.start_ms
        for index in range(policy.frames_per_candidate):
            timestamp = segment.start_ms + round(
                (index + 1) * span / (policy.frames_per_candidate + 1)
            )
            timestamp = min(timeline.duration_ms - 1, max(0, timestamp))
            reasons, score = selected.setdefault(
                timestamp, (set(), None)
            )
            reasons.add("candidate_context")
    cursor = policy.maximum_interval_ms
    while cursor < timeline.duration_ms:
        reasons, score = selected.setdefault(cursor, (set(), None))
        reasons.add("maximum_interval")
        cursor += policy.maximum_interval_ms
    for boundary in timeline.boundaries:
        timestamp = min(timeline.duration_ms - 1, max(0, boundary.timestamp_ms))
        reasons, _ = selected.setdefault(timestamp, (set(), None))
        reasons.add("visual_change")
        selected[timestamp] = (
            reasons,
            boundary.scores.get("scene_change"),
        )

    ordered = [
        (timestamp, reasons, score)
        for timestamp, (reasons, score) in sorted(selected.items())
    ]
    if len(ordered) <= policy.max_frames:
        return ordered
    if policy.max_frames == 1:
        return [ordered[len(ordered) // 2]]
    indexes = {
        round(index * (len(ordered) - 1) / (policy.max_frames - 1))
        for index in range(policy.max_frames)
    }
    return [ordered[index] for index in sorted(indexes)]


def difference_hash(path: Path) -> int:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for evidence frame deduplication") from exc
    with Image.open(path) as image:
        gray = image.convert("L").resize((9, 8))
        pixels = list(cast(Sequence[int], gray.get_flattened_data()))
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return value


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def near_duplicate_filter(
    frames: Iterable[EvidenceFrame],
    *,
    hamming_threshold: int = 4,
    lookback: int = 3,
    base_dir: Path | None = None,
) -> tuple[list[EvidenceFrame], list[EvidenceFrame]]:
    """Filter near duplicates while preserving frame timestamps and audit decisions."""

    kept: list[EvidenceFrame] = []
    discarded: list[EvidenceFrame] = []
    hashes: list[tuple[EvidenceFrame, int]] = []
    for frame in sorted(frames, key=lambda item: item.timestamp_ms):
        path = Path(frame.relative_path)
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        fingerprint = difference_hash(path)
        duplicate: tuple[EvidenceFrame, int] | None = None
        for prior, prior_hash in reversed(hashes[-lookback:]):
            distance = hamming_distance(fingerprint, prior_hash)
            if distance <= hamming_threshold:
                duplicate = (prior, distance)
                break
        if duplicate is not None:
            prior, distance = duplicate
            discarded.append(
                replace(
                    frame,
                    dedup=DedupDecision(
                        decision="discarded",
                        reason="near_duplicate",
                        distance=distance,
                        compared_frame_id=prior.evidence_frame_id,
                    ),
                )
            )
            continue
        kept_frame = replace(
            frame,
            dedup=DedupDecision(
                decision="kept",
                reason="local_change" if kept else "first_frame",
            ),
        )
        kept.append(kept_frame)
        hashes.append((kept_frame, fingerprint))
    return kept, discarded


def extract_evidence_frames(
    source: Path,
    output_dir: Path,
    timeline: CandidateTimeline,
    *,
    ffmpeg: FfmpegAdapter | None = None,
    policy: EvidencePolicy | None = None,
) -> tuple[list[EvidenceFrame], list[EvidenceFrame]]:
    adapter = ffmpeg or FfmpegAdapter()
    policy = policy or EvidencePolicy()
    frame_dir = output_dir / "frames"
    raw_frames: list[EvidenceFrame] = []
    for index, (timestamp, reasons, scene_score) in enumerate(
        select_evidence_timestamps(timeline, policy),
        start=1,
    ):
        frame_id = f"frame_{index:04d}"
        relative_path = Path("frames") / f"{frame_id}.jpg"
        adapter.extract_frame(
            source,
            timestamp_ms=timestamp,
            output=output_dir / relative_path,
            max_width=policy.max_width,
        )
        raw_frames.append(
            EvidenceFrame(
                evidence_frame_id=frame_id,
                timestamp_ms=timestamp,
                artifact_id=f"artifact_{frame_id}",
                relative_path=relative_path.as_posix(),
                extraction_reasons=tuple(sorted(reasons)),
                scene_score=scene_score,
                dedup=DedupDecision(decision="pending", reason="not_compared"),
            )
        )
    return near_duplicate_filter(
        raw_frames,
        hamming_threshold=policy.dedup_hamming_threshold,
        lookback=policy.dedup_lookback,
        base_dir=output_dir,
    )
