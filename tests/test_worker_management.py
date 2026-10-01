from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from reviewrelay.controller_store import ControllerError, ControllerState as S, ControllerStore
from reviewrelay.errors import ReviewRelayError
from reviewrelay.state import StateStore
from reviewrelay.worker_management import WorkerIdentity, WorkerStatus, worker_view
from reviewrelay.worker.errors import WorkerTurnAlreadyActive
from reviewrelay.worker.codex_app_server import CodexAppServerAdapter
from reviewrelay.worker.base import CodexWorkerSettings, WorkerTimeouts
from test_controller import h, git, run, Crash


def completed(h, observer=None):
    c, task = h.create(h.make(observer))
    task = run(c.run(task.task_id))
    assert task.state is S.COMPLETE, task.error_code
    identity = c.open_worker(task.task_id).identity
    return c, task, identity


def test_two_tasks_selection_targets_exact_thread_repo_branch_without_new_thread(h):
    c, a, ia = completed(h)
    _, b = h.create(c, task="TASK-2")
    h.actions = ["PASS"]
    b = run(c.run(b.task_id))
    ib = c.open_worker(b.task_id).identity
    assert ia.worker_thread_id != ib.worker_thread_id
    assert {v.identity.task_id for v in c.workers()} == {a.task_id, b.task_id}
    h.manual_no_commit = True
    for task, identity in ((a, ia), (b, ib)):
        git(h.repo, "switch", "-c", "execution-" + task.task_id, task.candidate_sha)
        branch = git(h.repo, "branch", "--show-current")
        assert c.request_worker_pause(identity).status is WorkerStatus.PAUSED_OWNER_STEER
        result = run(c.send_manual_instruction(identity, "Inspect this Task only; do not change files."))
        assert result.state is S.PAUSED_OWNER_STEER and result.steer_state == S.COMPLETE.value
        assert c.open_worker(task.task_id).identity == identity
        assert git(h.repo, "branch", "--show-current") == branch
        with StateStore(h.root) as store:
            record = store.get(h.project.project_id, task.task_id)
        assert record.worker_thread_id == identity.worker_thread_id
        assert record.worker_repo_path == str(h.repo)
        assert h.manuals[-1][0] == task.task_id
        assert f"WORKER_THREAD_ID={identity.worker_thread_id}" in h.manuals[-1][1]
        assert f"TASK_BRANCH={identity.task_branch}" in h.manuals[-1][1]
        assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
        run(c.resume_auto_relay(identity))
        assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE
    assert h.initial == 2 and h.fixes == 0 and h.sends == 2
    assert [t for t, _ in h.manuals] == [a.task_id, b.task_id]
    assert h.thread_ids == [ia.worker_thread_id, ib.worker_thread_id, ia.worker_thread_id, ib.worker_thread_id]
    events = c.store.events(h.project.project_id, a.task_id)
    assertion = next(json.loads(e["payload_json"]) for e in events if e["kind"] == "MANUAL_THREAD_IDENTITY_VERIFIED")
    assert assertion["THREAD_BEFORE_MANUAL_STEER"] == assertion["THREAD_AFTER_MANUAL_STEER"] == ia.worker_thread_id
    c.close()


@pytest.mark.parametrize("field,value", [("project_id", "unrelated"), ("worker_thread_id", "replacement"),
    ("repository", "C:/different-repository"), ("task_branch", "different-branch"), ("task_id", "OTHER-TASK")])
def test_foreign_or_stale_worker_selection_rejected_before_effect(h, field, value):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    bad = replace(identity, **{field: value})
    with pytest.raises(ControllerError):
        run(c.send_manual_instruction(bad, "Never dispatch this instruction"))
    assert h.initial == h.sends == 1 and not h.manuals
    c.close()


