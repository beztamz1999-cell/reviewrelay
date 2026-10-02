from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QInputDialog

from reviewrelay.controller_store import ControllerState as S, ControllerStore
from reviewrelay.projects import ConnectionStatus as Status, ProjectRegistry
from reviewrelay.task_ui import NewTaskDialog, TaskWindow
from reviewrelay.ui import ProjectHub
from test_controller import h, git, run
from test_project_ui import app


def spin(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        # Release the GIL as QApplication.exec does. QTest.qWait can starve the
        # background subprocess bootstrap and its Windows process-tree lock.
        time.sleep(.01)
        assert time.monotonic() < deadline, "Qt operation did not finish"
    app.processEvents()


@pytest.fixture
def window(app, h):
    window = TaskWindow(h.root, h.project.project_id, controller_factory=h.make)
    window.show()
    app.processEvents()
    yield window
    spin(app, lambda: not window.busy, timeout=120)
    window.close()
    app.processEvents()


def test_new_task_contract_defaults_and_explicit_test_registry(app):
    dialog = NewTaskDialog()
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok.isEnabled()
    dialog.task_id.setText("TASK-UI")
    dialog.title.setText("Harmless task")
    dialog.spec.setPlainText("Create the requested harmless file.")
    assert ok.isEnabled() and "Commit Task Specification" in ok.text()
    values = dialog.values()
    assert values["require_changes"] and not values["allow_spec_change"]
    assert values["max_fix_cycles"] == 3 and values["max_evidence_cycles"] == 5
    dialog.tests.setPlainText('{"unit": ["python", "-m", "pytest", "-q"]}')
    assert dialog.values()["tests"]["unit"] == ("python", "-m", "pytest", "-q")
    dialog.tests.setPlainText("[]")
    with pytest.raises(ValueError):
        dialog.values()
    dialog.close()


def test_hub_task_controls_require_complete_project_readiness(app, h):
    hub = ProjectHub(h.root)
    assert hub.tasks_button.isEnabled()
    with ProjectRegistry(h.root) as registry:
        registry.save(replace(h.project, codex_status=Status.NOT_CONFIGURED))
    hub.refresh_projects()
    assert not hub.tasks_button.isEnabled()
    hub.close()


def test_new_task_ui_commits_spec_without_worker_then_start_runs_complete(window, h, app, monkeypatch):
    monkeypatch.setattr(NewTaskDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(NewTaskDialog, "values", lambda self: dict(task_id="TASK-UI", title="UI task",
        spec="Create the harmless result file.\n"))
    window.new_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert window.task_id == "TASK-UI" and h.initial == h.sends == 0, window.message.text()
    with ControllerStore(h.root) as store:
        task = store.get(h.project.project_id, "TASK-UI")
    assert task.state is S.READY and task.base_sha == git(h.repo, "rev-parse", "HEAD")
    assert git(h.repo, "show", task.base_sha + ":.reviewrelay/tasks/TASK-UI.md") == task.spec.strip()
    assert window.start_button.isEnabled() and not window.owner_button.isEnabled()
    window.start_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: COMPLETE" in window.summary.text()
    assert "READY FOR OWNER REVIEW: YES" in window.summary.text()
    for label in ("Base SHA:", "Candidate SHA:", "Remote SHA:", "Codex:", "GitHub:", "ChatGPT:", "Local batches:"):
        assert label in window.summary.text()
    assert "GITHUB_PUSH_STARTED" in window.timeline.toPlainText()
    assert "TASK_COMPLETED" in window.timeline.toPlainText()
    assert h.initial == h.sends == 1 and h.fixes == 0
    assert not any(b.isEnabled() for b in (window.start_button, window.resume_button, window.stop_button, window.owner_button))


def test_ui_background_job_remains_responsive_and_cannot_dispatch_twice(window, h, app):
    c, task = h.create()
    c.close()
    window.refresh()
    gate, calls, ticks = threading.Event(), [], []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    def work():
        calls.append(1)
        gate.wait(5)
    timer.start()
    try:
        assert window.start_job(work, task.task_id)
        assert not window.start_job(work, task.task_id)
        assert not window.new_button.isEnabled() and not window.start_button.isEnabled()
        assert window.pause_button.isEnabled() and window.stop_button.isEnabled()
        spin(app, lambda: len(ticks) >= 4)
        assert window.busy and calls == [1]
        window.close()
        assert window.isVisible()
        window.pause_button.click()
        with ControllerStore(h.root) as store:
            assert store.control(h.project.project_id, task.task_id) == "PAUSE"
        window.stop_button.click()
        with ControllerStore(h.root) as store:
            assert store.control(h.project.project_id, task.task_id) == "STOP"
    finally:
        gate.set()
        timer.stop()
        spin(app, lambda: not window.busy)


def test_ui_continuation_turn_count_uses_all_durable_worker_counters(window, h, app):
    c, task = h.create()
    c.store.save(replace(task, counters={**task.counters, "worker_initial_turns": 1,
        "worker_fix_turns": 2, "worker_continuation_turns": 4, "worker_manual_turns": 1}), "fixture")
    c.close()
    window.refresh()
    assert "| turns: 8\n" in window.summary.text()


def test_owner_question_visible_and_explicit_input_only_routes_to_reviewer(window, h, app, monkeypatch):
    h.actions = ["OWNER_DECISION_REQUIRED", "PASS"]
    c, task = h.create()
    assert run(c.run(task.task_id)).state is S.PAUSED_OWNER
    c.close()
    window.refresh()
    assert "Owner question" in window.summary.text() and "Visible context" in window.summary.text()
    assert window.owner_button.isEnabled() and not window.start_button.isEnabled()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Keep the approved scope", True))
    window.owner_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: COMPLETE" in window.summary.text()
    assert h.initial == 1 and h.fixes == 0 and h.sends == 2
    assert "Keep the approved scope" in h.notifications[-1]["prompt"]


def test_reopening_task_window_restarts_refresh_and_preserves_spec(window, h, app):
    c, task = h.create()
    c.close()
    window.refresh()
    assert window.spec.toPlainText() == task.spec and window.spec.isReadOnly()
    window.close()
    assert not window.timer.isActive()
    window.show()
    app.processEvents()
    assert window.timer.isActive() and window.task_id == task.task_id


def test_stop_from_owner_pause_is_terminal_without_new_calls(window, h, app):
    h.actions = ["OWNER_DECISION_REQUIRED"]
    c, task = h.create()
    assert run(c.run(task.task_id)).state is S.PAUSED_OWNER
    c.close()
    window.refresh()
    window.stop_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: STOPPED" in window.summary.text()
    assert h.initial == h.sends == 1 and h.fixes == 0


def test_worker_panel_exact_identity_open_pause_manual_and_resume(window, h, app, monkeypatch):
    c, task = h.create()
    assert run(c.run(task.task_id)).state is S.COMPLETE
    identity = c.open_worker(task.task_id).identity
    c.close()
    h.manual_no_commit = True
    window.refresh()
    assert window.tasks.item(0).text() == task.task_id + " — COMPLETE"
    for field, value in (("PROJECT_ID", identity.project_id), ("TASK_ID", identity.task_id),
            ("WORKER_THREAD_ID", identity.worker_thread_id), ("REPOSITORY", identity.repository),
            ("TASK_BRANCH", identity.task_branch)):
        assert f"{field}={value}" in window.worker_identity.text()
    window.open_worker_button.click()
    assert "Opened worker" in window.message.text()
    window.pause_auto_button.click()
    assert "PAUSED_OWNER_STEER" in window.tasks.item(0).text()
    assert window.manual_button.isEnabled() and window.resume_auto_button.isEnabled()
    assert not window.resume_button.isEnabled()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Inspect only this selected Task", True))
    window.manual_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert h.manuals[0][0] == task.task_id and h.initial == h.sends == 1 and h.fixes == 0
    assert "MANUAL_THREAD_IDENTITY_VERIFIED" in window.timeline.toPlainText()
    assert identity.worker_thread_id in window.worker_identity.text()
    window.resume_auto_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: COMPLETE" in window.summary.text()
    assert h.initial == h.sends == 1 and len(h.manuals) == 1


def test_worker_panel_project_scoping_and_selected_task_only(window, h, app, monkeypatch):
    c, a = h.create()
    a = run(c.run(a.task_id))
    _, b = h.create(c, task="TASK-2")
    h.actions = ["PASS"]
    b = run(c.run(b.task_id))
    c.store.save(replace(a, project_id="unrelated", task_id="FOREIGN-TASK"), "FOREIGN_FIXTURE")
    c.close()
    window.refresh()
    assert window.tasks.count() == 2
    h.manual_no_commit = True
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Inspect only", True))
    for row, task in enumerate((a, b)):
        git(h.repo, "switch", "-c", "ui-execution-" + task.task_id, task.candidate_sha)
        window.tasks.setCurrentRow(row)
        window.pause_auto_button.click()
        window.manual_button.click()
        spin(app, lambda: not window.busy, timeout=120)
        assert h.manuals[-1][0] == task.task_id
    assert [t for t, _ in h.manuals] == [a.task_id, b.task_id]
    assert h.initial == h.sends == 2


def test_worker_pending_typed_status_disables_manual_action(window, h, app):
    c, task = h.create()
    task = run(c.run(task.task_id))
    c.store.put_effect(task, "unknown-send", "REVIEW_SEND", "AMBIGUOUS", {})
    c.close()
    window.refresh()
    window.pause_auto_button.click()
    assert "PAUSE_PENDING" in window.tasks.item(0).text()
    assert not window.manual_button.isEnabled() and not window.resume_auto_button.isEnabled()


def test_manual_job_keeps_original_selection_when_owner_selects_other_worker(window, h, app, monkeypatch):
    c, a = h.create()
    a = run(c.run(a.task_id))
    _, b = h.create(c, task="TASK-2")
    h.actions = ["PASS"]
    run(c.run(b.task_id))
    c.close()
    git(h.repo, "switch", "-c", "selected-ui-task", a.candidate_sha)
    window.refresh()
    window.tasks.setCurrentRow(0)
    window.pause_auto_button.click()
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Keep captured selection", True))
    captured = []
    monkeypatch.setattr(window, "start_job", lambda work, task_id: captured.append((work, task_id)))
    window.manual_button.click()
    window.tasks.setCurrentRow(1)
    assert window.task_id == b.task_id and captured[0][1] == a.task_id
    h.manual_no_commit = True
    captured[0][0]()
    assert h.manuals[-1][0] == a.task_id
    assert "WORKER_THREAD_ID=thread-" + a.task_id in h.manuals[-1][1]
