"""Collector-owned background process lifecycle for Agent invocations."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

ExecutorOperation = Literal["run", "resume", "status", "cancel"]


@dataclass(frozen=True, slots=True)
class ExecutorInvocation:
    operation: str | None
    collector_path: str | None
    workspace: str | None
    input_path: str | None = None
    query_plans_path: str | None = None
    session_id: str | None = None
    request_timeout_seconds: str = "30"
    max_rounds: str = "3"
    max_videos: str = "18"
    browser_channel: str = "auto"
    show_search_browsers: bool = False
    progress_format: str = "jsonl"
    control_directory: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    payload: dict[str, Any]
    exit_code: int = 0


@dataclass(slots=True)
class ExecutorFailure(Exception):
    code: str
    message: str
    exit_code: int = 30
    context: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": "error",
            "error": {"code": self.code, "message": self.message},
            **self.context,
        }


class CollectionExecutor:
    """Start and control durable collection processes through one cross-platform seam."""

    def invoke(self, invocation: ExecutorInvocation) -> ExecutorResult:
        operation = self._operation(invocation.operation)
        timeout = self._integer(
            invocation.request_timeout_seconds,
            code="request_timeout_invalid",
            message="RequestTimeoutSeconds must be an integer from 1 through 3600.",
            minimum=1,
            maximum=3600,
        )
        max_rounds = self._integer(
            invocation.max_rounds,
            code="max_rounds_invalid",
            message="MaxRounds must be a positive integer.",
            minimum=1,
        )
        max_videos = self._integer(
            invocation.max_videos,
            code="max_videos_invalid",
            message="MaxVideos must be a positive integer.",
            minimum=1,
        )
        if invocation.progress_format not in {"jsonl", "text"}:
            raise ExecutorFailure(
                "progress_format_invalid",
                "ProgressFormat must be jsonl or text.",
                40,
            )
        browser_channel = invocation.browser_channel.casefold()
        if browser_channel not in {"auto", "edge", "chrome"}:
            raise ExecutorFailure(
                "browser_channel_invalid",
                "BrowserChannel must be auto, edge, or chrome.",
                40,
            )
        workspace = self._required_path(
            invocation.workspace,
            code="workspace_required",
            message="Workspace is required.",
        )
        if operation == "run" and (not invocation.input_path or not invocation.query_plans_path):
            raise ExecutorFailure(
                "run_inputs_required",
                "InputPath and QueryPlansPath are required for run.",
                40,
            )
        if operation in {"resume", "status", "cancel"} and not invocation.session_id:
            raise ExecutorFailure(
                "session_id_required",
                "SessionId is required for resume, status, and cancel.",
                40,
            )
        collector_command = self._collector_command(invocation.collector_path)
        if operation == "status" or operation == "cancel":
            return self._relay(
                collector_command,
                operation,
                workspace,
                invocation.session_id or "",
            )
        arguments = self._collection_arguments(
            operation,
            workspace,
            input_path=invocation.input_path,
            query_plans_path=invocation.query_plans_path,
            session_id=invocation.session_id,
            request_timeout_seconds=timeout,
            max_rounds=max_rounds,
            max_videos=max_videos,
            browser_channel=browser_channel,
            show_search_browsers=invocation.show_search_browsers,
            progress_format=invocation.progress_format,
        )
        return ExecutorResult(
            self._start(
                operation,
                collector_command,
                arguments,
                workspace,
                invocation.session_id,
                invocation.control_directory,
            )
        )

    def _start(
        self,
        operation: Literal["run", "resume"],
        collector_command: list[str],
        arguments: list[str],
        workspace: Path,
        session_id: str | None,
        control_directory: str | None,
    ) -> dict[str, Any]:
        control_root = (
            Path(control_directory).expanduser().resolve()
            if control_directory
            else workspace / ".material-collector" / "agent-control"
        )
        control_root.mkdir(parents=True, exist_ok=True)
        with _launch_lock(control_root / ".launch.lock"):
            if operation == "resume":
                existing = self._live_execution(control_root, session_id or "")
                if existing is not None:
                    raise ExecutorFailure(
                        "execution_already_running",
                        "A collector executor is already running.",
                        context={"existing": existing},
                    )
                try:
                    status = self._relay(
                        collector_command,
                        "status",
                        workspace,
                        session_id or "",
                    )
                except ExecutorFailure as error:
                    raise ExecutorFailure(
                        "session_status_unavailable",
                        "The session runtime could not be verified before resume.",
                        context={"session_id": session_id},
                    ) from error
                runtime = status.payload.get("runtime")
                if status.exit_code != 0 or not isinstance(runtime, dict):
                    raise ExecutorFailure(
                        "session_status_unavailable",
                        "The session runtime could not be verified before resume.",
                        context={"session_id": session_id},
                    )
                if runtime.get("state") in {"executing", "cancelling"} and not runtime.get(
                    "lease_expired"
                ):
                    raise ExecutorFailure(
                        "execution_already_running",
                        "The session runtime reports a live executor.",
                        context={"existing": {"session_id": session_id, "runtime": runtime}},
                    )

            launch_id = f"launch_{uuid4().hex}"
            stdout_path = control_root / f"{launch_id}.stdout.json"
            stderr_path = control_root / f"{launch_id}.stderr.log"
            control_path = control_root / f"{launch_id}.control.json"
            handshake_path = control_root / f"{launch_id}.child.json"
            payload_path = control_root / f"{launch_id}.payload.json"
            stdout_path.touch()
            stderr_path.touch()
            started_at = _utc_now()
            payload = {
                "collector_command": collector_command,
                "arguments": arguments,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "control_path": str(control_path),
                "handshake_path": str(handshake_path),
            }
            _write_json(payload_path, payload)
            try:
                wrapper = _start_detached_worker(payload_path)
            except OSError as error:
                raise ExecutorFailure(
                    "collector_start_failed",
                    "The material collector process could not be started.",
                    context={
                        "artifacts": {
                            "stdout_path": str(stdout_path),
                            "stderr_path": str(stderr_path),
                            "control_path": str(control_path),
                        }
                    },
                ) from error

            control: dict[str, Any] = {
                "schema_version": "1.0",
                "status": "starting",
                "launch_id": launch_id,
                "operation": operation,
                "process_id": wrapper.pid,
                "process_start_time": started_at,
                "process_start_ticks": _process_start_ticks(wrapper.pid),
                "host_id": socket.gethostname(),
                "started_at": started_at,
                "workspace_path": str(workspace),
                "session_id": session_id if operation == "resume" else None,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "control_path": str(control_path),
                "child_handshake_path": str(handshake_path),
                "collector_process_id": None,
                "collector_process_start_time": None,
                "collector_process_start_ticks": None,
            }
            _write_json(control_path, control)
            handshake = _wait_for_json(handshake_path, timeout_seconds=3.0, process=wrapper)
            if handshake is None or not isinstance(handshake.get("process_id"), int):
                control["status"] = "exited"
                _write_json(control_path, control)
                raise ExecutorFailure(
                    "collector_start_not_confirmed",
                    "The wrapper did not confirm the collector child process.",
                    context={"execution": control},
                )
            control["collector_process_id"] = handshake["process_id"]
            control["collector_process_start_time"] = handshake.get("process_start_time")
            control["collector_process_start_ticks"] = handshake.get("process_start_ticks")
            _write_json(control_path, control)

            time.sleep(0.5)
            if wrapper.poll() is not None or not _process_alive(handshake["process_id"]):
                control["status"] = "exited"
                _write_json(control_path, control)
                raise ExecutorFailure(
                    "collector_exited_during_start",
                    "The material collector exited during startup stabilization.",
                    context={"execution": control},
                )

            resolved_session_id = session_id
            if operation == "run":
                resolved_session_id = _wait_for_session_id(
                    stderr_path,
                    wrapper,
                    timeout_seconds=3.0,
                )
                if resolved_session_id is None:
                    if wrapper.poll() is not None:
                        control["status"] = "exited"
                        _write_json(control_path, control)
                        raise ExecutorFailure(
                            "collector_exited_during_start",
                            "The material collector exited before reporting a session id.",
                            context={"execution": control},
                        )
                    raise ExecutorFailure(
                        "session_id_not_observed",
                        "The collector did not report a session id during startup.",
                        context={"execution": control},
                    )

            control["status"] = "started"
            control["session_id"] = resolved_session_id
            _write_json(control_path, control)
            return control

    def _relay(
        self,
        collector_command: list[str],
        operation: Literal["status", "cancel"],
        workspace: Path,
        session_id: str,
    ) -> ExecutorResult:
        try:
            completed = subprocess.run(
                _command_with_arguments(
                    collector_command,
                    [
                        operation,
                        "--workspace",
                        str(workspace),
                        "--session-id",
                        session_id,
                    ],
                ),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=_command_environment(collector_command),
            )
        except OSError as error:
            raise ExecutorFailure(
                "collector_start_failed",
                "The material collector process could not be invoked.",
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ExecutorFailure(
                "collector_response_invalid",
                "The material collector returned invalid JSON.",
                50,
            ) from error
        if not isinstance(payload, dict):
            raise ExecutorFailure(
                "collector_response_invalid",
                "The material collector returned invalid JSON.",
                50,
            )
        return ExecutorResult(payload, completed.returncode)

    def _live_execution(self, control_root: Path, session_id: str) -> dict[str, Any] | None:
        for path in control_root.glob("*.control.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except OSError, json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("session_id") != session_id:
                continue
            if record.get("host_id") != socket.gethostname():
                continue
            if _recorded_process_alive(
                record,
                process_id_key="process_id",
                start_ticks_key="process_start_ticks",
            ) or _recorded_process_alive(
                record,
                process_id_key="collector_process_id",
                start_ticks_key="collector_process_start_ticks",
            ):
                return record
        return None

    @staticmethod
    def _operation(value: str | None) -> ExecutorOperation:
        if value not in {"run", "resume", "status", "cancel"}:
            raise ExecutorFailure(
                "operation_invalid",
                "Operation must be run, resume, status, or cancel.",
                40,
            )
        return cast(ExecutorOperation, value)

    @staticmethod
    def _integer(
        value: str,
        *,
        code: str,
        message: str,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise ExecutorFailure(code, message, 40) from error
        if parsed < minimum or (maximum is not None and parsed > maximum):
            raise ExecutorFailure(code, message, 40)
        return parsed

    @staticmethod
    def _required_path(value: str | None, *, code: str, message: str) -> Path:
        if not value or not value.strip():
            raise ExecutorFailure(code, message, 40)
        return Path(value).expanduser().resolve()

    @staticmethod
    def _collector_command(value: str | None) -> list[str]:
        if value is None:
            return [sys.executable, "-m", "material_collector.adapters.cli.app"]
        if not value.strip():
            raise ExecutorFailure(
                "collector_path_invalid",
                "CollectorPath must not be empty.",
                40,
            )
        resolved = shutil.which(value)
        candidate = Path(resolved or value).expanduser()
        if not candidate.is_file():
            raise ExecutorFailure(
                "collector_not_found",
                "The material-collector executable could not be resolved.",
            )
        path = str(candidate.resolve())
        suffix = candidate.suffix.lower()
        if suffix == ".py":
            return [sys.executable, path]
        if suffix == ".ps1":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if shell is None:
                raise ExecutorFailure(
                    "collector_not_found",
                    "The material-collector executable could not be resolved.",
                )
            return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", path]
        if suffix in {".cmd", ".bat"}:
            return ["__material_collector_batch__", path]
        return [path]

    @staticmethod
    def _collection_arguments(
        operation: Literal["run", "resume"],
        workspace: Path,
        *,
        input_path: str | None,
        query_plans_path: str | None,
        session_id: str | None,
        request_timeout_seconds: int,
        max_rounds: int,
        max_videos: int,
        browser_channel: str,
        show_search_browsers: bool,
        progress_format: str,
    ) -> list[str]:
        arguments = [operation, "--workspace", str(workspace)]
        if operation == "run":
            arguments.extend(
                [
                    "--input",
                    str(Path(input_path or "").expanduser().resolve()),
                    "--query-plans",
                    str(Path(query_plans_path or "").expanduser().resolve()),
                    "--request-timeout-seconds",
                    str(request_timeout_seconds),
                    "--max-rounds",
                    str(max_rounds),
                    "--max-videos",
                    str(max_videos),
                    "--browser-channel",
                    browser_channel,
                ]
            )
        else:
            arguments.extend(["--session-id", session_id or ""])
        if show_search_browsers:
            arguments.append("--show-search-browsers")
        arguments.extend(["--progress-format", progress_format])
        return arguments


def _start_detached_worker(payload_path: Path) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "material_collector.infrastructure.executor_worker",
        str(payload_path),
    ]
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def _command_with_arguments(command: list[str], arguments: list[str]) -> list[str]:
    if command and command[0] == "__material_collector_batch__":
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            str(Path(__file__).with_name("executor_batch.cmd")),
            *arguments,
        ]
    return [*command, *arguments]


def _command_environment(command: list[str]) -> dict[str, str] | None:
    if command and command[0] == "__material_collector_batch__":
        return {**os.environ, "MATERIAL_COLLECTOR_BATCH_TARGET": command[1]}
    return None


def _wait_for_json(
    path: Path,
    *,
    timeout_seconds: float,
    process: subprocess.Popen[bytes],
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                return value
        if process.poll() is not None:
            return None
        time.sleep(0.025)
    return None


def _wait_for_session_id(
    stderr_path: Path,
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    pattern = re.compile(r'session_id"?\s*[:=]\s*"?(ses_[A-Za-z0-9_-]+)')
    while time.monotonic() < deadline:
        try:
            stderr_text = stderr_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            stderr_text = ""
        match = pattern.search(stderr_text)
        if match:
            return match.group(1)
        if process.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _process_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _process_start_ticks(process_id: int) -> int | str | None:
    if process_id <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FileTime(ctypes.Structure):
            _fields_ = (("low", ctypes.c_ulong), ("high", ctypes.c_ulong))

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return (int(created.high) << 32) | int(created.low)
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{process_id}/stat").read_text(encoding="utf-8")
            closing_parenthesis = stat.rfind(")")
            if closing_parenthesis < 0:
                return None
            fields_after_command = stat[closing_parenthesis + 2 :].split()
            return int(fields_after_command[19])
        except (OSError, ValueError, IndexError):
            return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(process_id)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return None
    value = completed.stdout.strip()
    return value or None


def _recorded_process_alive(
    record: dict[str, Any],
    *,
    process_id_key: str,
    start_ticks_key: str,
) -> bool:
    process_id = record.get(process_id_key)
    expected_start_ticks = record.get(start_ticks_key)
    if not isinstance(process_id, int) or expected_start_ticks is None:
        return False
    return _process_alive(process_id) and _process_start_ticks(process_id) == expected_start_ticks


@contextmanager
def _launch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + 10
        acquired = False
        while time.monotonic() < deadline and not acquired:
            acquired = _try_lock(lock_file)
            if not acquired:
                time.sleep(0.05)
        if not acquired:
            raise ExecutorFailure(
                "launch_lock_timeout",
                "Another launcher is still preparing a collector executor.",
            )
        try:
            yield
        finally:
            _unlock(lock_file)


def _try_lock(lock_file: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            flock: Any = fcntl.flock  # type: ignore[attr-defined]
            flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
    except OSError:
        return False
    return True


def _unlock(lock_file: Any) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    flock: Any = fcntl.flock  # type: ignore[attr-defined]
    flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