@pytest.mark.parametrize("fault", ["thread", "branch", "dirty", "spec"])
def test_manual_result_mismatch_or_invalid_git_fails_closed(h, fault):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    if fault in {"thread", "branch"}:
        h.manual_fault = fault
    else:
        h.worker_fault = fault
    with pytest.raises(Exception):
        run(c.send_manual_instruction(identity, "Perform an authorized local adjustment"))
    saved = c.store.get(h.project.project_id, task.task_id)
    assert saved.manual_pending and not saved.ready_for_owner_review
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
    assert h.initial == h.sends == 1 and len(h.manuals) == 1
    c.close()


def test_manual_candidate_uses_normal_candidate_publish_review_with_old_pass_invalidated(h):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    result = run(c.send_manual_instruction(identity, "Make a harmless committed adjustment"))
    assert result.state is S.PAUSED_OWNER_STEER and result.steer_state == S.PUBLISHING.value
    assert result.candidate_sha != task.candidate_sha and result.review_cycle == 2
    assert not result.ready_for_owner_review and result.last_review_key is None
    assert h.sends == 1
    h.actions = ["PASS"]
    run(c.resume_auto_relay(identity))
    result = run(c.run(task.task_id, resume=True))
    assert result.state is S.COMPLETE and result.ready_for_owner_review
    assert h.sends == 2 and h.initial == 1 and h.fixes == 0 and len(h.manuals) == 1
    assert git(h.remote, "rev-parse", "refs/heads/" + identity.task_branch) == result.candidate_sha
    c.close()


@pytest.mark.parametrize("checkpoint", ["WORKER_TURN_COMPLETED", "PUBLISHER_GITHUB_PUSH_STARTED",
    "REVIEW_SEND_IN_FLIGHT", "REVIEW_RAW_CAPTURED", "LOCAL_EVIDENCE_IN_FLIGHT", "EVIDENCE_SEND_IN_FLIGHT"])
def test_pause_during_effect_waits_for_safe_boundary_and_resume_never_duplicates(h, checkpoint):
    if "EVIDENCE" in checkpoint:
        h.actions = ["NEED_EVIDENCE", "PASS"]
    paused = []
    def observer(event):
        if event == checkpoint and not paused:
            identity = worker_view(c.store, h.project.project_id, "TASK-1").identity
            view = c.request_worker_pause(identity)
            paused.append(view.status)
    c, task = h.create(h.make(observer))
    result = run(c.run(task.task_id))
    assert paused == [WorkerStatus.PAUSE_PENDING]
    assert result.state is S.PAUSED_OWNER_STEER, result.error_code
    before = (h.initial, h.fixes, h.sends, h.batches,
        sum(e == "GITHUB_PUSH_STARTED" for e in h.publisher_events))
    identity = c.open_worker(task.task_id).identity
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
    assert before == (h.initial, h.fixes, h.sends, h.batches,
        sum(e == "GITHUB_PUSH_STARTED" for e in h.publisher_events))
    c.close()
    c = h.make()  # resume from durable state with a new controller instance
    run(c.resume_auto_relay(identity))
    result = run(c.run(task.task_id, resume=True))
    assert result.state is S.COMPLETE, result.error_code
    expected_sends = 2 if "EVIDENCE" in checkpoint else 1
    assert h.sends == expected_sends and h.initial == 1 and h.fixes == 0
    assert sum(e == "GITHUB_PUSH_STARTED" for e in h.publisher_events) == 1
    assert h.batches == (1 if "EVIDENCE" in checkpoint else 0)
    c.close()


def test_pause_during_unresolved_worker_turn_does_not_interrupt_or_compete(h):
    async def scenario():
        c = h.make()
        task = await c.create_task("TASK-1", "Harmless", "Implement one harmless result file.\n")
        h.worker_gate = asyncio.Event()
        running = asyncio.create_task(c.run(task.task_id))
        for _ in range(300):
            if c.store.get(h.project.project_id, task.task_id).worker_thread_id:
                break
            await asyncio.sleep(.02)
        identity = c.open_worker(task.task_id).identity
        assert identity.worker_thread_id
        assert c.request_worker_pause(identity).status is WorkerStatus.PAUSE_PENDING
        with pytest.raises(WorkerTurnAlreadyActive):
            await c.send_manual_instruction(identity, "Do not race the current turn")
        assert not h.manuals and not running.done() and not h.worker_gate.is_set()
        h.worker_gate.set()
        result = await running
        assert result.state is S.PAUSED_OWNER_STEER
        c.close()
    run(scenario())


