from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from reviewrelay.controller import TaskController
from reviewrelay.controller_store import ControllerError, ControllerState as S, ControllerStore
from reviewrelay.evidence_process import run_bounded
from reviewrelay.evidence_executor import LocalEvidenceExecutor
from reviewrelay.github_publish import GitHubCandidatePublisher, task_branch_name, task_spec_path
from reviewrelay.models import utc_now_iso
from reviewrelay.projects import ConnectionStatus as Status, ProjectRegistry
from reviewrelay.reviewer.base import AssistantResponse, SendResult, TurnBaseline
from reviewrelay.reviewer.errors import MessageSendFailed, MessageSendAmbiguous, ReviewerConversationChanged
from reviewrelay.state import StateStore
from reviewrelay.storage import PortableDataRoot
from reviewrelay.worker.base import TurnStatus, WorkerEvent, WorkerTurnResult
from reviewrelay.worker.lock import WorkerTaskLock


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def run(awaitable):
    return asyncio.run(awaitable)


class Crash(BaseException):
    pass


class Harness:
    def __init__(self, root, repo, remote, project):
        self.root, self.repo, self.remote, self.project = root, repo, remote, project
        self.actions = ["PASS"]
        self.initial = self.fixes = self.sends = self.batches = 0
        self.continuations = 0
        self.inspected_status = "INTERRUPTED"
        self.manuals = []
        self.manual_no_commit = False
        self.manual_fault = None
        self.worker_fault = None
        self.fix_fault = None
        self.fail_send_number = None
        self.review_fault = None
        self.publish_fault = None
        self.evidence_fault = None
        self.pause_after_completion = False
        self.worker_gate = None
        self.workers = []
        self.notifications = []
        self.thread_ids = []
        self.publisher_events = []
        self.pr_creates = 0
        self.controller = None

    def make(self, observer=None):
        harness = self
        class Worker:
            timeline = ()
            def __init__(self, config, task):
                self.task, self.config, self.thread, self.turn = task, config, None, None
                self.manual = False
                harness.workers.append(self)

            async def start_task(self, prompt):
                harness.initial += 1
                assert "do not push GitHub" in prompt and "implementation/audit reports" in prompt
                assert task_spec_path(self.task.task_id) in prompt
                self.thread = "thread-" + self.task.task_id
                return await self.start(prompt)

            async def send_instruction(self, prompt):
                self.manual = prompt.startswith("ReviewRelay Owner manual instruction")
                if self.manual:
                    harness.manuals.append((self.task.task_id, prompt))
                elif "after a proven completed turn" in prompt:
                    harness.continuations += 1
                else:
                    harness.fixes += 1
                    assert "reviewer instruction" in prompt and "validated fix" in prompt
                with StateStore(harness.root) as state:
                    self.thread = state.get(self.task.project_id, self.task.task_id).worker_thread_id
                return await self.start(prompt)

            async def start(self, prompt):
                self.turn = f"turn-{harness.initial}-{harness.fixes}-{len(harness.manuals)}-{harness.continuations}"
                with StateStore(harness.root) as state:
                    r = state.get(self.task.project_id, self.task.task_id)
                    state.save(replace(r, worker_thread_id=self.thread, worker_session_identity=self.thread,
                        worker_repo_path=str(harness.repo), worker_last_turn_id=self.turn, worker_last_turn_status="IN_PROGRESS"))
                harness.thread_ids.append(self.thread)
                return self.turn

            async def wait_until_done(self):
                if harness.fixes and harness.fix_fault:
                    harness.worker_fault = harness.fix_fault
                if harness.worker_gate:
                    await harness.worker_gate.wait()
                if harness.worker_fault == "failure":
                    raise ControllerError("Fake worker failed", code="WORKER_TURN_FAILED")
                if self.manual and harness.manual_fault == "branch":
                    git(harness.repo, "switch", "-c", "unexpected-branch")
                if harness.worker_fault != "no_commit" and not (self.manual and harness.manual_no_commit):
                    path = harness.repo / (task_spec_path(self.task.task_id) if harness.worker_fault == "spec" else "result.txt")
                    path.write_text("worker requirement rewrite" if harness.worker_fault == "spec" else f"candidate-{harness.initial}-{harness.fixes}-{len(harness.manuals)}\n", encoding="utf-8")
                    if harness.worker_fault != "dirty":
                        git(harness.repo, "add", "--", str(path.relative_to(harness.repo)))
                        git(harness.repo, "commit", "-m", "fake worker candidate")
                with StateStore(harness.root) as state:
                    r = state.get(self.task.project_id, self.task.task_id)
                    state.save(replace(r, worker_last_turn_status="COMPLETED"))
                if harness.pause_after_completion:
                    harness.controller.request_pause(self.task.task_id)
                    harness.pause_after_completion = False
                self.timeline = (WorkerEvent("COMMAND_COMPLETED", utc_now_iso(), self.thread, self.turn,
                    command="harmless offline verification", exit_code=0),
                    WorkerEvent("reasoning", utc_now_iso(), self.thread, self.turn, text="Never expose reasoning"))
                return WorkerTurnResult("wrong-thread" if harness.worker_fault == "thread" or (self.manual and harness.manual_fault == "thread") else self.thread,
                    self.turn, TurnStatus.COMPLETED, "Untrusted narrative SHA=" + "a" * 40, Path("unused-trace"))

            def get_session_identity(self):
                return self.thread

            async def resume_task(self):
                with StateStore(harness.root) as state:
                    self.thread = state.get(self.task.project_id, self.task.task_id).worker_thread_id
                return self.thread

            async def inspect_last_turn(self):
                with StateStore(harness.root) as state:
                    record = state.get(self.task.project_id, self.task.task_id)
                    state.save(replace(record, worker_last_turn_status=harness.inspected_status))
                return harness.inspected_status

            async def interrupt(self):
                if harness.worker_gate:
                    harness.worker_gate.set()

            async def close(self):
                pass

        class Reviewer:
            async def reconcile_visible_review(self, *, prompt, review_key, conversation_url, dispatched_at):
                if harness.review_fault == "wrong_pair":
                    raise ReviewerConversationChanged("Visible pairing differs")
                return SendResult(review_key, conversation_url, dispatched_at, "review", (),
                    hashlib.sha256(prompt.encode()).hexdigest(), TurnBaseline((), ("old-assistant",), 0), "user-reconciled")

            async def discard_unsent_prompt(self, *, prompt, conversation_url):
                if harness.review_fault == "owner_draft":
                    raise ReviewerConversationChanged("Owner draft must remain intact")
                return {"conversation_url": conversation_url, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "baseline": {"user_turn_ids": [], "assistant_turn_ids": [], "navigation_generation": 0}, "draft_cleared": True}

            async def send_review_pack(self, **kwargs):
                return await self.send(**kwargs)

            async def send_evidence(self, **kwargs):
                before = harness.initial + harness.fixes
                assert "LOCAL_VERIFICATION_RESULTS" in kwargs["prompt"]
                result = await self.send(**kwargs)
                assert harness.initial + harness.fixes == before
                return result

            async def send(self, **kwargs):
                assert kwargs["attachment_paths"] == ()
                harness.sends += 1
                harness.notifications.append(kwargs)
                if harness.review_fault == "preclick":
                    raise MessageSendFailed("Composer content did not match before send")
                if harness.review_fault == "ui_ambiguous":
                    pending = SendResult(kwargs["review_key"], kwargs["conversation_url"], utc_now_iso(), "review", (),
                        hashlib.sha256(kwargs["prompt"].encode()).hexdigest(), TurnBaseline((), ("old-assistant",), 0), "pending")
                    raise MessageSendAmbiguous("Unknown click confirmation", send_result=pending)
                if harness.review_fault == "ambiguous" or harness.sends == harness.fail_send_number:
                    raise ControllerError("Unknown send result", code="SEND_AMBIGUOUS")
                return SendResult(kwargs["review_key"], kwargs["conversation_url"], utc_now_iso(), "review", (),
                    hashlib.sha256(kwargs["prompt"].encode()).hexdigest(), TurnBaseline((), ("old-assistant",), 0), f"user-{harness.sends}")

            async def wait_response(self, sent):
                await asyncio.sleep(0)
                if harness.review_fault == "timeout":
                    raise ControllerError("Fake response timeout", code="REVIEWER_TIMEOUT")
                if harness.review_fault == "local_mutation":
                    (harness.repo / "outside.txt").write_text("mutation")
                if harness.review_fault == "remote_mutation":
                    git(harness.remote, "update-ref", "refs/heads/" + task_branch_name("TASK-1"), harness.base)
                prompt = harness.notifications[-1]["prompt"]
                head = re.search(r"HEAD_SHA=([0-9a-f]{40})", prompt)[1]
                cycle = int(re.search(r"REVIEW_CYCLE=(\d+)", prompt)[1])
                action = harness.actions.pop(0)
                data = dict(protocol="rr.v1", candidate_sha="f" * 40 if harness.review_fault == "stale" else head,
                    cycle=cycle, action=action)
                if action == "PASS":
                    data["findings"] = []
                elif action == "FIX_REQUIRED":
                    data.update(findings=[], worker_instruction="validated fix")
                elif action == "NEED_EVIDENCE":
                    data["evidence_requests"] = [{"kind": "git_status"}]
                    if harness.evidence_fault == "unsafe":
                        data["evidence_requests"] = [{"kind": "read_file", "path": "../escape"}]
                    if harness.evidence_fault == "test":
                        data["evidence_requests"] = [{"kind": "test", "test_id": "check"}]
                else:
                    data.update(reason="Owner question" if action == "OWNER_DECISION_REQUIRED" else "GITHUB_REVIEW_ACCESS_REQUIRED", context="Visible context")
                raw = "bad control" if harness.review_fault == "malformed" else "<RELAY_CONTROL>" + json.dumps(data) + "</RELAY_CONTROL>"
                return AssistantResponse(sent.review_key, sent.conversation_url, raw, sent.sent_at, utc_now_iso(),
                    "old-assistant" if harness.review_fault == "ownership" else f"assistant-{harness.sends}", True)

            async def close(self):
                pass

        class Evidence(LocalEvidenceExecutor):
            async def execute_batch(self, requests, context):
                harness.batches += 1
                before = harness.initial + harness.fixes
                batch = await super().execute_batch(requests, context)
                assert before == harness.initial + harness.fixes
                return replace(batch, complete=False) if harness.evidence_fault == "incomplete" else batch

        class Publisher(GitHubCandidatePublisher):
            async def publish(self, request):
                if harness.publish_fault:
                    raise ControllerError("Fake publish condition", code=harness.publish_fault)
                return await super().publish(request)

        class PR:
            async def find(self, repository, branch, base):
                if not harness.pr_creates:
                    return None
                return dict(number=11, url=f"https://github.com/{repository}/pull/11", headRefName=branch,
                    baseRefName=base, state="OPEN", headRefOid=git(harness.remote, "rev-parse", "refs/heads/" + branch), isCrossRepository=False)

            async def create(self, repository, branch, base, task_id):
                harness.pr_creates += 1

        async def network(argv, cwd, **kwargs):
            if any(operation in argv for operation in ("ls-remote", "push", "fetch")):
                argv = tuple(str(harness.remote) if a == "git@github.com:owner/project.git" else a for a in argv)
            return await run_bounded(argv, cwd, **kwargs)

        def published(event):
            harness.publisher_events.append(event)
            if harness.controller and harness.controller.observer:
                harness.controller.observer("PUBLISHER_" + event)
        controller = TaskController(self.root, self.project.project_id, worker_factory=Worker, reviewer_factory=lambda c: Reviewer(),
            publisher_factory=lambda c: Publisher(self.root, c, allow_local_remote=c.github.mode == "branch", runner=network,
                pr_service=PR() if c.github.mode == "pr" else None, checkpoint_observer=published),
            evidence_factory=Evidence, checkpoint_observer=observer)
        self.controller = controller
        return controller

    def create(self, controller=None, task="TASK-1", **kwargs):
        controller = controller or self.make()
        result = run(controller.create_task(task, "Harmless task", "Implement one harmless result file.\n", **kwargs))
        self.base = result.base_sha
        return controller, result


