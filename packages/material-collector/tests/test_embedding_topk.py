from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = (
    Path(__file__).parents[3]
    / "skills/search-understand-refine-video-materials/scripts/select_embedding_topk.py"
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("select_embedding_topk", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEmbedder:
    fingerprint = "test-model-sha256"

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "城市" in text or "city" in text:
                vectors.append([1.0, 0.0])
            elif "人群" in text or "people" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([-1.0, 0.0])
        return vectors


class FacetOutlierEmbedder:
    fingerprint = "facet-outlier-test"

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if text.startswith("query: "):
                vectors.append([0.0, 1.0] if "people" in text else [1.0, 0.0])
            elif "OUTLIER" in text:
                vectors.append([0.0, 1.0])
            elif "THIRD" in text:
                vectors.append([0.95, 0.31])
            elif "SECOND" in text:
                vectors.append([0.98, 0.2])
            else:
                vectors.append([1.0, 0.0])
        return vectors


def _fixture(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    candidates = [
        {
            "schema_version": "video-material-candidate-segment/v1",
            "candidate_segment_id": f"job_1:seg_{index}",
            "understanding_job_id": "job_1",
            "item_id": "proxy_1",
            "media_unit_ids": ["bilibili:1"],
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "duration_ms": 1000,
            "short_summary": description,
            "detailed_summary": description,
            "visual_summary": description,
        }
        for index, description in enumerate(["城市航拍 city", "街道人群 people", "无关室内烹饪"])
    ]
    path = tmp_path / "candidates.jsonl"
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    request = {
        "schema_version": "segment-selection-request/v2",
        "selection_id": "sel_1",
        "workflow_id": "vmw_1",
        "workflow_state_version": 1,
        "query_plan_id": "qp_1",
        "segment_id": "seg_1",
        "round_number": 1,
        "theme_segment": {"text": "城市", "intent": "城市人群", "narration_duration_ms": None},
        "required_visual_facets": [
            {"facet_id": "city", "description": "城市 city", "priority": 1},
            {"facet_id": "people", "description": "人群 people", "priority": 2},
        ],
        "max_k": 3,
        "max_k_source": "derived",
        "selection_targets": {"independent_candidates_per_required_facet": 2},
        "collection_session_ids": ["ses_1"],
        "understanding_catalogs": [
            {
                "item_id": "proxy_1",
                "understanding_job_id": "job_1",
                "status": "complete",
            }
        ],
        "candidate_artifact": {
            "path": str(path),
            "format": "jsonl",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "candidate_count": len(candidates),
        },
    }
    return request, path


def test_embedding_topk_scores_all_candidates_and_writes_audit(tmp_path: Path) -> None:
    module = _module()
    request, _ = _fixture(tmp_path)
    result = module.select(request, embedder=FakeEmbedder(), output_dir=tmp_path / "selection")

    assert result["status"] == "selected"
    assert result["selection_method"] == "embedding"
    assert result["lineage"]["candidate_count"] == 3
    assert result["selected"][0]["candidate_segment_id"] == "job_1:seg_0"
    assert len(result["selected"]) <= request["max_k"]
    audit = (tmp_path / "selection" / "selection-audit.jsonl").read_text(encoding="utf-8")
    assert len(audit.splitlines()) == 3
    assert result["audit_artifact"]["sha256"] == hashlib.sha256(audit.encode()).hexdigest()


def test_embedding_topk_rejects_tampered_candidate_artifact(tmp_path: Path) -> None:
    module = _module()
    request, path = _fixture(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(module.SelectionError, match="hash"):
        module.select(request, embedder=FakeEmbedder(), output_dir=tmp_path / "selection")


def test_embedding_topk_rejects_subagent_route(tmp_path: Path) -> None:
    module = _module()
    request, _ = _fixture(tmp_path)
    with pytest.raises(module.SelectionError, match="route"):
        module.select(
            request,
            embedder=FakeEmbedder(),
            output_dir=tmp_path / "selection",
            route={
                "schema_version": "video-material-selection-route/v1",
                "workflow_id": "vmw_1",
                "route": "subagent",
            },
        )


def test_global_facet_outlier_survives_theme_shortlist(tmp_path: Path) -> None:
    module = _module()
    request, path = _fixture(tmp_path)
    candidates = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for label, candidate in zip(("FIRST", "SECOND", "THIRD"), candidates, strict=True):
        candidate["visual_summary"] = label
        candidate["detailed_summary"] = label
        candidate["short_summary"] = label
    outlier = dict(candidates[-1])
    outlier.update(
        {
            "candidate_segment_id": "job_1:seg_3",
            "start_ms": 3000,
            "end_ms": 4000,
            "visual_summary": "OUTLIER",
            "detailed_summary": "OUTLIER",
            "short_summary": "OUTLIER",
        }
    )
    candidates.append(outlier)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidates),
        encoding="utf-8",
    )
    request["candidate_artifact"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    request["candidate_artifact"]["candidate_count"] = 4
    request["max_k"] = 2
    result = module.select(
        request, embedder=FacetOutlierEmbedder(), output_dir=tmp_path / "selection"
    )
    assert {item["candidate_segment_id"] for item in result["selected"]} == {
        "job_1:seg_0",
        "job_1:seg_3",
    }
    assert all(item["covered_facet_ids"] == [] for item in result["selected"])


def test_real_offline_e5_multilingual_smoke(tmp_path: Path) -> None:
    module = _module()
    try:
        directory = module.model_directory()
    except module.SelectionError:
        pytest.skip("Windows model cache is unavailable")
    if not (directory / "model.onnx").is_file():
        pytest.skip("Pinned E5 model has not been installed")
    pytest.importorskip("numpy")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    embedder = module.E5OnnxEmbedder(directory)
    vectors = embedder.encode(
        [
            "query: 沈阳故宫的建筑画面",
            "passage: 沈阳故宫建筑群的航拍画面",
            "passage: 一只猫在厨房里吃饭",
            "query: autumn mountain foliage in Liaoning",
            "passage: red maple leaves in the Liaoning mountains",
            "passage: a software configuration file",
        ]
    )
    assert module._dot(vectors[0], vectors[1]) > module._dot(vectors[0], vectors[2])
    assert module._dot(vectors[3], vectors[4]) > module._dot(vectors[3], vectors[5])
    request, _ = _fixture(tmp_path)
    result = module.select(
        request,
        embedder=embedder,
        output_dir=tmp_path / "selection",
        cache_dir=tmp_path / "cache",
        route={
            "schema_version": "video-material-selection-route/v1",
            "workflow_id": "vmw_1",
            "requested_mode": "auto",
            "route": "embedding",
            "reason": "host_subagent_unavailable",
            "capability_status": "unavailable",
        },
    )
    assert result["selection_method"] == "embedding"
    assert result["selection_route"]["reason"] == "host_subagent_unavailable"
    long_vector = embedder.encode(["passage: " + "城市的建筑与街道。" * 800 + "尾部的红枫叶。"])
    assert len(long_vector) == 1
    assert len(long_vector[0]) == len(vectors[0])


def test_candidate_embedding_text_includes_later_structured_evidence() -> None:
    module = _module()
    text = module._candidate_text(
        {
            "candidate_segment_id": "job_1:seg_1",
            "visual_summary": "城市",
            "actions": ["攀登山脊"],
            "transcript": {"text": "观众谈到秋天的红叶"},
        }
    )
    assert "攀登山脊" in text
    assert "秋天的红叶" in text


def test_checksum_locked_runtime_runs_selector_cli(tmp_path: Path) -> None:
    module = _module()
    try:
        runtime = module.model_directory() / "runtime" / "Scripts" / "python.exe"
    except module.SelectionError:
        pytest.skip("Windows embedding runtime is unavailable")
    if not runtime.is_file():
        pytest.skip("Checksum-locked embedding runtime has not been installed")
    request, _ = _fixture(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    route_path = tmp_path / "route.json"
    route_path.write_text(
        json.dumps(
            {
                "schema_version": "video-material-selection-route/v1",
                "workflow_id": "vmw_1",
                "requested_mode": "auto",
                "route": "embedding",
                "reason": "host_subagent_unavailable",
                "capability_status": "unavailable",
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            str(runtime),
            str(SCRIPT),
            "--request",
            str(request_path),
            "--route",
            str(route_path),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--output-dir",
            str(tmp_path / "selection"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(
        (tmp_path / "selection" / "selection-result.json").read_text(encoding="utf-8")
    )
    assert result["selection_method"] == "embedding"
