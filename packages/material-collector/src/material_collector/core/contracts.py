"""Versioned collection input and QueryPlan contracts."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from material_collector.core.errors import ContractError, ContractVersionError
from material_collector.core.media import PLATFORM_ORDER, Platform

CollectionSchemaVersion = Literal["1.0"]
QueryPlansSchemaVersion = Literal["2.0"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def normalize_multiline_text(value: str) -> str:
    """Normalize user-authored prose without changing its internal wording."""

    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


def normalize_inline_text(value: str) -> str:
    """Normalize a short identifier-adjacent phrase deterministically."""

    return " ".join(unicodedata.normalize("NFC", value).split())


def _validate_safe_id(value: str, field_name: str) -> str:
    normalized = normalize_inline_text(value)
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must start with an ASCII letter or digit and contain only "
            "letters, digits, '.', '_' or '-'."
        )
    return normalized


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectionSegmentDraft(_ContractModel):
    segment_id: str | None = None
    order: int | None = Field(default=None, ge=1)
    text: str
    content_suggestion: str | None = None

    @field_validator("segment_id")
    @classmethod
    def validate_segment_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_safe_id(value, "segment_id")

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = normalize_multiline_text(value)
        if not normalized:
            raise ValueError("segment text must not be empty")
        return normalized

    @field_validator("content_suggestion")
    @classmethod
    def normalize_content_suggestion(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_multiline_text(value)
        return normalized or None


class CollectionInputDraft(_ContractModel):
    schema_version: CollectionSchemaVersion
    full_script: str
    theme: str | None = None
    segments: tuple[CollectionSegmentDraft, ...] = Field(min_length=1)

    @field_validator("full_script")
    @classmethod
    def normalize_full_script(cls, value: str) -> str:
        normalized = normalize_multiline_text(value)
        if not normalized:
            raise ValueError("full_script must not be empty")
        return normalized

    @field_validator("theme")
    @classmethod
    def normalize_theme(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_multiline_text(value)
        return normalized or None


class CollectionSegment(_ContractModel):
    segment_id: str
    order: int
    text: str
    content_suggestion: str | None = None


class CollectionInput(_ContractModel):
    schema_version: CollectionSchemaVersion = "1.0"
    full_script: str
    theme: str | None = None
    segments: tuple[CollectionSegment, ...]


class VisualFacetDraft(_ContractModel):
    facet_id: str
    description: str

    @field_validator("facet_id")
    @classmethod
    def validate_facet_id(cls, value: str) -> str:
        return _validate_safe_id(value, "facet_id")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        normalized = normalize_inline_text(value)
        if not normalized:
            raise ValueError("facet description must not be empty")
        return normalized


class InitialQueryDraft(_ContractModel):
    query_id: str
    text: str
    target_platforms: tuple[Platform, ...] = Field(min_length=1)
    facet_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("query_id")
    @classmethod
    def validate_query_id(cls, value: str) -> str:
        return _validate_safe_id(value, "query_id")

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = normalize_inline_text(value)
        if not normalized:
            raise ValueError("query text must not be empty")
        return normalized

    @field_validator("facet_ids")
    @classmethod
    def validate_facet_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_safe_id(item, "facet_id") for item in value)


class QueryPlanDraft(_ContractModel):
    query_plan_id: str | None = None
    segment_id: str
    visual_strategy: str
    required_visual_facets: tuple[VisualFacetDraft, ...] = Field(min_length=1)
    initial_queries: tuple[InitialQueryDraft, ...] = Field(min_length=1)

    @field_validator("query_plan_id")
    @classmethod
    def validate_query_plan_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_safe_id(value, "query_plan_id")

    @field_validator("segment_id")
    @classmethod
    def validate_segment_id(cls, value: str) -> str:
        return _validate_safe_id(value, "segment_id")

    @field_validator("visual_strategy")
    @classmethod
    def normalize_visual_strategy(cls, value: str) -> str:
        normalized = normalize_multiline_text(value)
        if not normalized:
            raise ValueError("visual_strategy must not be empty")
        return normalized


class QueryPlansDraft(_ContractModel):
    schema_version: QueryPlansSchemaVersion
    platform_scope: tuple[Platform, ...] = Field(min_length=1)
    plans: tuple[QueryPlanDraft, ...] = Field(min_length=1)


def contract_schema_bundle() -> dict[str, dict[str, Any]]:
    """Return authoring schemas generated from the authoritative draft models."""

    return {
        "collection_input": CollectionInputDraft.model_json_schema(mode="validation"),
        "query_plans": QueryPlansDraft.model_json_schema(mode="validation"),
    }


class VisualFacet(_ContractModel):
    facet_id: str
    description: str


class InitialQuery(_ContractModel):
    query_id: str
    text: str
    target_platforms: tuple[Platform, ...]
    facet_ids: tuple[str, ...]


class QueryPlan(_ContractModel):
    query_plan_id: str
    segment_id: str
    visual_strategy: str
    required_visual_facets: tuple[VisualFacet, ...]
    initial_queries: tuple[InitialQuery, ...]


class QueryPlans(_ContractModel):
    schema_version: QueryPlansSchemaVersion = "2.0"
    platform_scope: tuple[Platform, ...]
    plans: tuple[QueryPlan, ...]


@dataclass(frozen=True)
class ContractWarning:
    code: str
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class NormalizedContracts:
    collection_input: CollectionInput
    query_plans: QueryPlans
    warnings: tuple[ContractWarning, ...]


def _pydantic_error(
    name: str,
    schema_version: str,
    error: ValidationError,
) -> ContractError:
    return ContractError(
        f"{name} does not satisfy schema version {schema_version}.",
        details={"document": name, "issues": error.errors(include_url=False, include_input=False)},
    )


def normalize_collection_input(data: Any) -> tuple[CollectionInput, tuple[ContractWarning, ...]]:
    """Validate and canonicalize one collection input document."""

    try:
        draft = CollectionInputDraft.model_validate(data)
    except ValidationError as error:
        raise _pydantic_error("collection_input", "1.0", error) from error

    normalized_segments: list[tuple[int, int, CollectionSegment]] = []
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for position, segment in enumerate(draft.segments, start=1):
        segment_id = segment.segment_id or f"seg_{position:03d}"
        order = segment.order or position
        if segment_id in seen_ids:
            raise ContractError(
                "Collection segment identifiers must be unique.",
                details={"segment_id": segment_id},
            )
        if order in seen_orders:
            raise ContractError(
                "Collection segment order values must be unique.",
                details={"order": order},
            )
        seen_ids.add(segment_id)
        seen_orders.add(order)
        normalized_segments.append(
            (
                order,
                position,
                CollectionSegment(
                    segment_id=segment_id,
                    order=order,
                    text=segment.text,
                    content_suggestion=segment.content_suggestion,
                ),
            )
        )

    normalized_segments.sort(key=lambda item: (item[0], item[1]))
    collection_input = CollectionInput(
        full_script=draft.full_script,
        theme=draft.theme,
        segments=tuple(item[2] for item in normalized_segments),
    )

    full_comparison = "".join(collection_input.full_script.split())
    segments_comparison = "".join(
        "".join(segment.text.split()) for segment in collection_input.segments
    )
    warnings: list[ContractWarning] = []
    if full_comparison != segments_comparison:
        warnings.append(
            ContractWarning(
                code="script_segment_text_mismatch",
                message=(
                    "The normalized concatenation of segment text differs from the full script."
                ),
                details={
                    "full_script_length": len(full_comparison),
                    "segment_text_length": len(segments_comparison),
                },
            )
        )
    return collection_input, tuple(warnings)


def normalize_query_plans(data: Any, collection_input: CollectionInput) -> QueryPlans:
    """Validate QueryPlans and their relationship to the frozen input."""

    if (
        isinstance(data, dict)
        and "schema_version" in data
        and data["schema_version"] != "2.0"
    ):
        raise ContractVersionError("query_plans", data["schema_version"], "2.0")

    try:
        draft = QueryPlansDraft.model_validate(data)
    except ValidationError as error:
        raise _pydantic_error("query_plans", "2.0", error) from error

    if len(set(draft.platform_scope)) != len(draft.platform_scope):
        raise ContractError(
            "The collection platform scope must not contain duplicates.",
            details={"document": "query_plans"},
        )
    platform_scope = tuple(
        platform for platform in PLATFORM_ORDER if platform in draft.platform_scope
    )

    input_segment_ids = tuple(segment.segment_id for segment in collection_input.segments)
    input_segment_set = set(input_segment_ids)
    plans_by_segment: dict[str, QueryPlan] = {}
    seen_plan_ids: set[str] = set()

    for plan in draft.plans:
        if plan.segment_id not in input_segment_set:
            raise ContractError(
                "A QueryPlan references an unknown collection segment.",
                details={"segment_id": plan.segment_id},
            )
        if plan.segment_id in plans_by_segment:
            raise ContractError(
                "Each collection segment must have exactly one QueryPlan.",
                details={"segment_id": plan.segment_id},
            )

        query_plan_id = plan.query_plan_id or f"qp_{plan.segment_id}"
        if query_plan_id in seen_plan_ids:
            raise ContractError(
                "QueryPlan identifiers must be unique.",
                details={"query_plan_id": query_plan_id},
            )
        seen_plan_ids.add(query_plan_id)

        facets: list[VisualFacet] = []
        facet_ids: set[str] = set()
        facet_descriptions: set[str] = set()
        for facet in plan.required_visual_facets:
            if facet.facet_id in facet_ids:
                raise ContractError(
                    "Visual facet identifiers must be unique within a QueryPlan.",
                    details={
                        "query_plan_id": query_plan_id,
                        "facet_id": facet.facet_id,
                    },
                )
            description_key = facet.description.casefold()
            if description_key in facet_descriptions:
                raise ContractError(
                    "Visual facet descriptions must be unique after normalization.",
                    details={
                        "query_plan_id": query_plan_id,
                        "description": facet.description,
                    },
                )
            facet_ids.add(facet.facet_id)
            facet_descriptions.add(description_key)
            facets.append(
                VisualFacet(facet_id=facet.facet_id, description=facet.description)
            )

        queries: list[InitialQuery] = []
        query_ids: set[str] = set()
        query_texts: set[str] = set()
        for query in plan.initial_queries:
            if query.query_id in query_ids:
                raise ContractError(
                    "Initial query identifiers must be unique within a QueryPlan.",
                    details={
                        "query_plan_id": query_plan_id,
                        "query_id": query.query_id,
                    },
                )
            query_text_key = query.text.casefold()
            if query_text_key in query_texts:
                raise ContractError(
                    "Initial query text must be unique after normalization.",
                    details={"query_plan_id": query_plan_id, "text": query.text},
                )
            if len(set(query.target_platforms)) != len(query.target_platforms):
                raise ContractError(
                    "Target platform lists must not contain duplicates.",
                    details={
                        "query_plan_id": query_plan_id,
                        "query_id": query.query_id,
                    },
                )
            if len(set(query.facet_ids)) != len(query.facet_ids):
                raise ContractError(
                    "Initial query facet references must not contain duplicates.",
                    details={
                        "query_plan_id": query_plan_id,
                        "query_id": query.query_id,
                    },
                )
            unknown_facets = sorted(set(query.facet_ids) - facet_ids)
            if unknown_facets:
                raise ContractError(
                    "An initial query references an unknown visual facet.",
                    details={
                        "query_plan_id": query_plan_id,
                        "query_id": query.query_id,
                        "unknown_facet_ids": unknown_facets,
                    },
                )

            target_platforms = tuple(
                platform for platform in PLATFORM_ORDER if platform in query.target_platforms
            )
            if target_platforms != platform_scope or len(query.target_platforms) != len(
                platform_scope
            ):
                raise ContractError(
                    "Every query expression must target the complete collection platform scope.",
                    details={
                        "query_plan_id": query_plan_id,
                        "query_id": query.query_id,
                        "platform_scope": platform_scope,
                        "target_platforms": target_platforms,
                    },
                )
            query_ids.add(query.query_id)
            query_texts.add(query_text_key)
            queries.append(
                InitialQuery(
                    query_id=query.query_id,
                    text=query.text,
                    target_platforms=target_platforms,
                    facet_ids=query.facet_ids,
                )
            )

        plans_by_segment[plan.segment_id] = QueryPlan(
            query_plan_id=query_plan_id,
            segment_id=plan.segment_id,
            visual_strategy=plan.visual_strategy,
            required_visual_facets=tuple(facets),
            initial_queries=tuple(queries),
        )

    missing_segments = [
        segment_id for segment_id in input_segment_ids if segment_id not in plans_by_segment
    ]
    if missing_segments:
        raise ContractError(
            "Every collection segment must have exactly one QueryPlan.",
            details={"missing_segment_ids": missing_segments},
        )

    return QueryPlans(
        platform_scope=platform_scope,
        plans=tuple(plans_by_segment[item] for item in input_segment_ids),
    )


def normalize_contracts(collection_data: Any, query_plan_data: Any) -> NormalizedContracts:
    """Validate both documents and their cross-document invariants."""

    collection_input, warnings = normalize_collection_input(collection_data)
    query_plans = normalize_query_plans(query_plan_data, collection_input)
    return NormalizedContracts(
        collection_input=collection_input,
        query_plans=query_plans,
        warnings=warnings,
    )