@pytest.fixture
def h(tmp_path, git_repo):
    root = PortableDataRoot(tmp_path / "data").create()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    git(git_repo, "push", "origin", "HEAD:refs/heads/main")
    with ProjectRegistry(root) as registry:
        project = registry.create("Project", str(git_repo), "EXISTING")
        project = registry.save(replace(project, local_status=Status.READY, github_status=Status.READY,
            github_repo_url="https://github.com/owner/project", github_git_url=str(remote), github_owner="owner", github_repo_name="project",
            github_default_branch="main", github_last_verified_at="offline-fixture", github_visibility="PRIVATE",
            chatgpt_conversation_url="https://chatgpt.com/c/offline-controller", chatgpt_status=Status.READY,
            worker_settings={"executable": "fixture.exe"}, codex_status=Status.READY))
    return Harness(root, git_repo, remote, project)


@pytest.mark.parametrize("actions,turns,fixes,reviews,evidence", [(["PASS"], 1, 0, 1, 0),
    (["NEED_EVIDENCE", "PASS"], 1, 0, 1, 1), (["FIX_REQUIRED", "PASS"], 1, 1, 2, 0),
    (["NEED_EVIDENCE", "FIX_REQUIRED", "PASS"], 1, 1, 2, 1)])
def test_autonomous_offline_routes_exact_git_candidates_same_thread_branch_no_attachments(h, actions, turns, fixes, reviews, evidence):
    h.actions = actions
    c, task = h.create()
    assert task.state is S.READY and task.base_sha == git(h.repo, "rev-parse", "HEAD")
    assert git(h.repo, "show", task.base_sha + ":" + task_spec_path(task.task_id)).strip() == task.spec.strip()
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE, result.error_code
    assert result.ready_for_owner_review
    assert result.counters == dict(worker_initial_turns=turns, worker_fix_turns=fixes, review_messages=reviews,
        evidence_messages=evidence, local_evidence_batches=evidence)
    assert h.initial == turns and h.fixes == fixes and h.batches == evidence
    assert len(set(h.thread_ids)) == 1
    assert result.review_cycle == fixes + 1
    assert git(h.remote, "rev-parse", "refs/heads/" + task_branch_name(task.task_id)) == result.candidate_sha == git(h.repo, "rev-parse", "HEAD")
    assert git(h.repo, "status", "--porcelain") == ""
    assert all(n["attachment_paths"] == () for n in h.notifications)
    assert all("PROJECT_ID=" in n["prompt"] and "REPO_URL=https://github.com/owner/project" in n["prompt"]
        for n in h.notifications if n["prompt"].startswith("REVIEWRELAY_REVIEW_REQUEST"))
    assert "TASK_COMPLETED" in [e["kind"] for e in c.store.events(h.project.project_id, task.task_id)]
    assert "CODEX_COMMAND_COMPLETED" in [e["kind"] for e in c.store.events(h.project.project_id, task.task_id)]
    assert "Never expose reasoning" not in str(c.store.events(h.project.project_id, task.task_id))
    c.close()


