from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from reviewrelay.config import GitHubConfig, ProjectConfig, RepoConfig, validate_branch_name
from reviewrelay.errors import ConfigError
from reviewrelay.evidence_process import ProcessEvidence, run_bounded
from reviewrelay.github_publish import (GitHubCandidatePublisher, GitHubCLI, GitHubPublishError,
                                      PublishRequest, task_branch_name, task_spec_path)
from reviewrelay.github_review import GitHubReviewBridge, review_notification
from reviewrelay.models import TaskState
from reviewrelay.protocol import ReviewerAction
from reviewrelay.reviewer.base import AssistantResponse, SendDisposition, SendResult, TurnBaseline
from reviewrelay.state import StateStore, SCHEMA_VERSION
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task import begin_task

URL = "https://chatgpt.com/c/fixture"


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, shell=False, capture_output=True, text=True, check=True).stdout.strip()


class Crash(BaseException):
    pass


class RecordingRunner:
    def __init__(self):
        self.calls = []
        self.push_mode = None
        self.remote_reads = 0
        self.mismatch = False

    async def __call__(self, argv, cwd, **kwargs):
        self.calls.append(argv)
        assert isinstance(argv, tuple)
        assert kwargs["timeout"] <= 300 and kwargs["stdout_cap"] <= 1024 * 1024
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        if "push" in argv and self.push_mode in {"timeout", "auth", "failure"}:
            return ProcessEvidence(argv, cwd and 1, .01, b"", b"authentication failed: secret-not-for-logs" if self.push_mode == "auth" else b"failure",
                                   timed_out=self.push_mode == "timeout")
        result = await run_bounded(argv, cwd, **kwargs)
        if "push" in argv and self.push_mode == "crash-after-push":
            raise Crash()
        if "ls-remote" in argv:
            self.remote_reads += 1
            if self.mismatch and any("push" in call for call in self.calls):
                ref = argv[-1]
                return replace(result, stdout=("a" * 40 + "\t" + ref + "\n").encode())
        return result

    @property
    def pushes(self):
        return [argv for argv in self.calls if "push" in argv]


@pytest.fixture
def setup(git_repo, tmp_path, commit_change):
    task_id = "task"
    spec = task_spec_path(task_id)
    base = commit_change(git_repo, spec, "# Task\nCreate result.txt. Keep this contract unchanged.\n", "Task contract before implementation")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(git_repo, "remote", "add", "origin", str(remote))
    root = PortableDataRoot(tmp_path / "data").create()
    config = ProjectConfig("p", RepoConfig(str(git_repo)), chatgpt={"conversation_url": URL}, github=GitHubConfig(enabled=True))
    begin_task(config, task_id, root)
    runner = RecordingRunner()
    def make(**kwargs):
        return GitHubCandidatePublisher(root, kwargs.pop("config", config), runner=runner, allow_local_remote=True, **kwargs)
    def candidate(content="result\n", cycle=1):
        sha = commit_change(git_repo, "result.txt", content)
        with StateStore(root) as state:
            r = state.get("p", task_id)
            state.save(replace(r, candidate_sha=sha, review_cycle=cycle, task_state=TaskState.VERIFY_CANDIDATE))
        return PublishRequest(task_id, base, sha, cycle)
    return root, config, git_repo, remote, runner, make, candidate


def bind(make, **kwargs):
    publisher = make(**kwargs)
    asyncio.run(publisher.bind_task("task"))
    return publisher


@pytest.mark.parametrize("task", ["task", "A..B", "thing.lock", "a.b", "a-b", "x" * 128])
def test_task_branch_safe_deterministic_collision_resistant(task):
    branch = task_branch_name(task)
    assert branch == task_branch_name(task) and branch.startswith("reviewrelay/")
    assert validate_branch_name(branch) == branch
    assert subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True).returncode == 0
    assert task_branch_name("a.b") != task_branch_name("a-b")


