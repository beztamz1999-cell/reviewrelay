from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from reviewrelay.config import ProjectConfig, RepoConfig
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.state import SCHEMA_VERSION, StateStore
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task import begin_task
from reviewrelay.worker import (
    CodexAppServerAdapter, CodexWorkerSettings, WorkerTimeouts, TurnStatus,
    WorkerAuthRequired, WorkerBindingMismatch, WorkerInteractionRequired, WorkerProcessDied,
    WorkerProtocolError, WorkerRequestFailed, WorkerStartFailed, WorkerThreadResumeFailed,
    WorkerTimeout, WorkerTurnAlreadyActive, WorkerTurnFailed, WorkerTurnInterrupted,
    build_app_server_command,
)
from reviewrelay.worker.diagnostics import BoundedTrace, safe_payload
from reviewrelay.worker.transport import decode_message


FIXTURE = Path(__file__).parent / "fixtures" / "fake_app_server.py"


@pytest.fixture
def worker_setup(tmp_path, git_repo):
    root = PortableDataRoot(tmp_path / "portable").create()
    config = ProjectConfig("worker-test", RepoConfig(str(git_repo)))
    begin_task(config, "task", root)
    fake_state = root.safe_path("logs/fake-thread.json")
    timeouts = WorkerTimeouts(startup_seconds=2, initialize_seconds=1, request_seconds=1,
                              idle_seconds=1, overall_seconds=3, shutdown_seconds=.3)

    def make(mode="normal", **overrides):
        settings = CodexWorkerSettings(timeouts=overrides.pop("timeouts", timeouts), **overrides)
        return CodexAppServerAdapter(root, config, "task", settings,
                                     process_command=(sys.executable, str(FIXTURE), str(fake_state), mode))

    return root, config, fake_state, make


def requests(path):
    return [json.loads(line) for line in path.with_suffix(".requests.jsonl").read_text(encoding="utf-8").splitlines()]


def test_start_persist_complete_final_and_normalized_events(worker_setup):
    root, config, fake_state, make = worker_setup

    async def run():
        adapter = make(model="fixture-configured", reasoning_effort="low")
        try:
            turn = await adapter.start_task("explicit instruction")
            result = await adapter.wait_until_done()
            assert turn == result.turn_id
            assert result.status is TurnStatus.COMPLETED
            assert result.thread_id == adapter.get_session_identity() == "fixture-thread"
            assert await adapter.get_final_response() == "fixture final without marker"
            assert (root.safe_path("active/worker-test/task/durable/worker-report.md").read_text()) == result.final_response
            kinds = {event.kind for event in adapter.timeline}
            assert {"WORKER_STARTED", "THREAD_CREATED", "TURN_STARTED", "AGENT_MESSAGE_DELTA",
                    "AGENT_MESSAGE_COMPLETED", "COMMAND_STARTED", "COMMAND_COMPLETED", "FILE_CHANGE",
                    "TOOL_ACTIVITY", "UNKNOWN_EVENT", "TURN_COMPLETED"} <= kinds
            command = next(event for event in adapter.timeline if event.kind == "COMMAND_COMPLETED")
            assert command.command == "echo fixture" and command.exit_code == 0
            assert next(event for event in adapter.timeline if event.kind == "FILE_CHANGE").paths == ("fixture.txt",)
            trace = result.trace_path.read_text(encoding="utf-8")
            assert result.trace_path.is_relative_to(root.path)
            assert "secret-test" not in trace and "hidden-test" not in trace
            assert "[REDACTED]" in trace
            assert all(isinstance(json.loads(line), dict) for line in trace.splitlines())
            assert any("diagnostic is not JSON" in text for text in adapter._transport.stderr)
            assert "fixture-secret" not in "".join(adapter._transport.stderr)
            with StateStore(root) as state:
                record = state.get(config.project_id, "task")
                assert record.worker_thread_id == "fixture-thread"
                assert record.worker_repo_path == str(Path(config.repo.path).resolve())
                assert record.worker_last_turn_id == turn
                assert record.worker_last_turn_status == "COMPLETED"
                assert record.worker_last_event_at
            sent = requests(fake_state)
            assert [r["method"] for r in sent][:4] == ["initialize", "initialized", "thread/start", "turn/start"]
            assert sent[0]["params"]["clientInfo"] == {"name": "reviewrelay", "title": "ReviewRelay", "version": "0.1.0"}
            assert sent[2]["params"]["model"] == "fixture-configured"
            assert sent[3]["params"]["effort"] == "low"
            assert adapter._transport.process.returncode is None
        finally:
            process = adapter._transport.process
            await adapter.close()
        assert process.returncode is not None

    asyncio.run(run())


