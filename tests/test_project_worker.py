"""Canonical Project worker and Owner-only prompt contracts; no live inference."""
from __future__ import annotations

import copy
import asyncio
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer, QThread
from PySide6.QtWidgets import QLabel, QDialog, QDialogButtonBox

from reviewrelay.controller import TaskController
from reviewrelay.controller_store import ControllerState as S
from reviewrelay.errors import StorageError, SchemaVersionError
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.github_publish import task_branch_name
from reviewrelay.projects import ProjectRegistry, ProjectError, local_identity
from reviewrelay.state import StateStore, SCHEMA_VERSION
from reviewrelay.task_ui import TaskWindow, NewTaskDialog
from reviewrelay.worker.discovery import ProjectWorkerService, WorkerProtocolClient, verify_thread
from reviewrelay.worker.codex_app_server import CodexAppServerAdapter
from reviewrelay.worker.errors import WorkerBindingMismatch, WorkerTurnAlreadyActive, WorkerProtocolError
from reviewrelay.worker_ui import WorkerChooser
from test_controller import h, run, git
from test_project_ui import app
from test_task_ui import spin


def thread(repo, identity="owner-thread", **changes):
    return dict(id=identity, cwd=str(repo), source="appServer", ephemeral=False, name="Thiết kế provenance",
        updatedAt=1, status={"type": "idle"}, turns=[], **changes)


class Protocol:
    def __init__(self, threads):
        self.threads = threads
        self.calls = []
        self.closed = 0
        self.started = 0
        self.pages = None

    def factory(self, project):
        owner = self
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                owner.closed += 1
            async def request(self, method, params):
                owner.calls.append((method, copy.deepcopy(params)))
                if method == "thread/list":
                    if owner.pages:
                        return owner.pages.pop(0)
                    return dict(data=list(owner.threads.values()), nextCursor=None)
                if method == "thread/read":
                    return dict(thread=copy.deepcopy(owner.threads[params["threadId"]]))
                if method == "thread/start":
                    owner.started += 1
                    value = thread(project.local_repo_path, "new-owner-thread")
                    owner.threads[value["id"]] = value
                    return dict(thread=value, cwd=project.local_repo_path)
                raise AssertionError(method)
        return Client()


def service(h, values):
    with ProjectRegistry(h.root) as registry:
        project = registry.get(h.project.project_id)
        if project.codex_worker_thread_id == "thread-TASK-1":
            registry.save(replace(project, codex_worker_thread_id=None, codex_worker_repo_path=None), event="PROJECT_WORKER_BOUND")
    protocol = Protocol({t["id"]: t for t in values})
    return ProjectWorkerService(h.root, client_factory=protocol.factory), protocol


def bound(h):
    worker, protocol = service(h, [thread(h.repo)])
    assert run(worker.discover(h.project.project_id)).status == "CONNECTED"
    return worker, protocol


def test_exact_cwd_unique_discovery_reads_before_binding_and_persists(h):
    worker, protocol = bound(h)
    method, params = protocol.calls[0]
    assert method == "thread/list"
    assert params == dict(cwd=str(h.repo.resolve()), archived=False, sortKey="recency_at", sortDirection="desc",
        sourceKinds=["cli", "vscode", "appServer"], limit=100)
    assert protocol.calls[1] == ("thread/read", {"threadId": "owner-thread", "includeTurns": True})
    with ProjectRegistry(h.root) as registry:
        project = registry.get(h.project.project_id)
        assert project.codex_worker_thread_id == "owner-thread"
        assert project.codex_worker_repo_path == str(h.repo)
        assert project.codex_worker_title == "Thiết kế provenance"
        assert project.codex_worker_verified_at and project.codex_worker_source == "appServer"
    assert protocol.closed == 1 and not protocol.started


def test_multiple_candidates_return_choices_then_selected_identity_is_read_again(h, app):
    worker, protocol = service(h, [thread(h.repo, "one"), thread(h.repo, "two")])
    result = run(worker.discover(h.project.project_id))
    assert result.status == "CHOOSE" and len(result.candidates) == 2
    with ProjectRegistry(h.root) as registry:
        project = registry.get(h.project.project_id)
        assert not project.codex_worker_thread_id
    chooser = WorkerChooser(project, result.candidates)
    assert not chooser.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    chooser.choices.setCurrentRow(1)
    assert chooser.selected_id == "two"
    assert "two" not in chooser.choices.item(1).text()
    run(worker.select(project.project_id, chooser.selected_id))
    assert protocol.calls[-1] == ("thread/read", {"threadId": "two", "includeTurns": True})
    chooser.close()


