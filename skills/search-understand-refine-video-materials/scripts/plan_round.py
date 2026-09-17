"""Create one immutable collector round from initial plans or a gap decision."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKFLOW_SCHEMA = "video-material-workflow/v1"
DECISION_SCHEMA = "video-material-gap-decision/v1"
BATCH_SCHEMA = "video-material-understanding-batch/v2"
ROUND_SCHEMA = "video-material-search-round/v1"
SELECTION_SCHEMA = "segment-selection-output/v2"
PLATFORMS = ("bilibili", "douyin", "xiaohongshu", "youtube", "tiktok")


class RoundPlanError(RuntimeError):
    """A search round cannot be derived without breaking workflow lineage."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "search_round_invalid",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class SearchBudgetExhausted(RoundPlanError):
    """No additional Collector round is admissible."""


def _queryplans_version_error(received_version: Any) -> RoundPlanError:
    return RoundPlanError(
        "query_plans schema version is unsupported.",
        code="contract_version_unsupported",
        details={
            "document": "query_plans",
            "received_version": received_version,
            "supported_versions": ["3.0"],
        },
    )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoundPlanError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise RoundPlanError(f"JSON artifact must contain an object: {path}")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _verified_snapshot(path_value: str, expected_hash: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve(strict=True)
    value = _load_object(path)
    if hashlib.sha256(_canonical_bytes(value)).hexdigest() != expected_hash:
        raise RoundPlanError(f"frozen semantic input hash mismatch: {path}")
    return value


def _plan_ref(workflow: dict[str, Any], segment_id: str) -> dict[str, Any]:
    plans = workflow.get("plans")
    if not isinstance(plans, list):
        raise RoundPlanError("workflow plans are invalid")
    match = next(
        (
            plan
            for plan in plans
            if isinstance(plan, dict) and plan.get("segment_id") == segment_id
        ),
        None,
    )
    if match is None:
        raise RoundPlanError(f"workflow does not contain segment: {segment_id}")
    return match


def _source_contracts(
    workflow: dict[str, Any],
    segment_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    semantic_input = workflow.get("semantic_input")
    if not isinstance(semantic_input, dict):
        raise RoundPlanError("workflow semantic_input is invalid")
    collection_input = _verified_snapshot(
        semantic_input["collection_input_path"],
        semantic_input["collection_input_sha256"],
    )
    query_plans = _verified_snapshot(
        semantic_input["initial_query_plans_path"],
        semantic_input["initial_query_plans_sha256"],
    )
    if query_plans.get("schema_version") != "3.0":
        raise _queryplans_version_error(query_plans.get("schema_version"))
    segments = collection_input.get("segments")
    plans = query_plans.get("plans")
    if not isinstance(segments, list) or not isinstance(plans, list):
        raise RoundPlanError("frozen input documents are invalid")
    segment = next(
        (
            item
            for item in segments
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    plan = next(
        (
            item
            for item in plans
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    if segment is None or plan is None:
        raise RoundPlanError("segment is missing from frozen semantic input")
    return collection_input, query_plans, segment, plan


def _validate_next_queries(
    decision: dict[str, Any],
    plan: dict[str, Any],
    previous_query_texts: set[str],
    platform_scope: list[str],
) -> list[dict[str, Any]]:
    queries = decision.get("next_queries")
    facets = plan.get("required_visual_facets")
    if not isinstance(queries, list) or not queries:
        raise RoundPlanError("an insufficient decision must contain next_queries")
    if not isinstance(facets, list):
        raise RoundPlanError("original visual facets are invalid")
    known_facets = {
        item["facet_id"]
        for item in facets
        if isinstance(item, dict) and isinstance(item.get("facet_id"), str)
    }
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for query in queries:
        if not isinstance(query, dict):
            raise RoundPlanError("next query entries must be objects")
        query_id = query.get("query_id")
        text = query.get("text")
        platform = query.get("platform")
        language = query.get("language")
        facet_ids = query.get("facet_ids")
        budget = query.get("budget", 20)
        if (
            not isinstance(query_id, str)
            or not isinstance(text, str)
            or not text.strip()
            or platform not in platform_scope
            or not isinstance(language, str)
            or not language.strip()
            or not isinstance(facet_ids, list)
            or not facet_ids
            or not set(facet_ids).issubset(known_facets)
            or not isinstance(budget, int)
            or not 1 <= budget <= 100
        ):
            raise RoundPlanError(
                "next query must name one in-scope platform, language, budget, and known facets"
            )
        text_key = f"{platform}:" + " ".join(text.split()).casefold()
        if text_key in previous_query_texts:
            raise RoundPlanError("next query repeats already-used query text")
        if query_id in seen_ids or text_key in seen_text:
            raise RoundPlanError("next query IDs and text must be unique")
        seen_ids.add(query_id)
        seen_text.add(text_key)
        normalized.append(
            {
                "query_id": query_id,
                "text": " ".join(text.split()),
                "platform": platform,
                "language": " ".join(language.split()),
                "facet_ids": facet_ids,
                "budget": budget,
            }
        )
    return normalized


def _query_history(
    artifacts: list[dict[str, Any]],
    *,
    segment_id: str,
    query_plan_id: str,
    expected_rounds: int,
    platform_scope: list[str],
) -> set[str]:
    if len(artifacts) != expected_rounds:
        raise RoundPlanError(
            "supplemental search requires one query plan artifact per prior round"
        )
    history: set[str] = set()
    artifact_hashes: set[str] = set()
    for artifact in artifacts:
        artifact_hash = hashlib.sha256(_canonical_bytes(artifact)).hexdigest()
        if artifact_hash in artifact_hashes:
            raise RoundPlanError("previous query plan artifacts must be unique")
        artifact_hashes.add(artifact_hash)
        if artifact.get("schema_version") != "3.0":
            raise _queryplans_version_error(artifact.get("schema_version"))
        if artifact.get("platform_scope") != platform_scope:
            raise RoundPlanError("previous query plan platform scope does not match workflow")
        plans = artifact.get("plans")
        if not isinstance(plans, list) or len(plans) != 1:
            raise RoundPlanError(
                "previous query plan artifact must contain exactly one plan"
            )
        plan = plans[0]
        if (
            not isinstance(plan, dict)
            or plan.get("segment_id") != segment_id
            or plan.get("query_plan_id") != query_plan_id
        ):
            raise RoundPlanError("previous query plan lineage does not match workflow")
        branches = plan.get("platform_branches")
        if not isinstance(branches, list) or not branches:
            raise RoundPlanError("previous query plan does not contain platform branches")
        for branch in branches:
            platform = branch.get("platform") if isinstance(branch, dict) else None
            queries = branch.get("queries") if isinstance(branch, dict) else None
            if platform not in platform_scope or not isinstance(queries, list):
                raise RoundPlanError("previous query plan contains an invalid platform branch")
            for query in queries:
                text = query.get("text") if isinstance(query, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise RoundPlanError("previous query plan contains invalid query text")
                history.add(f"{platform}:" + " ".join(text.split()).casefold())
    return history


def _validate_selection_evidence(
    decision: dict[str, Any],
    workflow: dict[str, Any],
    *,
    segment_id: str,
    query_plan_id: str,
    round_number: int,
) -> None:
    evidence = decision.get("evidence")
    if not isinstance(evidence, dict):
        raise RoundPlanError("gap decision is missing selection evidence")
    path_value = evidence.get("selection_result_path")
    expected_hash = evidence.get("selection_result_sha256")
    selection_id = evidence.get("selection_id")
    selected_ids = evidence.get("selected_candidate_segment_ids")
    if (
        not isinstance(path_value, str)
        or not isinstance(expected_hash, str)
        or not isinstance(selection_id, str)
        or not isinstance(selected_ids, list)
        or not all(isinstance(value, str) for value in selected_ids)
    ):
        raise RoundPlanError("gap decision selection evidence is invalid")
    path = Path(path_value).expanduser().resolve(strict=True)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise RoundPlanError("selection result hash does not match gap evidence")
    selection = _load_object(path)
    if (
        selection.get("schema_version") != SELECTION_SCHEMA
        or selection.get("selection_id") != selection_id
        or selection.get("workflow_id") != workflow.get("workflow_id")
        or selection.get("workflow_state_version") != workflow.get("state_version")
        or selection.get("segment_id") != segment_id
        or selection.get("query_plan_id") != query_plan_id
        or selection.get("round_number") != round_number
        or selection.get("status") != "selected"
    ):
        raise RoundPlanError("selection result lineage does not match gap decision")
    selected = selection.get("selected")
    if not isinstance(selected, list):
        raise RoundPlanError("selection result selected records are invalid")
    actual_ids = [
        item.get("candidate_segment_id")
        for item in selected
        if isinstance(item, dict)
        and isinstance(item.get("candidate_segment_id"), str)
    ]
    if len(actual_ids) != len(selected) or actual_ids != selected_ids:
        raise RoundPlanError("gap evidence selected candidates do not match result")


def _validate_gap_structure(
    decision: dict[str, Any],
    original_plan: dict[str, Any],
) -> None:
    facets = original_plan.get("required_visual_facets")
    if not isinstance(facets, list):
        raise RoundPlanError("original visual facets are invalid")
    known_facets = {
        item["facet_id"]
        for item in facets
        if isinstance(item, dict) and isinstance(item.get("facet_id"), str)
    }
    assessments = decision.get("facet_assessment")
    gaps = decision.get("gaps")
    if not isinstance(assessments, list) or not assessments:
        raise RoundPlanError("an insufficient decision must assess visual facets")
    assessed: set[str] = set()
    for assessment in assessments:
        if not isinstance(assessment, dict):
            raise RoundPlanError("facet assessment entries must be objects")
        facet_id = assessment.get("facet_id")
        if (
            facet_id not in known_facets
            or facet_id in assessed
            or assessment.get("status") not in {"covered", "partial", "missing"}
            or not isinstance(assessment.get("reason"), str)
            or not assessment["reason"].strip()
        ):
            raise RoundPlanError("facet assessment is invalid")
        assessed.add(facet_id)
    if assessed != known_facets:
        raise RoundPlanError("gap decision must assess every original visual facet")
    if not isinstance(gaps, list) or not gaps:
        raise RoundPlanError("an insufficient decision must contain gaps")
    for gap in gaps:
        facet_ids = gap.get("facet_ids") if isinstance(gap, dict) else None
        if (
            not isinstance(gap, dict)
            or not isinstance(gap.get("gap_id"), str)
            or not isinstance(gap.get("description"), str)
            or not gap["description"].strip()
            or not isinstance(facet_ids, list)
            or not facet_ids
            or not set(facet_ids).issubset(known_facets)
        ):
            raise RoundPlanError("gap entry is invalid")


def plan_round(
    *,
    workflow: dict[str, Any],
    segment_id: str,
    decision: dict[str, Any] | None,
    batch: dict[str, Any] | None,
    previous_query_plans: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if workflow.get("schema_version") != WORKFLOW_SCHEMA:
        raise RoundPlanError("workflow schema is unsupported")
    plan_ref = _plan_ref(workflow, segment_id)
    collection_input, query_plans, segment, original_plan = _source_contracts(
        workflow,
        segment_id,
    )
    platform_scope = workflow.get("platform_scope")
    if (
        not isinstance(platform_scope, list)
        or not platform_scope
        or platform_scope != query_plans.get("platform_scope")
        or len(platform_scope) != len(set(platform_scope))
        or not set(platform_scope).issubset(PLATFORMS)
    ):
        raise RoundPlanError("workflow platform scope is invalid")
    max_rounds = plan_ref.get("max_rounds")
    max_videos = plan_ref.get("max_videos")
    if not isinstance(max_rounds, int) or not isinstance(max_videos, int):
        raise RoundPlanError("workflow constraints are invalid")

    if decision is None:
        round_number = 1
        occupied = 0
        branches = original_plan.get("platform_branches")
        if not isinstance(branches, list) or not branches:
            raise RoundPlanError("initial query plan is invalid")
    else:
        if decision.get("schema_version") != DECISION_SCHEMA:
            raise RoundPlanError("gap decision schema is unsupported")
        if (
            decision.get("workflow_id") != workflow.get("workflow_id")
            or decision.get("workflow_state_version")
            != workflow.get("state_version")
            or decision.get("segment_id") != segment_id
            or decision.get("query_plan_id") != plan_ref.get("query_plan_id")
        ):
            raise RoundPlanError("gap decision lineage does not match the workflow")
        decision_status = decision.get("status")
        if decision_status != "insufficient":
            raise RoundPlanError("only an insufficient decision can create another round")
        previous_round = decision.get("round_number")
        if not isinstance(previous_round, int) or previous_round < 1:
            raise RoundPlanError("gap decision round_number is invalid")
        _validate_selection_evidence(
            decision,
            workflow,
            segment_id=segment_id,
            query_plan_id=plan_ref["query_plan_id"],
            round_number=previous_round,
        )
        _validate_gap_structure(decision, original_plan)
        if batch is None or batch.get("schema_version") != BATCH_SCHEMA:
            raise RoundPlanError("a cumulative understanding batch is required")
        if (
            batch.get("workflow_id") != workflow.get("workflow_id")
            or batch.get("workflow_state_version") != workflow.get("state_version")
            or batch.get("segment_id") != segment_id
            or batch.get("query_plan_id") != plan_ref.get("query_plan_id")
        ):
            raise RoundPlanError(
                "understanding batch lineage does not match the workflow segment"
            )
        if batch.get("platform_scope") != platform_scope:
            raise RoundPlanError(
                "understanding batch platform scope does not match the workflow"
            )
        budget = batch.get("budget")
        if not isinstance(budget, dict) or not isinstance(
            budget.get("occupied_media_unit_count"), int
        ):
            raise RoundPlanError("understanding batch budget is invalid")
        occupied = budget["occupied_media_unit_count"]
        round_number = previous_round + 1
        history = _query_history(
            previous_query_plans or [],
            segment_id=segment_id,
            query_plan_id=plan_ref["query_plan_id"],
            expected_rounds=previous_round,
            platform_scope=platform_scope,
        )
        declared_history = decision.get("previous_query_texts")
        if (
            not isinstance(declared_history, list)
            or {
                " ".join(value.split()).casefold()
                for value in declared_history
                if isinstance(value, str) and value.strip()
            }
            != history
        ):
            raise RoundPlanError(
                "gap decision query history does not match prior round artifacts"
            )
        queries = _validate_next_queries(
            decision,
            original_plan,
            history,
            platform_scope,
        )
        branches = []
        for platform in platform_scope:
            platform_queries = [
                {
                    "query_id": query["query_id"],
                    "text": query["text"],
                    "facet_ids": query["facet_ids"],
                    "budget": query["budget"],
                }
                for query in queries
                if query["platform"] == platform
            ]
            if not platform_queries:
                raise RoundPlanError(
                    "supplemental search must provide at least one query for every in-scope platform"
                )
            languages = {
                query["language"] for query in queries if query["platform"] == platform
            }
            if len(languages) != 1:
                raise RoundPlanError("one platform branch must use one language")
            branches.append(
                {
                    "platform": platform,
                    "language": languages.pop(),
                    "queries": platform_queries,
                }
            )

    remaining_rounds = max_rounds - round_number + 1
    remaining_slots = max_videos - occupied
    if remaining_rounds < 1 or remaining_slots < 1:
        reason = "max_rounds" if remaining_rounds < 1 else "max_videos"
        raise SearchBudgetExhausted(f"search budget is exhausted: {reason}")
    round_limit = math.ceil(remaining_slots / remaining_rounds)
    round_input = {
        "schema_version": "1.0",
        "full_script": collection_input["full_script"],
        "theme": collection_input.get("theme"),
        "segments": [segment],
    }
    round_query_plans = {
        "schema_version": "3.0",
        "platform_scope": platform_scope,
        "plans": [
            {
                "query_plan_id": original_plan["query_plan_id"],
                "segment_id": segment_id,
                "visual_strategy": original_plan["visual_strategy"],
                "required_visual_facets": original_plan["required_visual_facets"],
                "platform_branches": branches,
            }
        ],
    }
    round_plan = {
        "schema_version": ROUND_SCHEMA,
        "workflow_id": workflow["workflow_id"],
        "workflow_state_version": workflow["state_version"],
        "segment_id": segment_id,
        "query_plan_id": plan_ref["query_plan_id"],
        "round_number": round_number,
        "budget": {
            "max_rounds": max_rounds,
            "completed_rounds_before": round_number - 1,
            "remaining_search_rounds_including_current": remaining_rounds,
            "max_videos": max_videos,
            "occupied_media_units_before": occupied,
            "remaining_video_slots_before": remaining_slots,
            "round_admission_limit": round_limit,
        },
        "collector_runner_arguments": {
            "MaxRounds": remaining_rounds,
            "MaxVideos": remaining_slots,
        },
    }
    return round_plan, round_input, round_query_plans


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_bytes(value)
    if path.exists():
        if path.read_bytes() != content:
            raise RoundPlanError(f"refusing to overwrite conflicting artifact: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plan-video-material-round")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--decision")
    parser.add_argument("--batch")
    parser.add_argument("--previous-query-plans", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        workflow = _load_object(Path(arguments.workflow).resolve(strict=True))
        decision = (
            None
            if arguments.decision is None
            else _load_object(Path(arguments.decision).resolve(strict=True))
        )
        batch = (
            None
            if arguments.batch is None
            else _load_object(Path(arguments.batch).resolve(strict=True))
        )
        round_plan, round_input, query_plans = plan_round(
            workflow=workflow,
            segment_id=arguments.segment_id,
            decision=decision,
            batch=batch,
            previous_query_plans=[
                _load_object(Path(path).resolve(strict=True))
                for path in arguments.previous_query_plans
            ],
        )
        output_dir = Path(arguments.output_dir).expanduser().resolve(strict=False)
        _atomic_write_json(output_dir / "round-plan.json", round_plan)
        _atomic_write_json(output_dir / "collection-input.json", round_input)
        _atomic_write_json(output_dir / "query-plans.json", query_plans)
    except (RoundPlanError, OSError, ValueError, KeyError) as error:
        budget_exhausted = isinstance(error, SearchBudgetExhausted)
        code = (
            error.code
            if isinstance(error, RoundPlanError)
            else "search_round_invalid"
        )
        details = error.details if isinstance(error, RoundPlanError) else {}
        print(
            json.dumps(
                {
                    "schema_version": ROUND_SCHEMA,
                    "status": "budget_exhausted" if budget_exhausted else "error",
                    "code": "search_budget_exhausted" if budget_exhausted else code,
                    "message": str(error),
                    "details": details,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": ROUND_SCHEMA,
                "status": "ready",
                "round_plan_path": str(output_dir / "round-plan.json"),
                "input_path": str(output_dir / "collection-input.json"),
                "query_plans_path": str(output_dir / "query-plans.json"),
                "round_number": round_plan["round_number"],
                "round_admission_limit": round_plan["budget"][
                    "round_admission_limit"
                ],
                "collector_runner_arguments": round_plan[
                    "collector_runner_arguments"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
