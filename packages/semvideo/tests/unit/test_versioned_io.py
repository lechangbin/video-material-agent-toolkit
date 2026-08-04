from __future__ import annotations

import json

import pytest

from semvideo.infrastructure.io import (
    UnsupportedSchemaVersionError,
    read_versioned_json,
    read_versioned_json_lines,
)


def test_versioned_json_rejects_a_future_schema(tmp_path) -> None:
    path = tmp_path / "artifact.json"
    path.write_text(
        json.dumps({"schema_version": 2}),
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedSchemaVersionError):
        read_versioned_json(path)


def test_versioned_json_lines_rejects_a_future_schema(tmp_path) -> None:
    path = tmp_path / "artifact.jsonl"
    path.write_text(
        json.dumps({"schema_version": 1})
        + "\n"
        + json.dumps({"schema_version": 2})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedSchemaVersionError):
        read_versioned_json_lines(path)