def test_none_never_creates_and_explicit_creation_starts_once_then_reads(h):
    worker, protocol = service(h, [])
    assert run(worker.discover(h.project.project_id)).status == "NOT_FOUND"
    assert not protocol.started
    with ProjectRegistry(h.root) as registry:
        assert not registry.get(h.project.project_id).codex_worker_thread_id
    created = run(worker.create(h.project.project_id))
    assert created.thread_id == "new-owner-thread" and protocol.started == 1
    assert [m for m, _ in protocol.calls][-2:] == ["thread/start", "thread/read"]
    with pytest.raises(ProjectError, match="existing worker"):
        run(worker.create(h.project.project_id))
    assert protocol.started == 1


@pytest.mark.parametrize("mutation", ["repo", "id", "subagent", "review", "compact", "parent", "ephemeral", "missing_persistence", "active", "malformed_turn"])
def test_selected_unsafe_worker_refused_before_binding(h, mutation):
    value = thread(h.repo)
    if mutation == "repo": value["cwd"] = str(h.repo.parent)
    if mutation == "id": value["id"] = "different"
    if mutation == "subagent": value["source"] = {"subAgent": {"thread_spawn": {"parent_thread_id": "parent"}}}
    if mutation in {"review", "compact"}: value["source"] = {"subAgent": mutation}
    if mutation == "parent": value["parentThreadId"] = "parent"
    if mutation == "ephemeral": value["ephemeral"] = True
    if mutation == "missing_persistence": value.pop("ephemeral")
    if mutation == "active": value["turns"] = [{"id": "running", "status": "inProgress"}]
    if mutation == "malformed_turn": value["turns"] = [{"id": "bad", "status": "unknown"}]
    worker, protocol = service(h, [])
    protocol.threads["owner-thread"] = value
    with pytest.raises((WorkerBindingMismatch, WorkerTurnAlreadyActive, WorkerProtocolError)):
        run(worker.select(h.project.project_id, "owner-thread"))
    with ProjectRegistry(h.root) as registry:
        assert not registry.get(h.project.project_id).codex_worker_thread_id
    assert not protocol.started


def test_stored_worker_is_reverified_and_mismatch_never_silently_switches(h):
    worker, protocol = bound(h)
    protocol.calls.clear()
    assert run(worker.discover(h.project.project_id)).status == "CONNECTED"
    assert [m for m, _ in protocol.calls] == ["thread/read"]
    protocol.threads["owner-thread"]["cwd"] = str(h.repo.parent)
    protocol.threads["replacement"] = thread(h.repo, "replacement")
    assert run(worker.discover(h.project.project_id)).status == "STALE"
    with ProjectRegistry(h.root) as registry:
        assert registry.get(h.project.project_id).codex_worker_thread_id == "owner-thread"


def test_discovery_waits_for_all_pages_and_rejects_bad_pagination(h):
    worker, protocol = service(h, [thread(h.repo, "one"), thread(h.repo, "two")])
    protocol.pages = [dict(data=[protocol.threads["one"]], nextCursor="more"),
                      dict(data=[protocol.threads["two"]], nextCursor=None)]
    assert run(worker.discover(h.project.project_id)).status == "CHOOSE"
    assert [p for m, p in protocol.calls if m == "thread/list"][1]["cursor"] == "more"
    protocol.pages = [dict(data=[], nextCursor="same"), dict(data=[], nextCursor="same")]
    with pytest.raises(WorkerProtocolError): run(worker.discover(h.project.project_id))