def test_auto_loop_cannot_start_a_competing_turn_during_manual_turn(h):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    h.manual_no_commit = True
    async def scenario():
        h.worker_gate = asyncio.Event()
        manual = asyncio.create_task(c.send_manual_instruction(identity, "Inspect without changing files"))
        for _ in range(300):
            if h.manuals:
                break
            await asyncio.sleep(.02)
        assert len(h.manuals) == 1 and not manual.done()
        other = h.make()
        try:
            with pytest.raises(WorkerTurnAlreadyActive):
                await other.run(task.task_id, resume=True)
        finally:
            other.close()
        assert h.initial == h.sends == 1 and h.fixes == 0
        assert not h.worker_gate.is_set()
        h.worker_gate.set()
        assert (await manual).state is S.PAUSED_OWNER_STEER
    run(scenario())
    c.close()


def test_project_lock_rejection_preserves_owner_steer_workflow(h):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    before = c.store.get(h.project.project_id, task.task_id)
    project_lock = c._project_lock()
    other = h.make()
    try:
        with pytest.raises(WorkerTurnAlreadyActive):
            run(other.run(task.task_id, resume=True))
        assert c.store.get(h.project.project_id, task.task_id) == before
        assert c.store.control(h.project.project_id, task.task_id) == "STEER_PAUSE"
        assert h.initial == h.sends == 1 and not h.manuals
    finally:
        project_lock.close()
        other.close()
        c.close()


def test_no_candidate_preserves_pending_fix_and_routes_it_only_after_resume(h):
    h.actions = ["FIX_REQUIRED", "PASS"]
    paused = []
    def observer(event):
        if event == "REVIEW_RECEIVED" and not paused:
            paused.append(c.request_worker_pause(c.open_worker("TASK-1").identity))
    c, task = h.create(h.make(observer))
    task = run(c.run(task.task_id))
    assert task.state is S.PAUSED_OWNER_STEER and task.steer_state == S.PROCESSING_REVIEW.value
    identity = c.open_worker(task.task_id).identity
    h.manual_no_commit = True
    saved = run(c.send_manual_instruction(identity, "Inspect only"))
    assert saved.last_review_key == task.last_review_key and saved.candidate_sha == task.candidate_sha
    assert h.fixes == 0 and not saved.ready_for_owner_review
    run(c.resume_auto_relay(identity))
    assert run(c.run(task.task_id)).state is S.COMPLETE
    assert h.initial == 1 and h.fixes == 1 and len(h.manuals) == 1 and h.sends == 2
    c.close()


def test_acknowledged_manual_turn_completed_independently_recovers_read_only(h):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    def observer(event):
        if event == "WORKER_MANUAL_CONFIRMED":
            raise Crash(event)
    c.observer = observer
    with pytest.raises(Crash):
        run(c.send_manual_instruction(identity, "Harmless committed adjustment"))
    # Simulate completion by the already-dispatched external worker, not a new turn.
    (h.repo / "manual-complete.txt").write_text("offline completed candidate\n")
    git(h.repo, "add", "manual-complete.txt")
    git(h.repo, "commit", "-m", "offline completion of dispatched manual turn")
    record = c.store.state.get(h.project.project_id, task.task_id)
    c.store.state.save(replace(record, worker_last_turn_status="COMPLETED"))
    c.close()
    c = h.make()
    run(c.resume_auto_relay(identity))
    h.actions = ["PASS"]
    assert run(c.run(task.task_id)).state is S.COMPLETE
    assert len(h.manuals) == 1 and h.initial == 1 and h.fixes == 0 and h.sends == 2
    c.close()


