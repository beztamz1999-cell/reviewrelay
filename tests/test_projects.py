from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from reviewrelay.evidence_process import ProcessEvidence, run_bounded
from reviewrelay.github_publish import GitHubCandidatePublisher, GitHubPublishError, PublishRequest, task_spec_path
from reviewrelay.github_review import GitHubReviewBridge, review_notification
from reviewrelay.project_git import ProjectGit
from reviewrelay.project_setup import (GitHubRepository, GitHubRepositoryCLI, ProjectSetupService,
                                      check_codex, check_reviewer)
from reviewrelay.projects import (ConnectionStatus as Status, Project, ProjectError, ProjectKind,
                                 ProjectRegistry, github_identity)
from reviewrelay.state import SCHEMA_VERSION, StateStore
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task import begin_task
from reviewrelay.worker.base import CodexWorkerSettings
from reviewrelay.reviewer.base import SendResult, TurnBaseline


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, shell=False, capture_output=True, text=True, check=True).stdout.strip()


def run(awaitable):
    return asyncio.run(awaitable)


class Crash(BaseException):
    pass


class FakeGitHub:
    def __init__(self, remote):
        self.remote, self.rows, self.creates = str(remote), {}, []
        self.fail_after_create = False
        self.error = None

    def seed(self, identity="owner/project", visibility="PRIVATE", branch="main"):
        self.rows[identity.lower()] = GitHubRepository("https://github.com/" + identity, visibility, branch, self.remote)

    async def inspect(self, identity):
        if self.error:
            raise self.error
        return self.rows.get(identity.lower())

    async def create(self, identity, visibility):
        self.creates.append((identity, visibility))
        self.seed(identity, visibility, None)
        if self.fail_after_create:
            raise Crash()


class RecordingGit:
    def __init__(self):
        self.calls = []
        self.crash_after_push = False
        self.push_failure = False
        self.mismatch = False

    async def __call__(self, argv, cwd, **kwargs):
        assert isinstance(argv, tuple)
        assert kwargs["timeout"] <= 300 and kwargs["stdout_cap"] <= 1024 * 1024
        self.calls.append(argv)
        if "push" in argv and self.push_failure:
            return ProcessEvidence(argv, 1, .01, b"", b"authentication failed: secret-never-logged")
        result = await run_bounded(argv, cwd, **kwargs)
        if "push" in argv and self.crash_after_push:
            raise Crash()
        if self.mismatch and "ls-remote" in argv and any("push" in call for call in self.calls) and "--refs" in argv:
            return replace(result, stdout=("a" * 40 + "\t" + argv[-1] + "\n").encode())
        return result

    @property
    def pushes(self):
        return [args for args in self.calls if "push" in args]


@pytest.fixture
def setup(tmp_path, git_repo, monkeypatch):
    # Only isolated test commit identity; no changes to the Owner's Git configuration.
    for key, value in {"GIT_AUTHOR_NAME": "ReviewRelay Tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
                       "GIT_COMMITTER_NAME": "ReviewRelay Tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid"}.items():
        monkeypatch.setenv(key, value)
    root = PortableDataRoot(tmp_path / "data").create()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    github = FakeGitHub(remote)
    recorder = RecordingGit()
    probes = []
    async def reviewer(root, settings):
        probes.append(("reviewer", settings))
    async def runtime(path, settings):
        probes.append(("runtime", settings))
        return replace(settings, executable=str(Path(sys.executable).resolve()))
    async def auth(root, settings):
        probes.append(("auth", settings))
    def service(**kwargs):
        return ProjectSetupService(root, git=ProjectGit(runner=recorder), github=github, allow_local_remote=True,
            reviewer_probe=reviewer, runtime_probe=runtime, auth_probe=auth, **kwargs)
    return root, git_repo, remote, github, recorder, probes, service


def existing(setup):
    _, repo, _, _, _, _, make = setup
    service = make()
    project = run(service.register("Project", str(repo), ProjectKind.EXISTING))
    return service, project


@pytest.mark.parametrize("value", ["https://github.com/Owner/Repo", "https://github.com/Owner/Repo.git",
    "git@github.com:Owner/Repo.git", "ssh://git@github.com/Owner/Repo.git", "ssh://git@github.com:22/Owner/Repo.git"])
def test_github_url_normalization(value):
    identity = github_identity(value)
    assert identity.url == "https://github.com/Owner/Repo" and identity.key == "owner/repo"