def test_owner_pause_and_explicit_owner_input_go_to_reviewer_not_worker(h):
    h.actions = ["OWNER_DECISION_REQUIRED", "PASS"]
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_OWNER and result.reason == "Owner question" and result.context == "Visible context"
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_OWNER
    assert (h.initial, h.fixes, h.sends) == (1, 0, 1)
    c.resume_with_owner_decision(task.task_id, "Keep the requested harmless scope")
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE and h.fixes == 0 and h.sends == 2
    assert "REVIEWRELAY_OWNER_DECISION" in h.notifications[-1]["prompt"]
    assert result.owner_inputs[-1]["text"] == "Keep the requested harmless scope"
    c.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_preclick_reconciliation_reuses_review_key_candidate_and_worker(h, legacy):
    c, task = h.create()
    h.review_fault = "preclick"
    blocked = run(c.run(task.task_id))
    key = blocked.pending["key"]
    effect = c.store.effect(key)
    assert effect["status"] == "NOT_SENT"
    if legacy:
        c.store.put_effect(blocked, key, "REVIEW_SEND", "AMBIGUOUS", effect["payload"])
    identity = c.open_worker(task.task_id).identity
    h.review_fault = None
    pushes = h.publisher_events.count("GITHUB_PUSH_IN_FLIGHT")
    run(c.reconcile_unsent_review(identity))
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE and result.ready_for_owner_review
    assert result.candidate_sha == blocked.candidate_sha and result.worker_thread_id == identity.worker_thread_id
    assert h.initial == 1 and h.fixes == h.continuations == 0
    assert h.notifications[0]["review_key"] == h.notifications[1]["review_key"] == key
    assert h.publisher_events.count("GITHUB_PUSH_IN_FLIGHT") == pushes
    assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE and h.sends == 2
    c.close()


