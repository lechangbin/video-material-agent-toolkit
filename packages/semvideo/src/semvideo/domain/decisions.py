"""Semantic decisions made about deterministic candidate boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .timeline import DomainValidationError, _require_non_empty


class BoundaryDecisionKind(StrEnum):
    KEEP = "keep"
    REMOVE = "remove"
    REVIEW = "review"


class BoundaryRelationship(StrEnum):
    SAME_CONTINUOUS_EVENT = "same_continuous_event"
    SAME_TOPIC_NEW_EVENT = "same_topic_new_event"
    DIFFERENT_TOPIC = "different_topic"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    MODEL_DISAGREEMENT = "model_disagreement"


@dataclass(frozen=True, slots=True)
class BoundaryDecision:
    """A traceable keep/remove/review judgment for one candidate boundary."""

    candidate_boundary_id: str
    left_segment_id: str
    right_segment_id: str
    decision: BoundaryDecisionKind
    relationship: BoundaryRelationship
    reason: str
    model_run_id: str | None = None
    confidence: float | None = None
    review_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.candidate_boundary_id, "candidate_boundary_id")
        _require_non_empty(self.left_segment_id, "left_segment_id")
        _require_non_empty(self.right_segment_id, "right_segment_id")
        _require_non_empty(self.reason, "reason")

        try:
            decision = BoundaryDecisionKind(self.decision)
        except (TypeError, ValueError) as error:
            raise DomainValidationError(f"unknown boundary decision: {self.decision!r}") from error
        try:
            relationship = BoundaryRelationship(self.relationship)
        except (TypeError, ValueError) as error:
            raise DomainValidationError(
                f"unknown boundary relationship: {self.relationship!r}"
            ) from error
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "relationship", relationship)

        if self.model_run_id is not None:
            _require_non_empty(self.model_run_id, "model_run_id")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise DomainValidationError("confidence must be numeric")
            if not 0 <= self.confidence <= 1:
                raise DomainValidationError("confidence must be between 0 and 1")

        review_reasons = tuple(self.review_reasons)
        if any(
            not isinstance(review_reason, str) or not review_reason.strip()
            for review_reason in review_reasons
        ):
            raise DomainValidationError("review reasons must be non-empty strings")
        object.__setattr__(self, "review_reasons", review_reasons)
