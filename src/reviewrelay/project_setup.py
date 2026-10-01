"""Explicit Project setup operations, with durable external-effect checkpoints."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Protocol

from .config import GitHubConfig
from .evidence_process import run_bounded
from .models import utc_now_iso
from .project_git import ProjectGit
from .projects import ConnectionStatus as Status, ProjectError, ProjectKind, ProjectRegistry, github_identity
from .reviewer.base import BrowserBackend, ChatGPTWebSettings
from .reviewer.chatgpt_web import ChatGPTWebAdapter
from .reviewer.chrome_cdp import open_manual_auth_mode
from .worker.base import CodexWorkerSettings
from .worker.lock import WorkerTaskLock


@dataclass(frozen=True)
class GitHubRepository:
    url: str
    visibility: str
    default_branch: str | None = None
    git_url: str | None = None


class RepositoryService(Protocol):
    async def inspect(self, repository: str) -> GitHubRepository | None: ...
    async def create(self, repository: str, visibility: str) -> None: ...


class GitHubRepositoryCLI:
    """Official gh repository tooling; never invoke --source, --push, clone or token."""
    def __init__(self, cwd, *, runner=run_bounded, executable="gh", timeout=30):
        if not 0 < timeout <= 300:
            raise ValueError("GitHub timeout must be bounded")
        self.cwd, self.runner, self.executable, self.timeout = cwd, runner, executable, timeout

    async def _run(self, *args, missing_ok=False):
        result = await self.runner((self.executable, *args), str(self.cwd), timeout=self.timeout,
            stdout_cap=65536, stderr_cap=8192, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
        if result.launch_error:
            raise ProjectError("Install supported GitHub tooling for this operation", code="GITHUB_TOOLING_REQUIRED")
        if result.timed_out:
            raise ProjectError("GitHub operation timed out", code="GITHUB_TIMEOUT")
        if result.exit_code:
            auth = result.exit_code == 4 or any(t in result.stderr.lower() for t in (b"gh auth login", b"not logged", b"http 401", b"http 403"))
            if auth or args[:2] == ("auth", "status"):
                raise ProjectError("Authenticate supported GitHub tooling manually", code="GITHUB_AUTH_REQUIRED")
            if missing_ok and any(t in result.stderr.lower() for t in (b"could not resolve to a repository", b"not found", b"http 404")):
                return None
            raise ProjectError("GitHub request failed", code="GITHUB_SERVICE_FAILED")
        if result.stdout_truncated:
            raise ProjectError("GitHub response exceeded its limit", code="GITHUB_SERVICE_FAILED")
        return result.stdout.decode("utf-8", errors="strict")

    async def inspect(self, repository):
        repository = github_identity("https://github.com/" + repository).key
        await self._run("auth", "status", "--hostname", "github.com")
        raw = await self._run("repo", "view", repository, "--json", "nameWithOwner,url,visibility,defaultBranchRef,isArchived", missing_ok=True)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            identity = github_identity(data["url"])
            if identity.key != repository.lower() or data["nameWithOwner"].lower() != identity.key or data["visibility"] not in {"PUBLIC", "PRIVATE"}:
                raise ValueError()
            if data["isArchived"]:
                raise ProjectError("Repository is archived", code="GITHUB_REPOSITORY_READ_ONLY")
            branch = data["defaultBranchRef"]["name"] if data["defaultBranchRef"] else None
            return GitHubRepository(identity.url, data["visibility"], branch, identity.url + ".git")
        except (ValueError, TypeError, KeyError):
            raise ProjectError("GitHub returned an unexpected repository identity", code="GITHUB_IDENTITY_MISMATCH") from None

    async def create(self, repository, visibility):
        repository = github_identity("https://github.com/" + repository).key
        if visibility not in {"PUBLIC", "PRIVATE"}:
            raise ProjectError("Select PUBLIC or PRIVATE", code="GITHUB_VISIBILITY_REQUIRED")
        await self._run("auth", "status", "--hostname", "github.com")
        await self._run("repo", "create", repository, "--" + visibility.lower())


async def check_reviewer(root, settings):
    adapter = ChatGPTWebAdapter(root, settings)
    try:
        await adapter.start()
        await adapter.open_task_conversation(settings.conversation_url)
    finally:
        await adapter.close()


async def check_codex(path, settings, *, runner=run_bounded):
    executable = shutil.which(settings.executable)
    if executable is None:
        raise ProjectError("Select an installed Codex executable", code="CODEX_EXECUTABLE_NOT_FOUND")
    executable = str(Path(executable).resolve())
    if Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise ProjectError("Select the Codex executable rather than a shell wrapper", code="CODEX_RUNTIME_INCOMPATIBLE")
    async def call(*args):
        result = await runner((executable, *args), str(path), timeout=15, stdout_cap=8192, stderr_cap=8192, env=dict(os.environ))
        if result.launch_error:
            raise ProjectError("Codex executable could not start", code="CODEX_EXECUTABLE_NOT_FOUND")
        if result.timed_out or result.stdout_truncated:
            raise ProjectError("Codex capability check did not complete", code="CODEX_RUNTIME_UNAVAILABLE")
        return result
    version = await call("--version")
    help_result = await call("app-server", "--help")
    if version.exit_code or help_result.exit_code or b"--listen" not in help_result.stdout or b"stdio://" not in help_result.stdout:
        raise ProjectError("Selected Codex does not support the accepted app-server transport", code="CODEX_RUNTIME_INCOMPATIBLE")
    auth = await call("login", "status")
    if auth.exit_code:
        raise ProjectError("Authenticate Codex manually using its supported login", code="WORKER_AUTH_REQUIRED")
    return replace(settings, executable=executable)


class ProjectSetupService:
    def __init__(self, root, *, git=None, github=None, reviewer_probe=check_reviewer,
                 runtime_probe=check_codex, auth_probe=open_manual_auth_mode, allow_local_remote=False, checkpoint_observer=None):
        self.root = root.create()
        self.registry = ProjectRegistry(root)
        self.git = git or ProjectGit()
        self.github = github or GitHubRepositoryCLI(root.path)
        self.reviewer_probe, self.runtime_probe = reviewer_probe, runtime_probe
        self.auth_probe = auth_probe
        self.allow_local_remote, self.observer = allow_local_remote, checkpoint_observer

    def _lock(self, project_id):
        return WorkerTaskLock(self.root.safe_path(Path("config/project-locks") / (project_id + ".lock")))

    def _save(self, project, event, **changes):
        result = self.registry.save(replace(project, **changes), event=event)
        if self.observer:
            self.observer(event)
        return result

    def _error(self, project_id, exc, component="github"):
        project = self.registry.get(project_id)
        code = getattr(exc, "code", "PROJECT_SETUP_FAILED")
        self._save(project, "PROJECT_SETUP_STOPPED", last_error_code=code,
            **{component + "_status": Status.NEEDS_OWNER})

    async def register(self, name, path, kind, *, branch="main"):
        project = self.registry.create(name, path, kind, branch=branch)
        if project.kind is ProjectKind.NEW:
            return await self.initialize_local(project.project_id, confirmed=True)
        return await self.inspect_local(project.project_id)

    async def inspect_local(self, project_id):
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            discovery = await self.git.discover(project.local_repo_path)
            ready = discovery.is_git and discovery.head and discovery.clean and discovery.branch
            return self._save(project, "LOCAL_REPOSITORY_INSPECTED", local_status=Status.READY if ready else Status.NEEDS_OWNER,
                last_error_code=None if ready else "LOCAL_REPOSITORY_SETUP_REQUIRED")
        finally:
            lock.close()

    async def initialize_local(self, project_id, *, confirmed=False):
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            if confirmed is not True:
                raise ProjectError("Confirm Git initialization", code="GIT_INITIALIZATION_CONFIRMATION_REQUIRED")
            folder = Path(project.local_repo_path)
            if project.kind is ProjectKind.NEW and folder.exists() and any(p.name != ".git" for p in folder.iterdir()):
                raise ProjectError("Use Existing Project for a populated folder", code="NEW_PROJECT_NOT_EMPTY")
            discovery = await self.git.initialize(folder, project.initial_branch)
            if project.kind is ProjectKind.NEW and not discovery.head:
                plan = await self.git.snapshot_plan(project_id, folder)
                if plan.files:
                    raise ProjectError("A new empty project must not contain source", code="NEW_PROJECT_NOT_EMPTY")
                discovery = await self.git.commit_snapshot(plan, confirmed=True)
            return self._save(project, "LOCAL_REPOSITORY_INITIALIZED", local_status=Status.READY if discovery.head and discovery.clean else Status.NEEDS_OWNER,
                last_error_code=None if discovery.head else "INITIAL_SNAPSHOT_CONFIRMATION_REQUIRED")
        except Exception as exc:
            self._error(project_id, exc, "local")
            raise
        finally:
            lock.close()

    async def preview_snapshot(self, project_id):
        project = self.registry.get(project_id)
        return await self.git.snapshot_plan(project_id, project.local_repo_path)

    async def create_snapshot(self, plan, *, confirmed=False):
        lock = self._lock(plan.project_id)
        try:
            project = self.registry.get(plan.project_id)
            if str(Path(project.local_repo_path).resolve()) != plan.path:
                raise ProjectError("Snapshot does not belong to this Project", code="INITIAL_SNAPSHOT_CHANGED")
            discovery = await self.git.commit_snapshot(plan, confirmed=confirmed)
            return self._save(project, "INITIAL_SNAPSHOT_COMMITTED", local_status=Status.READY if discovery.clean else Status.NEEDS_OWNER, last_error_code=None)
        except Exception as exc:
            self._error(plan.project_id, exc, "local")
            raise
        finally:
            lock.close()

    async def detect_remotes(self, project_id):
        return (await self.git.discover(self.registry.get(project_id).local_repo_path)).remotes

    async def _local_candidate(self, project):
        discovery = await self.git.discover(project.local_repo_path)
        if not discovery.is_git or not discovery.head or not discovery.clean or not discovery.branch:
            raise ProjectError("A clean committed local branch is required", code="LOCAL_REPOSITORY_NOT_READY")
        return discovery

    def _transport(self, info):
        identity = github_identity(info.url)
        url = info.git_url or identity.url + ".git"
        if self.allow_local_remote and Path(url).is_dir():
            return str(Path(url).resolve())
        if github_identity(url).key != identity.key:
            raise ProjectError("Repository transport identity differs", code="GITHUB_IDENTITY_MISMATCH")
        return url

    async def bind_github(self, project_id, repository_url, *, action="link", visibility=None,
                          remote_name="origin", review_mode="branch", public_confirmed=False, credential_paths=()):
        identity = github_identity(repository_url)
        GitHubConfig(True, remote_name, "main", review_mode)
        if action not in {"create", "link"} or (action == "create" and visibility not in {"PUBLIC", "PRIVATE"}):
            raise ProjectError("Choose create/link and explicit visibility", code="GITHUB_VISIBILITY_REQUIRED")
        for path in credential_paths:
            if not isinstance(path, str) or "\x00" in path or PureWindowsPath(path).anchor or Path(path).is_absolute() or ".." in path.replace("\\", "/").split("/"):
                raise ProjectError("Sensitive paths must be repository relative", code="PROJECT_CREDENTIAL_PATH_INVALID")
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            if project.setup.get("verified_local_sha"):
                if (github_identity(project.github_repo_url).key != identity.key or project.github_remote_name != remote_name
                        or project.review_mode != review_mode):
                    raise ProjectError("A verified Project binding cannot silently change", code="GITHUB_SETUP_BINDING_MISMATCH")
                return await self._verify_bound_project(project)
            local = await self._local_candidate(project)
            intent = dict(action=action, repository=identity.key, visibility=visibility if action == "create" else None,
                remote=remote_name, review_mode=review_mode, local_sha=local.head, local_branch=local.branch)
            setup = dict(project.setup)
            if project.repository_ready and github_identity(project.github_repo_url).key != identity.key:
                raise ProjectError("A verified Project repository cannot silently change", code="GITHUB_SETUP_BINDING_MISMATCH")
            previous = setup.get("intent")
            if previous and previous != intent:
                if setup.get("create_status") in {"IN_FLIGHT", "AMBIGUOUS", "CONFIRMED"} or setup.get("remote_status") == "CONFIRMED" or setup.get("push_status"):
                    raise ProjectError("Resolve the persisted setup intent before changing it", code="GITHUB_SETUP_BINDING_MISMATCH")
                setup = {}
            setup["intent"] = intent
            if public_confirmed is True:
                setup["public_confirmed"] = True
            project = self._save(project, "GITHUB_SETUP_PLANNED", setup=setup, github_status=Status.SETTING_UP,
                github_owner=identity.owner, github_repo_name=identity.name, github_repo_url=identity.url,
                github_remote_name=remote_name, review_mode=review_mode, credential_paths=tuple(credential_paths), last_error_code=None)
            if action == "create" and visibility == "PUBLIC":
                if not setup.get("public_confirmed"):
                    raise ProjectError("This repository and its committed source will be publicly accessible.", code="PUBLIC_CONFIRMATION_REQUIRED")
                risks = await self.git.publication_risks(project.local_repo_path, local.head, credential_paths)
                if risks:
                    self._save(project, "PUBLICATION_RISK_DETECTED", setup={**setup, "risky_paths": risks})
                    raise ProjectError("Obvious sensitive paths require Owner resolution before public publication", code="PUBLICATION_RISK_REQUIRES_OWNER")
            info = await self.github.inspect(identity.key)
            if action == "create":
                stage = setup.get("create_status")
                if info is not None and stage not in {"IN_FLIGHT", "AMBIGUOUS", "CONFIRMED"}:
                    raise ProjectError("Repository already exists; choose Link Existing", code="GITHUB_REPO_ALREADY_EXISTS")
                if info is None:
                    if stage in {"IN_FLIGHT", "AMBIGUOUS", "CONFIRMED"}:
                        raise ProjectError("Creation outcome is ambiguous; resolve it without retrying", code="GITHUB_CREATE_AMBIGUOUS")
                    setup["create_status"] = "PLANNED"
                    project = self._save(project, "GITHUB_REPOSITORY_CREATE_PLANNED", setup=dict(setup))
                    setup["create_status"] = "IN_FLIGHT"
                    project = self._save(project, "GITHUB_REPOSITORY_CREATE_STARTED", setup=dict(setup))
                    try:
                        await self.github.create(identity.key, visibility)
                    except Exception:
                        setup["create_status"] = "AMBIGUOUS"
                        self._save(project, "GITHUB_REPOSITORY_CREATE_AMBIGUOUS", setup=dict(setup))
                        raise
                    setup["create_status"] = "CONFIRMED"
                    project = self._save(project, "GITHUB_REPOSITORY_CREATE_CONFIRMED", setup=dict(setup))
                    info = await self.github.inspect(identity.key)
                if info is None or info.visibility != visibility:
                    raise ProjectError("Created repository visibility cannot be proven", code="GITHUB_CREATE_AMBIGUOUS")
                setup["create_status"] = "CONFIRMED"
                project = self._save(project, "GITHUB_REPOSITORY_RECONCILED", setup=dict(setup))
            if info is None or github_identity(info.url).key != identity.key or info.visibility not in {"PUBLIC", "PRIVATE"}:
                raise ProjectError("GitHub repository identity is unavailable", code="GITHUB_IDENTITY_MISMATCH")
            transport = self._transport(info)
            branch = info.default_branch or local.branch
            GitHubConfig(True, remote_name, branch, review_mode)
            existing = (await self.git.text(project.local_repo_path, "remote")).splitlines()
            if remote_name in existing:
                fetch = (await self.git.text(project.local_repo_path, "remote", "get-url", "--all", remote_name)).splitlines()
                push = (await self.git.text(project.local_repo_path, "remote", "get-url", "--push", "--all", remote_name)).splitlines()
                if len(fetch) != 1 or len(push) != 1:
                    raise ProjectError("Select a remote with one fetch/push destination", code="GITHUB_REMOTE_CONFLICT")
                if self.allow_local_remote and fetch == push == [transport]:
                    pass
                elif github_identity(fetch[0]).key != identity.key or github_identity(push[0]).key != identity.key:
                    raise ProjectError("Remote already belongs to another repository; select a new remote name", code="GITHUB_REMOTE_CONFLICT")
                transport = push[0]
            if project.github_git_url and setup.get("remote_status") == "CONFIRMED" and project.github_git_url != transport:
                raise ProjectError("Persisted remote transport changed", code="GITHUB_SETUP_BINDING_MISMATCH")
            relation, remote_sha = await self.git.relationship(project.local_repo_path, transport, branch, local.head)
            if info.visibility == "PUBLIC" and relation in {"REMOTE_EMPTY", "LOCAL_AHEAD"}:
                if not setup.get("public_confirmed"):
                    raise ProjectError("This repository and its committed source will be publicly accessible.", code="PUBLIC_CONFIRMATION_REQUIRED")
                risks = await self.git.publication_risks(project.local_repo_path, local.head, credential_paths)
                if risks:
                    self._save(project, "PUBLICATION_RISK_DETECTED", setup={**setup, "risky_paths": risks})
                    raise ProjectError("Resolve obvious sensitive paths before publication", code="PUBLICATION_RISK_REQUIRES_OWNER")
            project = self._save(project, "GITHUB_REPOSITORY_IDENTITY_VERIFIED", github_visibility=info.visibility,
                github_default_branch=branch, github_git_url=transport, setup={**setup, "relationship": relation})
            setup = dict(project.setup)
            if remote_name not in existing:
                setup["remote_status"] = "PLANNED"
                project = self._save(project, "GITHUB_REMOTE_ADD_PLANNED", setup=dict(setup))
                await self.git.text(project.local_repo_path, "remote", "add", remote_name, transport)
            setup["remote_status"] = "CONFIRMED"
            project = self._save(project, "GITHUB_REMOTE_ADDED", setup=dict(setup))
            if setup.get("push_status") in {"IN_FLIGHT", "AMBIGUOUS", "CONFIRMED"}:
                if remote_sha != local.head:
                    raise ProjectError("Remote SHA does not prove the pending push; no retry", code="GITHUB_PUSH_AMBIGUOUS")
            elif relation in {"REMOTE_EMPTY", "LOCAL_AHEAD"}:
                setup.update(push_status="PLANNED", previous_remote_sha=remote_sha, local_sha=local.head)
                project = self._save(project, "PROJECT_INITIAL_PUSH_PLANNED", setup=dict(setup))
                fresh = await self._local_candidate(project)
                if fresh.head != local.head or await self.git.remote_sha(project.local_repo_path, transport, branch) != remote_sha:
                    raise ProjectError("Candidate or remote changed before publication", code="REMOTE_CANDIDATE_MISMATCH")
                setup["push_status"] = "IN_FLIGHT"
                project = self._save(project, "PROJECT_INITIAL_PUSH_STARTED", setup=dict(setup))
                try:
                    await self.git.text(project.local_repo_path, "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
                        "push", "--porcelain", transport, local.head + ":refs/heads/" + branch)
                except Exception:
                    setup["push_status"] = "AMBIGUOUS"
                    self._save(project, "PROJECT_INITIAL_PUSH_AMBIGUOUS", setup=dict(setup))
                    raise
                setup["push_status"] = "CONFIRMED"
                project = self._save(project, "PROJECT_INITIAL_PUSH_CONFIRMED", setup=dict(setup))
                remote_sha = await self.git.remote_sha(project.local_repo_path, transport, branch)
                if remote_sha != local.head:
                    raise ProjectError("Remote SHA differs from the exact local candidate", code="REMOTE_CANDIDATE_MISMATCH")
            fresh = await self._local_candidate(project)
            if fresh.head != local.head:
                raise ProjectError("Local candidate changed during setup", code="CANDIDATE_MUTATED_DURING_REVIEW")
            verified = await self.git.remote_sha(project.local_repo_path, transport, branch)
            if verified != remote_sha:
                raise ProjectError("Remote candidate changed during setup", code="REMOTE_CANDIDATE_MISMATCH")
            setup.update(verified_local_sha=local.head, verified_remote_sha=verified, relationship="MATCHING" if local.head == verified else relation)
            return self._save(project, "PROJECT_GITHUB_BOUND", setup=dict(setup), local_status=Status.READY,
                github_status=Status.READY, github_last_verified_at=utc_now_iso(), last_error_code=None)
        except Exception as exc:
            self._error(project_id, exc)
            raise
        finally:
            lock.close()

    async def _require_repository(self, project):
        if not project.repository_ready:
            raise ProjectError("Verify local ↔ GitHub before connecting reviewer or worker", code="PROJECT_GITHUB_REQUIRED")
        return await self._verify_bound_project(project)

    async def _verify_bound_project(self, project):
        if not project.github_repo_url or not project.github_last_verified_at:
            raise ProjectError("Complete initial GitHub binding first", code="PROJECT_GITHUB_REQUIRED")
        local = await self._local_candidate(project)
        fetch_urls = (await self.git.text(project.local_repo_path, "remote", "get-url", "--all", project.github_remote_name)).splitlines()
        push_urls = (await self.git.text(project.local_repo_path, "remote", "get-url", "--push", "--all", project.github_remote_name)).splitlines()
        if len(fetch_urls) != 1 or len(push_urls) != 1 or push_urls[0] != project.github_git_url:
            raise ProjectError("Registered remote destination changed", code="GITHUB_SETUP_BINDING_MISMATCH")
        push = push_urls[0]
        if not (self.allow_local_remote and fetch_urls[0] == push and Path(push).is_dir()):
            if github_identity(fetch_urls[0]).key != github_identity(project.github_repo_url).key:
                raise ProjectError("Registered fetch destination changed", code="GITHUB_SETUP_BINDING_MISMATCH")
        info = await self.github.inspect(github_identity(project.github_repo_url).key)
        if info is None or github_identity(info.url).key != github_identity(project.github_repo_url).key:
            raise ProjectError("Registered GitHub repository is unavailable", code="GITHUB_IDENTITY_MISMATCH")
        if info.visibility != project.github_visibility:
            raise ProjectError("Repository visibility changed; Owner resolution is required", code="GITHUB_VISIBILITY_CHANGED")
        relationship, remote = await self.git.relationship(project.local_repo_path, push, project.github_default_branch, local.head)
        if relationship == "REMOTE_EMPTY":
            raise ProjectError("Previously bound remote branch is missing", code="REMOTE_CANDIDATE_MISMATCH")
        return self._save(project, "PROJECT_GITHUB_REVERIFIED", local_status=Status.READY, github_status=Status.READY,
            github_last_verified_at=utc_now_iso(), last_error_code=None,
            setup={**project.setup, "verified_local_sha": local.head, "verified_remote_sha": remote, "relationship": relationship})

    async def verify_github(self, project_id):
        lock = self._lock(project_id)
        try:
            return await self._verify_bound_project(self.registry.get(project_id))
        except Exception as exc:
            self._error(project_id, exc)
            raise
        finally:
            lock.close()

    async def open_reviewer_auth(self, project_id, conversation_url, *, browser_profile="reviewer-chrome"):
        settings = ChatGPTWebSettings(conversation_url=conversation_url, browser_profile=browser_profile, browser_backend=BrowserBackend.GOOGLE_CHROME_CDP)
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            project = await self._require_repository(project)
            self._save(project, "PROJECT_MANUAL_AUTH_STARTED", chatgpt_status=Status.SETTING_UP,
                chatgpt_conversation_url=settings.conversation_url, reviewer_settings={"browser_profile": settings.browser_profile,
                "browser_backend": settings.browser_backend.value})
            await self.auth_probe(self.root, settings)
            return self._save(self.registry.get(project_id), "PROJECT_MANUAL_AUTH_CLOSED", chatgpt_status=Status.NOT_CONFIGURED)
        except Exception as exc:
            self._error(project_id, exc, "chatgpt")
            raise
        finally:
            lock.close()

    async def connect_reviewer(self, project_id, conversation_url, *, browser_profile="reviewer-chrome"):
        settings = ChatGPTWebSettings(conversation_url=conversation_url, browser_profile=browser_profile, browser_backend=BrowserBackend.GOOGLE_CHROME_CDP)
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            project = await self._require_repository(project)
            self._save(project, "PROJECT_REVIEWER_CHECK_STARTED", chatgpt_status=Status.SETTING_UP,
                chatgpt_conversation_url=settings.conversation_url, reviewer_settings={"browser_profile": settings.browser_profile,
                "browser_backend": settings.browser_backend.value})
            await self.reviewer_probe(self.root, settings)
            project = self.registry.get(project_id)
            project = await self._require_repository(project)
            return self._save(project, "PROJECT_REVIEWER_READY", chatgpt_status=Status.READY, last_error_code=None)
        except Exception as exc:
            self._error(project_id, exc, "chatgpt")
            raise
        finally:
            lock.close()

    async def connect_codex(self, project_id, *, executable, model=None, reasoning_effort=None):
        settings = CodexWorkerSettings(executable=executable, model=model, reasoning_effort=reasoning_effort)
        lock = self._lock(project_id)
        try:
            project = self.registry.get(project_id)
            project = await self._require_repository(project)
            self._save(project, "PROJECT_CODEX_CHECK_STARTED", codex_status=Status.SETTING_UP,
                worker_settings={"executable": settings.executable, "model": model, "reasoning_effort": reasoning_effort})
            checked = await self.runtime_probe(project.local_repo_path, settings)
            project = self.registry.get(project_id)
            project = await self._require_repository(project)
            return self._save(project, "PROJECT_CODEX_RUNTIME_READY", codex_status=Status.READY,
                worker_settings={"executable": checked.executable, "model": checked.model, "reasoning_effort": checked.reasoning_effort}, last_error_code=None)
        except Exception as exc:
            self._error(project_id, exc, "codex")
            raise
        finally:
            lock.close()

    def close(self):
        self.registry.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
