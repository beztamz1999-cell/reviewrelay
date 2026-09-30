"""Correlated newline-delimited JSON over a directly launched app-server process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from typing import Any, Awaitable, Callable

from .base import WorkerTimeouts
from .diagnostics import redact_text
from .errors import (WorkerAuthRequired, WorkerError, WorkerProcessDied, WorkerProtocolError, WorkerRequestFailed,
                     WorkerStartFailed, WorkerTimeout)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def decode_message(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise WorkerProtocolError("app-server stdout is not valid protocol JSON") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("Protocol message must be an object")
    if "method" in value:
        if not isinstance(value["method"], str) or not value["method"] or not isinstance(value.get("params", {}), dict):
            raise WorkerProtocolError("Malformed protocol method envelope")
        if "result" in value or "error" in value:
            raise WorkerProtocolError("Mixed request/response envelope")
    elif "id" not in value or ("result" in value) == ("error" in value):
        raise WorkerProtocolError("Malformed protocol response envelope")
    if "id" in value and (isinstance(value["id"], bool) or not isinstance(value["id"], (int, str))):
        raise WorkerProtocolError("Invalid request identity")
    return value


class AppServerTransport:
    def __init__(self, command: tuple[str, ...], cwd: str, timeouts: WorkerTimeouts,
                 on_event: Callable[[dict[str, Any]], Awaitable[None]],
                 on_failure: Callable[[WorkerError], None]) -> None:
        if not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
            raise ValueError("Process command must contain explicit non-empty arguments")
        self.command, self.cwd, self.timeouts = command, cwd, timeouts
        self.on_event, self.on_failure = on_event, on_failure
        self.process: asyncio.subprocess.Process | None = None
        self.failure: WorkerError | None = None
        self.stderr: deque[str] = deque(maxlen=16)
        self._tasks: list[asyncio.Task] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._control_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._sequence = 0
        self._closing = False

    async def start(self) -> None:
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            self.process = await asyncio.wait_for(asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024, **options,
            ), self.timeouts.startup_seconds)
        except (OSError, asyncio.TimeoutError) as exc:
            raise WorkerStartFailed("Could not launch Codex app-server") from exc
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    async def send(self, message: dict[str, Any]) -> None:
        if self.failure:
            raise self.failure
        async with self._write_lock:
            if self.process is None or self.process.stdin is None or self.process.returncode is not None:
                raise WorkerProcessDied("app-server is unavailable")
            try:
                self.process.stdin.write((json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
                await asyncio.wait_for(self.process.stdin.drain(), self.timeouts.request_seconds)
            except asyncio.TimeoutError as exc:
                error = WorkerTimeout("Protocol write timed out")
                self._fail(error)
                raise error from exc
            except (ConnectionError, OSError) as exc:
                error = WorkerProcessDied("app-server stdin closed")
                self._fail(error)
                raise error from exc

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        async with self._control_lock:
            self._sequence += 1
            identity = self._sequence
            future = asyncio.get_running_loop().create_future()
            self._pending[identity] = future
            try:
                await self.send({"id": identity, "method": method, "params": params})
                result = await asyncio.wait_for(asyncio.shield(future), timeout or self.timeouts.request_seconds)
                if not isinstance(result, dict):
                    raise WorkerProtocolError(f"Invalid {method} response body")
                return result
            except asyncio.TimeoutError as exc:
                error = WorkerTimeout(f"{method} timed out; the request outcome is unknown")
                self._fail(error)
                raise error from exc
            finally:
                self._pending.pop(identity, None)
                if future.done() and not future.cancelled():
                    future.exception()
                elif not future.done():
                    future.cancel()

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                if not line.endswith(b"\n"):
                    raise WorkerProtocolError("Unterminated protocol line")
                message = decode_message(line)
                if "method" in message:
                    await self.on_event(message)
                else:
                    identity = message["id"]
                    future = self._pending.get(identity)
                    if future is None or future.done():
                        raise WorkerProtocolError("Unknown or duplicate response request ID")
                    if "error" in message:
                        error = message["error"]
                        if not isinstance(error, dict) or not isinstance(error.get("code"), int) or not isinstance(error.get("message"), str):
                            raise WorkerProtocolError("Malformed RPC error")
                        failure = WorkerAuthRequired("Supported Codex login is required") if error["code"] == 401 else WorkerRequestFailed(f"app-server request failed (code {error['code']})")
                        future.set_exception(failure)
                    else:
                        future.set_result(message["result"])
            if not self._closing:
                raise WorkerProcessDied("app-server stdout closed unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                self._fail(exc if isinstance(exc, WorkerError) else WorkerProtocolError("Malformed worker event or protocol stream"))

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        buffer = b""
        dropping = False
        while chunk := await self.process.stderr.read(4096):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if dropping or len(line) > 8192:
                    self.stderr.append("[OVERSIZED STDERR LINE OMITTED]")
                else:
                    self.stderr.append(redact_text(line.decode("utf-8", errors="replace")))
                dropping = False
            if len(buffer) > 8192:
                buffer = b""
                dropping = True
        if buffer and not dropping:
            self.stderr.append(redact_text(buffer.decode("utf-8", errors="replace")))

    def _fail(self, error: WorkerError) -> None:
        if self.failure is not None:
            return
        self.failure = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self.on_failure(error)

    async def close(self) -> None:
        self._closing = True
        process = self.process
        if process is not None and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), self.timeouts.shutdown_seconds)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    cleanup = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(cleanup.wait(), self.timeouts.shutdown_seconds)
                    except asyncio.TimeoutError:
                        cleanup.kill()
                        await cleanup.wait()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(process.wait(), self.timeouts.shutdown_seconds)
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(WorkerProcessDied("app-server closed"))
