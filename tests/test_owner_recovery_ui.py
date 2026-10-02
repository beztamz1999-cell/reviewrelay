from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QCheckBox, QInputDialog

from reviewrelay.approval_ui import CommandApprovalDialog
from reviewrelay.controller_store import ControllerState as S, ControllerStore
from reviewrelay.owner_recovery import RecoveryAction as R, recovery_action
from reviewrelay.task_ui import TaskWindow
from test_controller import h, git, run
from test_project_ui import app
from test_task_ui import window, spin


def active_request(h):
    c, task = h.create()
    task = c.store.save(replace(task, state=S.WORKER_RUNNING,
        pending={"worker_kind": "WORKER_INITIAL", "number": 0}), "fixture")
    record = c.store.state.get(task.project_id, task.task_id)
    c.store.state.save(replace(record, worker_thread_id="thread-1", worker_session_identity="thread-1",
        worker_repo_path=str(h.repo), worker_last_turn_id="turn-1", worker_last_turn_status="IN_PROGRESS"))
    key = c._key(task, "WORKER_INITIAL", 0)
    c.store.put_effect(task, key, "WORKER_INITIAL", "IN_FLIGHT", {})
    c.close()
    return dict(project_id=task.project_id, task_id=task.task_id, thread_id="thread-1", turn_id="turn-1",
        item_id="command-1", command="git status --short\n# exact harmless command", cwd=str(h.repo), environment_id=None)


def background_approval(window, request, *, timeout=None):
    result = []
    async def execute():
        approval = window.approvals.approve(request)
        try:
            result.append(await asyncio.wait_for(approval, timeout) if timeout else await approval)
        except asyncio.TimeoutError:
            result.append("timeout")
    assert window.start_job(lambda: asyncio.run(execute()), request["task_id"])
    return result


def test_production_factory_injects_bounded_approval(app, h):
    c, task = h.create()
    c.close()
    w = TaskWindow(h.root, h.project.project_id)
    controller = w.factory()
    adapter = controller.worker_factory(controller._config(task), task)
    assert adapter._command_approval == w.approvals.approve
    controller.close()
    w.close()


@pytest.mark.parametrize("choice,decision", [("accept_once", "accept"), ("decline", "decline"),
    ("cancel", "cancel"), ("close", "cancel"), ("window_close", "cancel")])
def test_real_queued_background_approval_metadata_and_choices(window, h, app, monkeypatch, choice, decision):
    request = active_request(h)
    created = []
    original = CommandApprovalDialog.__init__
    def init(self, *args, **kwargs):
        created.append((threading.get_ident(), QThread.currentThread()))
        original(self, *args, **kwargs)
    monkeypatch.setattr(CommandApprovalDialog, "__init__", init)
    result = background_approval(window, request)
    spin(app, lambda: window.approvals.dialog is not None)
    dialog = window.approvals.dialog
    assert created == [(threading.get_ident(), app.thread())]
    assert dialog.command.toPlainText() == request["command"]
    for label, key in (("Project", "project_id"), ("Task", "task_id"), ("Codex thread", "thread_id"),
            ("Turn", "turn_id"), ("cwd", "cwd")):
        assert f"{label}: {request[key]}" in dialog.metadata.text()
    assert not dialog.findChildren(QCheckBox)
    assert [dialog.accept_once.text(), dialog.decline.text(), dialog.cancel.text()] == ["Accept once", "Decline", "Cancel"]
    if choice == "close":
        dialog.close()
    elif choice == "window_close":
        window.close()
    else:
        getattr(dialog, choice).click()
    spin(app, lambda: not window.busy)
    assert result == [decision] and window.approvals.dialog is None


@pytest.mark.parametrize("field,value", [("project_id", "foreign"), ("task_id", "OTHER"),
    ("thread_id", "foreign"), ("turn_id", "stale"), ("cwd", "C:/foreign"),
    ("environment_id", "remote"), ("command", ""), ("item_id", "")])
def test_foreign_stale_approval_never_displays(window, h, app, monkeypatch, field, value):
    request = active_request(h)
    request[field] = value
    monkeypatch.setattr(CommandApprovalDialog, "__init__", lambda *a, **kw: pytest.fail("Foreign approval displayed"))
    # Bind the job to the real Task, regardless of the claimed request Task.
    result = []
    window.start_job(lambda: result.append(asyncio.run(window.approvals.approve(request))), "TASK-1")
    spin(app, lambda: not window.busy)
    assert result == ["cancel"] and window.approvals.dialog is None


