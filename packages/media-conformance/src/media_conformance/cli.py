"""Structured public CLI for editing-media conformance."""

from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from media_conformance.contracts import (
    EditingMediaConformanceRequest,
    contract_schema_bundle,
)
from media_conformance.errors import ConformanceError
from media_conformance.runtime import ConformanceRuntime

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


class ExitCode(IntEnum):
    OK = 0
    ACTION_REQUIRED = 20
    REQUEST_INVALID = 40
    FAILED = 50


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _stop(code: ExitCode) -> NoReturn:
    raise typer.Exit(int(code))


def _load_request(path: Path) -> EditingMediaConformanceRequest:
    try:
        return EditingMediaConformanceRequest.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        raise ConformanceError(
            "conformance_request_invalid",
            "The conformance request is unavailable or invalid.",
            details={"exception_type": type(error).__name__},
        ) from error


def _execute(operation: Any) -> None:
    try:
        value = operation()
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        _emit(payload)
        if isinstance(payload, dict) and payload.get("status") in {"failed", "cancelled"}:
            _stop(ExitCode.FAILED)
    except ConformanceError as error:
        _emit({"schema_version": "media-conformance-cli/v1", "status": "error", "error": error.as_dict()})
        _stop(ExitCode.REQUEST_INVALID if error.code == "conformance_request_invalid" else ExitCode.FAILED)


@app.command("contracts")
def contracts_command() -> None:
    """Print authoritative JSON Schemas."""
    _emit({"schema_version": "media-conformance-contract-schemas/v1", "contracts": contract_schema_bundle()})


@app.command("prepare")
def prepare_command(
    request_path: Annotated[Path, typer.Option("--request", help="Versioned request JSON.")],
) -> None:
    """Render, verify, and atomically publish one Assembly Set."""
    _execute(lambda: ConformanceRuntime().prepare(_load_request(request_path)))


@app.command("resume")
def resume_command(
    request_path: Annotated[Path, typer.Option("--request", help="Frozen request JSON.")],
) -> None:
    """Resume from hash-verified outputs or rerender missing work."""
    _execute(lambda: ConformanceRuntime().prepare(_load_request(request_path)))


@app.command("status")
def status_command(
    request_path: Annotated[Path, typer.Option("--request", help="Frozen request JSON.")],
) -> None:
    """Inspect durable task state."""
    _execute(lambda: ConformanceRuntime().status(_load_request(request_path)))


@app.command("cancel")
def cancel_command(
    request_path: Annotated[Path, typer.Option("--request", help="Frozen request JSON.")],
) -> None:
    """Request cooperative cancellation."""
    _execute(lambda: ConformanceRuntime().cancel(_load_request(request_path)))


@app.command("verify-set")
def verify_set_command(
    request_path: Annotated[Path, typer.Option("--request", help="Frozen request JSON.")],
) -> None:
    """Repeat packet-level compatibility verification without rendering."""
    _execute(lambda: ConformanceRuntime().verify_existing_set(_load_request(request_path)))


def main() -> None:
    app()
