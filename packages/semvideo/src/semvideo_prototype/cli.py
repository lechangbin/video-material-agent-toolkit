"""CLI shell for the disposable real-video prototype."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .grid_pipeline import GridPrototypeOptions, run_grid_pipeline
from .pipeline import PrototypeOptions, doctor, run_pipeline


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="semvideo-prototype",
        description="PROTOTYPE: analyze and semantically merge a real local video.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check local media and model prerequisites.")

    process = subparsers.add_parser("process", help="Run the real-video prototype.")
    process.add_argument("source", type=Path)
    process.add_argument("--out", type=Path, required=True)
    process.add_argument("--scene-threshold", type=float, default=0.30)
    process.add_argument("--min-segment-seconds", type=float, default=0.75)
    process.add_argument("--max-segment-seconds", type=float, default=12.0)
    process.add_argument("--max-candidates", type=int, default=12)
    process.add_argument("--evidence-frames", type=int, default=2)
    process.add_argument("--crv-max-frames", type=int, default=80)
    process.add_argument("--batch-size", type=int, default=3)
    process.add_argument("--transcribe", action="store_true")
    process.add_argument("--no-llm", action="store_true")
    process.add_argument(
        "--export-segments",
        action="store_true",
        help="Render each final semantic segment to segments/*.mp4 with FFmpeg.",
    )
    process.add_argument("--overwrite", action="store_true")
    process.add_argument("--open-report", action="store_true")

    grid = subparsers.add_parser(
        "grid-segment",
        help="PROTOTYPE: let one LLM request segment whole-video 3x3 grids.",
    )
    grid.add_argument("source", type=Path)
    grid.add_argument("--out", type=Path, required=True)
    grid.add_argument("--scene-threshold", type=float, default=0.30)
    grid.add_argument("--max-frames", type=int, default=81)
    grid.add_argument("--periodic-anchor-seconds", type=float, default=4.0)
    grid.add_argument("--transcribe", action="store_true")
    grid.add_argument("--language", default="auto")
    grid.add_argument(
        "--whisper-model",
        choices=["tiny", "base", "small", "medium", "large", "turbo"],
        default="base",
    )
    grid.add_argument("--overwrite", action="store_true")
    grid.add_argument("--open-report", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "doctor":
            print(json.dumps(doctor(), ensure_ascii=False, indent=2))
            return
        if args.command == "grid-segment":
            manifest = run_grid_pipeline(
                GridPrototypeOptions(
                    source=args.source,
                    out_dir=args.out,
                    scene_threshold=args.scene_threshold,
                    max_frames=args.max_frames,
                    periodic_anchor_seconds=args.periodic_anchor_seconds,
                    transcribe=args.transcribe,
                    language=args.language,
                    whisper_model=args.whisper_model,
                    overwrite=args.overwrite,
                    open_report=args.open_report,
                )
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return

        manifest = run_pipeline(
            PrototypeOptions(
                source=args.source,
                out_dir=args.out,
                scene_threshold=args.scene_threshold,
                min_segment_seconds=args.min_segment_seconds,
                max_segment_seconds=args.max_segment_seconds,
                max_candidates=args.max_candidates,
                evidence_frames_per_segment=args.evidence_frames,
                crv_max_frames=args.crv_max_frames,
                batch_size=args.batch_size,
                transcribe=args.transcribe,
                no_llm=args.no_llm,
                export_segments=args.export_segments,
                overwrite=args.overwrite,
                open_report=args.open_report,
            )
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        raise SystemExit(7)
    except FileNotFoundError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        raise SystemExit(3)
    except Exception as exc:
        print(f"prototype failed: {exc}", file=sys.stderr)
        raise SystemExit(6)


if __name__ == "__main__":
    main()