@pytest.mark.parametrize("mutation", ["unknown_click", "send_result", "candidate", "owner_draft", "thread"])
def test_preclick_reconciliation_cannot_authorize_unknown_or_different_effect(h, mutation):
    c, task = h.create()
    h.review_fault = "preclick"
    blocked = run(c.run(task.task_id))
    identity = c.open_worker(task.task_id).identity
    key = blocked.pending["key"]
    effect = c.store.effect(key)
    if mutation == "unknown_click":
        c.store.save(replace(blocked, error_code="MESSAGE_SEND_AMBIGUOUS"), "fixture")
    elif mutation in {"send_result", "candidate"}:
        payload = {**effect["payload"], "send_result": {"user_turn_identity": "sent"}} if mutation == "send_result" else {**effect["payload"], "candidate_sha": "f" * 40}
        c.store.put_effect(blocked, key, "REVIEW_SEND", "AMBIGUOUS", payload)
    elif mutation == "owner_draft":
        h.review_fault = "owner_draft"
    elif mutation == "thread":
        identity = replace(identity, worker_thread_id="wrong-thread")
    with pytest.raises((ControllerError, ReviewerConversationChanged)):
        run(c.reconcile_unsent_review(identity))
    assert h.sends == 1 and h.initial == 1
    assert c.store.get(h.project.project_id, task.task_id).state is S.PAUSED_ERROR
    c.close()


@pytest.mark.parametrize("mutation", [None, "no_proof", "extra_dispatch", "wrong_pair"])
def test_visible_review_reconciliation_requires_durable_absence_and_one_dispatch(h, mutation):
    c, task = h.create()
    h.review_fault = "preclick"
    run(c.run(task.task_id))
    identity = c.open_worker(task.task_id).identity
    h.review_fault = None
    run(c.reconcile_unsent_review(identity))
    h.review_fault = "ui_ambiguous"
    blocked = run(c.run(task.task_id))
    key = blocked.pending['key']
    assert c.store.effect(key)['payload']['send_result']['user_turn_identity'] == 'pending'
    assert blocked.error_code == 'MESSAGE_SEND_AMBIGUOUS'
    if mutation == 'no_proof':
        with c.store.db:
            c.store.db.execute("DELETE FROM controller_events WHERE kind='REVIEW_PRE_CLICK_FAILURE_RECONCILED'")
    elif mutation == 'extra_dispatch':
        c.store.dispatch(blocked, key, 'REVIEW_SEND', 'review_messages')
    h.review_fault = 'wrong_pair' if mutation == 'wrong_pair' else None
    if mutation:
        with pytest.raises((ControllerError, ReviewerConversationChanged)):
            run(c.reconcile_visible_review(identity))
        assert c.store.review(key) is None
    else:
        run(c.reconcile_visible_review(identity))
        result = run(c.run(task.task_id))
        assert result.state is S.COMPLETE and result.ready_for_owner_review
        assert result.worker_thread_id == identity.worker_thread_id and result.candidate_sha == blocked.candidate_sha
        assert c.store.effect(key)['payload']['reconciled'] is True
        assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE
    assert (h.initial,h.fixes,h.sends)==(1,0,2)
    c.close()


@pytest.mark.parametrize("fault", ["dirty", "no_commit"])
def test_explicit_completed_worker_continuation_keeps_thread_and_initial_once(h, fault):
    (h.repo / "result.txt").write_text("initial\n")
    git(h.repo, "add", "--", "result.txt")
    git(h.repo, "commit", "-m", "fixture tracked implementation file")
    c, task = h.create()
    h.worker_fault = fault
    blocked = run(c.run(task.task_id))
    assert blocked.state is S.PAUSED_ERROR
    identity = c.open_worker(task.task_id).identity
    assert blocked.worker_thread_id == identity.worker_thread_id
    h.worker_fault = None
    planned = run(c.continue_incomplete_worker(identity, "Complete tests and commit the existing task work."))
    assert planned.state is S.WORKER_RUNNING
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE and result.ready_for_owner_review
    assert result.worker_thread_id == identity.worker_thread_id
    assert (h.initial, h.continuations, h.fixes, h.sends) == (1, 1, 0, 1)
    assert result.counters["worker_continuation_turns"] == 1
    assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE
    assert (h.initial, h.continuations, h.sends) == (1, 1, 1)
    with pytest.raises(ControllerError):
        run(c.continue_incomplete_worker(identity, "Never replay"))
    c.close()


@pytest.mark.parametrize("mutation", ["ambiguous", "thread", "spec", "untracked", "diff"])
def test_worker_continuation_fails_closed_on_unreconciled_state(h, mutation):
    (h.repo / "result.txt").write_text("initial\n")
    git(h.repo, "add", "--", "result.txt")
    git(h.repo, "commit", "-m", "fixture tracked implementation file")
    c, task = h.create()
    h.worker_fault = "dirty"
    blocked = run(c.run(task.task_id))
    identity = c.open_worker(task.task_id).identity
    if mutation == "ambiguous":
        key = c._key(blocked, "WORKER_INITIAL", 0)
        effect = c.store.effect(key)
        c.store.put_effect(blocked, key, "WORKER_INITIAL", "AMBIGUOUS", effect["payload"])
    elif mutation == "thread":
        identity = replace(identity, worker_thread_id="another-thread")
    elif mutation == "spec":
        (h.repo / task_spec_path(task.task_id)).write_text("changed")
    elif mutation == "untracked":
        (h.repo / "unrelated.txt").write_text("Owner work")
    if mutation == "diff":
        run(c.continue_incomplete_worker(identity, "Finish existing task"))
        (h.repo / "result.txt").write_text("changed after planning")
        assert run(c.run(task.task_id)).state is S.PAUSED_ERROR
    else:
        with pytest.raises(ControllerError):
            run(c.continue_incomplete_worker(identity, "Finish existing task"))
    assert (h.initial, h.continuations, h.sends) == (1, 0, 0)
    c.close()


