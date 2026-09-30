"""Bounded subprocess transport for local Git evidence and configured tests."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessEvidence:
    argv: tuple[str, ...]
    exit_code: int | None
    duration: float
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    launch_error: str | None = None


def legacy_test_argv(command: str) -> tuple[str, ...]:
    """Legacy compatibility: double-quote/backslash rules, never shell expansion.

    Single quotes and shell metacharacters are literal. Backslashes are preserved
    except immediately before a double quote (Windows CRT-style argument rules).
    Prefer explicit argv for new configuration; unbalanced quotes are rejected.
    """
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("Invalid legacy test command")
    args: list[str] = []
    part = ""
    quoted = False
    started = False
    i = 0
    while i < len(command):
        char = command[i]
        if char.isspace() and not quoted:
            if started:
                args.append(part)
                part, started = "", False
            i += 1
            continue
        started = True
        if char == "\\":
            j = i
            while j < len(command) and command[j] == "\\":
                j += 1
            count = j - i
            if j < len(command) and command[j] == '"':
                part += "\\" * (count // 2)
                if count % 2:
                    part += '"'
                else:
                    quoted = not quoted
                i = j + 1
            else:
                part += "\\" * count
                i = j
        elif char == '"':
            quoted = not quoted
            i += 1
        else:
            part += char
            i += 1
    if quoted:
        raise ValueError("Unbalanced double quotes in legacy test command")
    if started:
        args.append(part)
    if not args or not args[0]:
        raise ValueError("Missing test executable")
    return tuple(args)


class _WindowsJob:
    """Kill-on-close job: descendants are cleaned even if their parent exits."""

    def __init__(self, pid: int) -> None:
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.kernel = k
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = k.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not k.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                k.CloseHandle(process)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


async def run_bounded(
    argv: tuple[str, ...], cwd: str, *, timeout: float, stdout_cap: int, stderr_cap: int,
    env: dict[str, str] | None = None,
) -> ProcessEvidence:
    """Drain capped pipes concurrently, enforce total process/pipe time, clean tree."""
    started = time.monotonic()
    proc = None
    job = None
    out, err = bytearray(), bytearray()
    capped = [False, False]
    tasks: list[asyncio.Task] = []
    timed_out = False
    launch_error = None

    async def drain(stream, output: bytearray, cap: int, slot: int) -> None:
        while chunk := await stream.read(8192):
            available = max(0, cap - len(output))
            output.extend(chunk[:available])
            if len(chunk) > available:
                capped[slot] = True

    def stop_tree() -> None:
        if job is not None:
            job.close()
        elif proc is not None and os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc is not None and proc.returncode is None:
            proc.kill()

    try:
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
                   else {"start_new_session": True})
        proc = await asyncio.wait_for(asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
        ), timeout)
        if os.name == "nt":
            job = _WindowsJob(proc.pid)
        tasks = [asyncio.create_task(drain(proc.stdout, out, stdout_cap, 0)),
                 asyncio.create_task(drain(proc.stderr, err, stderr_cap, 1)),
                 asyncio.create_task(proc.wait())]
        remaining = max(0.001, timeout - (time.monotonic() - started))
        await asyncio.wait_for(asyncio.gather(*tasks), remaining)
    except TimeoutError:
        timed_out = True
    except (OSError, ValueError) as exc:
        launch_error = str(exc)
    finally:
        stop_tree()
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 5)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return ProcessEvidence(argv, proc.returncode if proc else None,
                           time.monotonic() - started, bytes(out), bytes(err),
                           *capped, timed_out, launch_error)