@pytest.mark.parametrize("value", ["https://evil.example/o/r", "http://github.com/o/r", "https://token@github.com/o/r",
    "https://github.com/o/r/tree/main", "https://github.com/o/r?token=secret", "https://github.com/o/r#fragment",
    "https://github.com:bad/o/r", "ssh://user@github.com/o/r", "git@github.com:../repo", "https://github.com/o/../repo"])
def test_invalid_github_identity_does_not_expose_credentials(value):
    with pytest.raises(ProjectError) as error:
        github_identity(value)
    assert error.value.code == "GITHUB_URL_INVALID" and "token" not in str(error.value)


def test_registry_persistence_rename_duplicates_and_unregister_preserves_source(setup, tmp_path):
    root, repo, remote, _, _, _, _ = setup
    with ProjectRegistry(root) as registry:
        project = registry.create("Before", str(repo), ProjectKind.EXISTING)
        updated = registry.rename(project.project_id, "After")
        assert updated.project_id == project.project_id
        with pytest.raises(ProjectError) as error:
            registry.create("Duplicate", str(repo / ".." / repo.name), ProjectKind.EXISTING)
        assert error.value.code == "PROJECT_REPO_ALREADY_REGISTERED"
        other = registry.create("Other", str(tmp_path / "other"), ProjectKind.NEW)
        registry.save(replace(updated, github_repo_url="https://github.com/Owner/Repo"))
        with pytest.raises(ProjectError) as error:
            registry.save(replace(other, github_repo_url="git@github.com:owner/repo.git"))
        assert error.value.code == "GITHUB_REPO_ALREADY_REGISTERED"
    with ProjectRegistry(root) as registry:
        assert registry.get(project.project_id).project_name == "After"
        assert len(registry.list()) == 2
        with pytest.raises(ProjectError):
            registry.unregister(project.project_id)
        registry.unregister(project.project_id, confirmed=True)
        assert len(registry.list()) == 1
    assert (repo / "base.txt").is_file() and (repo / ".git").is_dir() and remote.is_dir()


def test_new_empty_project_has_empty_commit_and_stays_not_ready(setup, tmp_path):
    root, _, _, _, recorder, _, make = setup
    with make() as service:
        project = run(service.register("Empty", str(tmp_path / "empty"), "NEW", branch="trunk"))
        assert project.local_status is Status.READY and not project.ready
        assert git(project.local_repo_path, "ls-tree", "--name-only", "HEAD") == ""
        assert git(project.local_repo_path, "branch", "--show-current") == "trunk"
        assert not any("push" in call for call in recorder.calls)
        with pytest.raises(ProjectError) as error:
            run(service.connect_codex(project.project_id, executable=sys.executable))
        assert error.value.code == "PROJECT_GITHUB_REQUIRED"
        with pytest.raises(ProjectError):
            begin_task(project.to_config(require_ready=False), "premature", root)


def test_new_populated_directory_is_not_snapshot_by_heuristic(setup):
    _, repo, _, _, recorder, _, make = setup
    before = git(repo, "rev-parse", "HEAD")
    with make() as service:
        with pytest.raises(ProjectError) as error:
            run(service.register("Wrong choice", str(repo), "NEW"))
        assert error.value.code == "NEW_PROJECT_NOT_EMPTY"
    assert git(repo, "rev-parse", "HEAD") == before and not recorder.pushes


def test_existing_non_git_initialization_and_snapshot_require_bound_confirmation(setup, tmp_path):
    _, _, _, _, recorder, _, make = setup
    folder = tmp_path / "populated"
    folder.mkdir()
    (folder / "source.txt").write_text("actual source")
    (folder / ".gitignore").write_text("ignored.txt\n")
    (folder / "ignored.txt").write_text("leave untouched")
    with make() as service:
        project = run(service.register("Import", str(folder), "EXISTING"))
        assert not (folder / ".git").exists() and project.local_status is Status.NEEDS_OWNER
        with pytest.raises(ProjectError):
            run(service.initialize_local(project.project_id))
        run(service.initialize_local(project.project_id, confirmed=True))
        assert not git(folder, "log", "--all", "--oneline")
        plan = run(service.preview_snapshot(project.project_id))
        assert set(plan.files) == {"source.txt", ".gitignore"} and plan.ignored == ("ignored.txt",)
        with pytest.raises(ProjectError) as error:
            run(service.create_snapshot(plan))
        assert error.value.code == "INITIAL_SNAPSHOT_CONFIRMATION_REQUIRED"
        (folder / "source.txt").write_text("changed since confirmation")
        with pytest.raises(ProjectError) as error:
            run(service.create_snapshot(plan, confirmed=True))
        assert error.value.code == "INITIAL_SNAPSHOT_CHANGED"
        latest = run(service.preview_snapshot(project.project_id))
        committed = run(service.create_snapshot(latest, confirmed=True))
        assert committed.local_status is Status.READY
        assert "ignored.txt" not in git(folder, "ls-tree", "--name-only", "HEAD")
        assert (folder / "ignored.txt").read_text() == "leave untouched"
    assert not recorder.pushes