@pytest.mark.parametrize("terminal", ["INTERRUPTED", "FAILED", "COMPLETED"])
def test_continuation_reconciliation_requires_authoritative_unsuccessful_terminal_turn(h, terminal):
    (h.repo / "result.txt").write_text("initial\n")
    git(h.repo, "add", "--", "result.txt")
    git(h.repo, "commit", "-m", "fixture tracked implementation file")
    c, task = h.create()
    h.worker_fault = "dirty"
    run(c.run(task.task_id))
    identity = c.open_worker(task.task_id).identity
    run(c.continue_incomplete_worker(identity, "Finish existing task"))
    h.worker_fault = "failure"
    blocked = run(c.run(task.task_id))
    assert blocked.state is S.PAUSED_ERROR and blocked.resume_state == S.WORKER_RUNNING.value
    with pytest.raises(ControllerError):
        run(c.continue_incomplete_worker(identity, "No blind retry"))
    h.inspected_status = terminal
    if terminal == "COMPLETED":
        with pytest.raises(ControllerError):
            run(c.reconcile_interrupted_continuation(identity))
    else:
        reconciled = run(c.reconcile_interrupted_continuation(identity))
        key = c._key(reconciled, "WORKER_CONTINUATION", 1)
        assert c.store.effect(key)["payload"]["terminal_status"] == terminal
        h.worker_fault = None
        run(c.continue_incomplete_worker(identity, "Finish existing task"))
        result = run(c.run(task.task_id))
        assert result.state is S.COMPLETE
        assert result.worker_thread_id == identity.worker_thread_id
        assert result.counters["worker_continuation_turns"] == 2
        assert c.request_worker_pause(identity).status.value == "PAUSED_OWNER_STEER"
        run(c.resume_auto_relay(identity))
        assert run(c.run(task.task_id, resume=True)).state is S.COMPLETE
        assert h.sends == 1 and h.initial == 1 and h.continuations == 2
    c.close()


@pytest.mark.parametrize("action", ["FIX_REQUIRED", "NEED_EVIDENCE"])
def test_cycle_limits_before_next_effect(h, action):
    h.actions = [action]
    c, task = h.create(max_fix_cycles=0, max_evidence_cycles=0)
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_OWNER and result.error_code == "OWNER_ESCALATION_REQUIRED"
    assert (h.fixes, h.batches, h.sends) == (0, 0, 1)
    with pytest.raises(ControllerError):
        c.resume_with_owner_decision(task.task_id, "Continue anyway")
    c.close()


@pytest.mark.parametrize("fault,code", [("failure", "WORKER_TURN_FAILED"), ("no_commit", "WORKER_NO_NEW_COMMIT"),
    ("dirty", "CANDIDATE_INVALID_DIRTY_WORKTREE"), ("spec", "TASK_SPEC_MUTATED"), ("thread", "WORKER_COMPLETION_INVALID")])
def test_worker_failures_cannot_publish_or_send(h, fault, code):
    h.worker_fault = fault
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.error_code == code
    assert h.sends == 0 and "GITHUB_PUSH_STARTED" not in h.publisher_events
    c.close()


@pytest.mark.parametrize("fault,code", [("ambiguous", "SEND_AMBIGUOUS"), ("timeout", "REVIEWER_TIMEOUT"),
    ("malformed", "MISSING_CONTROL_BLOCK"), ("stale", "STALE_REVIEW"), ("ownership", "REVIEW_OWNERSHIP_INVALID"),
    ("local_mutation", "CANDIDATE_MUTATED_DURING_REVIEW"), ("remote_mutation", "REMOTE_CANDIDATE_MUTATED")])
def test_review_failures_pause_without_routing_or_duplicate_send(h, fault, code):
    h.review_fault = fault
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.error_code == code
    before = h.sends
    h.review_fault = None
    run(c.run(task.task_id, resume=True))
    assert h.fixes == 0 and h.batches == 0 and h.sends == before
    c.close()


def test_review_error_never_calls_worker_again(h):
    h.actions = ["REVIEW_ERROR"]
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.reason == "GITHUB_REVIEW_ACCESS_REQUIRED"
    assert run(c.run(task.task_id, resume=True)).state is S.PAUSED_ERROR
    assert h.fixes == 0 and h.sends == 1
    c.close()


@pytest.mark.parametrize("fault", ["unsafe", "incomplete", "test"])
def test_evidence_validation_uses_phase5_configured_registry_and_never_worker(h, fault):
    h.evidence_fault = fault
    h.actions = ["NEED_EVIDENCE", "PASS"]
    c, task = h.create(tests={"check": (sys.executable, "-c", "print('harmless local fact')")} if fault == "test" else None)
    result = run(c.run(task.task_id))
    assert result.state is (S.COMPLETE if fault == "test" else S.PAUSED_ERROR), result.error_code
    assert h.initial == 1 and h.fixes == 0
    assert h.batches == (0 if fault == "unsafe" else 1)
    if fault == "test":
        assert "harmless local fact" in h.notifications[-1]["prompt"]
    c.close()