def test_restart_resume_same_thread_fix_and_prompt_file(worker_setup, tmp_path):
    root, _, fake_state, make = worker_setup
    prompt = tmp_path / "instruction.txt"
    prompt.write_text("initial explicit file", encoding="utf-8")

    async def run():
        first = make()
        await first.start_task(prompt_file=prompt)
        original = await first.wait_until_done()
        first_process = first._transport.process
        await first.close()
        second = make()
        try:
            assert await second.resume_task() == original.thread_id
            assert second._transport.process.pid != first_process.pid
            await second.send_instruction("fix only")
            fixed = await second.wait_until_done()
            assert fixed.thread_id == original.thread_id
            assert fixed.turn_id != original.turn_id
            assert fixed.trace_path != original.trace_path
            methods = [r["method"] for r in requests(fake_state)]
            assert methods.count("thread/start") == 1 and methods.count("thread/resume") == 1
            assert methods.count("turn/start") == 2
            assert [r for r in requests(fake_state) if r["method"] == "turn/start"][-1]["params"]["input"][0]["text"] == "fix only"
        finally:
            await second.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode,error", [
    ("init-invalid", WorkerProtocolError), ("init-error", WorkerRequestFailed),
    ("init-timeout", WorkerTimeout), ("init-dies", WorkerProcessDied),
    ("init-garbage", WorkerProtocolError), ("wrong-id", WorkerProtocolError),
])
def test_initialize_failures_never_emit_initialized_or_start_thread(worker_setup, mode, error):
    _, _, fake_state, make = worker_setup

    async def run():
        adapter = make(mode)
        try:
            with pytest.raises(error):
                await adapter.start_task("instruction")
            assert [r["method"] for r in requests(fake_state)] == ["initialize"]
        finally:
            await adapter.close()

    asyncio.run(run())


def test_startup_missing_executable(worker_setup):
    root, config, _, _ = worker_setup

    async def run():
        adapter = CodexAppServerAdapter(root, config, "task", process_command=(str(root.path / "missing.exe"),))
        try:
            with pytest.raises(WorkerStartFailed):
                await adapter.start_task("instruction")
        finally:
            await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode,error,status", [
    ("failed", WorkerTurnFailed, "FAILED"), ("interrupted", WorkerTurnInterrupted, "INTERRUPTED"),
    ("turn-dies", WorkerProcessDied, "PROCESS_DIED"), ("turn-garbage", WorkerProtocolError, "PROTOCOL_ERROR"),
    ("malformed-event", WorkerProtocolError, "PROTOCOL_ERROR"),
    ("interactive", WorkerInteractionRequired, "PROTOCOL_ERROR"), ("auth", WorkerAuthRequired, "PROTOCOL_ERROR"),
    ("idle", WorkerTimeout, "TIMEOUT"),
])
def test_turn_failure_status_cannot_be_overridden_by_narrative(worker_setup, mode, error, status):
    root, config, _, make = worker_setup

    async def run():
        adapter = make(mode)
        try:
            with pytest.raises(error):
                await adapter.start_task("instruction")
                await adapter.wait_until_done()
            with StateStore(root) as state:
                record = state.get(config.project_id, "task")
                assert record.worker_last_turn_status == status
                assert record.worker_thread_id == "fixture-thread"
            assert not root.safe_path("active/worker-test/task/durable/worker-report.md").exists()
        finally:
            await adapter.close()

    asyncio.run(run())


