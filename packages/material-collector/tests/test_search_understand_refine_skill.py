from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SKILL_ROOT = (
    Path(__file__).parents[3]
    / "skills"
    / "search-understand-refine-video-materials"
)


def _load_script(name: str) -> ModuleType:
    path = SKILL_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _semantic_contracts(
    tmp_path: Path,
    *platform_scope: str,
) -> tuple[Path, Path]:
    if not platform_scope:
        platform_scope = ("bilibili", "douyin", "xiaohongshu")
    input_path = tmp_path / "input.json"
    plans_path = tmp_path / "plans.json"
    _write_json(
        input_path,
        {
            "schema_version": "1.0",
            "theme": "城市",
            "full_script": "城市醒来。人群流动。",
            "segments": [
                {"segment_id": "seg_001", "order": 1, "text": "城市醒来。"},
                {"segment_id": "seg_002", "order": 2, "text": "人群流动。"},
            ],
        },
    )
    _write_json(
        plans_path,
        {
            "schema_version": "3.0",
            "platform_scope": list(platform_scope),
            "plans": [
                {
                    "query_plan_id": "qp_seg_001",
                    "segment_id": "seg_001",
                    "visual_strategy": "城市建立镜头",
                    "required_visual_facets": [
                        {"facet_id": "facet_city", "description": "城市清晨"}
                    ],
                    "platform_branches": [
                        {
                            "platform": platform,
                            "language": "zh-CN" if platform not in {"youtube", "tiktok"} else "en",
                            "queries": [{
                                "query_id": f"q_city_{platform}",
                                "text": "城市 清晨 航拍" if platform not in {"youtube", "tiktok"} else "city morning aerial",
                                "facet_ids": ["facet_city"],
                                "budget": 20,
                            }],
                        }
                        for platform in platform_scope
                    ],
                },
                {
                    "query_plan_id": "qp_seg_002",
                    "segment_id": "seg_002",
                    "visual_strategy": "通勤流动",
                    "required_visual_facets": [
                        {"facet_id": "facet_people", "description": "通勤人群"}
                    ],
                    "platform_branches": [
                        {
                            "platform": platform,
                            "language": "zh-CN" if platform not in {"youtube", "tiktok"} else "en",
                            "queries": [{
                                "query_id": f"q_people_{platform}",
                                "text": "早高峰 通勤 人群" if platform not in {"youtube", "tiktok"} else "rush hour commuters",
                                "facet_ids": ["facet_people"],
                                "budget": 20,
                            }],
                        }
                        for platform in platform_scope
                    ],
                },
            ],
        },
    )
    return input_path, plans_path


def _selection_evidence(
    tmp_path: Path,
    workflow: dict[str, Any],
    *,
    round_number: int = 1,
) -> dict[str, Any]:
    path = tmp_path / f"selection-result-{round_number}.json"
    payload = {
        "schema_version": "segment-selection-output/v2",
        "selection_id": f"sel_{round_number}",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "query_plan_id": "qp_seg_001",
        "segment_id": "seg_001",
        "round_number": round_number,
        "status": "selected",
        "selected": [
            {
                "candidate_segment_id": "job_1:segment_1",
                "understanding_job_id": "job_1",
                "media_unit_ids": ["bilibili:1"],
                "start_ms": 0,
                "end_ms": 1000,
            }
        ],
    }
    _write_json(path, payload)
    return {
        "selection_id": payload["selection_id"],
        "selection_result_path": str(path.resolve()),
        "selection_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "selected_candidate_segment_ids": ["job_1:segment_1"],
    }


