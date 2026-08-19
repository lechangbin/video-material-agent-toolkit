from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from media_conformance.cli import app


def _request(tmp_path: Path, *, declared_hash: str | None = None) -> Path:
    source = tmp_path / "source odd name.mp4"
    source.write_bytes(b"immutable-source")
    payload = {
        "schema_version": "editing-media-conformance-request/v1",
        "request_id": "request_cli_001",
        "idempotency_key": "idem_cli_001",
        "source": {
            "asset_id": "asset_cli_001",
            "sha256": declared_hash or hashlib.sha256(source.read_bytes()).hexdigest(),
            "path": str(source),
        },
        "ranges": [
            {"clip_id": "clip_001", "start_seconds": 0, "end_seconds": 1}
        ],
        "output_directory": str(tmp_path / "safe outputs"),
        "profile": {},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_contracts_expose_all_public_schemas() -> None:
    result = CliRunner().invoke(app, ["contracts"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "media-conformance-contract-schemas/v1"
    assert set(payload["contracts"]) == {"profile", "request", "result"}


def test_status_and_cancel_are_structured_public_commands(tmp_path: Path) -> None:
    request = _request(tmp_path)

    status = CliRunner().invoke(app, ["status", "--request", str(request)])
    cancel = CliRunner().invoke(app, ["cancel", "--request", str(request)])

    assert status.exit_code == 0
    assert json.loads(status.stdout)["status"] == "not_started"
    assert cancel.exit_code == 0
    assert json.loads(cancel.stdout)["status"] == "cancellation_requested"


def test_prepare_rejects_changed_source_before_ffmpeg_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, declared_hash="0" * 64)
    source = tmp_path / "source odd name.mp4"
    before = source.read_bytes()

    result = CliRunner().invoke(app, ["prepare", "--request", str(request)])

    assert result.exit_code == 50
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "source_hash_mismatch"
    assert source.read_bytes() == before
    assert not list((tmp_path / "safe outputs").rglob("*.partial.mp4"))


def test_invalid_request_returns_machine_error_and_contract_exit_code(
    tmp_path: Path,
) -> None:
    request = tmp_path / "invalid.json"
    request.write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(app, ["prepare", "--request", str(request)])

    assert result.exit_code == 40
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "conformance_request_invalid"
