from __future__ import annotations

from pathlib import Path

from PIL import Image

from semvideo.modules.evidence.contact_sheets import create_contact_sheets
from semvideo.modules.evidence.models import DedupDecision, EvidenceFrame


def test_contact_sheets_use_nine_ordered_cells(tmp_path: Path) -> None:
    frames: list[EvidenceFrame] = []
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    for index in range(10):
        path = frame_dir / f"frame_{index:04d}.jpg"
        Image.new("RGB", (80, 50), (index * 20, 0, 0)).save(path)
        frames.append(
            EvidenceFrame(
                evidence_frame_id=f"frame_{index:04d}",
                timestamp_ms=index * 1000,
                artifact_id=f"artifact_{index:04d}",
                relative_path=f"frames/{path.name}",
                extraction_reasons=("maximum_interval",),
                scene_score=None,
                dedup=DedupDecision("kept", "local_change"),
            )
        )

    sheets = create_contact_sheets(frames, evidence_dir=tmp_path)

    assert len(sheets) == 2
    assert len(sheets[0].cell_frame_ids) == 9
    assert sheets[1].cell_frame_ids == ("frame_0009",)
    assert (tmp_path / sheets[0].relative_path).is_file()