@pytest.mark.parametrize("transport", ["https://github.com/owner/project.git", "git@github.com:owner/project.git", "ssh://git@github.com/owner/project.git"])
def test_existing_remote_detection_is_only_a_proposal(setup, transport):
    _, repo, _, _, _, _, _ = setup
    git(repo, "remote", "add", "upstream", transport)
    with existing(setup)[0] as service:
        project = service.registry.list()[0]
        detected = run(service.detect_remotes(project.project_id))
        assert detected[0].repository_url == "https://github.com/owner/project"
        assert detected[0].name == "upstream" and project.github_status is Status.NOT_CONFIGURED


def test_private_create_exact_initial_push_and_local_end_to_end_project_task_smoke(setup, commit_change):
    root, _, remote, github, recorder, probes, make = setup
    repo = root.path.parent / "empty-e2e-project"
    service = make()
    project = run(service.register("Project", str(repo), "NEW"))
    assert git(repo, "ls-tree", "--name-only", "HEAD") == ""
    task = "TASK-101"
    bound = run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PRIVATE"))
    assert bound.repository_ready and not bound.ready and bound.github_repo_url == "https://github.com/owner/project"
    head = git(repo, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "refs/heads/main") == head
    assert len(recorder.pushes) == 1 and recorder.pushes[0][-1] == head + ":refs/heads/main"
    assert not any("force" in arg or arg == "HEAD" for call in recorder.pushes for arg in call)
    url = "https://chatgpt.com/c/project-fixture"
    run(service.open_reviewer_auth(project.project_id, url))
    assert service.registry.get(project.project_id).chatgpt_status is Status.NOT_CONFIGURED
    run(service.connect_reviewer(project.project_id, url))
    ready = run(service.connect_codex(project.project_id, executable=sys.executable, model="configured", reasoning_effort="low"))
    assert ready.ready and ready.status == "PROJECT_READY" and [p[0] for p in probes] == ["auth", "reviewer", "runtime"]
    assert ready.codex_worker_thread_id is None  # Runtime setup never creates or guesses a worker.
    config = ready.to_config()
    head = commit_change(repo, task_spec_path(task), "# Shared task\nImplement task result.\n")
    begin_task(config, task, root)
    publisher = GitHubCandidatePublisher(root, config, allow_local_remote=True)
    run(publisher.bind_task(task))
    candidate = commit_change(repo, "result.txt", "candidate\n")
    with StateStore(root) as state:
        state.save(replace(state.get(project.project_id, task), candidate_sha=candidate, review_cycle=1))
    published = run(publisher.publish(PublishRequest(task, head, candidate, 1)))
    assert git(remote, "rev-parse", "refs/heads/" + published.branch) == candidate
    prompt = review_notification(published)
    for value in ("PROJECT_ID=" + project.project_id, "PROJECT_NAME=Project", "REPO_URL=https://github.com/owner/project", "PR_URL=NONE", "HEAD_SHA=" + candidate, "TASK_SPEC_PATH=" + task_spec_path(task)):
        assert value in prompt
    assert "source attachments" not in prompt and len(prompt.encode()) < 3000
    sends = []
    class Reviewer:
        async def send_review_pack(self, **kwargs):
            sends.append(kwargs)
            return SendResult(kwargs["review_key"], kwargs["conversation_url"], "now", "review_pack", (),
                hashlib.sha256(kwargs["prompt"].encode()).hexdigest(), TurnBaseline((), (), 1), "owned-user")
    bridge = GitHubReviewBridge(root, config, publisher=publisher, reviewer=Reviewer())
    run(bridge.notify_reviewer(published, conversation_url=url))
    assert len(sends) == 1 and sends[0]["attachment_paths"] == ()
    bridge.close()
    service.registry.rename(project.project_id, "Renamed after task binding")
    run(publisher.verify_current(published))
    with StateStore(root) as state:
        assert state.get(project.project_id, task).worker_thread_id is None
    publisher.close()
    assert github.creates == [("owner/project", "PRIVATE")]
    service.close()