def test_workflow_initialization_and_gap_round_preserve_budget(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    root = tmp_path / "workflow"

    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=root,
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    initial, round_input, initial_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=None,
        batch=None,
    )

    assert initial["round_number"] == 1
    assert initial["budget"]["round_admission_limit"] == 6
    assert initial["collector_runner_arguments"] == {
        "MaxRounds": 3,
        "MaxVideos": 18,
    }
    assert [item["segment_id"] for item in round_input["segments"]] == ["seg_001"]
    assert len(initial_plans["plans"]) == 1
    other_profile = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow-cinematic",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="cinematic",
        max_rounds=3,
        max_videos=18,
    )
    assert other_profile["workflow_id"] != workflow["workflow_id"]

    decision = {
        "schema_version": "video-material-gap-decision/v1",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "round_number": 1,
        "status": "insufficient",
        "previous_query_texts": [
            "bilibili:城市 清晨 航拍",
            "douyin:城市 清晨 航拍",
            "xiaohongshu:城市 清晨 航拍",
        ],
        "evidence": _selection_evidence(tmp_path, workflow),
        "facet_assessment": [
            {
                "facet_id": "facet_city",
                "status": "missing",
                "reason": "缺少街道近景",
            }
        ],
        "gaps": [
            {
                "gap_id": "gap_1",
                "facet_ids": ["facet_city"],
                "description": "缺少街道近景",
            }
        ],
        "next_queries": [
            {
                "query_id": f"q_city_round_002_{platform}",
                "text": "城市 清晨 街道 近景",
                "platform": platform,
                "language": "zh-CN",
                "facet_ids": ["facet_city"],
                "budget": 20,
            }
            for platform in ("bilibili", "douyin", "xiaohongshu")
        ],
    }
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
        "budget": {"occupied_media_unit_count": 6},
    }
    refined, _refined_input, refined_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=decision,
        batch=batch,
        previous_query_plans=[initial_plans],
    )

    assert refined["round_number"] == 2
    assert refined["budget"]["round_admission_limit"] == 6
    assert refined["collector_runner_arguments"] == {
        "MaxRounds": 2,
        "MaxVideos": 12,
    }
    assert [
        branch["platform"]
        for branch in refined_plans["plans"][0]["platform_branches"]
    ] == ["bilibili", "douyin", "xiaohongshu"]
    mismatched_batch = {**batch, "platform_scope": ["bilibili"]}
    with pytest.raises(round_module.RoundPlanError, match="batch platform scope"):
        round_module.plan_round(
            workflow=workflow,
            segment_id="seg_001",
            decision=decision,
            batch=mismatched_batch,
            previous_query_plans=[initial_plans],
        )


def test_workflow_and_initial_round_freeze_a_bilibili_only_scope(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path, "bilibili")
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()

    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    _round, _round_input, round_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=None,
        batch=None,
    )

    assert workflow["platform_scope"] == ["bilibili"]
    assert round_plans["schema_version"] == "3.0"
    assert round_plans["platform_scope"] == ["bilibili"]
    assert round_plans["plans"][0]["platform_branches"][0]["platform"] == "bilibili"


def test_gap_round_cannot_broaden_a_bilibili_only_scope(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path, "bilibili")
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    _round, _round_input, previous_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=None,
        batch=None,
    )
    decision = {
        "schema_version": "video-material-gap-decision/v1",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "round_number": 1,
        "status": "insufficient",
        "previous_query_texts": ["bilibili:城市 清晨 航拍"],
        "evidence": _selection_evidence(tmp_path, workflow),
        "facet_assessment": [
            {
                "facet_id": "facet_city",
                "status": "missing",
                "reason": "缺少街道近景",
            }
        ],
        "gaps": [
            {
                "gap_id": "gap_1",
                "facet_ids": ["facet_city"],
                "description": "缺少街道近景",
            }
        ],
        "next_queries": [
            {
                "query_id": "q_scope_escape",
                "text": "城市 清晨 街道 近景",
                "platform": "douyin",
                "language": "zh-CN",
                "facet_ids": ["facet_city"],
                "budget": 20,
            }
        ],
    }
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "platform_scope": ["bilibili"],
        "budget": {"occupied_media_unit_count": 6},
    }

    with pytest.raises(round_module.RoundPlanError, match="in-scope platform"):
        round_module.plan_round(
            workflow=workflow,
            segment_id="seg_001",
            decision=decision,
            batch=batch,
            previous_query_plans=[previous_plans],
        )