def test_stale_while_dialog_open_cannot_accept(window, h, app):
    request = active_request(h)
    result = background_approval(window, request)
    spin(app, lambda: window.approvals.dialog is not None)
    with ControllerStore(h.root) as store:
        record = store.state.get(request["project_id"], request["task_id"])
        store.state.save(replace(record, worker_last_turn_id="next-turn"))
    window.approvals.dialog.accept_once.click()
    spin(app, lambda: not window.busy)
    assert result == ["cancel"]


def test_overall_timeout_dismisses_pending_dialog(window, h, app):
    result = background_approval(window, active_request(h), timeout=.6)
    spin(app, lambda: window.approvals.dialog is not None)
    spin(app, lambda: not window.busy and window.approvals.dialog is None)
    assert result == ["timeout"]


def test_app_shutdown_cancels_without_grant(window, h, app):
    result = background_approval(window, active_request(h))
    spin(app, lambda: window.approvals.dialog is not None)
    window.approvals.shutdown()
    spin(app, lambda: not window.busy)
    assert result == ["cancel"]


def test_manual_turn_approval_uses_same_bridge_and_bound_effect(window, h, app):
    request = active_request(h)
    with ControllerStore(h.root) as store:
        task = store.get(request["project_id"], request["task_id"])
        key = "manual-effect"
        store.save(replace(task, state=S.PAUSED_OWNER_STEER, worker_thread_id=request["thread_id"],
            manual_pending={"key": key}), "fixture")
        store.put_effect(task, key, "WORKER_MANUAL", "CONFIRMED",
            {"thread_id": request["thread_id"], "turn_id": request["turn_id"]})
    result = background_approval(window, request)
    spin(app, lambda: window.approvals.dialog is not None)
    # A generic accepted dialog result is not an explicit Accept once click.
    window.approvals.dialog.accept()
    spin(app, lambda: not window.busy)
    assert result == ["cancel"]


def dirty_worker(h, fault="dirty"):
    (h.repo / "result.txt").write_text("initial\n")
    git(h.repo, "add", "--", "result.txt")
    git(h.repo, "commit", "-m", "tracked fixture")
    c, task = h.create()
    h.worker_fault = fault
    blocked = run(c.run(task.task_id))
    return c, blocked, c.open_worker(task.task_id).identity


def track_recovery(monkeypatch, method):
    from reviewrelay.controller import TaskController
    original, calls = getattr(TaskController, method), []
    async def tracked(self, identity, *args):
        calls.append(identity)
        return await original(self, identity, *args)
    monkeypatch.setattr(TaskController, method, tracked)
    return calls


@pytest.mark.parametrize("fault", ["dirty", "no_commit"])
def test_continue_worker_ui_uses_exact_identity_and_normal_candidate_path(window, h, app, monkeypatch, fault):
    c, blocked, identity = dirty_worker(h, fault)
    c.close()
    window.refresh()
    assert window.recovery is R.CONTINUE_WORKER and window.recovery_button.isVisible()
    assert window.recovery_button.text() == "Continue Same Worker" and not window.resume_button.isEnabled()
    calls = track_recovery(monkeypatch, "continue_incomplete_worker")
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Finish this Task and commit the tested work", True))
    h.worker_fault = None
    window.recovery_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert calls == [identity] and "State: COMPLETE" in window.summary.text()
    assert h.initial == h.continuations == h.sends == 1
    assert set(h.thread_ids) == {identity.worker_thread_id}
    assert not window.recovery_button.isVisible()


def test_interrupted_ui_reconciles_first_without_dispatch(window, h, app, monkeypatch):
    c, blocked, identity = dirty_worker(h)
    run(c.continue_incomplete_worker(identity, "Finish same Task"))
    h.worker_fault = "failure"
    blocked = run(c.run(blocked.task_id))
    c.close()
    window.refresh()
    assert window.recovery is R.CHECK_WORKER
    calls = track_recovery(monkeypatch, "reconcile_interrupted_continuation")
    window.recovery_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert calls == [identity] and window.recovery is R.CONTINUE_WORKER
    assert h.continuations == 1 and h.initial == 1 and h.sends == 0


def test_unproven_terminal_worker_remains_paused(window, h, app):
    c, blocked, identity = dirty_worker(h)
    run(c.continue_incomplete_worker(identity, "Finish same Task"))
    h.worker_fault = "failure"
    run(c.run(blocked.task_id))
    c.close()
    h.inspected_status = "COMPLETED"
    window.refresh()
    window.recovery_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: PAUSED_ERROR" in window.summary.text()
    assert "WORKER_TURN_AMBIGUOUS" in window.message.text()
    assert h.initial == h.continuations == 1 and h.sends == 0