@pytest.mark.parametrize("operation", ["manual", "resume"])
def test_remote_mutation_during_owner_steer_permanently_invalidates_old_review(h, operation):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    ref = "refs/heads/" + identity.task_branch
    git(h.remote, "update-ref", ref, task.base_sha)
    with pytest.raises(ReviewRelayError):
        if operation == "manual":
            run(c.send_manual_instruction(identity, "Do not send on an invalidated review"))
        else:
            run(c.resume_auto_relay(identity))
    saved = c.store.get(h.project.project_id, task.task_id)
    assert saved.review_invalidated and not saved.ready_for_owner_review
    git(h.remote, "update-ref", ref, task.candidate_sha)
    with pytest.raises(ControllerError):
        run(c.resume_auto_relay(identity))
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
    assert h.initial == h.sends == 1 and not h.manuals
    c.close()


@pytest.mark.parametrize("checkpoint", ["WORKER_MANUAL_IN_FLIGHT", "WORKER_MANUAL_CONFIRMED", "WORKER_MANUAL_COMPLETED", "CANDIDATE_VERIFIED", "MANUAL_STEER_COMPLETED", "AUTO_RELAY_RESUMED"])
def test_manual_crash_recovery_never_repeats_turn_or_creates_thread(h, checkpoint):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    def crash(event):
        if event == checkpoint:
            raise Crash(event)
    c.observer = crash
    if checkpoint == "AUTO_RELAY_RESUMED":
        run(c.send_manual_instruction(identity, "Harmless adjustment"))
        with pytest.raises(Crash):
            run(c.resume_auto_relay(identity))
    else:
        with pytest.raises(Crash):
            run(c.send_manual_instruction(identity, "Harmless adjustment"))
    manual_count = len(h.manuals)
    c.close()
    c = h.make()
    saved = c.store.get(h.project.project_id, task.task_id)
    if checkpoint in {"WORKER_MANUAL_IN_FLIGHT", "WORKER_MANUAL_CONFIRMED"}:
        with pytest.raises(ControllerError):
            run(c.resume_auto_relay(identity))
        assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
    else:
        if saved.state is S.PAUSED_OWNER_STEER:
            run(c.resume_auto_relay(identity))
        h.actions = ["PASS"]
        assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE
    assert len(h.manuals) == manual_count and h.initial == 1 and h.fixes == 0
    c.close()


def test_manual_pause_before_first_thread_does_not_create_replacement(h):
    c, task = h.create()
    identity = c.open_worker(task.task_id).identity
    assert identity.worker_thread_id is None
    assert c.request_worker_pause(identity).status is WorkerStatus.PAUSED_OWNER_STEER
    with pytest.raises(ControllerError):
        run(c.send_manual_instruction(identity, "Do not create a thread"))
    assert h.initial == 0 and not h.manuals
    run(c.resume_auto_relay(identity))
    assert run(c.run(task.task_id)).state is S.COMPLETE
    assert h.initial == 1
    c.close()


@pytest.mark.parametrize("binding", ["repository", "thread", "branch", "github_repository"])
def test_changed_persisted_binding_rejected_before_manual_effect(h, binding):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    if binding in {"repository", "thread"}:
        record = c.store.state.get(h.project.project_id, task.task_id)
        changes = {"worker_repo_path": str(h.repo.parent)} if binding == "repository" else {"worker_thread_id": "another-thread"}
        c.store.state.save(replace(record, **changes))
    else:
        row = c.store.db.execute("SELECT metadata_json FROM github_publications WHERE project_id=? AND task_id=?",
            (h.project.project_id, task.task_id)).fetchone()
        with c.store.db:
            if binding == "branch":
                c.store.db.execute("UPDATE github_publications SET github_task_branch='other' WHERE project_id=? AND task_id=?",
                    (h.project.project_id, task.task_id))
            else:
                meta = json.loads(row[0])
                meta["repository"] = "unrelated/repository"
                c.store.db.execute("UPDATE github_publications SET metadata_json=? WHERE project_id=? AND task_id=?",
                    (json.dumps(meta), h.project.project_id, task.task_id))
    with pytest.raises(ControllerError):
        run(c.send_manual_instruction(identity, "Never send on a changed binding"))
    assert not h.manuals and h.initial == h.sends == 1
    c.close()


