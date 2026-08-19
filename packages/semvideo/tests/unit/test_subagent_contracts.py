from __future__ import annotations

from pathlib import Path

from semvideo.application.subagent_contracts import (
    AgentSemanticSegment,
    ContextTier,
    CrvProvenance,
    EvidenceReference,
    TimelineAnchorReference,
    VideoUnderstandingAgentRequest,
    VideoUnderstandingAgentResult,
    validate_result_against_request,
)


def request() -> VideoUnderstandingAgentRequest:
    return VideoUnderstandingAgentRequest(
        request_id="request_001",
        job_id="job_001",
        source_video_id="video_001",
        context_root=str(Path.cwd().resolve()),
        source_sha256="a" * 64,
        duration_seconds=10,
        context_tier=ContextTier.K512,
        context_hash="b" * 64,
        crv=CrvProvenance(
            runtime_version="0.7.16",
            evidence_profile="benchmark-candidate/v1",
            package_hash="c" * 64,
            evidence_hash="d" * 64,
        ),
        evidence=(
            EvidenceReference(evidence_id="frame_001", kind="frame"),
            EvidenceReference(evidence_id="frame_002", kind="frame"),
        ),
        anchors=(
            TimelineAnchorReference(anchor_id="anchor_0000", timestamp_seconds=0),
            TimelineAnchorReference(anchor_id="anchor_0001", timestamp_seconds=5),
            TimelineAnchorReference(anchor_id="anchor_0002", timestamp_seconds=10),
        ),
        observer_window_count=2,
        agent_attempt_id="agent_attempt_001",
    )


def test_context_tier_uses_explicit_capacity_not_model_identity() -> None:
    assert ContextTier.from_tokens(128_000) is ContextTier.K128
    assert ContextTier.from_tokens(255_999) is ContextTier.K128
    assert ContextTier.from_tokens(256_000) is ContextTier.K256
    assert ContextTier.from_tokens(512_000) is ContextTier.K512
    assert ContextTier.from_tokens(2_000_000) is ContextTier.M1


def test_result_validation_requires_continuous_cited_timeline() -> None:
    frozen = request()
    result = VideoUnderstandingAgentResult(
        request_id=frozen.request_id,
        request_hash=frozen.content_hash(),
        context_hash=frozen.context_hash,
        agent_attempt_id=frozen.agent_attempt_id,
        segments=(
            AgentSemanticSegment(
                segment_id="segment_001",
                start_anchor_id="anchor_0000",
                end_anchor_id="anchor_0001",
                start_seconds=0,
                end_seconds=5,
                summary="First event",
                confidence=0.9,
                evidence_refs=("frame_001",),
            ),
            AgentSemanticSegment(
                segment_id="segment_002",
                start_anchor_id="anchor_0001",
                end_anchor_id="anchor_0002",
                start_seconds=5,
                end_seconds=10,
                summary="Second event",
                confidence=0.8,
                evidence_refs=("frame_002",),
            ),
        ),
    )

    assert validate_result_against_request(frozen, result) == ()
