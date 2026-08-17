"""Build a verified Semvideo submission batch from collector result manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BATCH_SCHEMA = "video-material-understanding-batch/v2"
COLLECTION_SCHEMA = "2.0"
WORKFLOW_SCHEMA = "video-material-workflow/v1"
PLATFORMS = ("bilibili", "douyin", "xiaohongshu")


class BatchError(RuntimeError):
    """A collector artifact cannot safely cross the understanding boundary."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BatchError(f"invalid JSON artifact: {path}") from error
    if not isinstance(value, dict):
        raise BatchError(f"JSON artifact must contain an object: {path}")
    return value


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise BatchError(f"media asset cannot be read: {path}") from error
    return digest.hexdigest(), size


def _artifact_hash(path: Path) -> str:
    digest, _size = _hash_file(path)
    return digest


def _resolve_asset(workspace: Path, asset: dict[str, Any]) -> tuple[Path, str, int]:
    relative_path = asset.get("relative_path")
    declared_hash = asset.get("sha256")
    declared_size = asset.get("size_bytes")
    if (
        not isinstance(relative_path, str)
        or not isinstance(declared_hash, str)
        or not isinstance(declared_size, int)
    ):
        raise BatchError("proxy_asset is missing path, SHA-256, or size")
    relative = Path(relative_path)
    if relative.is_absolute():
        raise BatchError("proxy_asset.relative_path must be workspace-relative")
    resolved_workspace = workspace.resolve(strict=True)
    resolved = (resolved_workspace / relative).resolve(strict=True)
    if resolved_workspace not in resolved.parents:
        raise BatchError("proxy asset resolves outside the material workspace")
    if not resolved.is_file():
        raise BatchError(f"proxy asset is not a regular file: {resolved}")
    actual_hash, actual_size = _hash_file(resolved)
    if actual_hash != declared_hash.lower() or actual_size != declared_size:
        raise BatchError(f"proxy asset integrity mismatch: {resolved}")
    return resolved, actual_hash, actual_size


def _source_record(
    manifest: dict[str, Any],
    candidate: dict[str, Any],
    media_unit: dict[str, Any],
) -> dict[str, Any]:
    discoveries = candidate.get("discoveries")
    if not isinstance(discoveries, list):
        raise BatchError("candidate.discoveries must be an array")
    return {
        "collection_session_id": manifest["session_id"],
        "collection_state_version": manifest["state_version"],
        "media_unit_id": media_unit["media_unit_id"],
        "platform": candidate["platform"],
        "source_id": candidate["source_id"],
        "canonical_url": candidate["canonical_url"],
        "title": media_unit["title"],
        "work_group_id": media_unit.get("work_group_id"),
        "source_role": media_unit.get("source_role"),
        "discoveries": discoveries,
    }