@pytest.mark.parametrize("changed_destination", ["https://github.com/other/repository.git", "ssh://git@github.com/other/repository.git", "bare"])
def test_project_remote_mutation_before_task_binding_is_rejected(setup, commit_change, tmp_path, changed_destination):
    root, repo, _, _, recorder, _, _ = setup
    service, project = existing(setup)
    with service:
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PRIVATE"))
        run(service.connect_reviewer(project.project_id, "https://chatgpt.com/c/project-test"))
        ready = run(service.connect_codex(project.project_id, executable=sys.executable))
        task = "remote-mutation"
        spec = repo / task_spec_path(task)
        spec.parent.mkdir(parents=True, exist_ok=True)
        commit_change(repo, str(spec.relative_to(repo)), "Implement the exact task contract.\n")
        config = ready.to_config()
        begin_task(config, task, root)
        if changed_destination == "bare":
            other = tmp_path / "different.git"
            git(tmp_path, "init", "--bare", str(other))
            changed_destination = str(other)
        git(repo, "remote", "set-url", "origin", changed_destination)
        before = len(recorder.pushes)
        publisher = GitHubCandidatePublisher(root, config, runner=recorder, allow_local_remote=True)
        try:
            with pytest.raises(GitHubPublishError) as error:
                run(publisher.bind_task(task))
            assert error.value.code == "GITHUB_BINDING_MISMATCH"
            assert publisher.load(task) is None
            assert len(recorder.pushes) == before
            assert not any("ls-remote" in call and changed_destination in call for call in recorder.calls)
        finally:
            publisher.close()


def test_manual_auth_helper_reuses_central_profile_lock_without_cdp(setup, monkeypatch):
    root, _, _, _, _, _, _ = setup
    from reviewrelay.reviewer import chrome_cdp
    from reviewrelay.reviewer.base import ChatGPTWebSettings
    settings = ChatGPTWebSettings(conversation_url="https://chatgpt.com/c/auth-fixture", browser_profile="shared-project-profile")
    calls = []
    monkeypatch.setattr(chrome_cdp, "find_google_chrome", lambda: root.path / "fake-chrome.exe")
    def launch(executable, profile, **kwargs):
        calls.append((profile, kwargs))
        assert profile == root.path / "browser-profile/shared-project-profile"
        assert kwargs == {"mode": chrome_cdp.ChromeMode.AUTH, "conversation_url": settings.conversation_url}
        with pytest.raises(chrome_cdp.BrowserProfileInUseError):
            chrome_cdp.ReviewRelayProfileLock(root, settings.browser_profile).acquire()
        return object()
    async def exited(process, timeout_seconds):
        return True
    monkeypatch.setattr(chrome_cdp, "launch_chrome", launch)
    monkeypatch.setattr(chrome_cdp, "wait_for_chrome_exit", exited)
    run(chrome_cdp.open_manual_auth_mode(root, settings))
    with chrome_cdp.ReviewRelayProfileLock(root, settings.browser_profile):
        pass
    assert len(calls) == 1


def test_verified_project_recheck_is_read_only_even_after_local_task_candidate(setup, commit_change):
    _, repo, _, github, recorder, _, _ = setup
    service, project = existing(setup)
    bound = run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PRIVATE"))
    count = len(recorder.pushes)
    commit_change(repo, "task-work.txt", "local task candidate")
    verified = run(service.verify_github(bound.project_id))
    assert verified.repository_ready and verified.setup["relationship"] == "LOCAL_AHEAD"
    assert run(service.bind_github(bound.project_id, bound.github_repo_url)).repository_ready
    assert len(recorder.pushes) == count and len(github.creates) == 1
    service.close()


def test_same_repository_https_fetch_ssh_push_survives_reverification(setup):
    root, repo, remote, github, recorder, _, _ = setup
    https = "https://github.com/owner/project.git"
    ssh = "git@github.com:owner/project.git"
    github.seed()
    github.rows["owner/project"] = replace(github.rows["owner/project"], git_url=https)
    git(repo, "remote", "add", "origin", https)
    git(repo, "remote", "set-url", "--push", "origin", ssh)
    async def transport(argv, cwd, **kwargs):
        # Real bare Git effects; only network transport URLs are mapped offline.
        if any(operation in argv for operation in ("ls-remote", "fetch", "push")):
            argv = tuple(str(remote) if argument in {https, ssh} else argument for argument in argv)
        return await recorder(argv, cwd, **kwargs)
    with ProjectSetupService(root, git=ProjectGit(runner=transport), github=github) as service:
        project = run(service.register("Mixed transport", str(repo), "EXISTING"))
        bound = run(service.bind_github(project.project_id, "https://github.com/owner/project"))
        assert bound.repository_ready and bound.github_git_url == ssh
        before = len(recorder.pushes)
        assert run(service.verify_github(project.project_id)).repository_ready
        assert len(recorder.pushes) == before
        git(repo, "remote", "set-url", "origin", "https://github.com/other/project.git")
        with pytest.raises(ProjectError) as error:
            run(service.verify_github(project.project_id))
        assert error.value.code == "GITHUB_SETUP_BINDING_MISMATCH"
        assert not service.registry.get(project.project_id).repository_ready
        assert len(recorder.pushes) == before