def test_active_turn_rejected_and_supported_interrupt(worker_setup):
    _, _, fake_state, make = worker_setup

    async def run():
        adapter = make("hold")
        try:
            await adapter.start_task("initial")
            with pytest.raises(WorkerTurnAlreadyActive):
                await adapter.send_instruction("must not start")
            with pytest.raises(WorkerTurnAlreadyActive):
                await adapter.start_task("must not start")
            contender = make()
            try:
                with pytest.raises(WorkerTurnAlreadyActive):
                    await contender.resume_task()
            finally:
                await contender.close()
            await adapter.interrupt()
            with pytest.raises(WorkerTurnInterrupted):
                await adapter.wait_until_done()
            assert sum(r["method"] == "turn/start" for r in requests(fake_state)) == 1
        finally:
            await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["resume-fail", "resume-other-id", "thread-cwd"])
def test_resume_failure_never_creates_replacement_thread(worker_setup, mode):
    _, _, fake_state, make = worker_setup

    async def run():
        initial = make()
        await initial.start_task("initial")
        await initial.wait_until_done()
        await initial.close()
        resumed = make(mode)
        try:
            with pytest.raises(WorkerThreadResumeFailed):
                await resumed.send_instruction("fix")
            assert sum(r["method"] == "thread/start" for r in requests(fake_state)) == 1
            assert sum(r["method"] == "turn/start" for r in requests(fake_state)) == 1
        finally:
            await resumed.close()

    asyncio.run(run())


def test_wrong_repository_fails_before_worker_process(worker_setup, tmp_path):
    root, config, fake_state, _ = worker_setup

    async def run():
        adapter = CodexAppServerAdapter(root, replace(config, repo=RepoConfig(str(tmp_path / "missing"))), "task")
        try:
            with pytest.raises(WorkerBindingMismatch):
                await adapter.start_task("must not run")
            assert adapter._transport is None
            assert not fake_state.exists()
        finally:
            await adapter.close()

    asyncio.run(run())


def test_task_repo_binding_rejects_cwd_drift(worker_setup):
    root, config, _, make = worker_setup

    async def run():
        adapter = make()
        try:
            await adapter.start_task("initial")
            await adapter.wait_until_done()
            with StateStore(root) as state:
                state.save(replace(state.get(config.project_id, "task"), worker_repo_path=str(root.path)))
            with pytest.raises(WorkerBindingMismatch):
                await adapter.send_instruction("must not run")
        finally:
            await adapter.close()

    asyncio.run(run())


def test_sqlite_rejects_thread_bound_to_another_task(worker_setup):
    root, config, _, make = worker_setup

    async def run():
        adapter = make()
        await adapter.start_task("initial")
        await adapter.wait_until_done()
        await adapter.close()

    asyncio.run(run())
    with StateStore(root) as state:
        record = state.get(config.project_id, "task")
        with pytest.raises(sqlite3.IntegrityError):
            state.save(replace(record, task_id="another-task"))


@pytest.mark.parametrize("mode", ["events-before-response", "foreign-events"])
def test_event_stream_before_request_response_is_owned(worker_setup, mode):
    _, _, _, make = worker_setup

    async def run():
        adapter = make(mode)
        try:
            await adapter.start_task("initial")
            assert (await adapter.wait_until_done()).final_response == "fixture final without marker"
        finally:
            await adapter.close()

    asyncio.run(run())


def test_shutdown_is_bounded_even_if_child_ignores_stdin_close(worker_setup):
    _, _, _, make = worker_setup

    async def run():
        adapter = make("hang-close")
        await adapter.start_task("initial")
        process = adapter._transport.process
        await asyncio.wait_for(adapter.close(), 4)
        assert process.returncode is not None
        await adapter.close()

    asyncio.run(run())