def test_workflow_normalizes_optional_segment_and_query_plan_ids(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    collection = json.loads(input_path.read_text(encoding="utf-8"))
    collection["segments"][0].pop("segment_id")
    plans["plans"][0]["segment_id"] = "seg_001"
    plans["plans"][0].pop("query_plan_id")
    _write_json(plans_path, plans)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()

    _write_json(input_path, collection)
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )

    assert workflow["plans"][0] == {
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "max_rounds": 3,
        "max_videos": 18,
    }


def test_external_collector_normalization_matches_in_process_contracts(
    tmp_path: Path,
) -> None:
    module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    collector_value = shutil.which(
        "material-collector",
        path=str(Path(sys.executable).parent),
    )
    assert collector_value is not None
    expected = module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "in-process",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    external = module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "external",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
        collector_path=Path(collector_value),
    )

    assert external["workflow_id"] == expected["workflow_id"]
    assert (
        Path(external["semantic_input"]["collection_input_path"]).read_bytes()
        == Path(expected["semantic_input"]["collection_input_path"]).read_bytes()
    )
    assert (
        Path(external["semantic_input"]["initial_query_plans_path"]).read_bytes()
        == Path(expected["semantic_input"]["initial_query_plans_path"]).read_bytes()
    )


def test_in_process_workflow_reports_old_queryplans_as_version_incompatible(
    tmp_path: Path,
) -> None:
    module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plans["schema_version"] = "1.0"
    _write_json(plans_path, plans)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()

    with pytest.raises(module.WorkflowInitError) as raised:
        module.initialize(
            input_path=input_path,
            query_plans_path=plans_path,
            workflow_root=tmp_path / "workflow",
            material_workspace=material_workspace,
            semvideo_workspace=semvideo_workspace,
            semvideo_profile="default",
            max_rounds=3,
            max_videos=18,
        )

    assert raised.value.code == "contract_version_unsupported"
    assert raised.value.details == {
        "document": "query_plans",
        "received_version": "1.0",
            "supported_versions": ["3.0"],
    }


def test_workflow_cli_preserves_external_contract_version_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plans["schema_version"] = "1.0"
    _write_json(plans_path, plans)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    collector_value = shutil.which(
        "material-collector",
        path=str(Path(sys.executable).parent),
    )
    assert collector_value is not None

    return_code = module.main(
        [
            "--input",
            str(input_path),
            "--query-plans",
            str(plans_path),
            "--workflow-root",
            str(tmp_path / "workflow"),
            "--material-workspace",
            str(material_workspace),
            "--semvideo-workspace",
            str(semvideo_workspace),
            "--collector",
            collector_value,
        ]
    )

    assert return_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "contract_version_unsupported"
    assert payload["details"] == {
        "document": "query_plans",
        "received_version": "1.0",
            "supported_versions": ["3.0"],
    }


