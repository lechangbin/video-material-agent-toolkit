from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from semvideo.modules.evidence.frames import near_duplicate_filter
from semvideo.modules.evidence.models import DedupDecision, EvidenceFrame


def _frame(frame_id: str, timestamp_ms: int, path: Path) -> EvidenceFrame:
    return EvidenceFrame(
        evidence_frame_id=frame_id,
        timestamp_ms=timestamp_ms,
        artifact_id=f"artifact_{frame_id}",
        relative_path=path.name,
        extraction_reasons=("candidate_context",),
        scene_score=None,
        dedup=DedupDecision("pending", "not_compared"),
    )


def test_near_duplicate_filter_keeps_audit_decision(tmp_path: Path) -> None:
    first = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(first).rectangle((0, 0, 28, 63), fill="black")
    first.save(tmp_path / "one.jpg")
    first.save(tmp_path / "two.jpg")
    changed = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(changed).polygon([(0, 0), (63, 0), (63, 63)], fill="black")
    changed.save(tmp_path / "three.jpg")

    kept, discarded = near_duplicate_filter(
        [
            _frame("frame_0001", 1_000, tmp_path / "one.jpg"),
            _frame("frame_0002", 2_000, tmp_path / "two.jpg"),
            _frame("frame_0003", 3_000, tmp_path / "three.jpg"),
        ],
        hamming_threshold=2,
        base_dir=tmp_path,
    )

    assert [item.evidence_frame_id for item in kept] == [
        "frame_0001",
        "frame_0003",
    ]
    assert discarded[0].dedup.decision == "discarded"
    assert discarded[0].dedup.compared_frame_id == "frame_0001"
