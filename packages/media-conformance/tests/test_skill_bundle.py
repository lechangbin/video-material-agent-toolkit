"""Bundled Skill contract drift and bridge tests for media conformance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from media_conformance.contracts import (
    EditingMediaConformanceRequest,
    contract_schema_bundle,
)

REPOSITORY_ROOT = Path(__file__).parents[3]
SKILL_ROOT = (
    REPOSITORY_ROOT / "skills" / "search-understand-refine-video-materials"
)
SCHEMA_REFERENCES = SKILL_ROOT / "references" / "schemas"
EXAMPLE_REFERENCES = SKILL_ROOT / "references" / "examples"
BRIDGE_SCRIPT = SKILL_ROOT / "scripts" / "prepare_conformance_request.py"


def _load_bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_conformance_request", BRIDGE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_bundled_conformance_schemas_match_authoritative_models() -> None:
    schemas = contract_schema_bundle()

    assert json.loads(
        (
            SCHEMA_REFERENCES / "editing-media-conformance-request-v1.schema.json"
        ).read_text(encoding="utf-8")
    ) == schemas["request"]
    assert json.loads(
        (
            SCHEMA_REFERENCES / "editing-media-conformance-result-v1.schema.json"
        ).read_text(encoding="utf-8")
    ) == schemas["result"]
    assert json.loads(
        (
            SCHEMA_REFERENCES / "editing-delivery-profile-v1.schema.json"
        ).read_text(encoding="utf-8")
    ) == schemas["profile"]


def test_bundled_minimal_example_validates_with_authoritative_model() -> None:
    example = json.loads(
        (
            EXAMPLE_REFERENCES / "editing-media-conformance-request-v1.min.json"
        ).read_text(encoding="utf-8")
    )

    request = EditingMediaConformanceRequest.model_validate(example)

    assert request.schema_version == "editing-media-conformance-request/v1"
    assert request.source.asset_id == "hq_source_001"
    assert [item.clip_id for item in request.ranges] == [
        "clip_opening",
        "clip_closing",
    ]


def _workflow_fixture(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "video-material-workflow/v1",
        "workflow_id": "wf_test_001",
        "state_version": "video-material-workflow-state/v1",
    }


def _selection_fixture() -> dict[str, object]:
    return {
        "schema_version": "selection-result/v2",
        "selection_id": "sel_test_001",
        "selected": [],
    }


def _ranges_document(asset: Path, asset_id: str = "hq_source_001") -> dict[str, object]:
    return {
        "schema_version": "selected-source-ranges/v1",
        "asset_id": asset_id,
        "path": str(asset),
        "ranges": [
            {"clip_id": "clip_opening", "start_seconds": 1.0, "end_seconds": 2.5},
            {"clip_id": "clip_closing", "start_seconds": 4.0, "end_seconds": 6.0},
        ],
    }


def _run_bridge(
    tmp_path: Path,
    ranges: dict[str, object],
    *,
    asset: Path,
) -> tuple[int, Path, Path]:
    bridge = _load_bridge()
    workflow_path = _write_json(tmp_path / "workflow.json", _workflow_fixture(tmp_path))
    selection_path = _write_json(
        tmp_path / "selection-result.json", _selection_fixture()
    )
    ranges_path = _write_json(tmp_path / "ranges.json", ranges)
    output = tmp_path / "conformance" / "mc_test.json"
    output_directory = tmp_path / "conformed"
    code = bridge.main(
        [
            "--workflow", str(workflow_path),
            "--selection-result", str(selection_path),
            "--ranges", str(ranges_path),
            "--output", str(output),
            "--output-directory", str(output_directory),
        ]
    )
    return code, output, output_directory


def test_bridge_builds_valid_and_idempotent_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asset = tmp_path / "hq_source_001.mp4"
    asset.write_bytes(b"immutable-high-quality-source")

    code, output, output_directory = _run_bridge(
        tmp_path, _ranges_document(asset), asset=asset
    )

    assert code == 0
    request = EditingMediaConformanceRequest.model_validate_json(
        output.read_text(encoding="utf-8")
    )
    assert request.source.sha256 == hashlib.sha256(asset.read_bytes()).hexdigest()
    assert request.source.asset_id == "hq_source_001"
    assert request.request_id.startswith("mc_")
    assert request.idempotency_key.startswith("idem_")
    assert request.output_directory == output_directory.resolve()
    assert request.profile.width == 1920
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "prepared"
    assert report["command"].startswith("media-conformance prepare --request")

    first = output.read_text(encoding="utf-8")
    code_again, output_again, _ = _run_bridge(
        tmp_path, _ranges_document(asset), asset=asset
    )
    assert code_again == 0
    assert output_again.read_text(encoding="utf-8") == first


def test_bridge_rejects_declared_hash_mismatch(tmp_path: Path) -> None:
    asset = tmp_path / "hq_source_001.mp4"
    asset.write_bytes(b"immutable-high-quality-source")
    ranges = _ranges_document(asset)
    ranges["sha256"] = "0" * 64

    code, _, _ = _run_bridge(tmp_path, ranges, asset=asset)

    assert code == 2


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            {"asset_id": "../escape"},
            id="unsafe-asset-id",
        ),
        pytest.param(
            {
                "ranges": [
                    {
                        "clip_id": "clip_bad",
                        "start_seconds": 5.0,
                        "end_seconds": 5.0,
                    }
                ]
            },
            id="empty-range",
        ),
        pytest.param(
            {
                "ranges": [
                    {
                        "clip_id": "clip_bad",
                        "start_seconds": 5.0,
                        "end_seconds": 4.0,
                    }
                ]
            },
            id="reversed-range",
        ),
        pytest.param(
            {
                "ranges": [
                    {
                        "clip_id": "clip_a",
                        "start_seconds": 0.0,
                        "end_seconds": 1.0,
                    },
                    {
                        "clip_id": "clip_a",
                        "start_seconds": 1.0,
                        "end_seconds": 2.0,
                    },
                ]
            },
            id="duplicate-clip-id",
        ),
        pytest.param(
            {
                "ranges": [
                    {
                        "clip_id": "clip_neg",
                        "start_seconds": -1.0,
                        "end_seconds": 2.0,
                    }
                ]
            },
            id="negative-start",
        ),
    ],
)
def test_bridge_rejects_invalid_ranges(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    asset = tmp_path / "hq_source_001.mp4"
    asset.write_bytes(b"immutable-high-quality-source")
    ranges = _ranges_document(asset)
    ranges.update(mutation)

    code, _, _ = _run_bridge(tmp_path, ranges, asset=asset)

    assert code == 2


def test_bridge_rejects_relative_asset_path(tmp_path: Path) -> None:
    asset = tmp_path / "hq_source_001.mp4"
    asset.write_bytes(b"immutable-high-quality-source")
    ranges = _ranges_document(asset)
    ranges["path"] = "hq_source_001.mp4"

    code, _, _ = _run_bridge(tmp_path, ranges, asset=asset)

    assert code == 2


def test_bridge_rejects_conflicting_existing_request(tmp_path: Path) -> None:
    asset = tmp_path / "hq_source_001.mp4"
    asset.write_bytes(b"immutable-high-quality-source")

    code, output, _ = _run_bridge(tmp_path, _ranges_document(asset), asset=asset)
    assert code == 0
    original = json.loads(output.read_text(encoding="utf-8"))
    original["ranges"][0]["end_seconds"] = 9.9
    _write_json(output, original)

    code_conflict, _, _ = _run_bridge(
        tmp_path, _ranges_document(asset), asset=asset
    )

    assert code_conflict == 2