def test_round_cli_reports_frozen_old_queryplans_as_version_incompatible(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow_root = tmp_path / "workflow"
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=workflow_root,
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    frozen_plans_path = Path(
        workflow["semantic_input"]["initial_query_plans_path"]
    )
    frozen_plans = json.loads(frozen_plans_path.read_text(encoding="utf-8"))
    frozen_plans["schema_version"] = "1.0"
    _write_json(frozen_plans_path, frozen_plans)
    workflow["semantic_input"]["initial_query_plans_sha256"] = hashlib.sha256(
        round_module._canonical_bytes(frozen_plans)
    ).hexdigest()
    workflow_path = workflow_root / "workflow.json"
    _write_json(workflow_path, workflow)

    return_code = round_module.main(
        [
            "--workflow",
            str(workflow_path),
            "--segment-id",
            "seg_001",
            "--output-dir",
            str(tmp_path / "round"),
        ]
    )

    assert return_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "contract_version_unsupported"
    assert payload["details"] == {
        "document": "query_plans",
        "received_version": "1.0",
            "supported_versions": ["3.0"],
    }


def test_round_history_reports_old_queryplans_as_version_incompatible() -> None:
    module = _load_script("plan_round")

    with pytest.raises(module.RoundPlanError) as raised:
        module._query_history(
            [{"schema_version": "1.0"}],
            segment_id="seg_001",
            query_plan_id="qp_seg_001",
            expected_rounds=1,
            platform_scope=["bilibili"],
        )

    assert raised.value.code == "contract_version_unsupported"
    assert raised.value.details == {
        "document": "query_plans",
        "received_version": "1.0",
        "supported_versions": ["3.0"],
    }


def test_workflow_requires_one_branch_for_every_platform_in_scope(
    tmp_path: Path,
) -> None:
    module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plans["plans"][0]["platform_branches"] = plans["plans"][0][
        "platform_branches"
    ][:-1]
    _write_json(plans_path, plans)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()

    with pytest.raises(module.WorkflowInitError):
        module.initialize(
            input_path=input_path,
            query_plans_path=plans_path,
            workflow_root=tmp_path / "workflow",
            material_workspace=material_workspace,
            semvideo_workspace=semvideo_workspace,
            semvideo_profile="default",
            max_rounds=3,
            max_videos=18,
        )


def test_workflow_refuses_preexisting_conflicting_frozen_snapshot(
    tmp_path: Path,
) -> None:
    module = _load_script("init_workflow")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    frozen = tmp_path / "workflow" / "input" / "collection-input.json"
    _write_json(frozen, {"schema_version": "conflict"})

    with pytest.raises(module.WorkflowInitError, match="refusing to overwrite"):
        module.initialize(
            input_path=input_path,
            query_plans_path=plans_path,
            workflow_root=tmp_path / "workflow",
            material_workspace=material_workspace,
            semvideo_workspace=semvideo_workspace,
            semvideo_profile="default",
            max_rounds=3,
            max_videos=18,
        )


def test_gap_round_rejects_a_query_used_in_an_earlier_round(tmp_path: Path) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    decision = {
        "schema_version": "video-material-gap-decision/v1",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "round_number": 1,
        "status": "insufficient",
        "previous_query_texts": [
            "bilibili:城市 清晨 航拍",
            "douyin:城市 清晨 航拍",
            "xiaohongshu:城市 清晨 航拍",
        ],
        "evidence": _selection_evidence(tmp_path, workflow),
        "facet_assessment": [
            {
                "facet_id": "facet_city",
                "status": "missing",
                "reason": "仍缺少画面",
            }
        ],
        "gaps": [
            {
                "gap_id": "gap_1",
                "facet_ids": ["facet_city"],
                "description": "仍缺少画面",
            }
        ],
        "next_queries": [
            {
                "query_id": f"q_duplicate_{platform}",
                "text": " 城市  清晨 航拍 ",
                "platform": platform,
                "language": "zh-CN",
                "facet_ids": ["facet_city"],
                "budget": 20,
            }
            for platform in ("bilibili", "douyin", "xiaohongshu")
        ],
    }
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
        "budget": {"occupied_media_unit_count": 6},
    }
    _initial, _round_input, previous_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=None,
        batch=None,
    )

    with pytest.raises(round_module.RoundPlanError, match="already-used"):
        round_module.plan_round(
            workflow=workflow,
            segment_id="seg_001",
            decision=decision,
            batch=batch,
            previous_query_plans=[previous_plans],
        )


def test_gap_round_rejects_tampered_selection_evidence(tmp_path: Path) -> None:
    init_module = _load_script("init_workflow")
    round_module = _load_script("plan_round")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    _round, _round_input, previous_plans = round_module.plan_round(
        workflow=workflow,
        segment_id="seg_001",
        decision=None,
        batch=None,
    )
    evidence = _selection_evidence(tmp_path, workflow)
    evidence["selection_result_sha256"] = "0" * 64
    decision = {
        "schema_version": "video-material-gap-decision/v1",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "round_number": 1,
        "status": "insufficient",
        "previous_query_texts": ["城市 清晨 航拍"],
        "evidence": evidence,
        "facet_assessment": [
            {
                "facet_id": "facet_city",
                "status": "missing",
                "reason": "缺少街道近景",
            }
        ],
        "gaps": [
            {
                "gap_id": "gap_1",
                "facet_ids": ["facet_city"],
                "description": "缺少街道近景",
            }
        ],
        "next_queries": [
            {
                "query_id": "q_next",
                "text": "城市 清晨 街道 近景",
                "target_platforms": [
                    "bilibili",
                    "douyin",
                    "xiaohongshu",
                ],
                "facet_ids": ["facet_city"],
            }
        ],
    }
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
        "budget": {"occupied_media_unit_count": 6},
    }

    with pytest.raises(round_module.RoundPlanError, match="selection result hash"):
        round_module.plan_round(
            workflow=workflow,
            segment_id="seg_001",
            decision=decision,
            batch=batch,
            previous_query_plans=[previous_plans],
        )


