from __future__ import annotations

from typing import Any

import pytest

from material_collector.core.contracts import normalize_contracts
from material_collector.core.errors import ContractError


def collection_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "full_script": "第一段\n第二段",
        "segments": [
            {"text": "第二段", "order": 2},
            {"text": "第一段", "order": 1},
        ],
    }


def query_plan_document() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "plans": [
            {
                "segment_id": "seg_002",
                "visual_strategy": "第一段画面",
                "required_visual_facets": [
                    {"facet_id": "facet_first", "description": "第一段主体"}
                ],
                "initial_queries": [
                    {
                        "query_id": "query_first",
                        "text": " 第一段   素材 ",
                        "target_platforms": [
                            "xiaohongshu",
                            "bilibili",
                            "douyin",
                        ],
                        "facet_ids": ["facet_first"],
                    }
                ],
            },
            {
                "segment_id": "seg_001",
                "visual_strategy": "第二段画面",
                "required_visual_facets": [
                    {"facet_id": "facet_second", "description": "第二段主体"}
                ],
                "initial_queries": [
                    {
                        "query_id": "query_second",
                        "text": "第二段素材",
                        "target_platforms": [
                            "douyin",
                            "xiaohongshu",
                            "bilibili",
                        ],
                        "facet_ids": ["facet_second"],
                    }
                ],
            },
        ],
    }


def test_contracts_generate_ids_and_canonicalize_order() -> None:
    contracts = normalize_contracts(collection_document(), query_plan_document())

    assert [segment.segment_id for segment in contracts.collection_input.segments] == [
        "seg_002",
        "seg_001",
    ]
    assert [plan.segment_id for plan in contracts.query_plans.plans] == [
        "seg_002",
        "seg_001",
    ]
    assert contracts.query_plans.plans[0].query_plan_id == "qp_seg_002"
    query = contracts.query_plans.plans[0].initial_queries[0]
    assert query.text == "第一段 素材"
    assert query.target_platforms == ("bilibili", "douyin", "xiaohongshu")
    assert contracts.warnings == ()


def test_query_plan_cannot_override_runtime_constraints() -> None:
    plans = query_plan_document()
    plans["plans"][0]["max_videos"] = 100

    with pytest.raises(ContractError) as captured:
        normalize_contracts(collection_document(), plans)

    assert captured.value.code == "contract_invalid"
    assert captured.value.details["document"] == "query_plans"


def test_query_plan_must_cover_every_platform() -> None:
    plans = query_plan_document()
    plans["plans"][0]["initial_queries"][0]["target_platforms"] = ["bilibili"]

    with pytest.raises(ContractError) as captured:
        normalize_contracts(collection_document(), plans)

    assert captured.value.details["missing_platforms"] == [
        "douyin",
        "xiaohongshu",
    ]


def test_every_segment_requires_exactly_one_query_plan() -> None:
    plans = query_plan_document()
    plans["plans"].pop()

    with pytest.raises(ContractError) as captured:
        normalize_contracts(collection_document(), plans)

    assert captured.value.details["missing_segment_ids"] == ["seg_001"]


def test_script_mismatch_is_a_warning_not_a_rejection() -> None:
    collection = collection_document()
    collection["full_script"] = "不完全相同"

    contracts = normalize_contracts(collection, query_plan_document())

    assert [warning.code for warning in contracts.warnings] == [
        "script_segment_text_mismatch"
    ]