def test_owner_prompt_only_auto_id_local_title_auto_review_and_next_job_same_thread(h):
    bound(h)
    c = h.make()
    h.actions = ["FIX_REQUIRED", "PASS"]
    first = run(c.submit_request("  Thiết kế   provenance\nOwner detail"))
    assert first.state is S.COMPLETE and first.ready_for_owner_review, first.error_code
    assert first.task_id.startswith("job-") and len(first.task_id) == 36
    assert first.title == "Thiết kế provenance" and first.spec == "  Thiết kế   provenance\nOwner detail"
    assert first.worker_thread_id == "owner-thread" and h.thread_ids == ["owner-thread", "owner-thread"]
    assert first.candidate_sha == git(h.remote, "rev-parse", "refs/heads/" + task_branch_name(first.task_id))
    assert h.sends == 2 and h.fixes == 1 and h.thread_starts == 0
    h.actions = ["PASS"]
    second = run(c.submit_request("x" * 100))
    assert second.state is S.COMPLETE and second.ready_for_owner_review, second.error_code
    assert second.task_id != first.task_id and second.title == "x" * 80
    assert first.worker_thread_id == second.worker_thread_id == "owner-thread"
    assert h.initial == 2 and h.thread_starts == 0 and h.sends == 3
    assert c.store.state.get(h.project.project_id, first.task_id).worker_thread_id == "owner-thread"
    assert c.store.state.get(h.project.project_id, second.task_id).worker_thread_id == "owner-thread"
    # Resume confirmed work does not duplicate transport effects.
    run(c.run(second.task_id, resume=True))
    assert h.initial == 2 and h.thread_starts == 0 and h.sends == 3
    c.close()


@pytest.mark.parametrize("state", [S.READY, S.PAUSED_USER, S.PAUSED_OWNER_STEER, S.PAUSED_ERROR, S.WORKER_RUNNING, S.WAITING_REVIEW])
def test_unfinished_job_blocks_new_job_and_worker_change(h, state):
    worker, protocol = bound(h)
    c, task = h.create()
    c.store.save(replace(task, state=state), "fixture")
    before = git(h.repo, "rev-parse", "HEAD")
    with pytest.raises(StorageError) as error:
        run(c.submit_request("Another request"))
    assert error.value.code == "PROJECT_JOB_ACTIVE"
    with pytest.raises(StorageError): run(worker.discover(h.project.project_id, change=True))
    with pytest.raises(StorageError): run(worker.select(h.project.project_id, "owner-thread"))
    assert git(h.repo, "rev-parse", "HEAD") == before and h.initial == 0 and protocol.started == 0
    c.close()


@pytest.mark.parametrize("status", ["AMBIGUOUS", "RECONCILED_TERMINAL"])
def test_stopped_ambiguous_effect_still_blocks_project(h, status):
    bound(h)
    c, task = h.create()
    c.store.put_effect(task, "unresolved", "WORKER_INITIAL", status, {})
    c.store.save(replace(task, state=S.STOPPED), "fixture")
    with pytest.raises(StorageError): run(c.submit_request("Another request"))
    c.close()


def test_canonical_worker_cannot_belong_to_another_project_even_after_unbind(h):
    worker, protocol = bound(h)
    with ProjectRegistry(h.root) as registry:
        other = registry.create("Other", str(h.repo.parent / "other"), "EXISTING")
        with pytest.raises(ProjectError) as error:
            registry.save(replace(other, codex_worker_thread_id="owner-thread", codex_worker_repo_path=other.local_repo_path), event="PROJECT_WORKER_BOUND")
        assert error.value.code == "PROJECT_WORKER_ALREADY_OWNED"
        registry.state.save(TaskRecord(h.project.project_id, "history", task_state=TaskState.COMPLETE,
            worker_thread_id="owner-thread", worker_session_identity="owner-thread", worker_repo_path=str(h.repo)))
        with pytest.raises(sqlite3.IntegrityError):
            registry.state.save(TaskRecord(other.project_id, "job", worker_thread_id="owner-thread"))
    protocol.threads["new"] = thread(h.repo, "new")
    run(worker.select(h.project.project_id, "new"))
    with ProjectRegistry(h.root) as registry:
        assert registry.state.get(h.project.project_id, "history").worker_thread_id == "owner-thread"
        with pytest.raises(ProjectError):
            registry.save(replace(other, codex_worker_thread_id="owner-thread", codex_worker_repo_path=other.local_repo_path), event="PROJECT_WORKER_BOUND")


def v6_database(h, *, inconsistent=False):
    with StateStore(h.root) as state:
        state.save(TaskRecord(h.project.project_id, "history", task_state=TaskState.COMPLETE, base_sha="a" * 40,
            worker_thread_id="old-thread", worker_session_identity="old-thread", worker_repo_path=str(h.repo)))
    db = sqlite3.connect(h.root.path / "db/relay.db")
    db.execute("DROP TRIGGER worker_owner_insert")
    db.execute("DROP TRIGGER worker_owner_update")
    db.execute("DROP TABLE worker_owners")
    db.execute("CREATE UNIQUE INDEX worker_thread_identity ON tasks(worker_thread_id) WHERE worker_thread_id IS NOT NULL")
    db.execute("PRAGMA user_version=6")
    if inconsistent: db.execute("UPDATE tasks SET worker_repo_path=?", (str(h.repo.parent),))
    db.commit()
    db.close()