def _manifest(
    workspace: Path,
    *,
    session_id: str,
    units: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "session_id": session_id,
        "workspace_path": str(workspace),
        "platform_scope": ["bilibili"],
        "session_status": "integration_required",
        "state_version": 7,
        "generated_at": "2026-07-31T00:00:00Z",
        "candidates": [
            {
                "candidate_id": f"candidate_{session_id}",
                "platform": "bilibili",
                "source_id": f"source_{session_id}",
                "canonical_url": "https://www.bilibili.com/video/BV1TEST",
                "title": "测试视频",
                "author": "作者",
                "description": None,
                "published_at": None,
                "duration_seconds": 10,
                "discoveries": [
                    {
                        "query_plan_id": "qp_seg_001",
                        "segment_id": "seg_001",
                        "query_id": "q_city",
                        "round_number": 1,
                        "rank": 1,
                    }
                ],
                "media_units": units,
            }
        ],
        "work_groups": [],
    }


def _unit(
    *,
    media_unit_id: str,
    relative_path: str,
    digest: str,
    size: int,
    eligible: bool,
) -> dict[str, Any]:
    return {
        "media_unit_id": media_unit_id,
        "title": "测试视频",
        "canonical_url": "https://www.bilibili.com/video/BV1TEST",
        "duration_seconds": 10,
        "part_index": 1,
        "metadata": {},
        "status": "downloaded",
        "review": None,
        "proxy_asset": {
            "schema_version": "1.0",
            "asset_id": f"sha256:{digest}",
            "sha256": digest,
            "relative_path": relative_path,
            "size_bytes": size,
            "quality": "low_proxy",
            "media_unit_id": media_unit_id,
            "container": "mp4",
            "duration_seconds": 10,
            "width": 1280,
            "height": 720,
        },
        "high_quality_asset": None,
        "work_group_id": "wg_1",
        "source_role": "primary" if eligible else "fallback",
        "fallback_order": 1 if eligible else 2,
        "eligible_for_understanding": eligible,
    }


def test_prepare_batch_counts_fallback_but_deduplicates_understanding_by_hash(
    tmp_path: Path,
) -> None:
    module = _load_script("prepare_understanding_batch")
    workspace = tmp_path / "materials"
    asset_path = workspace / "assets" / "sha256" / "aa" / "video.mp4"
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b"same-video")
    digest = hashlib.sha256(b"same-video").hexdigest()
    relative = asset_path.relative_to(workspace).as_posix()
    first_path = tmp_path / "result-1.json"
    second_path = tmp_path / "result-2.json"
    _write_json(
        first_path,
        _manifest(
            workspace,
            session_id="ses_1",
            units=[
                _unit(
                    media_unit_id="bilibili:primary",
                    relative_path=relative,
                    digest=digest,
                    size=len(b"same-video"),
                    eligible=True,
                ),
                _unit(
                    media_unit_id="douyin:fallback",
                    relative_path=relative,
                    digest=digest,
                    size=len(b"same-video"),
                    eligible=False,
                ),
            ],
        ),
    )
    _write_json(
        second_path,
        _manifest(
            workspace,
            session_id="ses_2",
            units=[
                _unit(
                    media_unit_id="bilibili:reused",
                    relative_path=relative,
                    digest=digest,
                    size=len(b"same-video"),
                    eligible=True,
                )
            ],
        ),
    )

    batch = module.build_batch(
        [first_path, second_path],
        profile="default",
        workflow_id="vmw_test",
        workflow_state_version=1,
        segment_id="seg_001",
        query_plan_id="qp_seg_001",
        platform_scope=["bilibili"],
        material_workspace=workspace,
        semvideo_workspace=tmp_path,
    )

    assert batch["workflow_id"] == "vmw_test"
    assert batch["segment_id"] == "seg_001"
    assert batch["budget"]["occupied_media_unit_count"] == 3
    assert batch["item_count"] == 1
    assert batch["items"][0]["media_unit_ids"] == [
        "bilibili:primary",
        "bilibili:reused",
    ]
    assert len(batch["items"][0]["sources"]) == 2