@pytest.mark.parametrize("task", ["../escape", "-option", "space name", "$(cmd)", "@{x}", ""])
def test_unsafe_task_ids_rejected(task):
    with pytest.raises(Exception):
        task_branch_name(task)


def test_exact_push_verify_idempotency_and_new_candidate_same_branch(setup):
    root, config, repo, remote, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    result = asyncio.run(publisher.publish(request))
    assert result.head_sha == git(remote, "rev-parse", "refs/heads/" + result.branch) == request.candidate_sha
    assert result.pr_url is None and result.task_spec_path == task_spec_path("task")
    assert runner.pushes[0][-1] == f"{request.candidate_sha}:refs/heads/{result.branch}"
    assert not any("force" in argument or argument == "HEAD" for argument in runner.pushes[0])
    assert asyncio.run(publisher.publish(request)) == result
    assert len(runner.pushes) == 1
    publisher.close()
    publisher = make()
    assert asyncio.run(publisher.reconcile(request)) == result
    assert len(runner.pushes) == 1
    second = candidate("fixed\n", 2)
    fixed = asyncio.run(publisher.publish(second))
    assert fixed.branch == result.branch and fixed.head_sha != result.head_sha
    assert len(runner.pushes) == 2
    assert publisher.load("task")["github_last_remote_sha"] == fixed.head_sha
    publisher.close()


@pytest.mark.parametrize("event", ["GITHUB_PUSH_PLANNED", "GITHUB_PUSH_STARTED", "GITHUB_PUSH_CONFIRMED", "REMOTE_SHA_VERIFIED"])
def test_restart_publish_boundaries_no_duplicate_push(setup, event):
    _, _, _, _, runner, make, candidate = setup
    def crash(name):
        if name == event:
            raise Crash()
    publisher = bind(make, checkpoint_observer=crash)
    request = candidate()
    with pytest.raises(Crash):
        asyncio.run(publisher.publish(request))
    publisher.close()
    publisher = make()
    if event == "GITHUB_PUSH_STARTED":
        with pytest.raises(GitHubPublishError) as error:
            asyncio.run(publisher.publish(request))
        assert error.value.code == "GITHUB_PUSH_AMBIGUOUS" and not runner.pushes
    else:
        assert asyncio.run(publisher.publish(request)).head_sha == request.candidate_sha
        assert len(runner.pushes) == 1
    publisher.close()


def test_restart_after_remote_push_before_ack_is_read_only(setup):
    _, _, _, _, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    runner.push_mode = "crash-after-push"
    with pytest.raises(Crash):
        asyncio.run(publisher.publish(request))
    publisher.close()
    publisher = make()
    assert asyncio.run(publisher.reconcile(request)).head_sha == request.candidate_sha
    assert len(runner.pushes) == 1
    publisher.close()


@pytest.mark.parametrize("mode,code", [("timeout", "GITHUB_TIMEOUT"), ("auth", "GITHUB_AUTH_REQUIRED"), ("failure", "GITHUB_PROCESS_FAILED")])
def test_uncertain_push_never_retried_and_diagnostics_do_not_leak(setup, mode, code):
    _, _, _, _, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    runner.push_mode = mode
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(request))
    assert error.value.code == code and "secret-not-for-logs" not in str(error.value)
    with pytest.raises(GitHubPublishError, match="does not prove"):
        asyncio.run(publisher.publish(request))
    assert len(runner.pushes) == 1
    publisher.close()


def test_remote_mismatch_blocks_notification(setup):
    _, _, _, _, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    runner.mismatch = True
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(request))
    assert error.value.code == "REMOTE_CANDIDATE_MISMATCH"
    assert publisher.load("task")["github_publish_status"] != "READY_TO_NOTIFY_REVIEWER"
    publisher.close()


