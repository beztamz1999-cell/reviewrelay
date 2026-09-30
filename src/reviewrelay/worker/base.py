"""Small async boundary and observable event/result models for the worker."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class TurnStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    PROCESS_DIED = "PROCESS_DIED"
    TIMEOUT = "TIMEOUT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    timestamp: str
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    text: str | None = None
    command: str | None = None
    exit_code: int | None = None
    paths: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerTurnResult:
    thread_id: str
    turn_id: str
    status: TurnStatus
    final_response: str
    trace_path: Path


@dataclass(frozen=True)
class WorkerTimeouts:
    startup_seconds: float = 15
    initialize_seconds: float = 15
    request_seconds: float = 30
    idle_seconds: float = 180
    overall_seconds: float = 1800
    shutdown_seconds: float = 5

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive duration")


@dataclass(frozen=True)
class CodexWorkerSettings:
    executable: str = "codex"
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox: str = "workspace-write"
    timeouts: WorkerTimeouts = field(default_factory=WorkerTimeouts)
    max_trace_bytes: int = 2 * 1024 * 1024
    max_timeline_events: int = 1000

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or not self.executable.strip() or "\x00" in self.executable:
            raise ValueError("executable must be a non-empty string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("model must be a non-empty string")
        if self.reasoning_effort not in {None, "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("Unsupported reasoning effort")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("Worker sandbox must be read-only or workspace-write")
        for name in ("max_trace_bytes", "max_timeline_events"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "CodexWorkerSettings":
        values = dict(values)
        if values.pop("adapter", "codex") not in {"codex", "codex-app-server"}:
            raise ValueError("Only codex-app-server is supported")
        if values.pop("reuse_session", True) is not True:
            raise ValueError("Worker thread reuse is required")
        if "timeouts" in values:
            values["timeouts"] = WorkerTimeouts(**values["timeouts"])
        return cls(**values)


class WorkerAdapter(Protocol):
    async def start_task(self, prompt: str | None = None, *, prompt_file: Path | None = None) -> str: ...
    async def resume_task(self) -> str: ...
    async def send_instruction(self, prompt: str | None = None, *, prompt_file: Path | None = None) -> str: ...
    async def wait_until_done(self) -> WorkerTurnResult: ...
    async def get_final_response(self) -> str: ...
    def get_session_identity(self) -> str | None: ...
    async def interrupt(self) -> None: ...
    async def close(self) -> None: ...
