"""Agent-first Typer CLI Adapter."""

from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from typer._click.exceptions import UsageError

ClickExit = typer.Exit

from semvideo import (
    JOB_SCHEMA_VERSION,
    SKILL_PROTOCOL_VERSION,
    WORKSPACE_SCHEMA_VERSION,
    __version__,
)
from semvideo.application.configuration import (
    set_llm_provider_profile,
    set_media_tools,
)
from semvideo.application.diagnostics import run_doctor
from semvideo.application.evidence_benchmark import build_report
from semvideo.application.jobs import (
    cancel_job,
    get_job_admission,
    resume_job,
    retry_job,
    submit_job,
    wait_for_job,
)
from semvideo.application.queries import (
    get_inspection_report,
    get_job,
    get_segment,
    get_shot,
    list_jobs,
    list_segments,
    list_shots,
    read_job_logs,
)
from semvideo.application.subagent_handoff import (
    approve_profiles,
    import_result,
    prepare_request,
)
from semvideo.application.task_store import JobNotFoundError
from semvideo.application.workspace import (
    InvalidWorkspaceError,
    WorkspaceNotFoundError,
    WorkspacePaths,
    discover_workspace,
    initialize_workspace,
    read_workspace_marker,
)
from semvideo.config import load_workspace_config
from semvideo.errors import (
    ErrorCategory,
    RecoveryAction,
    SemvideoError,
    config_error,
)

app = typer.Typer(
    name="semvideo",
    help="Local Agent-first video understanding and semantic segmentation.",
    no_args_is_help=True,
)
workspace_app = typer.Typer(help="Inspect the current Semvideo workspace.")
job_app = typer.Typer(help="List, inspect, cancel, resume, and retry jobs.")
segment_app = typer.Typer(help="Inspect and export final semantic segments.")
shot_app = typer.Typer(help="Inspect detected shots and cinematography annotations.")
config_app = typer.Typer(help="Inspect and update non-secret workspace configuration.")
profile_app = typer.Typer(help="Inspect processing profiles.")
app.add_typer(workspace_app, name="workspace")
app.add_typer(job_app, name="job")
app.add_typer(segment_app, name="segment")
app.add_typer(shot_app, name="shot")
app.add_typer(config_app, name="config")
app.add_typer(profile_app, name="profile")

stdout = Console(stderr=False)
stderr = Console(stderr=True)


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, str):
        stdout.print(value)
    else:
        stdout.print_json(json.dumps(value, ensure_ascii=False))


def _abort(error: BaseException, *, json_output: bool) -> NoReturn:
    if isinstance(error, SemvideoError):
        payload = error.as_dict()
        exit_code = error.exit_code
    elif isinstance(error, (WorkspaceNotFoundError, InvalidWorkspaceError)):
        structured = config_error("workspace_invalid", str(error))
        payload = structured.as_dict()
        exit_code = structured.exit_code
    elif isinstance(error, JobNotFoundError):
        structured = SemvideoError(
            code="job_not_found",
            category=ErrorCategory.INPUT,
            message=f"任务不存在：{error}",
            recovery=RecoveryAction.USER_ACTION,
            exit_code=3,
        )
        payload = structured.as_dict()
        exit_code = structured.exit_code
    else:
        structured = SemvideoError(
            code="cli_internal_error",
            category=ErrorCategory.INTERNAL,
            message="CLI 发生未处理的程序错误。",
            recovery=RecoveryAction.REPORT_BUG,
            details={"exception_type": type(error).__name__, "reason": str(error)},
            exit_code=10,
        )
        payload = structured.as_dict()
        exit_code = 10
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
    else:
        stderr.print(f"[red]{payload['message']}[/red]")
        stderr.print(f"{payload['category']} · {payload['code']}")
    raise typer.Exit(exit_code)


def _native_cli_path(path: str | Path) -> Path:
    """Normalize an MSYS drive path before it crosses the CLI boundary."""

    raw = str(path)
    if (
        os.name == "nt"
        and len(raw) >= 3
        and raw[0] == "/"
        and raw[1].isalpha()
        and raw[2] == "/"
    ):
        return Path(f"{raw[1].upper()}:{raw[2:]}")
    return Path(path)


def _workspace(path: str | Path | None) -> WorkspacePaths:
    return (
        discover_workspace(explicit=_native_cli_path(path))
        if path
        else discover_workspace()
    )