@pytest.mark.parametrize("event,expected", [("WORKER_INITIAL_PLANNED", S.COMPLETE), ("WORKER_INITIAL_IN_FLIGHT", S.PAUSED_ERROR),
    ("WORKER_INITIAL_CONFIRMED", S.PAUSED_ERROR), ("WORKER_INITIAL_COMPLETED", S.COMPLETE),
    ("WORKER_TURN_COMPLETED", S.COMPLETE), ("CANDIDATE_VERIFIED", S.COMPLETE),
    ("PUBLISHER_GITHUB_PUSH_PLANNED", S.COMPLETE), ("PUBLISHER_GITHUB_PUSH_STARTED", S.PAUSED_ERROR),
    ("PUBLISHER_GITHUB_PUSH_CONFIRMED", S.COMPLETE), ("CONTROLLER_REMOTE_VERIFIED", S.COMPLETE),
    ("REVIEW_SEND_PLANNED", S.COMPLETE), ("REVIEW_SEND_IN_FLIGHT", S.PAUSED_ERROR),
    ("REVIEW_SEND_CONFIRMED", S.PAUSED_ERROR), ("REVIEW_RAW_CAPTURED", S.COMPLETE),
    ("REVIEW_PARSED", S.COMPLETE), ("REVIEW_RECEIVED", S.COMPLETE)])
def test_crash_recovery_boundaries_never_repeat_dispatched_effects(h, event, expected):
    c, task = h.create()
    def crash(name):
        if name == event:
            raise Crash()
    c.observer = crash
    with pytest.raises(Crash):
        run(c.run(task.task_id))
    c.close()
    before = (h.initial, h.fixes, h.sends, h.publisher_events.count("GITHUB_PUSH_STARTED"))
    recovered = h.make()
    result = run(recovered.run(task.task_id, resume=True))
    assert result.state is expected, (event, result.error_code)
    assert h.initial <= 1 and h.fixes == 0 and h.sends <= 1
    assert h.publisher_events.count("GITHUB_PUSH_STARTED") <= 1
    if expected is S.PAUSED_ERROR:
        assert before == (h.initial, h.fixes, h.sends, h.publisher_events.count("GITHUB_PUSH_STARTED"))
    recovered.close()


@pytest.mark.parametrize("event", ["TASK_CREATED", "TASK_SPEC_WRITTEN", "TASK_SPEC_COMMIT_EFFECT", "TASK_SPEC_COMMITTED", "TASK_READY"])
def test_task_spec_commit_recovery_is_exact_and_not_duplicated(h, event):
    def crash(name):
        if name == event:
            raise Crash()
    c = h.make(crash)
    with pytest.raises(Crash):
        h.create(c)
    c.close()
    recovered = h.make()
    result = run(recovered.run("TASK-1"))
    assert result.state is S.COMPLETE, result.error_code
    assert git(h.repo, "log", "--format=%s").splitlines().count("ReviewRelay task specification: TASK-1") == 1
    recovered.close()


def test_controller_task_os_lock_and_project_lock_exclude_other_processes(h):
    c, task = h.create()
    task_lock = c._task_lock(task.task_id)
    duplicate = h.make()
    try:
        with pytest.raises(Exception):
            run(duplicate.run(task.task_id))
    finally:
        task_lock.close()
        duplicate.close()
    code = "from pathlib import Path;from reviewrelay.worker.lock import WorkerTaskLock;WorkerTaskLock(Path(__import__('sys').argv[1]))"
    project_lock = c._project_lock()
    try:
        outcome = subprocess.run([sys.executable, "-c", code, str(h.root.path / "config/project-locks" / (h.project.project_id + ".lock"))], capture_output=True)
        assert outcome.returncode != 0
    finally:
        project_lock.close()
    c.close()


def test_pause_at_safe_checkpoint_and_resume_same_thread(h):
    c, task = h.create()
    h.pause_after_completion = True
    paused = run(c.run(task.task_id))
    assert paused.state is S.PAUSED_USER and h.sends == 0 and h.initial == 1
    recovered = h.make()
    result = run(recovered.run(task.task_id, resume=True))
    assert result.state is S.COMPLETE and result.worker_thread_id == paused.worker_thread_id and h.initial == 1
    c.close()
    recovered.close()


def test_stop_interrupts_active_wait_without_extra_turn(h):
    c, task = h.create()
    async def stopping():
        h.worker_gate = asyncio.Event()
        running = asyncio.create_task(c.run(task.task_id))
        while not h.initial:
            await asyncio.sleep(.01)
        c.request_stop(task.task_id)
        return await running
    result = run(stopping())
    assert result.state is S.STOPPED and not result.ready_for_owner_review and h.sends == 0
    assert run(c.run(task.task_id, resume=True)).state is S.STOPPED and h.initial == 1
    c.close()


def test_pr_mode_reuses_one_canonical_pr_for_fix_cycle(h):
    with ProjectRegistry(h.root) as registry:
        h.project = registry.save(replace(h.project, review_mode="pr", github_git_url="git@github.com:owner/project.git"))
    git(h.repo, "remote", "set-url", "origin", "git@github.com:owner/project.git")
    h.actions = ["FIX_REQUIRED", "PASS"]
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE, result.error_code
    assert h.pr_creates == 1 and result.published["pr_url"] == "https://github.com/owner/project/pull/11"
    assert all("PR_URL=https://github.com/owner/project/pull/11" in n["prompt"] for n in h.notifications)
    c.close()


@pytest.mark.parametrize("event,expected", [("REVIEW_FIX_REQUIRED", S.COMPLETE), ("WORKER_FIX_PLANNED", S.COMPLETE),
    ("WORKER_FIX_IN_FLIGHT", S.PAUSED_ERROR), ("WORKER_FIX_CONFIRMED", S.PAUSED_ERROR), ("WORKER_FIX_COMPLETED", S.COMPLETE)])