def test_candidate_mutation_after_planning_blocks_initial_push(setup, commit_change):
    _, repo, _, github, recorder, _, make = setup
    github.seed()
    def mutate(event):
        if event == "PROJECT_INITIAL_PUSH_PLANNED":
            commit_change(repo, "changed.txt", "changed while planning")
    with make(checkpoint_observer=mutate) as service:
        project = run(service.register("Import", str(repo), "EXISTING"))
        with pytest.raises(ProjectError) as error:
            run(service.bind_github(project.project_id, "https://github.com/owner/project"))
        assert error.value.code == "REMOTE_CANDIDATE_MISMATCH" and not recorder.pushes


def test_project_registration_forbids_thread_or_auth_settings_and_bad_names(setup):
    root, repo, _, _, _, _, _ = setup
    with ProjectRegistry(root) as registry:
        project = registry.create("Project", str(repo), "EXISTING")
        for values in ({"worker_settings": {"thread_id": "forbidden"}}, {"reviewer_settings": {"cookies": "forbidden"}},
                       {"project_name": "name\nHEAD_SHA=forged"}):
            with pytest.raises(ProjectError):
                registry.save(replace(project, **values))


def test_project_tasks_own_independent_worker_thread_ids(setup):
    root, _, _, github, _, _, _ = setup
    github.seed()
    service, project = existing(setup)
    run(service.bind_github(project.project_id, "https://github.com/owner/project"))
    run(service.connect_reviewer(project.project_id, "https://chatgpt.com/c/task-fixture"))
    ready = run(service.connect_codex(project.project_id, executable=sys.executable))
    config = ready.to_config()
    one = begin_task(config, "one", root)
    two = begin_task(config, "two", root)
    with StateStore(root) as state:
        state.save(replace(one, worker_thread_id="thread-one"))
        state.save(replace(two, worker_thread_id="thread-two"))
        assert state.get(project.project_id, "one").worker_thread_id != state.get(project.project_id, "two").worker_thread_id
    assert "worker_thread_id" not in asdict(service.registry.get(project.project_id))
    service.close()


@pytest.mark.parametrize("path", [".env", "config/.env.production", "keys/server.pem", "private.key", "id_rsa", "custom/credential.store"])
def test_public_confirmation_and_obvious_risks_before_repo_creation_or_push(setup, commit_change, path):
    _, repo, _, github, recorder, _, _ = setup
    commit_change(repo, path, "harmless test credential placeholder")
    service, project = existing(setup)
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PUBLIC"))
    assert error.value.code == "PUBLIC_CONFIRMATION_REQUIRED"
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PUBLIC", public_confirmed=True, credential_paths=("custom/credential.store",)))
    assert error.value.code == "PUBLICATION_RISK_REQUIRES_OWNER" and not github.creates and not recorder.pushes
    service.close()


def test_deleted_sensitive_paths_in_history_are_guarded(setup, commit_change):
    _, repo, _, github, recorder, _, _ = setup
    commit_change(repo, ".env", "harmless")
    git(repo, "rm", ".env")
    git(repo, "commit", "-m", "remove fixture")
    service, project = existing(setup)
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PUBLIC", public_confirmed=True))
    assert error.value.code == "PUBLICATION_RISK_REQUIRES_OWNER" and not github.creates and not recorder.pushes
    service.close()


def test_clean_public_creation_requires_explicit_visibility_and_confirmation(setup):
    _, _, _, github, _, _, _ = setup
    service, project = existing(setup)
    with pytest.raises(ProjectError):
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create"))
    assert not github.creates
    bound = run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PUBLIC", public_confirmed=True))
    assert bound.github_visibility == "PUBLIC" and bound.repository_ready
    service.close()