def build_batch(
    collection_results: list[Path],
    *,
    profile: str,
    workflow_id: str,
    workflow_state_version: int,
    segment_id: str,
    query_plan_id: str,
    platform_scope: list[str],
    material_workspace: Path,
    semvideo_workspace: Path,
) -> dict[str, Any]:
    if not collection_results:
        raise BatchError("at least one collection result is required")
    if not profile.strip():
        raise BatchError("profile must not be empty")
    if (
        not workflow_id
        or workflow_state_version < 1
        or not segment_id
        or not query_plan_id
    ):
        raise BatchError("workflow lineage is invalid")
    normalized_workflow_scope = [
        platform for platform in PLATFORMS if platform in platform_scope
    ]
    if (
        not platform_scope
        or platform_scope != normalized_workflow_scope
        or len(platform_scope) != len(set(platform_scope))
    ):
        raise BatchError("workflow platform scope is invalid")
    material_workspace = material_workspace.expanduser().resolve(strict=True)
    semvideo_workspace = semvideo_workspace.expanduser().resolve(strict=True)
    if not material_workspace.is_dir() or not semvideo_workspace.is_dir():
        raise BatchError("workflow workspaces must be existing directories")

    manifests: list[dict[str, Any]] = []
    manifest_refs: list[dict[str, Any]] = []
    occupied_media_unit_ids: set[str] = set()
    items_by_hash: dict[str, dict[str, Any]] = {}

    for raw_path in collection_results:
        path = raw_path.expanduser().resolve(strict=True)
        manifest = _load_object(path)
        if manifest.get("schema_version") != COLLECTION_SCHEMA:
            raise BatchError(f"unsupported collector schema: {path}")
        platform_scope = manifest.get("platform_scope")
        normalized_scope = [
            platform for platform in PLATFORMS if platform in (platform_scope or [])
        ]
        if (
            not isinstance(platform_scope, list)
            or not platform_scope
            or platform_scope != normalized_scope
            or len(platform_scope) != len(set(platform_scope))
        ):
            raise BatchError("collection result platform scope is invalid")
        if platform_scope != normalized_workflow_scope:
            raise BatchError(
                "collection result platform scope does not match the workflow"
            )
        if not isinstance(manifest.get("session_id"), str):
            raise BatchError("collection result is missing session_id")
        if not isinstance(manifest.get("state_version"), int):
            raise BatchError("collection result is missing state_version")
        workspace_value = manifest.get("workspace_path")
        candidates = manifest.get("candidates")
        if not isinstance(workspace_value, str) or not isinstance(candidates, list):
            raise BatchError("collection result is missing workspace_path or candidates")
        workspace = Path(workspace_value).expanduser().resolve(strict=True)
        if workspace != material_workspace:
            raise BatchError(
                "collection result workspace does not match frozen material workspace"
            )
        manifests.append(manifest)
        manifest_refs.append(
            {
                "path": str(path),
                "sha256": _artifact_hash(path),
                "session_id": manifest["session_id"],
                "state_version": manifest["state_version"],
            }
        )

        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise BatchError("candidate entries must be objects")
            if candidate.get("platform") not in normalized_workflow_scope:
                raise BatchError(
                    "collection candidate is outside the workflow platform scope"
                )
            media_units = candidate.get("media_units")
            if not isinstance(media_units, list):
                raise BatchError("candidate.media_units must be an array")
            if any(
                isinstance(media_unit, dict)
                and media_unit.get("proxy_asset") is not None
                for media_unit in media_units
            ):
                discoveries = candidate.get("discoveries")
                if not isinstance(discoveries, list) or not any(
                    isinstance(discovery, dict)
                    and discovery.get("segment_id") == segment_id
                    and discovery.get("query_plan_id") == query_plan_id
                    for discovery in discoveries
                ):
                    raise BatchError(
                        "collection result contains media outside workflow segment"
                    )
            for media_unit in media_units:
                if not isinstance(media_unit, dict):
                    raise BatchError("media unit entries must be objects")
                media_unit_id = media_unit.get("media_unit_id")
                asset = media_unit.get("proxy_asset")
                if asset is None:
                    continue
                if not isinstance(media_unit_id, str) or not isinstance(asset, dict):
                    raise BatchError("downloaded media unit has an invalid identity or asset")
                occupied_media_unit_ids.add(media_unit_id)
                asset_path, asset_hash, asset_size = _resolve_asset(workspace, asset)
                if media_unit.get("eligible_for_understanding") is not True:
                    continue
                source = _source_record(manifest, candidate, media_unit)
                existing = items_by_hash.get(asset_hash)
                if existing is None:
                    profile_hash = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:8]
                    existing = {
                        "item_id": f"proxy_{asset_hash[:24]}",
                        "asset_sha256": asset_hash,
                        "asset_size_bytes": asset_size,
                        "asset_path": str(asset_path),
                        "semvideo_profile": profile,
                        "semvideo_idempotency_key": (
                            f"material-proxy-v1-{asset_hash}-{profile_hash}"
                        ),
                        "media_unit_ids": [],
                        "sources": [],
                    }
                    items_by_hash[asset_hash] = existing
                elif (
                    existing["asset_size_bytes"] != asset_size
                    or Path(existing["asset_path"]) != asset_path
                ):
                    raise BatchError("one asset hash resolves to conflicting asset records")
                if media_unit_id not in existing["media_unit_ids"]:
                    existing["media_unit_ids"].append(media_unit_id)
                if source not in existing["sources"]:
                    existing["sources"].append(source)

    items = sorted(items_by_hash.values(), key=lambda item: item["item_id"])
    for item in items:
        item["media_unit_ids"].sort()
        item["sources"].sort(
            key=lambda source: (
                source["collection_session_id"],
                source["media_unit_id"],
            )
        )
    manifest_refs.sort(key=lambda item: (item["session_id"], item["path"]))
    occupied = sorted(occupied_media_unit_ids)
    return {
        "schema_version": BATCH_SCHEMA,
        "workflow_id": workflow_id,
        "workflow_state_version": workflow_state_version,
        "segment_id": segment_id,
        "query_plan_id": query_plan_id,
        "platform_scope": normalized_workflow_scope,
        "material_workspace": str(material_workspace),
        "semvideo_workspace": str(semvideo_workspace),
        "profile": profile,
        "collection_results": manifest_refs,
        "budget": {
            "occupied_media_unit_count": len(occupied),
            "occupied_media_unit_ids": occupied,
        },
        "item_count": len(items),
        "items": items,
    }


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise BatchError(f"refusing to overwrite conflicting artifact: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prepare-understanding-batch")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument(
        "--collection-result",
        action="append",
        required=True,
        dest="collection_results",
    )
    parser.add_argument("--profile", default="default")
    parser.add_argument("--output", required=True)
    return parser