@pytest.mark.parametrize("mutation", ["dirty", "head", "spec"])
def test_local_candidate_and_spec_mutation_before_push(setup, commit_change, mutation):
    _, _, repo, _, runner, make, candidate = setup
    def alter(event):
        if event == "GITHUB_PUSH_PLANNED":
            if mutation == "dirty":
                (repo / "untracked").write_text("mutation")
            else:
                commit_change(repo, "result.txt", "new HEAD")
    publisher = bind(make, checkpoint_observer=alter)
    if mutation == "spec":
        commit_change(repo, task_spec_path("task"), "Weakened requirements")
    request = candidate()
    with pytest.raises(Exception) as error:
        asyncio.run(publisher.publish(request))
    if mutation == "spec":
        assert error.value.code == "TASK_SPEC_MUTATED"
    assert not runner.pushes
    publisher.close()


def test_intentional_spec_change_bound_before_worker_is_allowed(setup, commit_change):
    _, _, repo, _, _, make, candidate = setup
    publisher = make()
    asyncio.run(publisher.bind_task("task", allow_spec_change=True))
    commit_change(repo, task_spec_path("task"), "Intentional revised contract")
    assert asyncio.run(publisher.publish(candidate())).head_sha
    publisher.close()


def test_remote_divergence_and_pending_review_mutation(setup, commit_change):
    _, _, repo, remote, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    published = asyncio.run(publisher.publish(request))
    other = commit_change(repo, "other.txt", "unrelated remote commit")
    git(repo, "push", str(remote), f"{other}:refs/heads/{published.branch}")
    git(repo, "reset", "--hard", request.candidate_sha)  # Isolate remote mutation from local mutation.
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.verify_current(published))
    assert error.value.code == "REMOTE_CANDIDATE_MUTATED"
    next_request = candidate("next\n", 2)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(next_request))
    assert error.value.code == "GITHUB_BRANCH_DIVERGED" and len(runner.pushes) == 1
    publisher.close()


class FakePR:
    def __init__(self):
        self.row = None
        self.creates = 0
        self.fail_after_create = False
    async def find(self, repository, branch, base):
        if self.row and hasattr(self, "remote"):
            self.row["headRefOid"] = git(self.remote, "rev-parse", "refs/heads/" + branch)
        return self.row
    async def create(self, repository, branch, base, task_id):
        self.creates += 1
        self.row = {"number": 7, "url": f"https://github.com/{repository}/pull/7",
                    "headRefName": branch, "baseRefName": base, "state": "OPEN", "isCrossRepository": False}
        if self.fail_after_create:
            raise Crash()


def pr_publisher(setup, service, **kwargs):
    _, config, _, remote, _, make, _ = setup
    publisher = make(config=replace(config, github=replace(config.github, mode="pr")), pr_service=service, **kwargs)
    service.remote = remote
    async def destination():
        return str(remote), "owner/test"
    publisher._remote = destination  # Deterministic fake GitHub identity over an actual bare Git transport.
    return publisher


@pytest.mark.parametrize("lost_ack", [False, True])
def test_one_pr_reused_across_restart_and_fix_cycles(setup, lost_ack):
    _, _, _, _, runner, _, candidate = setup
    pr = FakePR()
    pr.fail_after_create = lost_ack
    publisher = pr_publisher(setup, pr)
    asyncio.run(publisher.bind_task("task"))
    request = candidate()
    if lost_ack:
        with pytest.raises(Crash):
            asyncio.run(publisher.publish(request))
    else:
        assert asyncio.run(publisher.publish(request)).pr_number == 7
    publisher.close()
    publisher = pr_publisher(setup, pr)
    assert asyncio.run(publisher.publish(request)).pr_url == "https://github.com/owner/test/pull/7"
    second = candidate("fixed\n", 2)
    assert asyncio.run(publisher.publish(second)).pr_number == 7
    assert pr.creates == 1 and len(runner.pushes) == 2
    publisher.close()


def test_uncertain_pr_creation_cannot_create_another_pr(setup):
    _, _, _, _, _, _, candidate = setup
    pr = FakePR()
    def crash(name):
        if name == "GITHUB_PR_STARTED":
            raise Crash()
    publisher = pr_publisher(setup, pr, checkpoint_observer=crash)
    asyncio.run(publisher.bind_task("task"))
    request = candidate()
    with pytest.raises(Crash):
        asyncio.run(publisher.publish(request))
    publisher.close()
    publisher = pr_publisher(setup, pr)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(request))
    assert error.value.code == "GITHUB_PR_AMBIGUOUS" and pr.creates == 0
    publisher.close()