def test_prepare_batch_rejects_platform_scope_changes_between_rounds(
    tmp_path: Path,
) -> None:
    module = _load_script("prepare_understanding_batch")
    workspace = tmp_path / "materials"
    workspace.mkdir()
    first_path = tmp_path / "result-1.json"
    second_path = tmp_path / "result-2.json"
    _write_json(first_path, _manifest(workspace, session_id="ses_1", units=[]))
    second = _manifest(workspace, session_id="ses_2", units=[])
    second["platform_scope"] = ["bilibili", "douyin"]
    _write_json(second_path, second)

    with pytest.raises(module.BatchError, match="does not match"):
        module.build_batch(
            [first_path, second_path],
            profile="default",
            workflow_id="vmw_test",
            workflow_state_version=1,
            segment_id="seg_001",
            query_plan_id="qp_seg_001",
            platform_scope=["bilibili"],
            material_workspace=workspace,
            semvideo_workspace=tmp_path,
        )


def test_prepare_batch_rejects_candidate_outside_workflow_scope(
    tmp_path: Path,
) -> None:
    module = _load_script("prepare_understanding_batch")
    workspace = tmp_path / "materials"
    workspace.mkdir()
    result_path = tmp_path / "result.json"
    result = _manifest(workspace, session_id="ses_1", units=[])
    result["candidates"][0]["platform"] = "douyin"
    _write_json(result_path, result)

    with pytest.raises(module.BatchError, match="candidate.*outside"):
        module.build_batch(
            [result_path],
            profile="default",
            workflow_id="vmw_test",
            workflow_state_version=1,
            segment_id="seg_001",
            query_plan_id="qp_seg_001",
            platform_scope=["bilibili"],
            material_workspace=workspace,
            semvideo_workspace=tmp_path,
        )


def test_prepare_batch_rejects_collection_from_another_workspace(
    tmp_path: Path,
) -> None:
    module = _load_script("prepare_understanding_batch")
    expected_workspace = tmp_path / "expected"
    other_workspace = tmp_path / "other"
    expected_workspace.mkdir()
    other_workspace.mkdir()
    asset = other_workspace / "video.mp4"
    asset.write_bytes(b"video")
    digest = hashlib.sha256(b"video").hexdigest()
    result = tmp_path / "result.json"
    _write_json(
        result,
        _manifest(
            other_workspace,
            session_id="ses_other",
            units=[
                _unit(
                    media_unit_id="bilibili:other",
                    relative_path="video.mp4",
                    digest=digest,
                    size=5,
                    eligible=True,
                )
            ],
        ),
    )

    with pytest.raises(module.BatchError, match="frozen material workspace"):
        module.build_batch(
            [result],
            profile="default",
            workflow_id="vmw_test",
            workflow_state_version=1,
            segment_id="seg_001",
            query_plan_id="qp_seg_001",
            platform_scope=["bilibili"],
            material_workspace=expected_workspace,
            semvideo_workspace=tmp_path,
        )


