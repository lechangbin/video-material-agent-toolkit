"""Build timestamp-labelled 3x3 contact sheets from evidence frames."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .models import ContactSheet, EvidenceFrame


@dataclass(frozen=True, slots=True)
class ContactSheetPolicy:
    rows: int = 3
    columns: int = 3
    cell_width: int = 320
    cell_height: int = 200
    label_height: int = 28
    jpeg_quality: int = 90

    def __post_init__(self) -> None:
        if min(self.rows, self.columns, self.cell_width, self.cell_height) <= 0:
            raise ValueError("contact sheet dimensions must be positive")
        if self.label_height < 0:
            raise ValueError("label_height cannot be negative")
        if not 1 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")


def _format_timestamp(timestamp_ms: int) -> str:
    total_seconds, milliseconds = divmod(timestamp_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def create_contact_sheets(
    frames: Sequence[EvidenceFrame],
    *,
    evidence_dir: Path,
    output_dir: Path | None = None,
    policy: ContactSheetPolicy | None = None,
) -> list[ContactSheet]:
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is required to create contact sheets") from exc

    policy = policy or ContactSheetPolicy()
    output_dir = output_dir or evidence_dir / "contact-sheets"
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(frames, key=lambda item: item.timestamp_ms)
    page_size = policy.rows * policy.columns
    sheets: list[ContactSheet] = []
    for page_index, offset in enumerate(range(0, len(ordered), page_size), start=1):
        page = ordered[offset : offset + page_size]
        sheet_width = policy.columns * policy.cell_width
        sheet_height = policy.rows * (policy.cell_height + policy.label_height)
        canvas = Image.new("RGB", (sheet_width, sheet_height), "#111111")
        draw = ImageDraw.Draw(canvas)
        for cell_index, frame in enumerate(page):
            row, column = divmod(cell_index, policy.columns)
            x = column * policy.cell_width
            y = row * (policy.cell_height + policy.label_height)
            with Image.open(evidence_dir / frame.relative_path) as image:
                image = ImageOps.contain(
                    image.convert("RGB"),
                    (policy.cell_width, policy.cell_height),
                )
                paste_x = x + (policy.cell_width - image.width) // 2
                paste_y = y + (policy.cell_height - image.height) // 2
                canvas.paste(image, (paste_x, paste_y))
            draw.text(
                (x + 6, y + policy.cell_height + 6),
                f"{cell_index + 1}. {_format_timestamp(frame.timestamp_ms)}",
                fill="white",
            )
        sheet_id = f"sheet_{page_index:04d}"
        path = output_dir / f"{sheet_id}.jpg"
        canvas.save(path, format="JPEG", quality=policy.jpeg_quality)
        try:
            relative_path = path.relative_to(evidence_dir).as_posix()
        except ValueError:
            relative_path = path.resolve().as_posix()
        sheets.append(
            ContactSheet(
                contact_sheet_id=sheet_id,
                artifact_id=f"artifact_{sheet_id}",
                relative_path=relative_path,
                rows=policy.rows,
                columns=policy.columns,
                cell_frame_ids=tuple(frame.evidence_frame_id for frame in page),
            )
        )
    return sheets