def test_unresolved_effect_after_restart_keeps_pause_pending_fail_closed(h):
    c, task, identity = completed(h)
    c.store.put_effect(task, "unknown-effect", "REVIEW_SEND", "AMBIGUOUS", {})
    assert c.request_worker_pause(identity).status is WorkerStatus.PAUSE_PENDING
    with pytest.raises(ControllerError):
        run(c.send_manual_instruction(identity, "Never send"))
    assert h.initial == h.sends == 1 and not h.manuals
    c.close()


def test_generic_pause_resume_cannot_clear_owner_steer_and_stop_still_works(h):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    c.request_pause(task.task_id)
    assert c.store.control(h.project.project_id, task.task_id) == "STEER_PAUSE"
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER_STEER
    c.request_stop(task.task_id)
    assert run(c.run(task.task_id, resume=True)).state is S.STOPPED
    assert h.initial == h.sends == 1 and not h.manuals
    c.close()


def test_pending_steer_survives_generic_resume_from_old_pause(h):
    c, task, identity = completed(h)
    task = c.store.save(replace(task, state=S.PAUSED_USER, resume_state=S.PROCESSING_REVIEW.value), "PAUSED_FIXTURE")
    c.store.put_effect(task, "unresolved", "REVIEW_SEND", "AMBIGUOUS", {})
    assert c.request_worker_pause(identity).status is WorkerStatus.PAUSE_PENDING
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_USER
    assert c.store.control(h.project.project_id, task.task_id) == "STEER_PAUSE"
    assert h.initial == h.sends == 1
    c.close()


@pytest.mark.parametrize("mode", ["normal", "resume-other-id", "thread-cwd"])
def test_controller_manual_steer_uses_real_adapter_public_thread_resume_without_thread_start(h, mode):
    c, task, identity = completed(h)
    c.request_worker_pause(identity)
    fake_state = h.root.safe_path("logs/manual-appserver.json")
    fake_state.write_text(json.dumps({"id": identity.worker_thread_id, "cwd": str(h.repo),
        "turns": [{"id": task.worker_turn_id, "status": "completed", "items": []}]}), encoding="utf-8")
    fixture = Path(__file__).parent / "fixtures" / "fake_app_server.py"
    settings = CodexWorkerSettings(timeouts=WorkerTimeouts(startup_seconds=5, initialize_seconds=5,
        request_seconds=5, idle_seconds=5, overall_seconds=15, shutdown_seconds=.5))
    c.worker_factory = lambda config, t: CodexAppServerAdapter(h.root, config, t.task_id, settings,
        process_command=(sys.executable, str(fixture), str(fake_state), mode))
    if mode == "normal":
        result = run(c.send_manual_instruction(identity, "Inspect only, no files or commits"))
        assert result.state is S.PAUSED_OWNER_STEER and not result.manual_pending
        assert c.open_worker(task.task_id).identity == identity
    else:
        with pytest.raises(Exception):
            run(c.send_manual_instruction(identity, "Never dispatch to a mismatched thread"))
    requests = [json.loads(line) for line in fake_state.with_suffix(".requests.jsonl").read_text().splitlines()]
    methods = [r["method"] for r in requests]
    assert methods.count("thread/resume") == 1 and "thread/start" not in methods
    assert methods.count("turn/start") == (1 if mode == "normal" else 0)
    for request in requests:
        if request["method"] in {"thread/resume", "turn/start"}:
            assert request["params"]["threadId"] == identity.worker_thread_id
    assert h.initial == h.sends == 1 and not h.manuals
    c.close()