def test_collect_semvideo_catalog_reads_all_pages_and_full_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("collect_semvideo_catalog")
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "workflow_id": "vmw_test",
        "workflow_state_version": 1,
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "platform_scope": ["bilibili"],
        "material_workspace": str(tmp_path),
        "semvideo_workspace": str(tmp_path),
        "items": [
            {
                "item_id": "proxy_1",
                "asset_sha256": "a" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-test",
                "media_unit_ids": ["bilibili:1"],
                "sources": [
                    {
                        "collection_session_id": "ses_1",
                        "work_group_id": "wg_1",
                    }
                ],
            }
        ],
    }
    jobs = {
        "schema_version": "video-material-understanding-jobs/v1",
        "items": [
            {
                "item_id": "proxy_1",
                "asset_sha256": "a" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-test",
                "job_id": "job_1",
            }
        ],
    }

    def fake_run(
        _command: Path,
        arguments: list[str],
        _timeout: int,
    ) -> dict[str, Any]:
        if arguments[:2] == ["job", "status"]:
            return {"schema_version": 1, "job_id": "job_1", "state": "completed"}
        if arguments[:2] == ["segment", "list"]:
            offset = int(arguments[arguments.index("--offset") + 1])
            items = (
                [{"segment_id": "segment_1"}, {"segment_id": "segment_2"}]
                if offset == 0
                else []
            )
            return {
                "schema_version": 1,
                "job_id": "job_1",
                "total": 2,
                "offset": offset,
                "limit": 50,
                "items": items,
            }
        segment_id = arguments[3]
        if segment_id == "segment_1":
            start_ms, end_ms, ordinal = 0, 1000, 0
        else:
            start_ms, end_ms, ordinal = 1000, 2500, 1
        return {
            "schema_version": 1,
            "segment_id": segment_id,
            "job_id": "job_1",
            "ordinal": ordinal,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "title": segment_id,
            "short_summary": "摘要",
            "detailed_summary": "详细摘要",
            "visual_summary": "画面",
            "topics": [],
            "participants": [],
            "locations": [],
            "organizations": [],
            "objects": [],
            "actions": [],
            "keywords": [],
            "transcript": {"text": ""},
            "confidence": 0.9,
            "review_required": False,
            "review_reasons": [],
            "artifacts": {},
            "provenance": {},
        }

    monkeypatch.setattr(module, "_run_json", fake_run)

    catalogs, candidates = module.collect_catalog(
        batch=batch,
        jobs=jobs,
        command=tmp_path / "semvideo.exe",
        workspace=tmp_path,
    )

    assert catalogs["status"] == "complete"
    assert catalogs["workflow_id"] == "vmw_test"
    assert catalogs["segment_id"] == "seg_001"
    assert catalogs["complete_catalog_count"] == 1
    assert catalogs["catalogs"][0]["covered_ranges_ms"] == [[0, 2500]]
    assert [item["candidate_segment_id"] for item in candidates] == [
        "job_1:segment_1",
        "job_1:segment_2",
    ]
    assert candidates[0]["collection_session_ids"] == ["ses_1"]


def test_record_understanding_job_is_idempotent_and_rejects_rebinding() -> None:
    module = _load_script("record_understanding_job")
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "platform_scope": ["bilibili"],
        "items": [
            {
                "item_id": "proxy_1",
                "asset_sha256": "a" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-test",
            }
        ],
    }
    first = module.record_job(
        batch=batch,
        jobs=None,
        item_id="proxy_1",
        response={"job_id": "job_1", "state": "running"},
        response_hash="b" * 64,
    )
    repeated = module.record_job(
        batch=batch,
        jobs=first,
        item_id="proxy_1",
        response={"job_id": "job_1", "state": "running"},
        response_hash="b" * 64,
    )

    assert repeated == first
    with pytest.raises(module.JobRecordError, match="already bound"):
        module.record_job(
            batch=batch,
            jobs=first,
            item_id="proxy_1",
            response={"job_id": "job_2", "state": "running"},
            response_hash="c" * 64,
        )


