"""Typer adapter for the material collector application modules."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import BaseModel, ValidationError
from typer._click.exceptions import Abort, ClickException

from material_collector.application.media_actions import MediaApplication
from material_collector.application.session_runtime import SessionControlApplication
from material_collector.application.sessions import (
    CreateSessionRequest,
    RuntimeConstraints,
    SessionApplication,
)
from material_collector.application.source_manifest import SourceManifestApplication
from material_collector.application.workflow import CollectionWorkflow, WorkflowResult
from material_collector.core.contracts import normalize_contracts
from material_collector.core.errors import (
    CollectorError,
    ContractError,
    ContractVersionError,
    SessionNotFoundError,
    SessionStateError,
    WorkspaceError,
)
from material_collector.core.media import BrowserChannel, Platform
from material_collector.infrastructure.assets import WorkspaceAssetStoreFactory
from material_collector.infrastructure.authentication import (
    PlaywrightAuthenticationGateway,
)
from material_collector.infrastructure.executor import (
    CollectionExecutor,
    ExecutorFailure,
    ExecutorInvocation,
)
from material_collector.infrastructure.media_inspection import (
    LocalMediaFingerprintService,
)
from material_collector.infrastructure.platforms import (
    BilibiliAdapter,
    DouyinAdapter,
    PlaywrightPlatformTransport,
    XiaohongshuAdapter,
)
from material_collector.infrastructure.session_runtime_store import SqliteSessionRuntime
from material_collector.infrastructure.session_store import SqliteSessionStore
from material_collector.infrastructure.source_manifest_store import (
    SqliteSourceManifestStore,
)

app = typer.Typer(
    add_completion=False,
    help="Discover and retain source material for Agent-assisted short-video editing.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
sessions_app = typer.Typer(
    help="Inspect sessions in one explicit material workspace.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(sessions_app, name="sessions")
auth_app = typer.Typer(help="Manage reusable local platform login profiles.")
review_app = typer.Typer(help="Inspect and decide long-video review items.")
result_app = typer.Typer(help="Export the authoritative collection result.")
media_app = typer.Typer(help="Fetch media assets on demand.")
contracts_app = typer.Typer(help="Validate and normalize versioned input contracts.")
executor_app = typer.Typer(
    help="Start and control collector-owned background executions."
)
app.add_typer(auth_app, name="auth")
app.add_typer(review_app, name="review")
app.add_typer(result_app, name="result")
app.add_typer(media_app, name="media")
app.add_typer(contracts_app, name="contracts")
app.add_typer(executor_app, name="executor")


class ProgressFormat(StrEnum):
    JSONL = "jsonl"
    TEXT = "text"


def _application() -> SessionApplication:
    return SessionApplication(store=SqliteSessionStore())


def _session_control_application() -> SessionControlApplication:
    return SessionControlApplication(
        sessions=_application(),
        runtime=SqliteSessionRuntime(),
    )


def _manifest_application() -> SourceManifestApplication:
    return SourceManifestApplication(store=SqliteSourceManifestStore())


def _authentication() -> PlaywrightAuthenticationGateway:
    return PlaywrightAuthenticationGateway()


class _CliProgressReporter:
    def __init__(self, progress_format: ProgressFormat) -> None:
        self._progress_format = progress_format

    def report(self, event: str, details: dict[str, object]) -> None:
        if self._progress_format is ProgressFormat.TEXT:
            fields = " ".join(
                f"{key}={_human_progress_value(value)}"
                for key, value in sorted(details.items())
            )
            typer.echo(f"{event}: {fields}" if fields else event, err=True)
            return
        _emit_diagnostic(
            {
                "schema_version": "1.0",
                "event": event,
                "details": details,
            }
        )


def _human_progress_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _platform_adapters() -> dict[Platform, Any]:
    transport = PlaywrightPlatformTransport()
    adapters = (
        BilibiliAdapter(transport),
        DouyinAdapter(transport),
        XiaohongshuAdapter(transport),
    )
    return {adapter.platform: adapter for adapter in adapters}


def _workflow(*, progress: _CliProgressReporter) -> CollectionWorkflow:
    adapters = _platform_adapters()
    return CollectionWorkflow(
        authentication=_authentication(),
        search_providers=adapters,
        source_resolvers=adapters,
        media_fetchers=adapters,
        sessions=_application(),
        runtime=SqliteSessionRuntime(lease_ttl_seconds=60),
        manifest=_manifest_application(),
        asset_stores=WorkspaceAssetStoreFactory(),
        fingerprints=LocalMediaFingerprintService(),
        progress=progress,
    )


def _media_application() -> MediaApplication:
    return MediaApplication(
        media_fetchers=_platform_adapters(),
        manifest=_manifest_application(),
        asset_stores=WorkspaceAssetStoreFactory(),
    )


def _json_text(value: BaseModel | dict[str, Any]) -> str:
    payload = (
        value.model_dump(mode="json", exclude_none=False) if isinstance(value, BaseModel) else value
    )
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _emit_final(value: BaseModel | dict[str, Any]) -> None:
    typer.echo(_json_text(value), err=False)


def _emit_diagnostic(event: dict[str, Any]) -> None:
    typer.echo(_json_text(event), err=True)


def _exit_code_for(error: CollectorError) -> int:
    if isinstance(
        error,
        (ContractError, ContractVersionError, SessionNotFoundError, WorkspaceError),
    ):
        return 40
    if error.details.get("retryable") is True:
        return 30
    if error.code in {
        "auth_profile_busy",
        "auth_desktop_unavailable",
        "auth_login_timeout",
        "browser_channel_exhausted",
        "browser_channel_failed",
        "platform_timeout",
        "platform_navigation_timeout",
        "platform_response_timeout",
        "platform_transport_failed",
        "platform_download_failed",
        "platform_http_error",
    }:
        return 30
    if isinstance(error, SessionStateError):
        return 50
    return 50


def _fail(error: CollectorError) -> NoReturn:
    exit_code = _exit_code_for(error)
    payload = {
        "schema_version": "1.0",
        "status": "error",
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
        "action_required": None,
    }
    _emit_diagnostic(
        {
            "schema_version": "1.0",
            "event": "command_failed",
            "error_code": error.code,
            "exit_code": exit_code,
        }
    )
    _emit_final(payload)
    raise typer.Exit(exit_code)


def _execute[ResultModel: BaseModel | dict[str, Any]](
    operation: Callable[[], ResultModel],
) -> None:
    try:
        result = operation()
    except CollectorError as error:
        _fail(error)
    except ValidationError as error:
        _fail(
            ContractError(
                "CLI runtime constraints are invalid.",
                details={"issues": error.errors(include_url=False, include_input=False)},
            )
        )
    except Exception as error:  # pragma: no cover - last-resort CLI containment
        _emit_diagnostic(
            {
                "schema_version": "1.0",
                "event": "command_failed",
                "error_code": "internal_error",
                "exception_type": type(error).__name__,
                "exit_code": 50,
            }
        )
        _emit_final(
            {
                "schema_version": "1.0",
                "status": "error",
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected internal error occurred.",
                    "details": {},
                },
                "action_required": None,
            }
        )
        raise typer.Exit(50) from error
    _emit_final(result)


def _execute_async[ResultModel: BaseModel | dict[str, Any]](
    operation: Callable[[], Coroutine[Any, Any, ResultModel]],
    *,
    exit_code: Callable[[ResultModel], int] | None = None,
) -> None:
    holder: list[ResultModel] = []

    def run() -> ResultModel:
        result: ResultModel = asyncio.run(operation())
        holder.append(result)
        return result

    _execute(run)
    code = 0 if exit_code is None else exit_code(holder[0])
    if code:
        raise typer.Exit(code)


def _workflow_exit_code(result: WorkflowResult) -> int:
    if result.status in {
        "integration_required",
        "auth_required",
        "decision_required",
    }:
        return 20
    if result.status == "cancelled":
        return 21
    if result.issues:
        return 10
    return 0


def _read_json_contract(path: Path, document: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(
            f"{document} must be readable UTF-8 JSON.",
            details={"document": document},
        ) from error


@executor_app.command("invoke")
def executor_invoke_command(
    operation: Annotated[
        str | None,
        typer.Option("--operation", help="run, resume, status, or cancel."),
    ] = None,
    collector_path: Annotated[
        str | None,
        typer.Option(
            "--collector-path",
            help="Collector executable; defaults to this Python environment.",
        ),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Persistent material workspace."),
    ] = None,
    input_path: Annotated[
        str | None,
        typer.Option("--input", help="Collection input JSON for run."),
    ] = None,
    query_plans_path: Annotated[
        str | None,
        typer.Option("--query-plans", help="QueryPlan JSON for run."),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Existing session for resume/status/cancel."),
    ] = None,
    request_timeout_seconds: Annotated[
        str,
        typer.Option("--request-timeout-seconds"),
    ] = "30",
    max_rounds: Annotated[str, typer.Option("--max-rounds")] = "3",
    max_videos: Annotated[str, typer.Option("--max-videos")] = "18",
    progress_format: Annotated[str, typer.Option("--progress-format")] = "jsonl",
    control_directory: Annotated[
        str | None,
        typer.Option("--control-directory", help="Override executor artifact directory."),
    ] = None,
) -> None:
    """Invoke the cross-platform executor through its stable Agent contract."""

    invocation = ExecutorInvocation(
        operation=operation,
        collector_path=collector_path,
        workspace=workspace,
        input_path=input_path,
        query_plans_path=query_plans_path,
        session_id=session_id,
        request_timeout_seconds=request_timeout_seconds,
        max_rounds=max_rounds,
        max_videos=max_videos,
        progress_format=progress_format,
        control_directory=control_directory,
    )
    try:
        result = CollectionExecutor().invoke(invocation)
    except ExecutorFailure as error:
        _emit_final(error.payload())
        raise typer.Exit(error.exit_code) from error
    except Exception as error:  # pragma: no cover - last-resort executor containment
        _emit_final(
            {
                "schema_version": "1.0",
                "status": "error",
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected executor error occurred.",
                },
            }
        )
        raise typer.Exit(50) from error
    _emit_final(result.payload)
    if result.exit_code:
        raise typer.Exit(result.exit_code)


@contracts_app.command("normalize")
def normalize_contracts_command(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    query_plans_path: Annotated[
        Path,
        typer.Option(
            "--query-plans",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
) -> None:
    """Return the exact normalized contracts used when creating a session."""

    def operation() -> dict[str, Any]:
        normalized = normalize_contracts(
            _read_json_contract(input_path, "collection_input"),
            _read_json_contract(query_plans_path, "query_plans"),
        )
        return {
            "schema_version": "material-collector-normalized-contracts/v1",
            "status": "normalized",
            "collection_input": normalized.collection_input.model_dump(
                mode="json",
                exclude_none=False,
            ),
            "query_plans": normalized.query_plans.model_dump(
                mode="json",
                exclude_none=False,
            ),
            "warnings": [asdict(warning) for warning in normalized.warnings],
        }

    _execute(operation)


@app.command("run")
def run_command(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            help="Persistent material workspace that will own this session.",
        ),
    ],
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 JSON collection input using schema version 1.0.",
        ),
    ],
    query_plans_path: Annotated[
        Path,
        typer.Option(
            "--query-plans",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="UTF-8 JSON QueryPlan document using schema version 2.0.",
        ),
    ],
    max_rounds: Annotated[
        int,
        typer.Option("--max-rounds", min=1, help="Maximum rounds per QueryPlan."),
    ] = 3,
    max_videos: Annotated[
        int,
        typer.Option(
            "--max-videos",
            min=1,
            help="Maximum newly admitted media units per QueryPlan.",
        ),
    ] = 18,
    auth_profile: Annotated[
        str,
        typer.Option(
            "--auth-profile",
            help="Local authentication profile identifier.",
        ),
    ] = "default",
    browser_channel: Annotated[
        BrowserChannel,
        typer.Option(
            "--browser-channel",
            case_sensitive=False,
            help="Native browser channel: auto prefers Edge, then Chrome.",
        ),
    ] = BrowserChannel.AUTO,
    auth_wait_seconds: Annotated[
        int,
        typer.Option(
            "--auth-wait-seconds",
            min=1,
            help="Maximum headed-login wait time.",
        ),
    ] = 600,
    request_timeout_seconds: Annotated[
        int,
        typer.Option(
            "--request-timeout-seconds",
            min=1,
            help="Per-request platform network timeout.",
        ),
    ] = 30,
    progress_format: Annotated[
        ProgressFormat,
        typer.Option(
            "--progress-format",
            case_sensitive=False,
            help="Progress format written to stderr.",
        ),
    ] = ProgressFormat.JSONL,
    show_search_browsers: Annotated[
        bool,
        typer.Option(
            "--show-search-browsers",
            help="Show one identifiable search window per in-scope platform.",
        ),
    ] = False,
) -> None:
    """Create a session and run collection to its next durable checkpoint."""

    async def operation() -> WorkflowResult:
        constraints = RuntimeConstraints(
            max_rounds=max_rounds,
            max_videos=max_videos,
            auth_profile=auth_profile,
            browser_channel=browser_channel,
            auth_wait_seconds=auth_wait_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )
        request = CreateSessionRequest(
            workspace=workspace,
            input_path=input_path,
            query_plans_path=query_plans_path,
            constraints=constraints,
        )
        session = _application().create_session(request)
        return await _workflow(
            progress=_CliProgressReporter(progress_format)
        ).run(
            workspace,
            session.session_id,
            show_search_browsers=show_search_browsers,
        )

    _execute_async(operation, exit_code=_workflow_exit_code)


@app.command("resume")
def resume_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Persistent material workspace."),
    ],
    session_id: Annotated[
        str,
        typer.Option("--session-id", help="Collection session identifier."),
    ],
    progress_format: Annotated[
        ProgressFormat,
        typer.Option(
            "--progress-format",
            case_sensitive=False,
            help="Progress format written to stderr.",
        ),
    ] = ProgressFormat.JSONL,
    show_search_browsers: Annotated[
        bool,
        typer.Option(
            "--show-search-browsers",
            help="Show search windows for this resume execution only.",
        ),
    ] = False,
) -> None:
    """Resume from the first uncommitted stage."""

    _execute_async(
        lambda: _workflow(
            progress=_CliProgressReporter(progress_format)
        ).run(
            workspace,
            session_id,
            show_search_browsers=show_search_browsers,
        ),
        exit_code=_workflow_exit_code,
    )


@app.command("cancel")
def cancel_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Persistent material workspace."),
    ],
    session_id: Annotated[
        str,
        typer.Option("--session-id", help="Collection session identifier."),
    ],
) -> None:
    """Request cooperative cancellation for a session."""

    def operation() -> dict[str, Any]:
        return _session_control_application().request_cancel(workspace, session_id)

    _execute(operation)


@app.command("status")
def status_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Persistent material workspace."),
    ],
    session_id: Annotated[
        str,
        typer.Option("--session-id", help="Collection session identifier."),
    ],
) -> None:
    """Read one collection session without changing its state."""

    def operation() -> dict[str, Any]:
        return _session_control_application().get_status(workspace, session_id)

    _execute(operation)


@sessions_app.command("list")
def list_sessions_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", help="Persistent material workspace."),
    ],
) -> None:
    """List sessions found by scanning one explicit material workspace."""

    _execute(lambda: _application().list_sessions(workspace))


@auth_app.command("status")
def auth_status_command(
    platform: Annotated[
        Platform,
        typer.Option("--platform", case_sensitive=False),
    ],
    auth_profile: Annotated[
        str,
        typer.Option("--auth-profile", help="Local authentication profile."),
    ] = "default",
    browser_channel: Annotated[
        BrowserChannel,
        typer.Option("--browser-channel", case_sensitive=False),
    ] = BrowserChannel.CHROME,
) -> None:
    """Probe one platform without opening an interactive login."""

    _execute_async(
        lambda: _authentication().probe(platform, auth_profile, browser_channel)
    )


@auth_app.command("login")
def auth_login_command(
    platform: Annotated[
        Platform,
        typer.Option("--platform", case_sensitive=False),
    ],
    auth_profile: Annotated[str, typer.Option("--auth-profile")] = "default",
    auth_wait_seconds: Annotated[
        int,
        typer.Option("--auth-wait-seconds", min=1),
    ] = 600,
    browser_channel: Annotated[
        BrowserChannel,
        typer.Option("--browser-channel", case_sensitive=False),
    ] = BrowserChannel.AUTO,
) -> None:
    """Ensure one platform is authenticated, opening a browser when needed."""

    async def operation() -> dict[str, Any]:
        selection = await _authentication().ensure_authenticated(
            (platform,),
            auth_profile,
            auth_wait_seconds,
            browser_channel=browser_channel,
        )
        return {
            "schema_version": "1.0",
            "status": "authenticated",
            "auth_profile": auth_profile,
            "browser_channel": selection.browser_channel.value,
            "platforms": [
                probe.model_dump(mode="json", exclude_none=False)
                for probe in selection.probes
            ],
            "action_required": None,
        }

    _execute_async(operation)


@auth_app.command("logout")
def auth_logout_command(
    platform: Annotated[
        Platform,
        typer.Option("--platform", case_sensitive=False),
    ],
    confirm: Annotated[str, typer.Option("--confirm")],
    auth_profile: Annotated[str, typer.Option("--auth-profile")] = "default",
    browser_channel: Annotated[
        BrowserChannel,
        typer.Option("--browser-channel", case_sensitive=False),
    ] = BrowserChannel.CHROME,
) -> None:
    """Delete one platform profile after explicit platform confirmation."""

    async def operation() -> dict[str, Any]:
        await _authentication().logout(
            platform,
            auth_profile,
            confirm,
            browser_channel,
        )
        return {
            "schema_version": "1.0",
            "status": "logged_out",
            "auth_profile": auth_profile,
            "platform": platform.value,
            "browser_channel": browser_channel.value,
            "action_required": None,
        }

    _execute_async(operation)


@review_app.command("list")
def review_list_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    session_id: Annotated[str, typer.Option("--session-id")],
) -> None:
    """List only media units awaiting a human decision."""

    _execute(lambda: _media_application().list_reviews(workspace, session_id))


def _review_decision(
    workspace: Path,
    session_id: str,
    media_unit_id: str,
    *,
    approved: bool,
) -> None:
    _execute(
        lambda: _media_application().decide_review(
            workspace,
            session_id,
            media_unit_id,
            approved=approved,
        )
    )


@review_app.command("approve")
def review_approve_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    session_id: Annotated[str, typer.Option("--session-id")],
    media_unit_id: Annotated[str, typer.Option("--media-unit-id")],
) -> None:
    """Approve a media unit for later manual or on-demand use."""

    _review_decision(
        workspace,
        session_id,
        media_unit_id,
        approved=True,
    )


@review_app.command("reject")
def review_reject_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    session_id: Annotated[str, typer.Option("--session-id")],
    media_unit_id: Annotated[str, typer.Option("--media-unit-id")],
) -> None:
    """Reject a media unit."""

    _review_decision(
        workspace,
        session_id,
        media_unit_id,
        approved=False,
    )


@result_app.command("export")
def result_export_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    session_id: Annotated[str, typer.Option("--session-id")],
) -> None:
    """Republish and return the authoritative source manifest."""

    _execute(lambda: _media_application().export_result(workspace, session_id))


@media_app.command("fetch-hq")
def media_fetch_high_quality_command(
    workspace: Annotated[Path, typer.Option("--workspace")],
    session_id: Annotated[str, typer.Option("--session-id")],
    media_unit_id: Annotated[str, typer.Option("--media-unit-id")],
) -> None:
    """Idempotently fetch one complete high-quality media unit."""

    async def operation() -> BaseModel:
        session = _application().get_session(workspace, session_id)
        application = _media_application()
        platform = application.platform_for_media(
            workspace,
            session_id,
            media_unit_id,
        )
        if session.selected_browser_channel is None:
            raise SessionStateError(
                "The session has not frozen a successful browser channel."
            )
        await _authentication().ensure_authenticated(
            (platform,),
            session.constraints.auth_profile,
            session.constraints.auth_wait_seconds,
            browser_channel=session.selected_browser_channel,
        )
        return await application.fetch_high_quality(
            workspace,
            session_id,
            media_unit_id,
            auth_profile=session.constraints.auth_profile,
            browser_channel=session.selected_browser_channel,
            request_timeout_seconds=session.constraints.request_timeout_seconds,
        )

    _execute_async(operation)


def _emit_usage_error(error: ClickException) -> int:
    _emit_final(
        {
            "schema_version": "1.0",
            "status": "error",
            "error": {
                "code": "cli_usage_error",
                "message": error.format_message(),
                "details": {},
            },
            "action_required": None,
        }
    )
    return 40


def main() -> int:
    """Console-script entry point with stable machine-readable usage failures."""

    command = typer.main.get_command(app)
    try:
        result = command.main(
            args=sys.argv[1:],
            prog_name="material-collector",
            standalone_mode=False,
        )
    except ClickException as error:
        return _emit_usage_error(error)
    except Abort:
        _emit_final(
            {
                "schema_version": "1.0",
                "status": "cancelled",
                "error": None,
                "action_required": None,
            }
        )
        return 130
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
