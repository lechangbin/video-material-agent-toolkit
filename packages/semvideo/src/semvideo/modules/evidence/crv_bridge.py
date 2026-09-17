"""Narrow local-file CRV bridge and normalized evidence projection."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import atomic_write_json
from semvideo.infrastructure.managed_crv import FrozenCrvRuntime
from semvideo.modules.evidence.crv_contracts import (
    CrvEvidenceFrame,
    CrvTranscriptSpan,
    GlobalUnderstandingEvidencePackage,
)


class CrvBridge:
    """Run CRV with memory disabled against one local analysis proxy only."""

    def __init__(self, runtime: FrozenCrvRuntime) -> None:
        self.runtime = runtime

    def extract(
        self,
        analysis_proxy: Path,
        destination: Path,
        *,
        source_video_id: str,
        source_sha256: str,
        evidence_profile: str,
        max_frames: int,
        language: str,
        transcribe: bool,
    ) -> GlobalUnderstandingEvidencePackage:
        source = analysis_proxy.resolve(strict=True)
        if not source.is_file() or max_frames < 1:
            raise _error("crv_input_invalid", "CRV requires one local proxy and a frame cap.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="crv-", dir=destination.parent))
        environment = dict(os.environ)
        environment["CRV_NO_MEMORY"] = "1"
        arguments = [
            str(self.runtime.executable),
            str(source),
            "-o",
            str(staging),
            "--grid",
            "--max-frames",
            str(max_frames),
            "--lang",
            language or "auto",
        ]
        if not transcribe:
            arguments.append("--no-transcribe")
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            env=environment,
        )
        if completed.returncode != 0:
            _remove_staging(staging, destination.parent)
            raise _error(
                "crv_extraction_failed",
                "CRV could not extract normalized global evidence.",
            )
        try:
            package = self._normalize(
                staging,
                source_video_id=source_video_id,
                source_sha256=source_sha256,
                analysis_proxy_sha256=_sha256_file(source),
                evidence_profile=evidence_profile,
            )
            atomic_write_json(
                staging / "global-understanding-evidence.json",
                package.model_dump(mode="json"),
            )
            if destination.exists():
                raise _error("crv_output_conflict", "CRV output destination already exists.")
            os.replace(staging, destination)
            return package
        except BaseException:
            if staging.exists():
                _remove_staging(staging, destination.parent)
            raise

    def _normalize(
        self,
        root: Path,
        *,
        source_video_id: str,
        source_sha256: str,
        analysis_proxy_sha256: str,
        evidence_profile: str,
    ) -> GlobalUnderstandingEvidencePackage:
        timestamps = json.loads((root / "frames.json").read_text(encoding="utf-8"))
        rows = timestamps.get("frames")
        if not isinstance(rows, list) or not rows:
            raise _error("crv_output_invalid", "CRV returned no timestamped frames.")
        frames: list[CrvEvidenceFrame] = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise _error("crv_output_invalid", "CRV frame metadata is invalid.")
            relative = Path("frames") / str(row.get("file"))
            path = root / relative
            timestamp = row.get("timestamp_sec")
            if not path.is_file() or not isinstance(timestamp, (int, float)):
                raise _error("crv_output_invalid", "CRV frame metadata is incomplete.")
            with Image.open(path) as image:
                width, height = image.size
            frames.append(
                CrvEvidenceFrame(
                    frame_id=f"crv_frame_{index:04d}",
                    timestamp_seconds=float(timestamp),
                    relative_path=relative.as_posix(),
                    sha256=_sha256_file(path),
                    width=width,
                    height=height,
                )
            )
        transcript = self._transcript(root)
        content = {
            "frames": [frame.model_dump(mode="json") for frame in frames],
            "transcript": [span.model_dump(mode="json") for span in transcript],
        }
        return GlobalUnderstandingEvidencePackage(
            source_video_id=source_video_id,
            source_sha256=source_sha256,
            analysis_proxy_sha256=analysis_proxy_sha256,
            crv_version=self.runtime.version,
            evidence_profile=evidence_profile,
            runtime_package_hash=self.runtime.package_hash,
            frames=tuple(frames),
            transcript=transcript,
            evidence_hash=hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    @staticmethod
    def _transcript(root: Path) -> tuple[CrvTranscriptSpan, ...]:
        path = root / "transcript.json"
        if not path.is_file():
            return ()
        value: Any = json.loads(path.read_text(encoding="utf-8"))
        rows = value.get("segments", value) if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise _error("crv_output_invalid", "CRV transcript metadata is invalid.")
        spans: list[CrvTranscriptSpan] = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            start = row.get("start", row.get("start_sec"))
            end = row.get("end", row.get("end_sec"))
            text = row.get("text")
            if (
                isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and float(end) > float(start)
                and isinstance(text, str)
                and text.strip()
            ):
                spans.append(
                    CrvTranscriptSpan(
                        span_id=f"crv_transcript_{index:04d}",
                        start_seconds=float(start),
                        end_seconds=float(end),
                        text=text.strip(),
                    )
                )
        return tuple(spans)


def _remove_staging(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != parent.resolve() or not resolved.name.startswith("crv-"):
        raise _error("crv_output_path_invalid", "Refusing unsafe CRV cleanup.")
    import shutil

    shutil.rmtree(resolved, ignore_errors=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _error(code: str, message: str) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.DEPENDENCY,
        message=message,
        recovery=RecoveryAction.REPORT_BUG,
        exit_code=4,
    )
