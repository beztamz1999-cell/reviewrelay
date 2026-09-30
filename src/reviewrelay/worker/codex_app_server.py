"""Task-bound Codex app-server transport; no orchestration or narrative-based truth."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import __version__
from ..config import ProjectConfig, validate_identifier
from ..git import GitClient
from ..models import utc_now_iso
from ..state import StateStore
from ..storage import PortableDataRoot, TaskStorage
from .base import CodexWorkerSettings, TurnStatus, WorkerEvent, WorkerTurnResult
from .diagnostics import BoundedTrace, safe_payload
from .errors import (WorkerAuthRequired, WorkerBindingMismatch, WorkerError, WorkerInteractionRequired,
                     WorkerProcessDied, WorkerProtocolError, WorkerRequestFailed, WorkerResponseMissing,
                     WorkerThreadResumeFailed, WorkerTimeout, WorkerTurnAlreadyActive, WorkerTurnFailed,
                     WorkerTurnInterrupted)
from .lock import WorkerTaskLock
from .transport import AppServerTransport


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise WorkerProtocolError(f"Missing or invalid {label}")
    return value


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def build_app_server_command(settings: CodexWorkerSettings) -> tuple[str, ...]:
    return (settings.executable, "app-server", "--listen", "stdio://")


class CodexAppServerAdapter:
    def __init__(self, data_root: PortableDataRoot, config: ProjectConfig, task_id: str,
                 settings: CodexWorkerSettings | None = None, *,
                 process_command: tuple[str, ...] | None = None) -> None:
        self.data_root = data_root.create()
        self.config = config
        self.task_id = validate_identifier(task_id, "task_id")
        self.settings = settings or CodexWorkerSettings.from_mapping(config.worker)
        self.storage = TaskStorage(data_root)
        self.state = StateStore(data_root)
        self._command = process_command or build_app_server_command(self.settings)
        self._transport: AppServerTransport | None = None
        self._lock: WorkerTaskLock | None = None
        self._operation_lock = asyncio.Lock()
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._done: asyncio.Future | None = None
        self._result: WorkerTurnResult | None = None
        self._messages: dict[str, dict[str, Any]] = {}
        self._early_events: list[dict[str, Any]] | None = None
        self._trace: BoundedTrace | None = None
        self._started_at = self._last_activity = 0.0
        self._closed = False
        self.timeline: deque[WorkerEvent] = deque(maxlen=self.settings.max_timeline_events)

    def _record(self):
        record = self.state.get(self.config.project_id, self.task_id)
        if record is None or not record.base_sha:
            raise WorkerBindingMismatch("Task must have a persisted Phase 1 baseline before worker execution")
        if not (self.storage.task_root(record.project_id, record.task_id) / "durable").is_dir():
            raise WorkerBindingMismatch("Managed task storage is missing")
        return record

    def _save(self, **fields: Any) -> None:
        record = replace(self._record(), updated_at=utc_now_iso(), **fields)
        self.state.save(record)
        self.storage.persist_task_record(record)

    def _repo(self) -> str:
        try:
            inspection = GitClient().inspect(self.config.repo.path, expected_repository=self.config.repo.path)
        except Exception as exc:
            raise WorkerBindingMismatch("Configured repository is missing or differs from its expected Git root") from exc
        record = self._record()
        if record.worker_repo_path and not _same_path(record.worker_repo_path, inspection.repository_root):
            raise WorkerBindingMismatch("Persisted worker repository does not match this task")
        if self._thread_id and record.worker_thread_id != self._thread_id:
            raise WorkerBindingMismatch("Persisted worker thread changed during this session")
        return inspection.repository_root

    def _emit(self, kind: str, **fields: Any) -> None:
        self.timeline.append(WorkerEvent(kind, utc_now_iso(), self._thread_id, self._turn_id, **fields))

    async def _ensure_ready(self) -> None:
        if self._closed:
            raise WorkerProcessDied("Adapter is closed; create a new adapter and resume the persisted thread")
        if self._transport is not None:
            if self._transport.failure:
                raise self._transport.failure
            return
        repo = self._repo()
        path = self.data_root.assert_managed_path(self.storage.task_root(self.config.project_id, self.task_id) / "durable" / "worker.lock")
        self._lock = WorkerTaskLock(path)
        transport = self._transport = AppServerTransport(self._command, repo, self.settings.timeouts, self._on_event, self._on_failure)
        try:
            await transport.start()
            result = await transport.request("initialize", {"clientInfo": {"name": "reviewrelay", "title": "ReviewRelay", "version": __version__}}, self.settings.timeouts.initialize_seconds)
            if not isinstance(result.get("userAgent"), str) or not result["userAgent"]:
                raise WorkerProtocolError("Invalid initialize success response")
            await transport.send({"method": "initialized", "params": {}})
            self._emit("WORKER_STARTED")
        except BaseException:
            await transport.close()
            self._transport = None
            self._lock.close()
            self._lock = None
            raise

    def get_session_identity(self) -> str | None:
        return self._thread_id or self._record().worker_thread_id

    @staticmethod
    def _prompt(prompt: str | None, prompt_file: Path | None) -> str:
        if (prompt is None) == (prompt_file is None):
            raise ValueError("Supply exactly one explicit prompt text or prompt file")
        text = Path(prompt_file).read_text(encoding="utf-8") if prompt_file is not None else prompt
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Worker prompt must be non-empty and at most 1 MiB")
        return text

    def _check_idle(self) -> None:
        if self._closed:
            raise WorkerProcessDied("Adapter is closed; resume using a new adapter")
        if self._early_events is not None or (self._done is not None and not self._done.done()):
            raise WorkerTurnAlreadyActive("This task already has an active worker turn")

    def _check_control_available(self) -> None:
        self._check_idle()
        if self._operation_lock.locked():
            raise WorkerTurnAlreadyActive("A task start/resume/instruction operation is already active")

    async def start_task(self, prompt: str | None = None, *, prompt_file: Path | None = None) -> str:
        text = self._prompt(prompt, prompt_file)
        self._check_control_available()
        async with self._operation_lock:
            self._check_idle()
            if self._record().worker_thread_id or self._record().worker_session_identity:
                raise WorkerBindingMismatch("Task already has a worker identity; resume it explicitly")
            repo = self._repo()
            await self._ensure_ready()
            assert self._transport
            # Re-read after acquiring task ownership, before the external side effect.
            record = self._record()
            if record.worker_thread_id or record.worker_repo_path or record.worker_last_turn_status:
                raise WorkerBindingMismatch("Task already has worker startup/binding state; it may not create another thread")
            params: dict[str, Any] = {"cwd": repo, "approvalPolicy": "never", "sandbox": self.settings.sandbox, "ephemeral": False}
            if self.settings.model:
                params["model"] = self.settings.model
            self._save(worker_repo_path=repo, worker_last_turn_status="THREAD_STARTING")
            result = await self._transport.request("thread/start", params)
            thread_id = self._validate_thread(result, repo)
            try:
                self._save(worker_thread_id=thread_id, worker_session_identity=thread_id, worker_repo_path=repo,
                           worker_last_turn_status="THREAD_CREATED")
            except sqlite3.IntegrityError as exc:
                raise WorkerBindingMismatch("Codex thread is already bound to another task") from exc
            self._thread_id = thread_id
            self._emit("THREAD_CREATED")
            return await self._start_turn(text)

    def _validate_thread(self, result: dict[str, Any], repo: str, expected_id: str | None = None) -> str:
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise WorkerProtocolError("Missing thread in app-server response")
        identity = _id(thread.get("id"), "thread ID")
        if expected_id and identity != expected_id:
            raise WorkerBindingMismatch("app-server returned a different thread identity")
        if not isinstance(thread.get("cwd"), str) or not _same_path(thread["cwd"], repo):
            raise WorkerBindingMismatch("Codex thread belongs to a different repository")
        if not isinstance(result.get("cwd"), str) or not _same_path(result["cwd"], repo):
            raise WorkerBindingMismatch("app-server thread configuration has a different cwd")
        return identity

    async def resume_task(self) -> str:
        self._check_control_available()
        async with self._operation_lock:
            self._check_idle()
            return await self._resume()

    async def _resume(self) -> str:
        repo = self._repo()
        record = self._record()
        if not record.worker_thread_id or not record.worker_repo_path or record.worker_session_identity != record.worker_thread_id:
            raise WorkerBindingMismatch("Task has no consistent persisted Codex thread binding")
        await self._ensure_ready()
        assert self._transport
        if self._thread_id == record.worker_thread_id:
            return self._thread_id
        try:
            # Do not override cwd: inspect the stored thread's repository before any turn.
            result = await self._transport.request("thread/resume", {"threadId": record.worker_thread_id})
            self._validate_thread(result, repo, record.worker_thread_id)
        except (WorkerRequestFailed, WorkerBindingMismatch) as exc:
            raise WorkerThreadResumeFailed("Could not resume the exact saved Codex thread") from exc
        turns = result["thread"].get("turns")
        if not isinstance(turns, list):
            raise WorkerProtocolError("Malformed resumed thread turns")
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("status") not in {"inProgress", "completed", "failed", "interrupted"}:
                raise WorkerProtocolError("Invalid resumed turn status")
            _id(turn.get("id"), "resumed turn ID")
        if any(turn["status"] == "inProgress" for turn in turns):
            raise WorkerTurnAlreadyActive("Resumed thread has an unresolved active turn")
        if record.worker_last_turn_status in {"STARTING", "IN_PROGRESS"}:
            matching = [turn for turn in turns if turn.get("id") == record.worker_last_turn_id]
            if len(matching) != 1 or matching[0].get("status") not in {"completed", "failed", "interrupted"}:
                raise WorkerTurnAlreadyActive("Persisted turn outcome is unresolved; no replacement turn may start")
            self._save(worker_last_turn_status=matching[0]["status"].upper())
        self._thread_id = record.worker_thread_id
        self._emit("THREAD_RESUMED")
        return self._thread_id

    async def send_instruction(self, prompt: str | None = None, *, prompt_file: Path | None = None) -> str:
        text = self._prompt(prompt, prompt_file)
        self._check_control_available()
        async with self._operation_lock:
            self._check_idle()
            await self._resume()
            return await self._start_turn(text)

    async def _start_turn(self, prompt: str) -> str:
        self._check_idle()
        repo = self._repo()
        assert self._transport and self._thread_id
        if self._trace:
            self._trace.close()
        trace_path = self.data_root.assert_managed_path(self.storage.task_root(self.config.project_id, self.task_id) / "scratch" / "worker" / f"turn-{uuid4().hex}-events.jsonl")
        self._trace = BoundedTrace(trace_path, self.settings.max_trace_bytes)
        self._messages = {}
        self._turn_id = None
        self._result = None
        self._done = asyncio.get_running_loop().create_future()
        self._early_events = []
        self._save(worker_last_turn_id=None, worker_last_turn_status="STARTING", worker_last_event_at=utc_now_iso())
        params: dict[str, Any] = {"threadId": self._thread_id, "cwd": repo, "input": [{"type": "text", "text": prompt}], "approvalPolicy": "never"}
        if self.settings.model:
            params["model"] = self.settings.model
        if self.settings.reasoning_effort:
            params["effort"] = self.settings.reasoning_effort
        try:
            result = await self._transport.request("turn/start", params)
            turn = result.get("turn")
            if not isinstance(turn, dict) or turn.get("status") not in {"inProgress", "completed", "failed", "interrupted"}:
                raise WorkerProtocolError("Invalid turn/start response")
            self._turn_id = _id(turn.get("id"), "turn ID")
            # A fatal stream event may arrive immediately after the ACK but before this coroutine resumes.
            # Preserve that already-recorded failure instead of overwriting it with IN_PROGRESS.
            if self._done.done():
                self._save(worker_last_turn_id=self._turn_id)
            else:
                self._save(worker_last_turn_id=self._turn_id, worker_last_turn_status="IN_PROGRESS")
            self._started_at = self._last_activity = asyncio.get_running_loop().time()
            buffered, self._early_events = self._early_events, None
            for event in buffered:
                await self._consume_event(event)
            return self._turn_id
        except BaseException as exc:
            self._early_events = None
            error = exc if isinstance(exc, WorkerError) else WorkerProtocolError("Turn start did not complete safely")
            self._on_failure(error)
            await self._transport.close()
            raise

    async def _on_event(self, message: dict[str, Any]) -> None:
        method = message["method"]
        if "id" in message:
            assert self._transport
            await self._transport.send({"id": message["id"], "error": {"code": -32601, "message": "ReviewRelay Phase 4 does not support interactive server requests"}})
            if method.startswith("account/"):
                raise WorkerAuthRequired("Codex authentication requires supported manual login")
            raise WorkerInteractionRequired("Codex requested an interaction; the turn must stop for caller handling")
        # Legacy raw events can contain reasoning. Retain only their envelope/type.
        if method.startswith(("codex/event/", "item/reasoning/", "item/rawResponse")):
            message = {"method": method, "params": {"omitted": "non-display reasoning/raw event"}}
        if self._trace:
            self._trace.append(message)
        if self._early_events is not None:
            if len(self._early_events) >= 1000:
                raise WorkerProtocolError("Too many events before turn/start acknowledgement")
            self._early_events.append(message)
        else:
            await self._consume_event(message)

    async def _consume_event(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params", {})
        if method in {"turn/started", "turn/completed", "item/started", "item/completed", "item/agentMessage/delta"}:
            thread_id = _id(params.get("threadId"), "event thread ID")
            if thread_id != self._thread_id:
                return
            turn_id = _id(params.get("turn", {}).get("id") if method.startswith("turn/") else params.get("turnId"), "event turn ID")
            if turn_id != self._turn_id:
                return
        if self._done is None or self._done.done():
            return
        if params.get("threadId") == self._thread_id:
            self._last_activity = asyncio.get_running_loop().time()
            self._save(worker_last_event_at=utc_now_iso())
        raw = safe_payload(message)
        if method == "item/agentMessage/delta":
            item_id = _id(params.get("itemId"), "message item ID")
            if not isinstance(params.get("delta"), str):
                raise WorkerProtocolError("Message delta must be text")
            self._emit("AGENT_MESSAGE_DELTA", item_id=item_id, text=safe_payload(params["delta"]), raw=raw)
        elif method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict):
                raise WorkerProtocolError("Item event has no item")
            identity, item_type = _id(item.get("id"), "item ID"), _id(item.get("type"), "item type")
            completed = method == "item/completed"
            if item_type == "agentMessage" and completed:
                if not isinstance(item.get("text"), str) or len(item["text"].encode("utf-8")) > 2 * 1024 * 1024:
                    raise WorkerProtocolError("Invalid completed agent message")
                self._capture_message(item)
                self._emit("AGENT_MESSAGE_COMPLETED", item_id=identity, text=safe_payload(item["text"]), raw=raw)
            elif item_type == "commandExecution":
                if not isinstance(item.get("command"), str):
                    raise WorkerProtocolError("Command activity must contain a command string")
                exit_code = item.get("exitCode")
                if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
                    raise WorkerProtocolError("Invalid command exit code")
                self._emit("COMMAND_COMPLETED" if completed else "COMMAND_STARTED", item_id=identity,
                           command=safe_payload(item["command"]), exit_code=exit_code, raw=raw)
            elif item_type == "fileChange":
                changes = item.get("changes")
                if not isinstance(changes, list) or any(not isinstance(change, dict) or not isinstance(change.get("path"), str) for change in changes):
                    raise WorkerProtocolError("Invalid file-change paths")
                self._emit("FILE_CHANGE", item_id=identity, paths=tuple(change["path"] for change in changes), raw=raw)
            elif item_type != "reasoning":
                self._emit("TOOL_ACTIVITY", item_id=identity, raw=raw)
        elif method == "turn/started":
            if params["turn"].get("status") != "inProgress":
                raise WorkerProtocolError("Invalid turn/started status")
            self._emit("TURN_STARTED", raw=raw)
        elif method == "turn/completed":
            turn = params["turn"]
            status = turn.get("status")
            if status not in {"completed", "failed", "interrupted"}:
                raise WorkerProtocolError("Invalid turn/completed status")
            items = turn.get("items")
            if not isinstance(items, list):
                raise WorkerProtocolError("Completed turn must contain an item list")
            for item in items:
                if not isinstance(item, dict):
                    raise WorkerProtocolError("Invalid completed turn item")
                if item.get("type") == "agentMessage":
                    if not isinstance(item.get("text"), str):
                        raise WorkerProtocolError("Invalid completed turn agent text")
                    self._capture_message(item)
            self._save(worker_last_turn_status=status.upper(), worker_last_event_at=utc_now_iso())
            if status == "completed":
                messages = list(self._messages.values())
                finals = [item["text"] for item in messages if item.get("phase") == "final_answer"]
                if not finals:
                    finals = [item["text"] for item in messages if item.get("phase") is None][-1:]
                final_text = "\n\n".join(finals)
                if final_text:
                    self.storage.persist_worker_report(self.config.project_id, self.task_id, final_text)
                assert self._trace and self._thread_id and self._turn_id
                self._result = WorkerTurnResult(self._thread_id, self._turn_id, TurnStatus.COMPLETED, final_text, self._trace.path)
                self._emit("TURN_COMPLETED", raw=raw)
                self._done.set_result(self._result)
            else:
                self._emit("TURN_FAILED" if status == "failed" else "TURN_INTERRUPTED", raw=raw)
                error = turn.get("error") or {}
                auth_info = error.get("codexErrorInfo") if isinstance(error, dict) else None
                failure = WorkerAuthRequired("Codex authentication is required") if auth_info == "unauthorized" else (
                    WorkerTurnFailed("Codex turn failed") if status == "failed" else WorkerTurnInterrupted("Codex turn was interrupted"))
                self._done.set_exception(failure)
            self._trace.close()
        else:
            self._emit("TOOL_ACTIVITY" if method.startswith("item/") else "UNKNOWN_EVENT", raw=raw)

    def _capture_message(self, item: dict[str, Any]) -> None:
        identity = _id(item.get("id"), "agent message ID")
        text = item.get("text")
        if not isinstance(text, str):
            raise WorkerProtocolError("Agent response must contain text")
        if item.get("phase") not in {None, "commentary", "final_answer"}:
            raise WorkerProtocolError("Unknown agent message phase")
        total = len(text.encode("utf-8")) + sum(len(message["text"].encode("utf-8")) for key, message in self._messages.items() if key != identity)
        if total > 2 * 1024 * 1024 or (identity not in self._messages and len(self._messages) >= 1024):
            raise WorkerProtocolError("Worker response exceeded its bounded capture limit")
        self._messages[identity] = item

    def _on_failure(self, error: WorkerError) -> None:
        if self._done is not None and not self._done.done():
            status = "PROCESS_DIED" if isinstance(error, WorkerProcessDied) else "TIMEOUT" if isinstance(error, WorkerTimeout) else "PROTOCOL_ERROR"
            self._save(worker_last_turn_status=status, worker_last_event_at=utc_now_iso())
            self._done.set_exception(error)
        if isinstance(error, WorkerProcessDied):
            self._emit("WORKER_PROCESS_EXITED")

    async def wait_until_done(self) -> WorkerTurnResult:
        if self._done is None:
            raise WorkerResponseMissing("No worker turn has started")
        try:
            while not self._done.done():
                now = asyncio.get_running_loop().time()
                remaining = min(self.settings.timeouts.overall_seconds - (now - self._started_at),
                                self.settings.timeouts.idle_seconds - (now - self._last_activity))
                if remaining <= 0:
                    await self._timeout_turn()
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(self._done), min(remaining, 0.1))
                except asyncio.TimeoutError:
                    continue
            return await self._done
        except WorkerError:
            if self._transport and self._transport.failure:
                await self._transport.close()
            raise

    async def _timeout_turn(self) -> None:
        self._on_failure(WorkerTimeout("Worker turn exceeded its idle or overall timeout"))
        if self._transport and self._thread_id and self._turn_id:
            try:
                await self._transport.request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})
            except WorkerError:
                pass
            await self._transport.close()

    async def get_final_response(self) -> str:
        result = await self.wait_until_done()
        if not result.final_response:
            raise WorkerResponseMissing("Completed turn did not contain a final agent-facing response")
        return result.final_response

    async def interrupt(self) -> None:
        if self._done is None or self._done.done():
            return
        if not self._thread_id or not self._turn_id or self._transport is None:
            raise WorkerProtocolError("Cannot interrupt an unacknowledged worker turn")
        await self._transport.request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._operation_lock.locked():
                # Fail any in-flight request and let its owner unwind before closing SQLite.
                if self._transport:
                    await self._transport.close()
                await asyncio.wait_for(self._operation_lock.acquire(), self.settings.timeouts.shutdown_seconds)
                self._operation_lock.release()
            if self._done is not None and not self._done.done():
                try:
                    await asyncio.wait_for(self.interrupt(), self.settings.timeouts.shutdown_seconds)
                    await asyncio.wait_for(asyncio.shield(self._done), self.settings.timeouts.shutdown_seconds)
                except (WorkerError, asyncio.TimeoutError):
                    pass
                if not self._done.done():
                    self._on_failure(WorkerProcessDied("Adapter closed during an active turn"))
            if self._transport:
                await self._transport.close()
                if self._trace and self._transport.stderr:
                    diagnostics = BoundedTrace(self._trace.path.with_suffix(".stderr.jsonl"), 64 * 1024)
                    for text in self._transport.stderr:
                        diagnostics.append({"stream": "stderr", "text": text})
                    diagnostics.close()
                self._emit("WORKER_PROCESS_EXITED")
        finally:
            if self._trace:
                self._trace.close()
            if self._done and self._done.done() and not self._done.cancelled():
                self._done.exception()
            if self._lock:
                self._lock.close()
            self.state.close()