def test_v6_migration_preserves_history_and_allows_sequential_same_project(h):
    v6_database(h)
    with StateStore(h.root) as state:
        old = state.get(h.project.project_id, "history")
        assert old.base_sha == "a" * 40 and old.worker_thread_id == "old-thread"
        assert state._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
        state.save(replace(old, task_id="next", task_state=TaskState.COMPLETE))
        assert state.get(h.project.project_id, "history") == old
        assert not state._connection.execute("SELECT 1 FROM sqlite_master WHERE name='worker_thread_identity'").fetchone()


def test_inconsistent_migration_is_atomic_and_preserves_v6(h):
    v6_database(h, inconsistent=True)
    with pytest.raises(SchemaVersionError): StateStore(h.root)
    db = sqlite3.connect((h.root.path / "db/relay.db").as_uri() + "?mode=ro", uri=True)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 6
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='worker_thread_identity'").fetchone()
    db.close()


def test_real_adapter_reads_and_resumes_canonical_then_two_turns_without_thread_start(h, tmp_path):
    bound(h)
    c, task = h.create()
    c.close()
    state_path = tmp_path / "server.json"
    state_path.write_text(json.dumps(thread(h.repo)), encoding="utf-8")
    config = h.project.to_config()
    command = (sys.executable, str(Path(__file__).parent / "fixtures/fake_app_server.py"), str(state_path), "normal")
    async def exercise():
        adapter = CodexAppServerAdapter(h.root, config, task.task_id, process_command=command)
        try:
            assert await adapter.resume_task() == "owner-thread"
            for _ in range(2):
                await adapter.send_instruction("Harmless fixture request")
                assert (await adapter.wait_until_done()).thread_id == "owner-thread"
            changed = json.loads(state_path.read_text())
            changed["cwd"] = str(h.repo.parent)
            state_path.write_text(json.dumps(changed))
            with pytest.raises(WorkerBindingMismatch): await adapter.send_instruction("Must not dispatch")
        finally:
            await adapter.close()
    run(exercise())
    calls = [json.loads(s) for s in state_path.with_suffix(".requests.jsonl").read_text().splitlines()]
    methods = [r.get("method") for r in calls]
    assert methods.count("thread/start") == 0 and methods.count("thread/resume") == 1
    assert methods.count("thread/read") == 4 and methods.count("turn/start") == 2


def test_owner_ui_auto_discovery_prompt_send_and_hidden_technical_identity(h, app):
    worker, protocol = service(h, [thread(h.repo)])
    window = TaskWindow(h.root, h.project.project_id, controller_factory=h.make, worker_service_factory=lambda: worker)
    window.show()
    spin(app, lambda: window.worker_card.connected == h.project.project_id and not window.busy, timeout=30)
    assert "Đã kết nối" in window.worker_card.label.text()
    assert not window.diagnostics.isVisible() and not window.new_button.isVisible()
    assert window.send_button.text() == "Gửi yêu cầu"
    window.prompt.setPlainText("Sửa kết quả an toàn")
    assert window.send_button.isEnabled()
    window.send_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert h.initial == h.sends == 1 and h.thread_starts == 0, window.diagnostic_error.text()
    assert "sẵn sàng để bạn duyệt" in window.owner_summary.text()
    assert "job-" not in window.tasks.item(0).text()
    visible = "\n".join(label.text() for label in window.findChildren(QLabel) if label.isVisible())
    assert "owner-thread" not in visible and "PROJECT_ID=" not in visible and "SHA:" not in visible
    window.diagnostics_button.click()
    assert window.diagnostics.isVisible() and "owner-thread" in window.project_identity.text()
    assert "Candidate SHA:" in window.summary.text() and "TASK_ID=job-" in window.worker_identity.text()
    assert window.timeline.toPlainText()
    window.close()


def test_dialog_normal_flow_has_only_prompt_and_collapsed_options(app):
    dialog = NewTaskDialog()
    dialog.show()
    assert not hasattr(dialog, "task_id") and not hasattr(dialog, "title")
    assert not dialog.fix_limit.isVisible() and not dialog.require_changes.isVisible()
    dialog.spec.setPlainText("Only prompt")
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert dialog.values()["prompt"] == "Only prompt"
    dialog.advanced_button.click()
    assert dialog.fix_limit.isVisible()
    dialog.close()


