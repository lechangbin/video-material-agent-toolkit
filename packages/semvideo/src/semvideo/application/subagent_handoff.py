"""Durable public handoff between Semvideo and generic multimodal subagents."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from semvideo.application.subagent_contracts import (
    CinematographyReference,
    ContextTier,
    CrvProvenance,
    EvidenceReference,
    TimelineAnchorReference,
    VideoUnderstandingAgentRequest,
    VideoUnderstandingAgentResult,
    validate_result_against_request,
)
from semvideo.application.task_store import TaskStore, new_opaque_id
from semvideo.application.workspace import WorkspacePaths
from semvideo.errors import ErrorCategory, RecoveryAction, SemvideoError
from semvideo.infrastructure.io import atomic_write_json, read_json
from semvideo.modules.evidence.crv_contracts import GlobalUnderstandingEvidencePackage

PROFILE_SCHEMA = "subagent-evidence-profiles/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _profile_path(workspace: WorkspacePaths) -> Path:
    return workspace.data / "subagent-evidence-profiles.json"


def approve_profiles(workspace: WorkspacePaths, input_path: Path) -> dict[str, Any]:
    """Install human-approved, benchmark-derived tier profiles atomically."""

    value = read_json(input_path)
    if value.get("schema_version") != PROFILE_SCHEMA or value.get("status") != "approved":
        raise _error(
            "subagent_evidence_profile_unapproved",
            "Evidence profiles must carry explicit human approval.",
        )
    tiers = value.get("tiers")
    if not isinstance(tiers, dict) or set(tiers) != {"128k", "256k", "512k", "1m"}:
        raise _error(
            "subagent_evidence_profile_incomplete",
            "All four context tiers require benchmark-derived profiles.",
        )
    for tier, profile in tiers.items():
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("max_images"), int)
            or profile["max_images"] < 1
            or not isinstance(profile.get("observer_window_count"), int)
            or profile["observer_window_count"] < 1
            or not isinstance(profile.get("safety_margin_tokens"), int)
            or profile["safety_margin_tokens"] < 1
        ):
            raise _error(
                "subagent_evidence_profile_invalid",
                f"The {tier} evidence profile is invalid.",
            )
    payload = {**value, "content_hash": _canonical_hash(value)}
    atomic_write_json(_profile_path(workspace), payload)
    return {
        "schema_version": PROFILE_SCHEMA,
        "status": "approved",
        "content_hash": payload["content_hash"],
        "path": str(_profile_path(workspace)),
    }


def require_approved_profile(
    workspace: WorkspacePaths, effective_context_tokens: int
) -> tuple[ContextTier, dict[str, Any], str]:
    """Return one approved tier profile or stop before media work starts."""

    try:
        tier = ContextTier.from_tokens(effective_context_tokens)
    except ValueError as error:
        raise _error(
            "subagent_context_below_minimum",
            "Generic video-understanding subagents require at least 128K context.",
        ) from error
    path = _profile_path(workspace)
    if not path.is_file():
        raise _error(
            "subagent_evidence_profile_unapproved",
            "No benchmark-derived evidence profile has been approved.",
        )
    profiles = read_json(path)
    profile = profiles.get("tiers", {}).get(tier.value)
    if not isinstance(profile, dict):
        raise _error(
            "subagent_evidence_profile_incomplete",
            f"The {tier.value} evidence profile is missing.",
        )
    return tier, profile, str(profiles.get("content_hash") or "")


def prepare_request(
    workspace: WorkspacePaths,
    job_id: str,
    *,
    effective_context_tokens: int | None,
    subagent_slots: int | None,
    repair: bool = False,
) -> dict[str, Any]:
    """Freeze one immutable request and release the Worker while it waits."""

    if effective_context_tokens is None:
        raise _error(
            "subagent_context_required",
            "Effective subagent context tokens are required before automatic execution.",
        )
    if subagent_slots is not None and subagent_slots < 1:
        raise _error(
            "subagent_capability_unavailable",
            "The Agent host reported no generic subagent capacity.",
        )
    try:
        tier = ContextTier.from_tokens(effective_context_tokens)
    except ValueError as error:
        raise _error(
            "subagent_context_below_minimum",
            "Generic video-understanding subagents require at least 128K context.",
        ) from error
    tier, tier_profile, profile_hash = require_approved_profile(
        workspace, effective_context_tokens
    )
    store = TaskStore(workspace)
    job = store.read_job(job_id)
    root = store.job_path(job_id)
    handoff = root / "subagent"
    handoff.mkdir(parents=True, exist_ok=True)
    lineage_path = handoff / "lineage.json"
    lineage: dict[str, Any] = (
        read_json(lineage_path) if lineage_path.is_file() else {"attempts": []}
    )
    attempts = lineage.get("attempts")
    if not isinstance(attempts, list):
        raise _error("subagent_lineage_invalid", "Subagent attempt lineage is corrupt.")
    if attempts and not repair:
        latest = Path(str(attempts[-1]["request_path"]))
        return {
            "schema_version": "semvideo-subagent-checkpoint/v1",
            "status": "awaiting_subagent",
            "request_path": str(latest),
            "request_sha256": _sha256(latest),
            "attempt": len(attempts),
            "subagent_slots": subagent_slots or 1,
        }
    if repair and (not attempts or len(attempts) >= 3):
        raise _error(
            "subagent_repair_exhausted",
            "The initial attempt and two fresh repairs have been exhausted.",
        )

    evidence_path = root / "evidence" / "global-understanding-evidence.json"
    try:
        evidence_package = GlobalUnderstandingEvidencePackage.model_validate(
            read_json(evidence_path)
        )
    except (OSError, ValidationError) as error:
        raise _error(
            "crv_evidence_unavailable",
            "A validated CRV evidence package is required before subagent handoff.",
        ) from error
    facts = read_json(root / "media" / "facts.json")
    duration_ms = facts.get("duration_ms")
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise _error("media_facts_invalid", "The job duration is unavailable.")
    max_images = int(tier_profile["max_images"])
    selected_frames = evidence_package.frames[:max_images]
    evidence_refs: list[EvidenceReference] = [
        EvidenceReference(
            evidence_id=frame.frame_id,
            kind="frame",
            relative_path=(Path("evidence") / frame.relative_path).as_posix(),
            sha256=frame.sha256,
            start_seconds=frame.timestamp_seconds,
        )
        for frame in selected_frames
    ]
    for span in evidence_package.transcript:
        evidence_refs.append(
            EvidenceReference(
                evidence_id=span.span_id,
                kind="transcript",
                start_seconds=span.start_seconds,
                end_seconds=span.end_seconds,
            )
        )
    analysis_media = facts.get("analysis_media")
    relative_analysis_path = (
        str(analysis_media.get("relative_path", ""))
        if isinstance(analysis_media, dict)
        else ""
    )
    analysis_proxy = (
        workspace.sources / str(job["source_video_id"]) / relative_analysis_path
    )
    if analysis_proxy.is_file():
        evidence_refs.append(
            EvidenceReference(
                evidence_id="analysis_proxy",
                kind="analysis_proxy",
                local_path=str(analysis_proxy.resolve()),
                sha256=evidence_package.analysis_proxy_sha256,
                start_seconds=0,
                end_seconds=duration_ms / 1000,
            )
        )
    cinematography = _cinematography_references(root)
    anchor_rows = read_json(root / "segmentation" / "semantic-anchors.json")
    raw_anchors = anchor_rows.get("anchors", anchor_rows)
    if not isinstance(raw_anchors, list):
        raise _error("semantic_anchors_invalid", "Semantic anchors are unavailable.")
    anchors = tuple(
        TimelineAnchorReference(
            anchor_id=str(row["anchor_id"]),
            timestamp_seconds=int(row["timestamp_ms"]) / 1000,
        )
        for row in raw_anchors
        if isinstance(row, dict)
    )
    context_payload = {
        "tier": tier.value,
        "profile_hash": profile_hash,
        "evidence": [item.model_dump(mode="json") for item in evidence_refs],
        "cinematography": [item.model_dump(mode="json") for item in cinematography],
        "anchors": [item.model_dump(mode="json") for item in anchors],
    }
    attempt_id = new_opaque_id("agentattempt")
    request = VideoUnderstandingAgentRequest(
        request_id=new_opaque_id("understandingrequest"),
        job_id=job_id,
        source_video_id=str(job["source_video_id"]),
        context_root=str(root.resolve()),
        source_sha256=evidence_package.source_sha256,
        duration_seconds=duration_ms / 1000,
        context_tier=tier,
        context_hash=_canonical_hash(context_payload),
        crv=CrvProvenance(
            runtime_version=evidence_package.crv_version,
            evidence_profile=evidence_package.evidence_profile,
            package_hash=evidence_package.runtime_package_hash,
            evidence_hash=evidence_package.evidence_hash,
        ),
        evidence=tuple(evidence_refs),
        cinematography=cinematography,
        anchors=anchors,
        observer_window_count=int(tier_profile["observer_window_count"]),
        agent_attempt_id=attempt_id,
        prior_attempt_id=(str(attempts[-1]["agent_attempt_id"]) if attempts else None),
    )
    request_path = handoff / f"request-{len(attempts) + 1:02d}.json"
    _write_once(request_path, request.model_dump(mode="json"))
    attempts.append(
        {
            "agent_attempt_id": attempt_id,
            "request_path": str(request_path),
            "request_sha256": _sha256(request_path),
            "validation_errors": [],
        }
    )
    atomic_write_json(lineage_path, {"schema_version": 1, "attempts": attempts})
    state = store.read_state(job_id)
    store.write_state(
        job_id,
        state="awaiting_subagent",
        current_stage="subagent",
        attempt_id=(str(state["attempt_id"]) if state.get("attempt_id") else None),
        progress={"attempt": len(attempts), "request_path": str(request_path)},
        event_type="subagent_request_ready",
    )
    return {
        "schema_version": "semvideo-subagent-checkpoint/v1",
        "status": "awaiting_subagent",
        "request_path": str(request_path),
        "request_sha256": _sha256(request_path),
        "attempt": len(attempts),
        "subagent_slots": subagent_slots or 1,
    }


def import_result(
    workspace: WorkspacePaths, job_id: str, result_path: Path
) -> dict[str, Any]:
    """Validate and atomically import one subagent result without private edits."""

    store = TaskStore(workspace)
    root = store.job_path(job_id)
    handoff = root / "subagent"
    lineage_path = handoff / "lineage.json"
    lineage = read_json(lineage_path)
    attempts = lineage.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise _error("subagent_request_missing", "No subagent request exists for this job.")
    current = attempts[-1]
    request_path = Path(str(current["request_path"]))
    try:
        request = VideoUnderstandingAgentRequest.model_validate(read_json(request_path))
        result = VideoUnderstandingAgentResult.model_validate(read_json(result_path))
    except (OSError, ValidationError) as error:
        raise _error(
            "subagent_result_invalid", "The subagent result does not satisfy its schema."
        ) from error
    errors = validate_result_against_request(request, result)
    current["result_path"] = str(result_path.resolve())
    current["result_sha256"] = _sha256(result_path)
    current["validation_errors"] = list(errors)
    atomic_write_json(lineage_path, {"schema_version": 1, "attempts": attempts})
    if errors:
        report = {
            "schema_version": "subagent-validation-report/v1",
            "job_id": job_id,
            "agent_attempt_id": request.agent_attempt_id,
            "errors": list(errors),
            "repair_attempts_remaining": max(0, 3 - len(attempts)),
        }
        report_path = root / "reports" / f"subagent-validation-{len(attempts):02d}.json"
        atomic_write_json(report_path, report)
        if len(attempts) >= 3:
            final_report = root / "reports" / "subagent-final-error.json"
            atomic_write_json(
                final_report,
                {
                    **report,
                    "status": "failed",
                    "lineage_path": str(lineage_path),
                },
            )
            state = store.read_state(job_id)
            store.write_state(
                job_id,
                state="failed",
                current_stage="subagent",
                attempt_id=(
                    str(state["attempt_id"]) if state.get("attempt_id") else None
                ),
                failure={
                    "code": "subagent_repair_exhausted",
                    "message": "The initial result and two repairs were invalid.",
                    "report_path": str(final_report),
                },
                event_type="subagent_repairs_exhausted",
            )
        return {
            "schema_version": "semvideo-subagent-import/v1",
            "status": "repair_required" if len(attempts) < 3 else "failed",
            "validation_errors": list(errors),
            "report_path": str(report_path),
            "attempts_used": len(attempts),
        }
    destination = handoff / "validated-result.json"
    _write_once(destination, result.model_dump(mode="json"))
    state = store.read_state(job_id)
    store.write_state(
        job_id,
        state="subagent_ready",
        current_stage="subagent",
        attempt_id=(str(state["attempt_id"]) if state.get("attempt_id") else None),
        progress={"result_path": str(destination), "result_sha256": _sha256(destination)},
        event_type="subagent_result_validated",
    )
    return {
        "schema_version": "semvideo-subagent-import/v1",
        "status": "ready_to_resume",
        "result_path": str(destination),
        "result_sha256": _sha256(destination),
        "attempts_used": len(attempts),
    }


def _cinematography_references(root: Path) -> tuple[CinematographyReference, ...]:
    path = root / "semantics" / "cinematography-annotations.jsonl"
    if not path.is_file():
        return ()
    timeline_value = read_json(root / "segmentation" / "shot-timeline.json")
    raw_shots = timeline_value.get("shots", [])
    shots = {
        str(row["shot_id"]): (int(row["start_ms"]), int(row["end_ms"]))
        for row in raw_shots
        if isinstance(row, dict)
        and "shot_id" in row
        and "start_ms" in row
        and "end_ms" in row
    }
    values: list[CinematographyReference] = []
    content_hash = _sha256(path)
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        shot_id = str(row["shot_id"])
        if shot_id not in shots:
            raise _error(
                "cinematography_reference_invalid",
                f"Cinematography annotation references unknown shot {shot_id}.",
            )
        start_ms, end_ms = shots[shot_id]
        values.append(
            CinematographyReference(
                annotation_id=f"cinematography_{index:04d}",
                shot_id=shot_id,
                start_seconds=start_ms / 1000,
                end_seconds=end_ms / 1000,
                relative_path="semantics/cinematography-annotations.jsonl",
                sha256=content_hash,
            )
        )
    return tuple(values)


def _write_once(path: Path, value: object) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise _error("subagent_artifact_conflict", "A frozen subagent artifact conflicts.")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _error(code: str, message: str) -> SemvideoError:
    return SemvideoError(
        code=code,
        category=ErrorCategory.INPUT,
        message=message,
        recovery=RecoveryAction.USER_ACTION,
        exit_code=3,
    )