def test_v2_migration_preserves_existing_state_and_worker_fields_reload(tmp_path):
    root = PortableDataRoot(tmp_path / "portable").create()
    old = TaskRecord("project", "old-task", task_state=TaskState.WAIT_REVIEW, base_sha="a" * 40,
                     candidate_sha="b" * 40, fix_cycle_count=2, evidence_cycle_count=3, last_review_action="FIX_REQUIRED")
    with StateStore(root) as state:
        state.save(old)
    connection = sqlite3.connect(root.safe_path("db/relay.db"))
    connection.execute("DROP INDEX worker_thread_identity")
    for column in ("worker_thread_id", "worker_repo_path", "worker_last_turn_id", "worker_last_turn_status", "worker_last_event_at"):
        connection.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    with StateStore(root) as state:
        assert state.get("project", "old-task") == old
        assert state._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        worker = replace(old, worker_thread_id="thread", worker_repo_path="repo", worker_last_turn_id="turn",
                         worker_last_turn_status="COMPLETED", worker_last_event_at="timestamp")
        state.save(worker)
    with StateStore(root) as state:
        assert state.get("project", "old-task") == worker


@pytest.mark.parametrize("line", [b"garbage\n", b"[]\n", b'{"id":1,"id":1,"result":{}}\n',
    b'{"id":true,"result":{}}\n', b'{"id":1,"result":{},"error":{}}\n',
    b'{"method":"event","params":42}\n', b'{"result":{}}\n', b'{"id":1,"result":NaN}\n'])
def test_malformed_protocol_envelopes_fail_closed(line):
    with pytest.raises(WorkerProtocolError):
        decode_message(line)


def test_trace_cap_timeline_cap_and_known_auth_redaction(worker_setup):
    _, _, _, make = worker_setup

    async def run():
        adapter = make(max_trace_bytes=500, max_timeline_events=3)
        try:
            await adapter.start_task("instruction")
            result = await adapter.wait_until_done()
            assert result.trace_path.stat().st_size <= 500
            assert adapter._trace.truncated
            assert len(adapter.timeline) == 3
        finally:
            await adapter.close()

    asyncio.run(run())
    assert safe_payload({"authorization": "secret", "refreshToken": "secret", "nested": {"api_key": "secret"}}) == {
        "authorization": "[REDACTED]", "refreshToken": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}}


def test_worker_settings_fixed_argv_and_config_validation():
    configured = CodexWorkerSettings.from_mapping({"adapter": "codex", "reuse_session": True, "model": "owner-choice", "reasoning_effort": "low"})
    assert build_app_server_command(configured) == ("codex", "app-server", "--listen", "stdio://")
    for values in ({"reuse_session": False}, {"adapter": "exec"}, {"sandbox": "danger-full-access"}, {"reasoning_effort": "bad"}):
        with pytest.raises((ValueError, TypeError)):
            CodexWorkerSettings.from_mapping(values)
    with pytest.raises(ValueError):
        WorkerTimeouts(idle_seconds=float("inf"))


def test_raw_trace_symlink_cannot_escape_data_root(worker_setup, tmp_path):
    root, _, _, make = worker_setup
    scratch = root.safe_path("active/worker-test/task/scratch")
    external = tmp_path / "external"
    external.mkdir()
    try:
        (scratch / "worker").symlink_to(external, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(scratch / "worker"), str(external)],
                              capture_output=True, shell=False)
        assert made.returncode == 0, "Windows directory junction fixture must be available"

    async def run():
        from reviewrelay.errors import PathSafetyError
        adapter = make()
        try:
            with pytest.raises(PathSafetyError):
                await adapter.start_task("instruction")
            assert list(external.iterdir()) == []
        finally:
            await adapter.close()

    asyncio.run(run())