def test_understanding_jobs_carry_forward_only_matching_batch_identity() -> None:
    module = _load_script("record_understanding_job")
    batch = {
        "schema_version": "video-material-understanding-batch/v2",
        "platform_scope": ["bilibili"],
        "items": [
            {
                "item_id": "proxy_1",
                "asset_sha256": "a" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-test",
            },
            {
                "item_id": "proxy_2",
                "asset_sha256": "b" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-new",
            },
        ],
    }
    previous = {
        "schema_version": "video-material-understanding-jobs/v1",
        "items": [
            {
                "item_id": "proxy_1",
                "asset_sha256": "a" * 64,
                "semvideo_profile": "default",
                "semvideo_idempotency_key": "material-proxy-v1-test",
                "job_id": "job_1",
                "submission_state": "running",
                "process_response_sha256": "c" * 64,
            }
        ],
    }

    carried = module.merge_jobs(batch=batch, job_artifacts=[previous])

    assert [item["item_id"] for item in carried["items"]] == ["proxy_1"]
    mismatched = json.loads(json.dumps(previous))
    mismatched["items"][0]["semvideo_profile"] = "cinematic"
    with pytest.raises(module.JobRecordError, match="does not match batch"):
        module.merge_jobs(batch=batch, job_artifacts=[mismatched])


def test_selection_request_rejects_catalog_candidate_hash_mismatch(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    selection_module = _load_script("build_selection_request")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    candidate_path = tmp_path / "candidate-segments.jsonl"
    candidate_path.write_text(
        json.dumps({"candidate_segment_id": "job_1:segment_1"}) + "\n",
        encoding="utf-8",
    )
    catalogs = {
        "schema_version": "video-material-understanding-catalogs/v1",
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": "seg_001",
        "query_plan_id": "qp_seg_001",
        "catalogs": [
            {
                "item_id": "proxy_1",
                "understanding_job_id": "job_1",
                "status": "complete",
                "collection_session_ids": ["ses_1"],
            }
        ],
        "candidate_artifact": {
            "path": str(candidate_path),
            "sha256": "0" * 64,
            "candidate_count": 1,
        },
    }

    with pytest.raises(
        selection_module.SelectionRequestError,
        match="candidate artifact does not match",
    ):
        selection_module.build_request(
            workflow=workflow,
            segment_id="seg_001",
            round_number=1,
            catalogs=catalogs,
            candidate_path=candidate_path,
            narration_duration_ms=None,
        )


def test_workflow_state_persists_per_segment_terminal_outcomes(
    tmp_path: Path,
) -> None:
    init_module = _load_script("init_workflow")
    state_module = _load_script("record_segment_state")
    input_path, plans_path = _semantic_contracts(tmp_path)
    material_workspace = tmp_path / "materials"
    semvideo_workspace = tmp_path / "semvideo"
    material_workspace.mkdir()
    semvideo_workspace.mkdir()
    workflow = init_module.initialize(
        input_path=input_path,
        query_plans_path=plans_path,
        workflow_root=tmp_path / "workflow",
        material_workspace=material_workspace,
        semvideo_workspace=semvideo_workspace,
        semvideo_profile="default",
        max_rounds=3,
        max_videos=18,
    )
    selection = tmp_path / "selection.json"
    gap = tmp_path / "gap.json"
    _write_json(selection, {"schema_version": "segment-selection-output/v2"})
    _write_json(gap, {"schema_version": "video-material-gap-decision/v1"})

    first = state_module.record_state(
        workflow=workflow,
        state=None,
        segment_id="seg_001",
        status="sufficient",
        round_number=1,
        result_artifact=selection,
        collection_session_ids=["ses_1"],
        semvideo_job_ids=["job_1"],
    )
    final = state_module.record_state(
        workflow=workflow,
        state=first,
        segment_id="seg_002",
        status="stopped_with_gaps",
        round_number=3,
        result_artifact=gap,
        collection_session_ids=["ses_2"],
        semvideo_job_ids=["job_2"],
    )

    assert first["status"] == "active"
    assert final["status"] == "completed_with_gaps"
    assert final["revision"] == 2
    assert [item["status"] for item in final["segments"]] == [
        "sufficient",
        "stopped_with_gaps",
    ]


def test_skill_declares_all_three_tool_boundaries() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "material-collector" in skill
    assert "Semvideo" in skill
    assert "select-video-segments" in skill