@app.callback(invoke_without_command=True)
def root_callback(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version information."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    if version:
        _emit(
            {
                "cli_version": __version__,
                "workspace_schema": {"min": 1, "max": WORKSPACE_SCHEMA_VERSION},
                "job_schema": {"min": 1, "max": JOB_SCHEMA_VERSION},
                "skill_protocol_version": SKILL_PROTOCOL_VERSION,
            },
            json_output=json_output,
        )
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command("init")
def init_command(
    workspace_root: str,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        workspace = initialize_workspace(_native_cli_path(workspace_root))
        marker = read_workspace_marker(workspace)
        _emit(
            {
                "schema_version": 1,
                "workspace_id": marker["workspace_id"],
                "root": str(workspace.root),
                "data_root": str(workspace.data),
                "config_path": str(workspace.config),
            },
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@workspace_app.command("show")
def workspace_show(
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        marker = read_workspace_marker(resolved)
        _emit(
            {
                "schema_version": 1,
                "workspace_id": marker["workspace_id"],
                "workspace_schema_version": marker["schema_version"],
                "root": str(resolved.root),
                "data_root": str(resolved.data),
                "config_path": str(resolved.config),
            },
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@app.command("doctor")
def doctor(
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        _emit(run_doctor(resolved), json_output=json_output)
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@app.command("process")
def process_command(
    input_video: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    profile: str = typer.Option("default", "--profile"),
    render: bool | None = typer.Option(None, "--render/--no-render"),
    wait: bool = typer.Option(False, "--wait"),
    quiet: bool = typer.Option(False, "--quiet"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    subagent_context_tokens: int | None = typer.Option(
        None, "--subagent-context-tokens"
    ),
    subagent_slots: int | None = typer.Option(None, "--subagent-slots"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        result = submit_job(
            resolved,
            _native_cli_path(input_video),
            profile=profile,
            render=render,
            idempotency_key=idempotency_key,
            subagent_context_tokens=subagent_context_tokens,
            subagent_slots=subagent_slots,
        )
        if wait:
            progress_sink = None
            if not quiet:
                progress_sink = lambda event: (
                    typer.echo(
                        json.dumps(event, ensure_ascii=False),
                        err=True,
                    )
                    if json_output
                    else stderr.print(
                        f"[cyan]{event['job_id']}[/cyan] "
                        f"{event['stage']} · {event['message']}"
                    )
                )
            final_state = wait_for_job(
                resolved,
                str(result["job_id"]),
                progress_sink=progress_sink,
            )
            result["state"] = final_state["state"]
            if isinstance(final_state.get("result"), dict):
                result.update(final_state["result"])
            result["failure"] = final_state.get("failure")
        _emit(result, json_output=json_output)
        if result["state"] == "failed":
            raise typer.Exit(6)
        if result["state"] == "cancelled":
            raise typer.Exit(7)
        if result["state"] == "interrupted":
            raise typer.Exit(9)
    except typer.Exit:
        raise
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("list")
def job_list(
    workspace: str | None = typer.Option(None, "--workspace"),
    state: str | None = typer.Option(None, "--state"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            list_jobs(_workspace(workspace), state=state),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("status")
def job_status(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            get_job(_workspace(workspace), job_id),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("admission")
def job_admission(
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            get_job_admission(_workspace(workspace)),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("subagent-request")
def job_subagent_request(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    effective_context_tokens: int | None = typer.Option(
        None, "--effective-context-tokens"
    ),
    subagent_slots: int | None = typer.Option(None, "--subagent-slots"),
    repair: bool = typer.Option(False, "--repair"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Freeze or export one generic-subagent request checkpoint."""
    try:
        _emit(
            prepare_request(
                _workspace(workspace),
                job_id,
                effective_context_tokens=effective_context_tokens,
                subagent_slots=subagent_slots,
                repair=repair,
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("subagent-import")
def job_subagent_import(
    job_id: str,
    result: str = typer.Option(..., "--result"),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Atomically validate and import one generic-subagent result."""
    try:
        _emit(
            import_result(
                _workspace(workspace), job_id, _native_cli_path(result)
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@profile_app.command("approve-subagent-evidence")
def approve_subagent_evidence(
    input_path: str = typer.Option(..., "--input"),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Install all four benchmark-derived evidence profiles after human approval."""
    try:
        _emit(
            approve_profiles(_workspace(workspace), _native_cli_path(input_path)),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@profile_app.command("benchmark-subagent-evidence")
def benchmark_subagent_evidence(
    input_path: str = typer.Option(..., "--input"),
    output_directory: str = typer.Option(..., "--output"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Aggregate reproducible four-tier measurements without approving them."""
    try:
        _emit(
            build_report(
                _native_cli_path(input_path),
                _native_cli_path(output_directory),
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("logs")
def job_logs(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    follow: bool = typer.Option(False, "--follow"),
    quiet: bool = typer.Option(False, "--quiet"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        line_sink = None
        if quiet:
            line_sink = lambda _line: None
        elif json_output and follow:
            line_sink = lambda line: typer.echo(
                json.dumps(
                    {"type": "log", "line": line},
                    ensure_ascii=False,
                ),
                err=True,
            )
        elif not json_output:
            line_sink = lambda line: typer.echo(line)
        result = read_job_logs(
            _workspace(workspace),
            job_id,
            follow=follow,
            line_sink=line_sink,
        )
        if json_output:
            _emit(result, json_output=True)
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("cancel")
def job_cancel(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            cancel_job(_workspace(workspace), job_id),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


def _resume(job_id: str, workspace: str | None, json_output: bool) -> None:
    try:
        resolved = _workspace(workspace)
        _emit(resume_job(resolved, job_id), json_output=json_output)
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@job_app.command("resume")
def job_resume(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _resume(job_id, workspace, json_output)


@job_app.command("retry")
def job_retry(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    from_stage: str | None = typer.Option(None, "--from"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        _emit(
            retry_job(resolved, job_id, from_stage=from_stage),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@segment_app.command("list")
def segment_list(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    review_only: bool = typer.Option(False, "--review-only"),
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(50, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            list_segments(
                _workspace(workspace),
                job_id,
                review_only=review_only,
                offset=offset,
                limit=limit,
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@segment_app.command("show")
def segment_show(
    job_id: str,
    segment_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            get_segment(_workspace(workspace), job_id, segment_id),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@segment_app.command("export")
def segment_export(
    job_id: str,
    segment_id: str,
    output: str = typer.Option(..., "--output"),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        from semvideo.application.processor import export_segment

        _emit(
            export_segment(
                resolved,
                job_id,
                segment_id,
                _native_cli_path(output),
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@shot_app.command("list")
def shot_list(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    viewpoint: list[str] | None = typer.Option(None, "--viewpoint"),
    scale: list[str] | None = typer.Option(None, "--scale"),
    motion: list[str] | None = typer.Option(None, "--motion"),
    speed: list[str] | None = typer.Option(None, "--speed"),
    keyword: list[str] | None = typer.Option(None, "--keyword"),
    offset: int = typer.Option(0, "--offset", min=0),
    limit: int = typer.Option(50, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            list_shots(
                _workspace(workspace),
                job_id,
                viewpoint=viewpoint,
                scale=scale,
                motion=motion,
                speed=speed,
                keyword=keyword,
                offset=offset,
                limit=limit,
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@shot_app.command("show")
def shot_show(
    job_id: str,
    shot_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            get_shot(_workspace(workspace), job_id, shot_id),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@shot_app.command("export")
def shot_export(
    job_id: str,
    shot_id: str,
    output: str = typer.Option(..., "--output"),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        from semvideo.application.processor import export_shot

        _emit(
            export_shot(
                _workspace(workspace),
                job_id,
                shot_id,
                _native_cli_path(output),
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@app.command("inspect")
def inspect_job(
    job_id: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    open_report: bool = typer.Option(False, "--open"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        path = get_inspection_report(_workspace(workspace), job_id)
        if open_report:
            webbrowser.open(path.as_uri())
        _emit(
            {"schema_version": 1, "job_id": job_id, "report": str(path)},
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@config_app.command("show")
def config_show(
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        resolved = _workspace(workspace)
        config = load_workspace_config(resolved.data)
        value = config.model_dump(mode="json")
        value["llm"]["max_input_tokens"] = config.llm.max_input_tokens
        value["llm"]["credential_present"] = config.credential_present()
        _emit(value, json_output=json_output)
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@config_app.command("set-media-tools")
def config_set_media_tools(
    ffmpeg: str = typer.Option(..., "--ffmpeg"),
    ffprobe: str = typer.Option(..., "--ffprobe"),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            set_media_tools(
                _workspace(workspace),
                ffmpeg_path=_native_cli_path(ffmpeg),
                ffprobe_path=_native_cli_path(ffprobe),
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@config_app.command("set-llm-provider")
def config_set_llm_provider(
    provider: str = typer.Argument(..., help="Supported provider profile name."),
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        _emit(
            set_llm_provider_profile(
                _workspace(workspace),
                provider=provider,
            ),
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@profile_app.command("list")
def profile_list(
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        config = load_workspace_config(_workspace(workspace).data)
        _emit(
            {
                "schema_version": 1,
                "items": [{"name": config.profile, "version": 1}],
            },
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


@profile_app.command("validate")
def profile_validate(
    name: str,
    workspace: str | None = typer.Option(None, "--workspace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        config = load_workspace_config(_workspace(workspace).data)
        if name != config.profile:
            raise config_error(
                "profile_not_found",
                f"处理策略不存在：{name}",
                profile=name,
            )
        _emit(
            {"schema_version": 1, "name": name, "version": 1, "valid": True},
            json_output=json_output,
        )
    except BaseException as exc:
        _abort(exc, json_output=json_output)


def main() -> None:
    """Console entry point that structures Click/Typer invocation errors."""

    try:
        exit_code = app(standalone_mode=False)
        if isinstance(exit_code, int) and exit_code:
            raise SystemExit(exit_code)
    except UsageError as exc:
        payload = {
            "schema_version": 1,
            "code": "cli_invocation_invalid",
            "category": ErrorCategory.INVOCATION.value,
            "message": str(exc),
            "retryable": True,
            "recovery": RecoveryAction.CORRECT_AND_RETRY.value,
            "details": {},
        }
        if "--json" in sys.argv[1:]:
            typer.echo(json.dumps(payload, ensure_ascii=False), err=True)
        else:
            stderr.print(f"[red]{payload['message']}[/red]")
            stderr.print(f"{payload['category']} · {payload['code']}")
        raise SystemExit(2)
    except ClickExit as exc:
        raise SystemExit(exc.exit_code)