def test_card_multiple_candidates_uses_gui_chooser_and_never_infers(h, app):
    worker, protocol = service(h, [thread(h.repo, "thread-one"), thread(h.repo, "thread-two")])
    observed = []
    timer = QTimer()
    timer.setInterval(10)
    def choose():
        dialog = app.activeModalWidget()
        if isinstance(dialog, WorkerChooser):
            observed.append(QThread.currentThread() == app.thread())
            assert all("thread-" not in dialog.choices.item(i).text() for i in range(dialog.choices.count()))
            dialog.choices.setCurrentRow(1)
            dialog.accept()
    timer.timeout.connect(choose)
    window = TaskWindow(h.root, h.project.project_id, controller_factory=h.make, worker_service_factory=lambda: worker)
    try:
        timer.start()
        window.show()
        spin(app, lambda: window.worker_card.connected == h.project.project_id and not window.busy, timeout=30)
        with ProjectRegistry(h.root) as registry:
            assert registry.get(h.project.project_id).codex_worker_thread_id == "thread-two"
        assert observed == [True] and not protocol.started and not h.initial
    finally:
        timer.stop()
        window.close()


def test_none_card_create_button_requires_explicit_owner_action(h, app):
    worker, protocol = service(h, [])
    window = TaskWindow(h.root, h.project.project_id, controller_factory=h.make, worker_service_factory=lambda: worker)
    window.show()
    spin(app, lambda: window.worker_card.allow_create and not window.busy, timeout=30)
    assert window.worker_card.create_button.isVisible() and protocol.started == 0
    window.worker_card.create_button.click()
    spin(app, lambda: window.worker_card.connected == h.project.project_id and not window.busy, timeout=30)
    assert protocol.started == 1 and h.initial == 0
    assert not window.worker_card.create_button.isVisible()
    window.close()


def test_supported_stdio_discovery_transport_initializes_lists_reads_and_closes(h, tmp_path):
    service(h, [])  # Start with no persisted binding, as for first discovery.
    state_path = tmp_path / "server.json"
    state_path.write_text(json.dumps(thread(h.repo)), encoding="utf-8")
    command = (sys.executable, str(Path(__file__).parent / "fixtures/fake_app_server.py"), str(state_path), "normal")
    worker = ProjectWorkerService(h.root, client_factory=lambda project: WorkerProtocolClient(project, process_command=command))
    assert run(worker.discover(h.project.project_id)).status == "CONNECTED"
    calls = [json.loads(s) for s in state_path.with_suffix(".requests.jsonl").read_text().splitlines()]
    assert [r.get("method") for r in calls] == ["initialize", "initialized", "thread/list", "thread/read"]


def test_hidden_or_immediately_closed_window_never_starts_discovery(h, app):
    worker, protocol = service(h, [thread(h.repo)])
    window = TaskWindow(h.root, h.project.project_id, controller_factory=h.make, worker_service_factory=lambda: worker)
    app.processEvents()
    assert not protocol.calls
    window.show()
    window.close()
    app.processEvents()
    assert not protocol.calls and not window.busy


def test_pause_during_supported_resume_stops_before_next_worker_dispatch(h):
    c, task = h.create()
    async def exercise():
        h.resume_gate, h.resume_entered = asyncio.Event(), asyncio.Event()
        running = asyncio.create_task(c.run(task.task_id))
        await asyncio.wait_for(h.resume_entered.wait(), 10)
        identity = c.open_worker(task.task_id).identity
        c.request_worker_pause(identity)
        h.resume_gate.set()
        result = await running
        assert result.state is S.PAUSED_OWNER_STEER
        assert result.counters["worker_initial_turns"] == 0 and h.initial == h.sends == 0
        await c.resume_auto_relay(identity)
        result = await c.run(task.task_id)
        assert result.state is S.COMPLETE and h.initial == h.sends == 1 and h.thread_starts == 0
    run(exercise())
    c.close()


def test_v6_multiple_unfinished_jobs_refuses_migration(h):
    v6_database(h)
    path = h.root.path / "db/relay.db"
    with sqlite3.connect(path) as db:
        for task_id in ("one", "two"):
            db.execute("INSERT INTO controller_tasks VALUES(?,?,?,NULL)", (h.project.project_id, task_id, json.dumps({"state": "READY"})))
    with pytest.raises(SchemaVersionError, match="Multiple unfinished"):
        StateStore(h.root)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM controller_tasks").fetchone()[0] == 2