def test_public_reconciliation_does_not_create_a_pr_after_lost_push_ack(setup):
    _, _, _, _, runner, _, candidate = setup
    pr = FakePR()
    publisher = pr_publisher(setup, pr)
    asyncio.run(publisher.bind_task("task"))
    request = candidate()
    runner.push_mode = "crash-after-push"
    with pytest.raises(Crash):
        asyncio.run(publisher.publish(request))
    publisher.close()
    publisher = pr_publisher(setup, pr)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.reconcile(request))
    assert error.value.code == "GITHUB_PR_REQUIRED"
    assert pr.creates == 0 and len(runner.pushes) == 1
    # Explicit publishing may finish the missing PR after the push is proven; never repush.
    assert asyncio.run(publisher.publish(request)).pr_number == 7
    assert pr.creates == 1 and len(runner.pushes) == 1
    publisher.close()


class FakeReviewer:
    def __init__(self):
        self.calls = []
        self.response_action = "PASS"
        self.mutate = None
    async def send_review_pack(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["attachment_paths"] == ()
        prompt = kwargs["prompt"]
        self.sha = re.search(r"HEAD_SHA=(\w+)", prompt)[1]
        self.cycle = int(re.search(r"REVIEW_CYCLE=(\d+)", prompt)[1])
        return SendResult(kwargs["review_key"], kwargs["conversation_url"], "now", "review_pack", (),
                          hashlib.sha256(prompt.encode()).hexdigest(), TurnBaseline(("old-u",), ("old-a",), 1), "new-u")
    async def wait_response(self, sent):
        if self.mutate:
            self.mutate()
        payload = {"protocol": "rr.v1", "candidate_sha": self.sha, "cycle": self.cycle, "action": self.response_action}
        if self.response_action == "PASS":
            payload["findings"] = []
        else:
            payload["reason"] = "GITHUB_REVIEW_ACCESS_REQUIRED"
        return AssistantResponse(sent.review_key, sent.conversation_url,
            "<RELAY_CONTROL>" + json.dumps(payload) + "</RELAY_CONTROL>", "now", "later", "new-a", True)


@pytest.mark.parametrize("action", ["PASS", "REVIEW_ERROR"])
def test_compact_no_attachment_notification_owned_response_and_durable_decision(setup, action):
    root, config, _, _, _, make, candidate = setup
    publisher = bind(make)
    published = asyncio.run(publisher.publish(candidate()))
    reviewer = FakeReviewer()
    reviewer.response_action = action
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer)
    sent = asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    decision = asyncio.run(bridge.capture_review(published, sent))
    assert decision.action.value == action and len(reviewer.calls) == 1
    prompt = reviewer.calls[0]["prompt"]
    assert len(prompt.encode()) < 3000 and prompt.startswith("REVIEWRELAY_REVIEW_REQUEST")
    for expected in ("PR=NONE", f"BASE_SHA={published.base_sha}", f"HEAD_SHA={published.head_sha}", "TASK_SPEC_PATH=.reviewrelay/tasks/task.md"):
        assert expected in prompt
    assert reviewer.calls[0]["attachment_paths"] == ()
    assert asyncio.run(bridge.capture_review(published)) == decision
    bridge.close()
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer)
    assert asyncio.run(bridge.capture_review(published)) == decision
    with pytest.raises(GitHubPublishError, match="resend"):
        asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    assert len(reviewer.calls) == 1
    assert list(root.safe_path("active/p/task/durable/reviews").glob("*.md"))
    assert bridge.state.get("p", "task").task_state is TaskState.PARSE_REVIEW
    bridge.close()
    publisher.close()