def test_fix_restart_never_creates_duplicate_turn_or_new_thread(h, event, expected):
    h.actions = ["FIX_REQUIRED", "PASS"]
    c, task = h.create()
    c.observer = lambda name: (_ for _ in ()).throw(Crash()) if name == event else None
    with pytest.raises(Crash):
        run(c.run(task.task_id))
    c.close()
    before = h.fixes
    recovered = h.make()
    result = run(recovered.run(task.task_id, resume=True))
    assert result.state is expected, result.error_code
    assert h.initial == 1 and h.fixes <= 1 and len(set(h.thread_ids)) == 1
    if expected is S.PAUSED_ERROR:
        assert h.fixes == before
    recovered.close()


@pytest.mark.parametrize("event,expected", [("EVIDENCE_REQUESTED", S.COMPLETE), ("LOCAL_EVIDENCE_PLANNED", S.COMPLETE),
    ("LOCAL_EVIDENCE_IN_FLIGHT", S.PAUSED_ERROR), ("EVIDENCE_COMPLETED", S.COMPLETE),
    ("EVIDENCE_SEND_PLANNED", S.COMPLETE), ("EVIDENCE_SEND_IN_FLIGHT", S.PAUSED_ERROR),
    ("EVIDENCE_SEND_CONFIRMED", S.PAUSED_ERROR)])
def test_evidence_restart_no_duplicate_continuation_or_worker(h, event, expected):
    h.actions = ["NEED_EVIDENCE", "PASS"]
    c, task = h.create()
    c.observer = lambda name: (_ for _ in ()).throw(Crash()) if name == event else None
    with pytest.raises(Crash):
        run(c.run(task.task_id))
    c.close()
    recovered = h.make()
    result = run(recovered.run(task.task_id, resume=True))
    assert result.state is expected, result.error_code
    assert h.initial == 1 and h.fixes == 0 and h.batches <= 1 and h.sends <= 2
    recovered.close()


@pytest.mark.parametrize("condition", ["dirty", "missing", "mutated"])
def test_task_baseline_and_canonical_spec_fail_closed(h, condition):
    c = h.make()
    if condition == "dirty":
        (h.repo / "unrelated.txt").write_text("Owner changes")
        with pytest.raises(ControllerError) as error:
            h.create(c)
        assert error.value.code == "BLOCKED_DIRTY_BASELINE"
        assert not (h.repo / task_spec_path("TASK-1")).exists()
    else:
        _, task = h.create(c)
        path = h.repo / task_spec_path(task.task_id)
        if condition == "missing":
            path.unlink()
        else:
            path.write_text("changed requirement")
        result = run(c.run(task.task_id))
        assert result.state is S.PAUSED_ERROR and result.error_code == "TASK_BASELINE_CHANGED"
    assert h.initial == h.sends == 0
    c.close()


@pytest.mark.parametrize("task_id", ["../escape", "a/b", "C:\\escape", "\\\\server\\share", "/absolute", ".."])
def test_unsafe_task_id_never_writes_source(h, task_id):
    c = h.make()
    before = git(h.repo, "rev-parse", "HEAD")
    with pytest.raises(Exception):
        h.create(c, task=task_id)
    assert git(h.repo, "rev-parse", "HEAD") == before and git(h.repo, "status", "--porcelain") == ""
    c.close()


@pytest.mark.parametrize("condition", ["GITHUB_AUTH_REQUIRED", "GITHUB_BRANCH_DIVERGED", "REMOTE_CANDIDATE_MISMATCH"])
def test_publisher_failure_blocks_all_review_notifications(h, condition):
    h.publish_fault = condition
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.error_code == condition and h.sends == 0
    c.close()


def test_failed_fix_and_ambiguous_evidence_send_never_repeat(h):
    h.actions = ["NEED_EVIDENCE", "FIX_REQUIRED", "PASS"]
    h.fix_fault = "failure"
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.error_code == "WORKER_TURN_FAILED"
    assert h.initial == 1 and h.fixes == 1 and h.sends == 2
    run(c.run(task.task_id, resume=True))
    assert h.fixes == 1 and h.sends == 2
    c.close()


def test_evidence_send_ambiguous_recovery_does_not_repeat(h):
    h.actions = ["NEED_EVIDENCE", "PASS"]
    h.fail_send_number = 2
    c, task = h.create()
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and h.batches == 1 and h.sends == 2
    result = run(c.run(task.task_id, resume=True))
    assert result.error_code == "EVIDENCE_SEND_AMBIGUOUS"
    assert h.initial == 1 and h.fixes == 0 and h.batches == 1 and h.sends == 2
    c.close()