def test_real_child_spawn_uses_argv_pipes_and_bound_cwd(worker_setup, monkeypatch):
    _, config, _, make = worker_setup
    original = asyncio.create_subprocess_exec
    observed = []

    async def spawn(*args, **kwargs):
        observed.append((args, kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def run():
        adapter = make()
        try:
            await adapter.start_task("instruction")
            await adapter.wait_until_done()
        finally:
            await adapter.close()

    asyncio.run(run())
    args, options = observed[0]
    assert args[0] == sys.executable and args[1] == str(FIXTURE)
    assert options["cwd"] == str(Path(config.repo.path).resolve())
    assert "shell" not in options
    assert options["stdin"] == options["stdout"] == options["stderr"] == asyncio.subprocess.PIPE


def test_dead_process_turn_is_not_replaced_on_resume(worker_setup):
    _, _, fake_state, make = worker_setup

    async def run():
        adapter = make("turn-dies")
        try:
            with pytest.raises(WorkerProcessDied):
                await adapter.start_task("initial")
                await adapter.wait_until_done()
        finally:
            await adapter.close()
        resumed = make()
        try:
            with pytest.raises(WorkerTurnAlreadyActive):
                await resumed.send_instruction("must not replace unresolved turn")
            assert sum(r["method"] == "thread/start" for r in requests(fake_state)) == 1
            assert sum(r["method"] == "turn/start" for r in requests(fake_state)) == 1
        finally:
            await resumed.close()

    asyncio.run(run())


def test_overall_turn_timeout_is_independent_of_idle_timeout(worker_setup):
    _, _, _, make = worker_setup

    async def run():
        adapter = make("hold", timeouts=WorkerTimeouts(idle_seconds=5, overall_seconds=.1, shutdown_seconds=.3))
        try:
            await adapter.start_task("initial")
            with pytest.raises(WorkerTimeout):
                await adapter.wait_until_done()
            assert adapter._transport.process.returncode is not None
        finally:
            await adapter.close()

    asyncio.run(run())


def test_concurrent_control_attempt_is_rejected_at_call_time(worker_setup):
    _, _, _, make = worker_setup

    async def run():
        adapter = make("slow-start")
        running = asyncio.create_task(adapter.start_task("initial"))
        try:
            await asyncio.sleep(.02)
            with pytest.raises(WorkerTurnAlreadyActive):
                await adapter.send_instruction("must not queue another turn")
            await running
            await adapter.wait_until_done()
        finally:
            await adapter.close()

    asyncio.run(run())


def test_close_during_initialize_stops_child_and_unwinds_control(worker_setup):
    _, _, _, make = worker_setup

    async def run():
        adapter = make("init-timeout")
        running = asyncio.create_task(adapter.start_task("must never reach worker"))
        while adapter._transport is None or adapter._transport.process is None:
            await asyncio.sleep(.01)
        process = adapter._transport.process
        await asyncio.wait_for(adapter.close(), 3)
        with pytest.raises((WorkerProcessDied, WorkerTimeout)):
            await running
        assert process.returncode is not None

    asyncio.run(run())


def test_worker_trace_is_disposable_scratch_and_report_survives_gc(worker_setup):
    root, config, _, make = worker_setup

    async def run():
        adapter = make()
        await adapter.start_task("initial")
        result = await adapter.wait_until_done()
        await adapter.close()
        return result

    result = asyncio.run(run())
    from reviewrelay.gc import GarbageCollector
    from reviewrelay.storage import TaskStorage
    with StateStore(root) as state:
        completed = replace(state.get(config.project_id, "task"), task_state=TaskState.COMPLETE)
        state.save(completed)
        TaskStorage(root).persist_task_record(completed)
    assert GarbageCollector(root).compact_completed_task(config.project_id, "task")
    assert not result.trace_path.exists()
    assert root.safe_path("active/worker-test/task/durable/worker-report.md").read_text() == result.final_response


def test_unacknowledged_thread_creation_cannot_be_replaced(worker_setup):
    root, config, _, make = worker_setup
    with StateStore(root) as state:
        record = state.get(config.project_id, "task")
        state.save(replace(record, worker_repo_path=config.repo.path, worker_last_turn_status="THREAD_STARTING"))

    async def run():
        adapter = make()
        try:
            with pytest.raises(WorkerBindingMismatch):
                await adapter.start_task("must not duplicate external creation")
        finally:
            await adapter.close()

    asyncio.run(run())