def review_block(h, visible=False):
    c, task = h.create()
    h.review_fault = "preclick"
    blocked = run(c.run(task.task_id))
    identity = c.open_worker(task.task_id).identity
    if visible:
        h.review_fault = None
        run(c.reconcile_unsent_review(identity))
        h.review_fault = "ui_ambiguous"
        blocked = run(c.run(task.task_id))
    return c, blocked, identity


@pytest.mark.parametrize("visible,action,method", [(False, R.RETRY_REVIEW, "reconcile_unsent_review"),
    (True, R.RECOVER_REVIEW, "reconcile_visible_review")])
def test_review_ui_routes_reconciliation_before_controller_run(window, h, app, monkeypatch, visible, action, method):
    c, blocked, identity = review_block(h, visible)
    c.close()
    h.review_fault = None
    window.refresh()
    assert window.recovery is action and window.recovery_button.text() == action.value
    calls = track_recovery(monkeypatch, method)
    pushes, sends = h.publisher_events.count("GITHUB_PUSH_IN_FLIGHT"), h.sends
    window.recovery_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert calls == [identity] and "State: COMPLETE" in window.summary.text()
    assert h.sends == sends + (0 if visible else 1) and h.initial == 1
    assert h.publisher_events.count("GITHUB_PUSH_IN_FLIGHT") == pushes


@pytest.mark.parametrize("fault", ["wrong_pair", "owner_draft"])
def test_runtime_recovery_proof_failure_stays_paused_and_never_sends(window, h, app, fault):
    c, blocked, identity = review_block(h, visible=fault == "wrong_pair")
    c.close()
    window.refresh()
    sends = h.sends
    h.review_fault = fault
    window.recovery_button.click()
    spin(app, lambda: not window.busy, timeout=120)
    assert "State: PAUSED_ERROR" in window.summary.text() and h.sends == sends
    assert "REVIEWER_CONVERSATION_CHANGED" in window.message.text()


@pytest.mark.parametrize("mutation", ["no_proof", "extra_dispatch", "payload", "foreign", "other_effect"])
def test_unproven_review_has_no_generic_retry(window, h, app, mutation):
    c, blocked, identity = review_block(h, visible=True)
    key = blocked.pending["key"]
    effect = c.store.effect(key)
    if mutation == "no_proof":
        with c.store.db:
            c.store.db.execute("DELETE FROM controller_events WHERE kind='REVIEW_PRE_CLICK_FAILURE_RECONCILED'")
    elif mutation == "extra_dispatch":
        c.store.dispatch(blocked, key, "REVIEW_SEND", "review_messages")
    elif mutation == "payload":
        c.store.put_effect(blocked, key, "REVIEW_SEND", "AMBIGUOUS", {**effect["payload"], "cycle": 99})
    elif mutation == "foreign":
        assert recovery_action(c, replace(identity, project_id="foreign")) is None
        c.store.state.save(replace(c.store.state.get(identity.project_id, identity.task_id), worker_repo_path="C:/foreign"))
    else:
        c.store.put_effect(blocked, "unresolved", "GITHUB_PUSH", "IN_FLIGHT", {})
    c.close()
    window.refresh()
    assert window.recovery is None and not window.recovery_button.isVisible() and not window.resume_button.isEnabled()
    assert "State: PAUSED_ERROR" in window.summary.text()


@pytest.mark.parametrize("state", [S.READY, S.COMPLETE, S.WAITING_REVIEW])
def test_normal_tasks_never_offer_recovery(window, h, app, state):
    c, task = h.create()
    c.store.save(replace(task, state=state), "fixture")
    c.close()
    window.refresh()
    assert window.recovery is None and not window.recovery_button.isVisible()


def test_queued_recovery_rechecks_state_and_captured_identity(window, h, app, monkeypatch):
    c, blocked, identity = dirty_worker(h)
    window.refresh()
    captured = []
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args: ("Finish same Task", True))
    monkeypatch.setattr(window, "start_job", lambda work, task_id: captured.append((work, task_id)))
    window.recovery_button.click()
    assert captured[0][1] == identity.task_id
    c.store.put_effect(blocked, "unresolved", "REVIEW_SEND", "AMBIGUOUS", {})
    c.close()
    with pytest.raises(ValueError, match="no longer available"):
        captured[0][0]()
    assert h.continuations == h.sends == 0