def _workflow_lineage(
    workflow: dict[str, Any],
    segment_id: str,
    profile: str,
) -> tuple[str, int, str, list[str]]:
    if workflow.get("schema_version") != WORKFLOW_SCHEMA:
        raise BatchError("workflow schema is unsupported")
    workflow_id = workflow.get("workflow_id")
    state_version = workflow.get("state_version")
    platform_scope = workflow.get("platform_scope")
    plans = workflow.get("plans")
    if (
        not isinstance(workflow_id, str)
        or not isinstance(state_version, int)
        or not isinstance(platform_scope, list)
        or not platform_scope
        or platform_scope
        != [platform for platform in PLATFORMS if platform in platform_scope]
        or len(platform_scope) != len(set(platform_scope))
        or not isinstance(plans, list)
    ):
        raise BatchError("workflow identity is invalid")
    if workflow.get("semvideo_profile") != profile:
        raise BatchError("batch profile does not match workflow Semvideo profile")
    plan = next(
        (
            item
            for item in plans
            if isinstance(item, dict) and item.get("segment_id") == segment_id
        ),
        None,
    )
    if plan is None or not isinstance(plan.get("query_plan_id"), str):
        raise BatchError("workflow does not contain the requested segment")
    return workflow_id, state_version, plan["query_plan_id"], platform_scope


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        workflow = _load_object(Path(arguments.workflow).resolve(strict=True))
        workflow_id, state_version, query_plan_id, platform_scope = _workflow_lineage(
            workflow,
            arguments.segment_id,
            arguments.profile,
        )
        payload = build_batch(
            [Path(value) for value in arguments.collection_results],
            profile=arguments.profile,
            workflow_id=workflow_id,
            workflow_state_version=state_version,
            segment_id=arguments.segment_id,
            query_plan_id=query_plan_id,
            platform_scope=platform_scope,
            material_workspace=Path(workflow["material_workspace"]),
            semvideo_workspace=Path(workflow["semvideo_workspace"]),
        )
        output = Path(arguments.output).expanduser().resolve(strict=False)
        _atomic_write_json(output, payload)
    except (BatchError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema_version": BATCH_SCHEMA,
                    "status": "error",
                    "code": "understanding_batch_invalid",
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "schema_version": BATCH_SCHEMA,
                "status": "ready",
                "output_path": str(output),
                "item_count": payload["item_count"],
                "occupied_media_unit_count": payload["budget"][
                    "occupied_media_unit_count"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