def test_pr_notification_contains_canonical_pr_url(setup):
    _, _, _, _, _, _, candidate = setup
    publisher = pr_publisher(setup, FakePR())
    asyncio.run(publisher.bind_task("task"))
    published = asyncio.run(publisher.publish(candidate()))
    assert "PR=https://github.com/owner/test/pull/7" in review_notification(published)
    publisher.close()


def test_remote_mutation_while_response_pending_cannot_validate(setup, commit_change):
    root, config, repo, remote, _, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    published = asyncio.run(publisher.publish(request))
    reviewer = FakeReviewer()
    reviewer.mutate = lambda: git(remote, "update-ref", "refs/heads/" + published.branch, request.base_sha)
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer)
    sent = asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(bridge.capture_review(published, sent))
    assert error.value.code == "REMOTE_CANDIDATE_MUTATED"
    assert bridge._load(sent.review_key)["decision_json"] is None
    bridge.close()
    publisher.close()


def test_schema3_upgrade_preserves_task_rows_and_rolls_back_on_failure(setup, monkeypatch):
    root, _, _, _, _, _, _ = setup
    with StateStore(root) as state:
        before = state.get("p", "task")
        for table in ("github_publications", "github_events", "github_reviews", "projects", "project_events"):
            state._connection.execute(f"DROP TABLE {table}")
        state._connection.execute("PRAGMA user_version=3")
        state._connection.commit()
    original = StateStore._github_tables
    def broken(self):
        original(self)
        raise RuntimeError("migration failure")
    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_github_tables", broken)
        with pytest.raises(RuntimeError):
            StateStore(root)
    with sqlite3.connect(root.safe_path("db/relay.db")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='github_publications'").fetchall()
    with StateStore(root) as state:
        assert state.get("p", "task") == before
        assert state._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5


def test_github_configuration_roundtrip_and_no_token_field():
    import yaml
    config = ProjectConfig("p", RepoConfig("repo"), github=GitHubConfig(True, "origin", "release/main", "pr"))
    assert ProjectConfig.from_yaml(yaml.safe_dump(config.to_mapping())) == config
    for settings in ({"token": "secret"}, {"remote": "https://token@github.com/a/b"}, {"base_branch": "--option"}, {"enabled": "yes"}, {"mode": "invalid"}, {"mode": []}):
        with pytest.raises(ConfigError):
            ProjectConfig.from_yaml(yaml.safe_dump({"project_id": "p", "repo": {"path": "repo"}, "github": settings}))


def test_missing_committed_spec_and_late_binding_rejected(setup):
    root, config, _, _, runner, make, candidate = setup
    publisher = make()
    begin_task(config, "missing", root)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.bind_task("missing"))
    assert error.value.code == "TASK_SPEC_REQUIRED"
    candidate()
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.bind_task("task"))
    assert error.value.code == "TASK_SPEC_BINDING_REQUIRED" and not runner.pushes
    publisher.close()


@pytest.mark.parametrize("destination", [
    "https://credential-not-for-logs@github.com/owner/repo.git",
    "https://github.com:invalid/owner/repo.git",
    "https://[invalid/owner/repo.git",
])
def test_remote_credentials_or_changed_destination_are_never_published(setup, destination):
    _, _, repo, _, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    git(repo, "remote", "set-url", "origin", destination)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(request))
    assert error.value.code == "GITHUB_REMOTE_INVALID"
    assert "credential-not-for-logs" not in str(error.value) and not runner.pushes
    publisher.close()


