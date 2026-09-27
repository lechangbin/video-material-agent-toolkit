"""Offline, deterministic global Top-K over structured Semvideo observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol

REQUEST_SCHEMA = "segment-selection-request/v2"
RESULT_SCHEMA = "segment-selection-output/v2"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_FILES = {
    "model.onnx": "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
}


class SelectionError(RuntimeError):
    """The request, model, or candidate artifact is unusable."""


class Embedder(Protocol):
    fingerprint: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def model_directory() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise SelectionError("LOCALAPPDATA is required for the offline model cache")
    return (
        Path(local)
        / "video-material-agent-toolkit"
        / "models"
        / "multilingual-e5-small"
        / MODEL_REVISION
    )


class E5OnnxEmbedder:
    """Small multilingual E5 ONNX runtime with no runtime network access."""

    fingerprint = (
        f"intfloat/multilingual-e5-small@{MODEL_REVISION}:onnx-fp32:all-fields:chunked-mean-v1"
    )

    def __init__(self, directory: Path | None = None) -> None:
        directory = directory or model_directory()
        for name, expected in MODEL_FILES.items():
            path = directory / name
            if not path.is_file() or _hash_file(path) != expected:
                raise SelectionError(
                    f"offline embedding model missing or checksum mismatch: {path}"
                )
        try:
            import numpy as np  # type: ignore[import-not-found]
            import onnxruntime as ort  # type: ignore[import-not-found]
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except ImportError as error:
            raise SelectionError(
                "offline embedding dependencies are missing; rerun the repository bootstrap"
            ) from error
        self._np = np
        self._tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        special_ids = self._tokenizer.encode("", add_special_tokens=True).ids
        if len(special_ids) != 2:
            raise SelectionError("embedding tokenizer special-token layout is unsupported")
        self._start_id, self._end_id = special_ids
        self._pad_id = self._tokenizer.token_to_id("<pad>")
        if self._pad_id is None:
            raise SelectionError("embedding tokenizer padding token is missing")
        self._session = ort.InferenceSession(
            str(directory / "model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {item.name for item in self._session.get_inputs()}

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        windows: list[tuple[int, list[int], int]] = []
        for text_index, text in enumerate(texts):
            if text.startswith("query: "):
                prefix, body = "query: ", text[7:]
            elif text.startswith("passage: "):
                prefix, body = "passage: ", text[9:]
            else:
                raise SelectionError("E5 input must start with query: or passage:")
            prefix_ids = self._tokenizer.encode(prefix, add_special_tokens=False).ids
            body_ids = self._tokenizer.encode(body, add_special_tokens=False).ids
            body_limit = 512 - len(prefix_ids) - 2
            if body_limit < 1:
                raise SelectionError("E5 prefix exceeds model context")
            for offset in range(0, max(1, len(body_ids)), body_limit):
                chunk = body_ids[offset : offset + body_limit]
                windows.append(
                    (
                        text_index,
                        [self._start_id, *prefix_ids, *chunk, self._end_id],
                        max(1, len(chunk)),
                    )
                )
        sums: list[list[float] | None] = [None] * len(texts)
        weights = [0] * len(texts)
        for start in range(0, len(windows), 32):
            batch = windows[start : start + 32]
            np = self._np
            width = max(len(ids) for _, ids, _ in batch)
            inputs = {
                "input_ids": np.asarray(
                    [ids + [self._pad_id] * (width - len(ids)) for _, ids, _ in batch],
                    dtype=np.int64,
                ),
                "attention_mask": np.asarray(
                    [[1] * len(ids) + [0] * (width - len(ids)) for _, ids, _ in batch],
                    dtype=np.int64,
                ),
                "token_type_ids": np.zeros((len(batch), width), dtype=np.int64),
            }
            hidden = self._session.run(
                None, {key: value for key, value in inputs.items() if key in self._input_names}
            )[0]
            mask = inputs["attention_mask"][..., None]
            pooled = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            vectors = (pooled / np.maximum(norms, 1e-12)).tolist()
            for (text_index, _, weight), vector in zip(batch, vectors, strict=True):
                if sums[text_index] is None:
                    sums[text_index] = [0.0] * len(vector)
                current = sums[text_index]
                assert current is not None
                for dimension, value in enumerate(vector):
                    current[dimension] += float(value) * weight
                weights[text_index] += weight
        if any(vector is None for vector in sums):
            raise SelectionError("embedding model did not encode every input")
        output: list[list[float]] = []
        for index, vector in enumerate(sums):
            assert vector is not None
            output.append([value / weights[index] for value in vector])
        return _normalized(output)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise SelectionError(f"refusing to overwrite conflicting selection artifact: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _read_candidates(request: dict[str, Any]) -> list[dict[str, Any]]:
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise SelectionError("selection request schema is unsupported")
    artifact = request.get("candidate_artifact")
    if not isinstance(artifact, dict) or artifact.get("format") != "jsonl":
        raise SelectionError("candidate artifact declaration is invalid")
    path = Path(artifact["path"]).expanduser().resolve(strict=True)
    if _hash_file(path) != artifact.get("sha256"):
        raise SelectionError("candidate artifact hash mismatch")
    catalogs = request.get("understanding_catalogs")
    if not isinstance(catalogs, list) or not catalogs:
        raise SelectionError("complete understanding catalogs are required")
    jobs = {
        item.get("understanding_job_id")
        for item in catalogs
        if isinstance(item, dict) and item.get("status") == "complete"
    }
    if (
        not jobs
        or len(jobs) != len(catalogs)
        or not all(isinstance(job, str) and job for job in jobs)
    ):
        raise SelectionError("all contributing understanding catalogs must be complete")
    catalog_items = {item["understanding_job_id"]: item.get("item_id") for item in catalogs}
    candidates: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    raw_count = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            raw_count += 1
            item = json.loads(line)
            if not isinstance(item, dict):
                raise SelectionError("candidate record must be an object")
            candidate_id = item.get("candidate_segment_id")
            if (
                item.get("schema_version") != "video-material-candidate-segment/v1"
                or not isinstance(candidate_id, str)
                or not candidate_id
                or item.get("understanding_job_id") not in jobs
                or not isinstance(item.get("item_id"), str)
                or not item["item_id"]
                or item["item_id"] != catalog_items.get(item.get("understanding_job_id"))
                or not isinstance(item.get("media_unit_ids"), list)
                or not item["media_unit_ids"]
                or not all(isinstance(value, str) and value for value in item["media_unit_ids"])
                or not isinstance(item.get("start_ms"), int)
                or not isinstance(item.get("end_ms"), int)
                or item["start_ms"] < 0
                or item["end_ms"] <= item["start_ms"]
            ):
                raise SelectionError("candidate lineage or time range is invalid")
            if candidate_id in seen:
                if seen[candidate_id] != item:
                    raise SelectionError("candidate ID maps to conflicting records")
                continue
            seen[candidate_id] = item
            candidates.append(item)
    if raw_count != artifact.get("candidate_count"):
        raise SelectionError("candidate count does not match declared artifact")
    if not candidates:
        raise SelectionError("candidate artifact is empty")
    return candidates


def _normalized(vectors: list[list[float]]) -> list[list[float]]:
    if not vectors or not vectors[0]:
        raise SelectionError("embedding model returned empty vectors")
    dimension = len(vectors[0])
    result = []
    for vector in vectors:
        if len(vector) != dimension or not all(math.isfinite(value) for value in vector):
            raise SelectionError("embedding model returned malformed vectors")
        length = math.sqrt(sum(value * value for value in vector))
        if length < 1e-12:
            raise SelectionError("embedding model returned a zero vector")
        result.append([value / length for value in vector])
    return result


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _time_overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left.get("item_id") != right.get("item_id"):
        return 0.0
    overlap = max(
        0, min(left["end_ms"], right["end_ms"]) - max(left["start_ms"], right["start_ms"])
    )
    shorter = min(left["end_ms"] - left["start_ms"], right["end_ms"] - right["start_ms"])
    return float(overlap / shorter)


def _candidate_text(item: dict[str, Any]) -> str:
    # Titles and source descriptions are deliberately excluded.
    fields = (
        "visual_summary",
        "detailed_summary",
        "short_summary",
        "topics",
        "participants",
        "locations",
        "organizations",
        "objects",
        "actions",
        "keywords",
        "transcript",
    )
    parts = []
    for key in fields:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
        elif isinstance(value, (dict, list)) and value:
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    text = "\n".join(parts)
    if not text:
        raise SelectionError(
            f"candidate lacks structured understanding: {item['candidate_segment_id']}"
        )
    return "passage: " + text


def _passage_vectors(
    candidates: list[dict[str, Any]],
    request: dict[str, Any],
    embedder: Embedder,
    cache_dir: Path | None,
) -> list[list[float]]:
    key = hashlib.sha256(
        (str(request["candidate_artifact"]["sha256"]) + ":" + embedder.fingerprint).encode()
    ).hexdigest()
    cache_path = cache_dir / f"{key}.json" if cache_dir is not None else None
    if cache_path is not None and cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cached.get("candidate_ids") == [item["candidate_segment_id"] for item in candidates]
            and isinstance(cached.get("vectors"), list)
            and len(cached["vectors"]) == len(candidates)
        ):
            return _normalized(cached["vectors"])
    vectors = _normalized(embedder.encode([_candidate_text(item) for item in candidates]))
    if len(vectors) != len(candidates):
        raise SelectionError("embedding model returned wrong candidate count")
    if cache_path is not None:
        _atomic_write(
            cache_path,
            _json_bytes(
                {
                    "candidate_ids": [item["candidate_segment_id"] for item in candidates],
                    "model_fingerprint": embedder.fingerprint,
                    "vectors": vectors,
                }
            ),
        )
    return vectors


def select(
    request: dict[str, Any],
    *,
    embedder: Embedder,
    output_dir: Path,
    cache_dir: Path | None = None,
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if route is not None and (
        route.get("schema_version") != "video-material-selection-route/v1"
        or route.get("workflow_id") != request.get("workflow_id")
        or route.get("route") != "embedding"
    ):
        raise SelectionError("frozen selection route does not permit embedding")
    candidates = _read_candidates(request)
    facets = request.get("required_visual_facets")
    theme = request.get("theme_segment")
    max_k = request.get("max_k")
    if (
        not isinstance(facets, list)
        or not facets
        or not isinstance(theme, dict)
        or not isinstance(theme.get("text"), str)
        or not isinstance(theme.get("intent"), str)
        or not isinstance(max_k, int)
        or isinstance(max_k, bool)
        or max_k < 1
    ):
        raise SelectionError("selection theme, facets or max_k is invalid")
    facet_ids = [facet.get("facet_id") for facet in facets if isinstance(facet, dict)]
    if (
        len(facet_ids) != len(facets)
        or not all(isinstance(facet_id, str) and facet_id for facet_id in facet_ids)
        or len(set(facet_ids)) != len(facets)
    ):
        raise SelectionError("required facet IDs are invalid")
    targets = request.get("selection_targets")
    if not isinstance(targets, dict):
        raise SelectionError("selection targets are invalid")
    narration_duration = theme.get("narration_duration_ms")
    if narration_duration is not None and (
        not isinstance(narration_duration, int)
        or isinstance(narration_duration, bool)
        or narration_duration < 1
    ):
        raise SelectionError("narration duration is invalid")
    passages = _passage_vectors(candidates, request, embedder, cache_dir)
    query_texts = ["query: " + theme["text"] + " " + theme["intent"]]
    query_texts.extend("query: " + str(facet.get("description", "")) for facet in facets)
    queries = _normalized(embedder.encode(query_texts))
    if len(queries) != len(query_texts):
        raise SelectionError("embedding model returned wrong query count")
    theme_scores = [_dot(queries[0], vector) for vector in passages]
    facet_scores = [[_dot(query, vector) for vector in passages] for query in queries[1:]]
    ranked = sorted(
        range(len(candidates)),
        key=lambda index: (-theme_scores[index], candidates[index]["candidate_segment_id"]),
    )
    rank_positions = {index: position for position, index in enumerate(ranked, start=1)}
    # Keep a global thematic shortlist *and* a shortlist for every facet. A
    # facet's sole relevant candidate may rank low on the broad theme query.
    shortlist_size = min(len(candidates), max_k)
    eligible = set(ranked[:shortlist_size])
    for scores in facet_scores:
        facet_ranked = sorted(
            range(len(candidates)),
            key=lambda index: (-scores[index], candidates[index]["candidate_segment_id"]),
        )
        eligible.update(facet_ranked[:shortlist_size])
    # Relative ranks, not absolute cosine cutoffs: E5 scores cluster near 1.
    facet_best = [max(scores) for scores in facet_scores]
    theme_best = max(theme_scores)
    eligible = {
        index
        for index in eligible
        if theme_scores[index] >= theme_best - 0.04
        or any(
            scores[index] >= best - 0.04
            for scores, best in zip(facet_scores, facet_best, strict=True)
        )
    }
    facet_proxies = {
        index: [
            facet_ids[facet_index]
            for facet_index, scores in enumerate(facet_scores)
            if scores[index] >= facet_best[facet_index] - 0.025
        ]
        for index in range(len(candidates))
    }
    selected: list[int] = []
    proxy_count = {facet_id: 0 for facet_id in facet_ids}
    decision_components: dict[int, dict[str, float | int]] = {}
    target_per_facet = targets.get("independent_candidates_per_required_facet", 2)
    if not isinstance(target_per_facet, int) or target_per_facet < 1:
        raise SelectionError("independent candidate target is invalid")
    # Reserve one distinct global candidate for each facet only when that
    # facet has a meaningful *relative* separation in this candidate batch.
    for scores in facet_scores:
        if len(selected) >= max_k:
            break
        median = sorted(scores)[len(scores) // 2]
        if max(scores) - median < 0.03:
            continue
        stage_options = eligible - set(selected)
        if not stage_options:
            break
        index = max(
            stage_options,
            key=lambda item: (
                scores[item]
                - 0.08
                * max(
                    (
                        _time_overlap_ratio(candidates[item], candidates[prior])
                        for prior in selected
                    ),
                    default=0.0,
                ),
                theme_scores[item],
                candidates[item]["candidate_segment_id"],
            ),
        )
        stage_overlap = max(
            (_time_overlap_ratio(candidates[index], candidates[prior]) for prior in selected),
            default=0.0,
        )
        selected.append(index)
        for facet_id in facet_proxies[index]:
            proxy_count[facet_id] += 1
        decision_components[index] = {
            "score": round(scores[index] - 0.08 * stage_overlap, 8),
            "coverage_proxy_bonus": 0.0,
            "semantic_redundancy": 0.0,
            "time_overlap_penalty": round(0.08 * stage_overlap, 8),
            "same_media_penalty": 0.0,
            "decision_step": len(selected),
        }
    while len(selected) < min(max_k, len(eligible)):
        options = []
        for index in eligible - set(selected):
            coverage_bonus = 0.04 * sum(
                1 for facet_id in facet_proxies[index] if proxy_count[facet_id] < target_per_facet
            )
            redundancy = max(
                (_dot(passages[index], passages[prior]) for prior in selected), default=0.0
            )
            same_media = any(
                candidates[index].get("item_id") == candidates[prior].get("item_id")
                for prior in selected
            )
            overlap_penalty = 0.08 * max(
                (_time_overlap_ratio(candidates[index], candidates[prior]) for prior in selected),
                default=0.0,
            )
            score = (
                theme_scores[index]
                + coverage_bonus
                - 0.06 * max(0.0, redundancy)
                - (0.015 if same_media else 0.0)
                - overlap_penalty
            )
            decision_components[index] = {
                "score": round(score, 8),
                "coverage_proxy_bonus": round(coverage_bonus, 8),
                "semantic_redundancy": round(max(0.0, redundancy), 8),
                "time_overlap_penalty": round(overlap_penalty, 8),
                "same_media_penalty": 0.015 if same_media else 0.0,
                "decision_step": len(selected) + 1,
            }
            options.append((score, -rank_positions[index], index))
        if not options:
            break
        index = max(options)[2]
        selected.append(index)
        for facet_id in facet_proxies[index]:
            proxy_count[facet_id] += 1
        if all(value >= target_per_facet for value in proxy_count.values()) and (
            narration_duration is None
            or sum(candidates[item]["end_ms"] - candidates[item]["start_ms"] for item in selected)
            >= 3 * narration_duration
        ):
            break
    warnings = ["向量相似度仅用于候选排序；视觉 facet 覆盖仍需主代理根据结构化证据判断。"]
    if len(eligible) < len(candidates):
        warnings.append("相对相似度质量门槛排除了低相关候选；不为填满 max_k 补入弱素材。")
    if narration_duration is None:
        warnings.append("旁白时长未知，未应用三倍素材时长停止条件。")
    selected_records = []
    for rank, index in enumerate(selected, start=1):
        item = candidates[index]
        selected_records.append(
            {
                "rank": rank,
                "candidate_segment_id": item["candidate_segment_id"],
                "understanding_job_id": item["understanding_job_id"],
                "media_unit_ids": item["media_unit_ids"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "relevance": "partial",
                "covered_facet_ids": [],
                "facet_similarity_proxy_ids": facet_proxies[index],
                "evidence": [
                    item.get("visual_summary")
                    or item.get("detailed_summary")
                    or item.get("short_summary")
                    or _candidate_text(item)[9:]
                ],
                "selection_reason": "全局向量相关性、facet 相似度代理信号与冗余惩罚共同排序；实际覆盖待主代理判断。",
            }
        )
    audit = [
        {
            "candidate_segment_id": item["candidate_segment_id"],
            "theme_cosine": round(theme_scores[index], 8),
            "facet_cosine": {
                facet_id: round(scores[index], 8)
                for facet_id, scores in zip(facet_ids, facet_scores, strict=True)
            },
            "status": "selected" if index in selected else "reserve",
            "relative_theme_rank": rank_positions[index],
            "quality_gate_passed": index in eligible,
            "omission_reason": None
            if index in selected
            else (
                "below_relative_quality_gate"
                if index not in eligible
                else "not_selected_within_k_ceiling"
            ),
            "decision_components": decision_components.get(index),
        }
        for index, item in enumerate(candidates)
    ]
    audit_bytes = b"".join(_json_bytes(item) for item in audit)
    output_dir = output_dir.expanduser().resolve(strict=False)
    audit_path = output_dir / "selection-audit.jsonl"
    _atomic_write(audit_path, audit_bytes)
    result = {
        "schema_version": RESULT_SCHEMA,
        **{
            key: request[key]
            for key in (
                "selection_id",
                "workflow_id",
                "workflow_state_version",
                "query_plan_id",
                "segment_id",
                "round_number",
                "max_k",
                "max_k_source",
            )
        },
        "status": "selected",
        "selection_method": "embedding",
        "selection_route": route or {"requested_mode": "embedding_only", "reason": "explicit"},
        "model_fingerprint": embedder.fingerprint,
        "selected": selected_records,
        "selection_stage_uncovered_facet_ids": facet_ids,
        "warnings": warnings,
        "audit_artifact": {
            "path": str(audit_path),
            "sha256": hashlib.sha256(audit_bytes).hexdigest(),
        },
        "lineage": {
            "candidate_artifact_sha256": request["candidate_artifact"]["sha256"],
            "candidate_count": request["candidate_artifact"]["candidate_count"],
            "unique_candidate_count": len(candidates),
            "shortlisted_count": len(eligible),
            "understanding_job_ids": sorted({item["understanding_job_id"] for item in candidates}),
            "collection_session_ids": request.get("collection_session_ids", []),
        },
    }
    _atomic_write(output_dir / "selection-result.json", _json_bytes(result))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="select-embedding-topk")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--route", required=True)
    arguments = parser.parse_args(argv)
    try:
        request = json.loads(Path(arguments.request).read_text(encoding="utf-8"))
        route_decision = json.loads(Path(arguments.route).read_text(encoding="utf-8"))
        result = select(
            request,
            embedder=E5OnnxEmbedder(),
            output_dir=Path(arguments.output_dir),
            cache_dir=Path(arguments.cache_dir),
            route=route_decision,
        )
    except (SelectionError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "schema_version": RESULT_SCHEMA,
                    "status": "error",
                    "code": "embedding_selection_failed",
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
                "schema_version": RESULT_SCHEMA,
                "status": "selected",
                "selection_id": result["selection_id"],
                "result_path": str(Path(arguments.output_dir).resolve() / "selection-result.json"),
                "selected_count": len(result["selected"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
