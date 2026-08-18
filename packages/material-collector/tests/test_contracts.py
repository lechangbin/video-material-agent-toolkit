from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from material_collector.core.contracts import (
    contract_schema_bundle,
    normalize_contracts,
)
from material_collector.core.errors import ContractError

REPOSITORY_ROOT = Path(__file__).parents[3]
CONTRACT_REFERENCES = REPOSITORY_ROOT / "skills" / "collect-video-materials" / "references"


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
        "schema_version": "2.0",
        "platform_scope": ["bilibili", "douyin", "xiaohongshu"],
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


def test_bundled_contract_schemas_match_authoritative_pydantic_models() -> None:
    schemas = contract_schema_bundle()

    assert json.loads(
        (CONTRACT_REFERENCES / "schemas" / "collection-input-1.0.schema.json").read_text(
            encoding="utf-8"
        )
    ) == schemas["collection_input"]
    assert json.loads(
        (CONTRACT_REFERENCES / "schemas" / "query-plans-2.0.schema.json").read_text(
            encoding="utf-8"
        )
    ) == schemas["query_plans"]


def test_bundled_minimal_examples_normalize_with_authoritative_models() -> None:
    collection = json.loads(
        (CONTRACT_REFERENCES / "examples" / "collection-input-1.0.min.json").read_text(
            encoding="utf-8"
        )
    )
    query_plans = json.loads(
        (CONTRACT_REFERENCES / "examples" / "query-plans-2.0.min.json").read_text(
            encoding="utf-8"
        )
    )

    normalized = normalize_contracts(collection, query_plans)

    assert normalized.collection_input.schema_version == "1.0"
    assert normalized.query_plans.schema_version == "2.0"
    assert normalized.query_plans.platform_scope == ("bilibili",)


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


@pytest.mark.parametrize(
    "platform_scope",
    [[], ["bilibili", "bilibili"]],
)
def test_platform_scope_must_be_non_empty_and_unique(
    platform_scope: list[str],
) -> None:
    plans = query_plan_document()
    plans["platform_scope"] = platform_scope

    with pytest.raises(ContractError):
        normalize_contracts(collection_document(), plans)


def test_every_query_must_target_the_complete_platform_scope() -> None:
    plans = query_plan_document()
    plans["plans"][0]["initial_queries"][0]["target_platforms"] = ["bilibili"]

    with pytest.raises(ContractError) as captured:
        normalize_contracts(collection_document(), plans)

    assert captured.value.details["platform_scope"] == (
        "bilibili",
        "douyin",
        "xiaohongshu",
    )
    assert captured.value.details["target_platforms"] == ("bilibili",)


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