@pytest.mark.parametrize("case", ["empty", "matching", "local_ahead", "remote_ahead", "diverged", "unrelated"])
def test_link_existing_history_relationships_never_rewrite(setup, commit_change, tmp_path, case):
    _, repo, remote, github, recorder, _, _ = setup
    base = git(repo, "rev-parse", "HEAD")
    if case != "empty":
        git(repo, "push", str(remote), base + ":refs/heads/main")
    if case == "local_ahead":
        commit_change(repo, "new.txt", "local ahead")
    if case in {"remote_ahead", "diverged"}:
        clone = tmp_path / "clone"
        git(tmp_path, "clone", "--branch", "main", str(remote), str(clone))
        commit_change(clone, "remote.txt", "remote ahead")
        git(clone, "push", str(remote), "HEAD:refs/heads/main")
        if case == "diverged":
            commit_change(repo, "different.txt", "local divergence")
    if case == "unrelated":
        independent = tmp_path / "independent"
        independent.mkdir()
        git(independent, "init", "-b", "main")
        git(independent, "commit", "--allow-empty", "-m", "unrelated history")
        git(independent, "push", str(remote), "HEAD:refs/heads/unrelated")
        git(remote, "update-ref", "refs/heads/main", git(independent, "rev-parse", "HEAD"))
    github.seed()
    service, project = existing(setup)
    before = git(repo, "rev-parse", "HEAD")
    if case in {"diverged", "unrelated"}:
        with pytest.raises(ProjectError) as error:
            run(service.bind_github(project.project_id, "https://github.com/owner/project"))
        assert error.value.code == "GITHUB_HISTORY_" + case.upper()
        assert not recorder.pushes
    else:
        bound = run(service.bind_github(project.project_id, "https://github.com/owner/project"))
        assert bound.repository_ready and len(recorder.pushes) == (1 if case in {"empty", "local_ahead"} else 0)
    assert git(repo, "rev-parse", "HEAD") == before
    assert not any(arg in {"reset", "rebase", "merge", "--force", "-f"} for call in recorder.calls for arg in call)
    service.close()


@pytest.mark.parametrize("event", ["GITHUB_REPOSITORY_CREATE_PLANNED", "GITHUB_REPOSITORY_CREATE_STARTED", "GITHUB_REPOSITORY_CREATE_CONFIRMED", "GITHUB_REMOTE_ADD_PLANNED", "GITHUB_REMOTE_ADDED", "PROJECT_INITIAL_PUSH_PLANNED", "PROJECT_INITIAL_PUSH_STARTED", "PROJECT_INITIAL_PUSH_CONFIRMED"])
def test_setup_restart_checkpoints_no_duplicate_repos_or_uncertain_pushes(setup, event):
    _, repo, _, github, recorder, _, make = setup
    def crash(kind):
        if kind == event:
            raise Crash()
    service = make(checkpoint_observer=crash)
    project = run(service.register("Import", str(repo), "EXISTING"))
    kwargs = dict(action="create", visibility="PRIVATE")
    with pytest.raises(Crash):
        run(service.bind_github(project.project_id, "https://github.com/owner/project", **kwargs))
    service.close()
    with make() as restarted:
        if event in {"GITHUB_REPOSITORY_CREATE_STARTED", "PROJECT_INITIAL_PUSH_STARTED"}:
            with pytest.raises(ProjectError) as error:
                run(restarted.bind_github(project.project_id, "https://github.com/owner/project", **kwargs))
            assert error.value.code in {"GITHUB_CREATE_AMBIGUOUS", "GITHUB_PUSH_AMBIGUOUS"}
        else:
            assert run(restarted.bind_github(project.project_id, "https://github.com/owner/project", **kwargs)).repository_ready
    assert len(github.creates) <= 1 and len(recorder.pushes) <= 1


