from __future__ import annotations

import json
from pathlib import Path

from semvideo.application.evidence_benchmark import build_report


def test_benchmark_measures_all_tiers_without_approving_profiles(tmp_path: Path) -> None:
    tiers = (("128k", 128_000), ("256k", 256_000), ("512k", 512_000), ("1m", 1_000_000))
    payload = {
        "schema_version": "subagent-evidence-benchmark-input/v1",
        "runs": [
            {
                "fixture_id": f"fixture_{tier}",
                "tier": tier,
                "context_tokens": tokens,
                "image_count": 12,
                "serialized_image_tokens": 20_000,
                "transcript_tokens": 4_000,
                "cinematography_tokens": 2_000,
                "request_overhead_tokens": 1_000,
                "output_reserve_tokens": 8_000,
                "overlap_tokens": 1_000,
                "observer_window_count": 2,
                "validation_failures": 0,
                "repair_attempts": 0,
                "latency_ms": 100,
                "semantic_coverage": 0.9,
                "crv_version": "0.9.3",
                "semvideo_version": "0.2.0",
            }
            for tier, tokens in tiers
        ],
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    result = build_report(input_path, tmp_path / "report")

    assert result["status"] == "measured"
    assert result["approval"]["status"] == "pending_human_approval"
    assert set(result["tiers"]) == {"128k", "256k", "512k", "1m"}
    assert Path(result["report_path"]).is_file()