def test_schema5_to6_retains_project_worker_bridge_rows_and_rolls_back_atomically(h, monkeypatch):
    c, task = h.create()
    with StateStore(h.root) as state:
        r = state.get(h.project.project_id, task.task_id)
        state.save(replace(r, worker_thread_id="saved-thread", worker_session_identity="saved-thread"))
        before_task = state.get(h.project.project_id, task.task_id)
        before_project = tuple(state._connection.execute("SELECT * FROM projects").fetchone())
        before_pub = tuple(state._connection.execute("SELECT * FROM github_publications").fetchone())
        state._connection.execute("INSERT INTO github_reviews VALUES('legacy',?,?,?,1,'VALIDATED','{}','saved raw','{}','now')",
            (h.project.project_id, task.task_id, task.base_sha))
        for table in ("controller_tasks", "controller_effects", "controller_reviews", "controller_events"):
            state._connection.execute("DROP TABLE " + table)
        state._connection.execute("PRAGMA user_version=5")
        state._connection.commit()
    c.close()
    original = StateStore._controller_tables
    def fail(self):
        original(self)
        raise RuntimeError("atomic migration test")
    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_controller_tables", fail)
        with pytest.raises(RuntimeError):
            StateStore(h.root)
    with sqlite3.connect(h.root.path / "db/relay.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='controller_tasks'").fetchall()
    with StateStore(h.root) as state:
        assert state.get(h.project.project_id, task.task_id) == before_task
        assert tuple(state._connection.execute("SELECT * FROM projects").fetchone()) == before_project
        assert tuple(state._connection.execute("SELECT * FROM github_publications").fetchone()) == before_pub
        assert state._connection.execute("SELECT raw_text FROM github_reviews").fetchone()[0] == "saved raw"
        assert state._connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_unrelated_tasks_have_distinct_threads_and_branches(h):
    first, task = h.create(task="TASK-1")
    a = run(first.run(task.task_id))
    first.close()
    h.actions = ["PASS"]
    second, task = h.create(task="TASK-2")
    b = run(second.run(task.task_id))
    assert a.state is b.state is S.COMPLETE, b.error_code
    assert a.worker_thread_id != b.worker_thread_id
    assert a.published["branch"] != b.published["branch"]
    assert git(h.remote, "rev-parse", "refs/heads/" + a.published["branch"]) == a.candidate_sha
    assert git(h.remote, "rev-parse", "refs/heads/" + b.published["branch"]) == b.candidate_sha
    assert h.initial == 2 and h.fixes == 0
    second.close()


def test_cleanup_failure_is_durable_and_releases_controller_locks(h):
    c, task = h.create()
    factory = c.worker_factory
    def failing_close(config, task):
        worker = factory(config, task)
        async def close():
            raise ControllerError("Safe fake cleanup condition", code="WORKER_CLOSE_FAILED")
        worker.close = close
        return worker
    c.worker_factory = failing_close
    result = run(c.run(task.task_id))
    assert result.state is S.COMPLETE
    events = c.store.events(h.project.project_id, task.task_id)
    assert any(e["kind"] == "ADAPTER_CLEANUP_FAILED" and "WORKER_CLOSE_FAILED" in e["payload_json"] for e in events)
    lock = c._task_lock(task.task_id)
    lock.close()
    lock = c._project_lock()
    lock.close()
    c.close()


@pytest.mark.parametrize("action", ["FIX_REQUIRED", "NEED_EVIDENCE"])
def test_cycle_limits_after_one_successful_effect_do_not_issue_another(h, action):
    h.actions = [action, action]
    c, task = h.create(max_fix_cycles=1, max_evidence_cycles=1)
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_OWNER and result.error_code == "OWNER_ESCALATION_REQUIRED"
    assert h.fixes == (1 if action == "FIX_REQUIRED" else 0)
    assert h.batches == (1 if action == "NEED_EVIDENCE" else 0) and h.sends == 2
    c.close()


def test_large_evidence_pauses_without_attachment_or_worker_fallback(h):
    h.actions = ["NEED_EVIDENCE", "PASS"]
    h.evidence_fault = "test"
    c, task = h.create(tests={"check": (sys.executable, "-c", "print('x'*20000)")})
    result = run(c.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.error_code == "EVIDENCE_TEXT_LIMIT_REQUIRES_OWNER"
    assert h.initial == 1 and h.fixes == 0 and h.sends == 1 and h.batches == 1
    c.close()


def test_spec_commit_reconciliation_rejects_whitespace_only_requirement_change(h):
    c = h.make(lambda event: (_ for _ in ()).throw(Crash()) if event == "TASK_SPEC_COMMIT_EFFECT" else None)
    with pytest.raises(Crash):
        h.create(c)
    c.close()
    path = h.repo / task_spec_path("TASK-1")
    path.write_text(" Implement one harmless result file.\n", encoding="utf-8")
    git(h.repo, "add", "--", task_spec_path("TASK-1"))
    git(h.repo, "commit", "--amend", "--no-edit")
    recovered = h.make()
    result = run(recovered.run("TASK-1"))
    assert result.state is S.PAUSED_ERROR and result.error_code == "TASK_SPEC_COMMIT_AMBIGUOUS"
    assert h.initial == h.sends == 0
    recovered.close()


@pytest.mark.parametrize("mutation", ["local", "remote"])
def test_captured_review_invalidation_survives_restoring_old_candidate(h, mutation):
    c, task = h.create()
    c.observer = lambda event: (_ for _ in ()).throw(Crash()) if event == "REVIEW_RECEIVED" else None
    with pytest.raises(Crash):
        run(c.run(task.task_id))
    captured = c.store.get(h.project.project_id, task.task_id)
    assert c.store.review(captured.last_review_key)["decision_json"]
    c.close()
    if mutation == "local":
        (h.repo / "unexpected.txt").write_text("outside mutation")
    else:
        git(h.remote, "update-ref", "refs/heads/" + task_branch_name(task.task_id), task.base_sha)
    recovered = h.make()
    result = run(recovered.run(task.task_id))
    assert result.state is S.PAUSED_ERROR and result.review_invalidated
    if mutation == "local":
        (h.repo / "unexpected.txt").unlink()
    else:
        git(h.remote, "update-ref", "refs/heads/" + task_branch_name(task.task_id), captured.candidate_sha)
    assert run(recovered.run(task.task_id, resume=True)).state is S.PAUSED_ERROR
    assert h.sends == h.initial == 1 and h.fixes == h.batches == 0
    recovered.close()