@pytest.mark.parametrize("mutation", ["local", "stale", "malformed"])
def test_pending_review_fails_closed_and_stays_invalidated(setup, mutation):
    root, config, repo, _, _, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    published = asyncio.run(publisher.publish(request))
    reviewer = FakeReviewer()
    if mutation == "local":
        reviewer.mutate = lambda: (repo / "untracked").write_text("unexpected")
    elif mutation == "stale":
        original = reviewer.wait_response
        async def stale(sent):
            response = await original(sent)
            return replace(response, text=response.text.replace(request.candidate_sha, "a" * 40))
        reviewer.wait_response = stale
    else:
        async def malformed(sent):
            return AssistantResponse(sent.review_key, sent.conversation_url, "PASS without a control block", "now", "later", "new-a", False)
        reviewer.wait_response = malformed
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer)
    sent = asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    with pytest.raises(Exception) as error:
        asyncio.run(bridge.capture_review(published, sent))
    if mutation == "local":
        assert error.value.code == "CANDIDATE_MUTATED_DURING_REVIEW"
    assert bridge._load(sent.review_key)["status"] == "INVALIDATED"
    assert bridge.state.get("p", "task").task_state is TaskState.PAUSED_ERROR
    if mutation == "local":
        (repo / "untracked").unlink()
    with pytest.raises(GitHubPublishError):
        asyncio.run(bridge.capture_review(published, sent))
    assert len(reviewer.calls) == 1
    bridge.close()
    publisher.close()


@pytest.mark.parametrize("event", ["REVIEW_NOTIFICATION_PLANNED", "REVIEW_NOTIFICATION_STARTED", "REVIEW_NOTIFICATION_SENT"])
def test_notification_restart_never_blindly_resends(setup, event):
    root, config, _, _, _, make, candidate = setup
    publisher = bind(make)
    published = asyncio.run(publisher.publish(candidate()))
    reviewer = FakeReviewer()
    def crash(name):
        if name == event:
            raise Crash()
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer, checkpoint_observer=crash)
    with pytest.raises(Crash):
        asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    bridge.close()
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=reviewer)
    if event == "REVIEW_NOTIFICATION_PLANNED":
        asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    else:
        with pytest.raises(GitHubPublishError):
            asyncio.run(bridge.notify_reviewer(published, conversation_url=URL))
    assert len(reviewer.calls) == (0 if event == "REVIEW_NOTIFICATION_STARTED" else 1)
    bridge.close()
    publisher.close()


def test_optional_gh_cli_uses_fixed_bounded_argv_and_reuses_pr():
    calls = []
    async def runner(argv, cwd, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1" and kwargs["timeout"] == 60
        return ProcessEvidence(argv, 0, .01, json.dumps([{"number": 1, "url": "https://github.com/o/r/pull/1",
            "headRefName": "reviewrelay/task", "baseRefName": "main", "state": "OPEN"}]).encode(), b"")
    service = GitHubCLI("repo", runner=runner)
    assert asyncio.run(service.find("o/r", "reviewrelay/task", "main"))["number"] == 1
    asyncio.run(service.create("o/r", "reviewrelay/task", "main", "task"))
    assert calls[0][:3] == ("gh", "pr", "list")
    assert calls[1][:3] == ("gh", "pr", "create") and "--draft" in calls[1]


def test_divergent_local_history_cannot_update_task_branch(setup):
    _, _, repo, _, runner, make, candidate = setup
    publisher = bind(make)
    request = candidate()
    asyncio.run(publisher.publish(request))
    git(repo, "reset", "--hard", request.base_sha)
    different = candidate("different history\n", 2)
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(different))
    assert error.value.code == "GITHUB_BRANCH_DIVERGED" and len(runner.pushes) == 1
    publisher.close()


@pytest.mark.parametrize("wrong", ["head", "fork"])
def test_pr_must_point_to_exact_same_repository_candidate(setup, wrong):
    _, _, _, _, _, _, candidate = setup
    pr = FakePR()
    publisher = pr_publisher(setup, pr)
    asyncio.run(publisher.bind_task("task"))
    request = candidate()
    asyncio.run(publisher.publish(request))
    original = pr.find
    async def incorrect(*args):
        row = dict(await original(*args))
        if wrong == "head":
            row["headRefOid"] = "a" * 40
        else:
            row["isCrossRepository"] = True
        return row
    pr.find = incorrect
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(publisher.publish(request))
    assert error.value.code == "GITHUB_PR_HEAD_MISMATCH" and pr.creates == 1
    publisher.close()