@pytest.mark.parametrize("boundary", ["repo", "push"])
def test_crash_after_success_before_ack_reconciles_without_repeating_effect(setup, boundary):
    _, _, _, github, recorder, _, make = setup
    service, project = existing(setup)
    github.fail_after_create = boundary == "repo"
    recorder.crash_after_push = boundary == "push"
    with pytest.raises(Crash):
        run(service.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PRIVATE"))
    service.close()
    with make() as restarted:
        assert run(restarted.bind_github(project.project_id, "https://github.com/owner/project", action="create", visibility="PRIVATE")).repository_ready
    assert len(github.creates) == 1 and len(recorder.pushes) == 1


def test_failed_push_auth_redacted_and_not_retried(setup):
    _, _, _, github, recorder, _, _ = setup
    github.seed()
    recorder.push_failure = True
    service, project = existing(setup)
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project"))
    assert error.value.code == "GITHUB_AUTH_REQUIRED" and "secret" not in str(error.value)
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project"))
    assert error.value.code == "GITHUB_PUSH_AMBIGUOUS" and len(recorder.pushes) == 1
    assert not service.registry.get(project.project_id).ready
    service.close()


def test_remote_mismatch_blocks_project_readiness(setup):
    _, _, _, github, recorder, _, _ = setup
    github.seed()
    service, project = existing(setup)
    recorder.mismatch = True
    with pytest.raises(ProjectError) as error:
        run(service.bind_github(project.project_id, "https://github.com/owner/project"))
    assert error.value.code == "REMOTE_CANDIDATE_MISMATCH" and not service.registry.get(project.project_id).repository_ready
    service.close()


def test_reviewer_runtime_gate_and_non_ready_failures(setup):
    _, _, _, github, _, probes, _ = setup
    service, project = existing(setup)
    with pytest.raises(ProjectError) as error:
        run(service.connect_reviewer(project.project_id, "https://chatgpt.com/c/fixture"))
    assert error.value.code == "PROJECT_GITHUB_REQUIRED" and not probes
    with pytest.raises(Exception):
        run(service.connect_reviewer(project.project_id, "https://evil.example/c/fixture"))
    github.seed()
    run(service.bind_github(project.project_id, "https://github.com/owner/project"))
    async def unavailable(*args):
        raise ProjectError("Fixture connection unavailable", code="LOGIN_REQUIRED")
    service.reviewer_probe = unavailable
    with pytest.raises(ProjectError):
        run(service.connect_reviewer(project.project_id, "https://chatgpt.com/c/fixture"))
    assert not service.registry.get(project.project_id).ready
    assert service.registry.get(project.project_id).chatgpt_status is Status.NEEDS_OWNER
    service.close()


@pytest.mark.parametrize("failure,code", [("missing", "CODEX_EXECUTABLE_NOT_FOUND"), ("old", "CODEX_RUNTIME_INCOMPATIBLE"), ("auth", "WORKER_AUTH_REQUIRED"), (None, None)])
def test_codex_runtime_local_capability_checks_no_threads_or_inference(tmp_path, failure, code):
    calls = []
    async def runner(argv, cwd, **kwargs):
        calls.append(argv)
        if argv[1:] == ("--version",):
            text, exit_code = b"codex-cli 0.159.2", 0
        elif argv[1:] == ("app-server", "--help"):
            text, exit_code = b"--listen stdio://" if failure != "old" else b"old server", 0
        else:
            assert argv[1:] == ("login", "status")
            text, exit_code = b"supported login status", 1 if failure == "auth" else 0
        return ProcessEvidence(argv, exit_code, .01, text, b"")
    executable = str(tmp_path / "missing-executable") if failure == "missing" else sys.executable
    if code:
        with pytest.raises(ProjectError) as error:
            run(check_codex(tmp_path, CodexWorkerSettings(executable=executable), runner=runner))
        assert error.value.code == code
    else:
        settings = run(check_codex(tmp_path, CodexWorkerSettings(executable=executable), runner=runner))
        assert Path(settings.executable).is_absolute()
    assert not any(arg.startswith("thread/") or arg in {"exec", "turn/start"} for call in calls for arg in call)


@pytest.mark.parametrize("failure,code", [("missing", "GITHUB_TOOLING_REQUIRED"), ("auth", "GITHUB_AUTH_REQUIRED"), (None, None)])
def test_supported_gh_creation_is_fixed_argv_without_implicit_push(tmp_path, failure, code):
    calls = []
    async def runner(argv, cwd, **kwargs):
        calls.append(argv)
        assert isinstance(argv, tuple) and kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        if failure == "missing":
            return ProcessEvidence(argv, None, .01, b"", b"", launch_error="missing")
        if failure == "auth":
            return ProcessEvidence(argv, 4, .01, b"", b"gh auth login: secret-never-logged")
        data = {"url": "https://github.com/owner/project", "nameWithOwner": "owner/project", "visibility": "PRIVATE", "defaultBranchRef": {"name": "main"}, "isArchived": False}
        return ProcessEvidence(argv, 0, .01, json.dumps(data).encode(), b"")
    client = GitHubRepositoryCLI(tmp_path, runner=runner)
    if code:
        with pytest.raises(ProjectError) as error:
            run(client.create("owner/project", "PRIVATE"))
        assert error.value.code == code and "secret" not in str(error.value)
    else:
        run(client.create("owner/project", "PRIVATE"))
        assert run(client.inspect("owner/project")).default_branch == "main"
        assert ("gh", "repo", "create", "owner/project", "--private") in calls
    assert not any(arg in {"--push", "--source", "--clone", "token"} for call in calls for arg in call)


def test_phase5_and_bridge_migration_preserves_tasks_and_publish_review_rows(setup, monkeypatch):
    root, repo, _, _, _, _, _ = setup
    from reviewrelay.config import ProjectConfig, RepoConfig
    old = replace(begin_task(ProjectConfig("legacy", RepoConfig(str(repo))), "task", root),
        worker_thread_id="kept-thread", worker_session_identity="kept-thread", worker_repo_path=str(repo), worker_last_turn_status="COMPLETED")
    with StateStore(root) as state:
        state.save(old)
        # A real Phase 5 schema has worker state but none of the later tables.
        db = state._connection
        for name in ("worker_owner_insert", "worker_owner_update"):
            db.execute("DROP TRIGGER " + name)
        db.execute("DROP TABLE worker_owners")
        db.execute("CREATE UNIQUE INDEX worker_thread_identity ON tasks(worker_thread_id) WHERE worker_thread_id IS NOT NULL")
        for name in ("projects", "project_events", "github_publications", "github_events", "github_reviews", "controller_tasks", "controller_effects", "controller_reviews", "controller_events"):
            db.execute("DROP TABLE " + name)
        db.execute("PRAGMA user_version=3")
        db.commit()
    with StateStore(root) as state:
        assert state.get("legacy", "task") == old
        assert state._connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
        state._connection.execute("INSERT INTO github_events(project_id,task_id,kind,payload_json,created_at) VALUES('legacy','task','saved','{}','now')")
        state._connection.execute("INSERT INTO github_publications VALUES('legacy','task','origin','main','reviewrelay/task',NULL,NULL,?,?, 'READY_TO_NOTIFY_REVIEWER','now','{}')", (old.base_sha, old.base_sha))
        state._connection.execute("INSERT INTO github_reviews VALUES('kept-review','legacy','task',?,1,'VALIDATED','{}','owned raw response','{}','now')", (old.base_sha,))
        for table in ("controller_tasks", "controller_effects", "controller_reviews", "controller_events"):
            state._connection.execute("DROP TABLE " + table)
        state._connection.execute("DROP TABLE projects")
        state._connection.execute("DROP TABLE project_events")
        for name in ("worker_owner_insert", "worker_owner_update"):
            state._connection.execute("DROP TRIGGER " + name)
        state._connection.execute("DROP TABLE worker_owners")
        state._connection.execute("CREATE UNIQUE INDEX worker_thread_identity ON tasks(worker_thread_id) WHERE worker_thread_id IS NOT NULL")
        state._connection.execute("PRAGMA user_version=4")
        state._connection.commit()
    original = StateStore._project_tables
    def fail(self):
        original(self)
        raise RuntimeError("migration failure")
    with monkeypatch.context() as patch:
        patch.setattr(StateStore, "_project_tables", fail)
        with pytest.raises(RuntimeError):
            StateStore(root)
    with sqlite3.connect(root.path / "db/relay.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchall()
    with StateStore(root) as state:
        assert state.get("legacy", "task") == old
        assert state._connection.execute("SELECT kind FROM github_events").fetchone()[0] == "saved"
        assert state._connection.execute("SELECT raw_text FROM github_reviews").fetchone()[0] == "owned raw response"
        assert state._connection.execute("SELECT github_last_remote_sha FROM github_publications").fetchone()[0] == old.base_sha


def test_default_reviewer_probe_only_opens_readiness_never_sends(setup, monkeypatch):
    root, _, _, _, _, _, _ = setup
    from reviewrelay.reviewer.base import ChatGPTWebSettings
    calls = []
    class Adapter:
        def __init__(self, *args):
            pass
        async def start(self):
            calls.append("start")
        async def open_task_conversation(self, url):
            calls.append(url)
        async def close(self):
            calls.append("close")
        async def send_review_pack(self, **kwargs):
            pytest.fail("Configuration must never send a message")
    monkeypatch.setattr("reviewrelay.project_setup.ChatGPTWebAdapter", Adapter)
    run(check_reviewer(root, ChatGPTWebSettings(conversation_url="https://chatgpt.com/c/fixture")))
    assert calls == ["start", "https://chatgpt.com/c/fixture", "close"]
