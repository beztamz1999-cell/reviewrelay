"""Exact-candidate publishing; local Git remains authoritative."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .config import ProjectConfig, validate_branch_name, validate_identifier
from .errors import CandidateInvalidDirtyWorktree, CandidateMutatedDuringReview, ReviewRelayError
from .evidence_process import run_bounded
from .git import GitClient
from .models import utc_now_iso
from .projects import Project, ProjectError, github_identity
from .state import StateStore
from .storage import PortableDataRoot, TaskStorage
from .worker.lock import WorkerTaskLock


class GitHubPublishError(ReviewRelayError):
    code = "GITHUB_PUBLISH_ERROR"


async def _git_command(runner, executable, cwd, timeout, args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0")
    result = await runner((executable, *args), cwd, timeout=timeout,
                          stdout_cap=1024 * 1024, stderr_cap=8192, env=env)
    if result.launch_error:
        raise GitHubPublishError("Git process could not start", code="GITHUB_PROCESS_FAILED")
    if result.timed_out:
        raise GitHubPublishError("Git request timed out", code="GITHUB_TIMEOUT")
    if result.exit_code or result.stdout_truncated:
        auth = any(term in result.stderr.lower() for term in (b"authentication failed", b"could not read username",
            b"permission denied", b"terminal prompts disabled", b"http 401", b"http 403", b"returned error: 401", b"returned error: 403"))
        raise GitHubPublishError("Git request failed", code="GITHUB_AUTH_REQUIRED" if auth else "GITHUB_PROCESS_FAILED")
    return result.stdout.decode("utf-8", errors="strict")


class _BoundedGitClient(GitClient):
    """Preserve Phase 1 verification, with the Phase 5 bounded process transport."""
    def __init__(self, runner, timeout):
        super().__init__()
        self.runner, self.timeout = runner, timeout

    def run(self, repository, *args):
        # Phase 1 synchronous verification runs on a worker thread, outside the caller's event loop.
        return asyncio.run(_git_command(self.runner, self.executable, str(repository), self.timeout, args))


def task_branch_name(task_id: str) -> str:
    validate_identifier(task_id, "task_id")
    slug = re.sub(r"[^A-Za-z0-9-]+", "-", task_id).strip("-")[:80] or "task"
    return validate_branch_name(f"reviewrelay/{slug}-{hashlib.sha256(task_id.encode()).hexdigest()[:10]}")


def task_spec_path(task_id: str) -> str:
    return f".reviewrelay/tasks/{validate_identifier(task_id, 'task_id')}.md"


@dataclass(frozen=True)
class PublishRequest:
    task_id: str
    base_sha: str
    candidate_sha: str
    review_cycle: int


@dataclass(frozen=True)
class PublishedCandidate:
    project_id: str
    task_id: str
    repository: str
    branch: str
    base_sha: str
    head_sha: str
    review_cycle: int
    task_spec_path: str
    pr_url: str | None = None
    pr_number: int | None = None
    project_name: str | None = None
    repository_url: str | None = None


class CandidatePublisher(Protocol):
    async def publish(self, request: PublishRequest) -> PublishedCandidate: ...
    async def reconcile(self, request: PublishRequest) -> PublishedCandidate: ...
    async def verify_current(self, candidate: PublishedCandidate) -> None: ...


class GitHubPRService(Protocol):
    async def find(self, repository: str, branch: str, base: str) -> dict | None: ...
    async def create(self, repository: str, branch: str, base: str, task_id: str) -> None: ...


class GitHubCLI:
    """Optional official gh CLI, restricted to one task PR."""
    def __init__(self, cwd: str, *, executable="gh", runner=run_bounded, timeout=60):
        self.cwd, self.executable, self.runner, self.timeout = cwd, executable, runner, timeout

    async def _run(self, *args: str) -> str:
        env = dict(os.environ, GH_PROMPT_DISABLED="1")
        result = await self.runner((self.executable, *args), self.cwd, timeout=self.timeout,
                                   stdout_cap=1024 * 1024, stderr_cap=8192, env=env)
        if result.launch_error:
            raise GitHubPublishError("Configured GitHub CLI is unavailable", code="GITHUB_CLI_UNAVAILABLE")
        if result.timed_out:
            raise GitHubPublishError("GitHub CLI timed out", code="GITHUB_TIMEOUT")
        if result.exit_code or result.stdout_truncated:
            auth = result.exit_code == 4 or any(term in result.stderr.lower() for term in (b"gh auth login", b"authentication", b"not logged", b"http 401"))
            raise GitHubPublishError("GitHub CLI request failed", code="GITHUB_AUTH_REQUIRED" if auth else "GITHUB_PR_FAILED")
        return result.stdout.decode("utf-8", errors="strict")

    async def find(self, repository, branch, base):
        raw = await self._run("pr", "list", "--repo", repository, "--head", branch, "--base", base,
            "--state", "all", "--limit", "100", "--json", "number,url,headRefName,baseRefName,state,headRefOid,isCrossRepository")
        try:
            rows = json.loads(raw)
            matches = [row for row in rows if row["headRefName"] == branch and row["baseRefName"] == base]
        except (ValueError, TypeError, KeyError) as exc:
            raise GitHubPublishError("Malformed PR discovery response") from exc
        if len(matches) > 1:
            raise GitHubPublishError("Multiple task PRs exist", code="GITHUB_PR_AMBIGUOUS")
        if matches and matches[0]["state"] != "OPEN":
            raise GitHubPublishError("Canonical task PR is already closed", code="GITHUB_PR_CLOSED")
        return matches[0] if matches else None

    async def create(self, repository, branch, base, task_id):
        await self._run("pr", "create", "--repo", repository, "--head", branch, "--base", base,
            "--draft", "--title", f"ReviewRelay: {task_id}", "--body",
            f"Review the exact candidate on this task branch against {task_spec_path(task_id)}. Local Git remains execution truth.")


class GitHubCandidatePublisher:
    def __init__(self, root: PortableDataRoot, config: ProjectConfig, *, git=None, runner=run_bounded,
                 pr_service: GitHubPRService | None = None, timeout=60, allow_local_remote=False,
                 checkpoint_observer: Callable[[str], None] | None = None):
        if not config.github.enabled:
            raise GitHubPublishError("GitHub publishing is disabled", code="GITHUB_DISABLED")
        if not config.repo.strict_commit_mode or not config.repo.require_clean_baseline:
            raise GitHubPublishError("Publishing requires strict clean committed candidates")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
            raise ValueError("Publish timeout must be bounded")
        self.root, self.config = root.create(), config
        self.git, self.runner, self.timeout = git or _BoundedGitClient(runner, timeout), runner, timeout
        self.pr = pr_service or (GitHubCLI(config.repo.path, runner=runner, timeout=timeout) if config.github.mode == "pr" else None)
        self.allow_local_remote, self.observer = allow_local_remote, checkpoint_observer
        self.state, self.storage = StateStore(root), TaskStorage(root)

    def load(self, task_id: str) -> dict | None:
        row = self.state._connection.execute("SELECT * FROM github_publications WHERE project_id=? AND task_id=?",
                                            (self.config.project_id, task_id)).fetchone()
        return {**dict(row), "metadata": json.loads(row["metadata_json"])} if row else None

    def _save(self, row: dict, event: str) -> None:
        names = ("project_id", "task_id", "github_remote", "github_base_branch", "github_task_branch", "github_pr_url",
                 "github_pr_number", "github_last_local_sha", "github_last_remote_sha", "github_publish_status", "github_published_at")
        with self.state._connection as db:
            db.execute("""INSERT INTO github_publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id,task_id) DO UPDATE SET
                github_pr_url=excluded.github_pr_url, github_pr_number=excluded.github_pr_number,
                github_last_local_sha=excluded.github_last_local_sha, github_last_remote_sha=excluded.github_last_remote_sha,
                github_publish_status=excluded.github_publish_status, github_published_at=excluded.github_published_at,
                metadata_json=excluded.metadata_json""", (*[row.get(name) for name in names], json.dumps(row["metadata"], sort_keys=True)))
            db.execute("INSERT INTO github_events(project_id,task_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                (self.config.project_id, row["task_id"], event, json.dumps({"local_sha": row.get("github_last_local_sha"),
                 "remote_sha": row.get("github_last_remote_sha"), "status": row["github_publish_status"],
                 "branch": row["github_task_branch"], "pr_url": row.get("github_pr_url")}), utc_now_iso()))
        if self.observer:
            self.observer(event)

    def _lock(self, task_id):
        return WorkerTaskLock(self.root.assert_managed_path(
            self.storage.task_root(self.config.project_id, task_id) / "durable" / "github.lock"))

    async def _git(self, *args):
        return (await _git_command(self.runner, self.git.executable, self.config.repo.path, self.timeout, args)).strip()

    async def _remote(self) -> tuple[str, str]:
        urls = (await self._git("remote", "get-url", "--push", "--all", self.config.github.remote)).splitlines()
        if len(urls) != 1:
            raise GitHubPublishError("Exactly one publish destination is required", code="GITHUB_REMOTE_INVALID")
        url = urls[0]
        project = self._project()
        try:
            identity = github_identity(url)
            if project and identity.key != github_identity(project.github_repo_url).key:
                raise GitHubPublishError("Publish destination differs from its Project", code="GITHUB_BINDING_MISMATCH")
            return url, f"{identity.owner}/{identity.name}"
        except ProjectError:
            pass
        if self.allow_local_remote and Path(url).is_dir() and self.config.github.mode == "branch":
            checked = Path(url).resolve()
            if project:
                if str(checked) != project.github_git_url:
                    raise GitHubPublishError("Publish destination differs from its Project", code="GITHUB_BINDING_MISMATCH")
                return str(checked), f"{project.github_owner}/{project.github_repo_name}"
            return str(checked), f"local/{checked.name}"
        raise GitHubPublishError("Publish remote must be a credential-free supported GitHub URL", code="GITHUB_REMOTE_INVALID")

    def _project(self):
        row = self.state._connection.execute("SELECT record_json FROM projects WHERE project_id=?", (self.config.project_id,)).fetchone()
        return Project.from_json(row[0]) if row else None

    def _verify_project(self, row=None):
        project = self._project()
        if row and row["metadata"].get("registered_project") and project is None:
            raise GitHubPublishError("Project registration was removed", code="PROJECT_NOT_FOUND")
        if project:
            expected = project.to_config()
            if (expected.github != self.config.github or expected.chatgpt != self.config.chatgpt
                    or expected.worker != self.config.worker or Path(expected.repo.path).resolve() != Path(self.config.repo.path).resolve()):
                raise GitHubPublishError("Task configuration differs from its Project", code="GITHUB_BINDING_MISMATCH")
            if row and (row["metadata"].get("project_repository_url") != project.github_repo_url):
                raise GitHubPublishError("Task Project binding changed", code="GITHUB_BINDING_MISMATCH")
        return project

    async def _remote_sha(self, url, branch):
        ref = "refs/heads/" + branch
        lines = (await self._git("ls-remote", "--refs", url, ref)).splitlines()
        if not lines:
            return None
        if len(lines) != 1 or not re.fullmatch(r"[0-9a-f]{40,64}\s+" + re.escape(ref), lines[0]):
            raise GitHubPublishError("Remote identity response is malformed", code="REMOTE_CANDIDATE_MISMATCH")
        return lines[0].split()[0]

    async def _spec_blob(self, sha, path):
        line = await self._git("ls-tree", sha, "--", path)
        match = re.fullmatch(r"100(?:644|755) blob ([0-9a-f]{40,64})\t" + re.escape(path), line)
        if not match:
            raise GitHubPublishError("A committed regular task spec is required", code="TASK_SPEC_REQUIRED")
        content = await self._git("show", f"{sha}:{path}")
        if not content.strip():
            raise GitHubPublishError("Task spec must not be empty", code="TASK_SPEC_REQUIRED")
        return match[1]

    async def bind_task(self, task_id: str, *, allow_spec_change: bool = False) -> None:
        """Call after Phase 1 begin_task and before the first worker turn."""
        validate_identifier(task_id, "task_id")
        if not isinstance(allow_spec_change, bool):
            raise ValueError("Spec-change intent must be explicit")
        lock = self._lock(task_id)
        try:
            if self.load(task_id):
                raise GitHubPublishError("Task is already bound", code="GITHUB_TASK_ALREADY_BOUND")
            project = self._verify_project()
            r = self.state.get(self.config.project_id, task_id)
            canonical_only = bool(r and project and project.codex_worker_thread_id
                and r.worker_thread_id == r.worker_session_identity == project.codex_worker_thread_id
                and r.worker_repo_path == project.codex_worker_repo_path
                and not r.worker_last_turn_id and not r.worker_last_turn_status)
            if r is None or (r.worker_thread_id and not canonical_only) or r.worker_last_turn_id or r.worker_last_turn_status or r.candidate_sha or r.review_cycle:
                raise GitHubPublishError("Bind the task spec before implementation", code="TASK_SPEC_BINDING_REQUIRED")
            actual = await asyncio.to_thread(self.git.verify_candidate, self.config.repo.path, strict_commit_mode=True)
            if actual.candidate_sha != r.base_sha:
                raise CandidateMutatedDuringReview("Baseline changed before task-spec binding")
            url, repository = await self._remote()
            path = task_spec_path(task_id)
            blob = await self._spec_blob(r.base_sha, path)
            row = {"project_id": self.config.project_id, "task_id": task_id, "github_remote": self.config.github.remote,
                "github_base_branch": self.config.github.base_branch, "github_task_branch": task_branch_name(task_id),
                "github_publish_status": "TASK_BOUND", "metadata": {"repo_path": str(Path(self.config.repo.path).resolve()),
                "remote_url": url, "repository": repository, "mode": self.config.github.mode, "base_sha": r.base_sha,
                "task_spec_path": path, "task_spec_blob": blob, "allow_spec_change": allow_spec_change,
                "registered_project": bool(project), "project_name": project.project_name if project else None,
                "project_repository_url": project.github_repo_url if project else None}}
            self._save(row, "TASK_SPEC_BOUND")
        finally:
            lock.close()

    async def _validate(self, request, row):
        self._verify_project(row)
        r = self.state.get(self.config.project_id, request.task_id)
        if (r is None or not row or r.base_sha != request.base_sha or r.candidate_sha != request.candidate_sha
                or r.review_cycle != request.review_cycle or request.review_cycle < 1
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", request.candidate_sha)):
            raise GitHubPublishError("Candidate identity does not match task state", code="GITHUB_BINDING_MISMATCH")
        meta = row["metadata"]
        if (row["github_remote"] != self.config.github.remote or row["github_base_branch"] != self.config.github.base_branch
                or meta["mode"] != self.config.github.mode or meta["base_sha"] != request.base_sha
                or meta["repo_path"] != str(Path(self.config.repo.path).resolve())):
            raise GitHubPublishError("Publish configuration changed", code="GITHUB_BINDING_MISMATCH")
        actual = await asyncio.to_thread(self.git.verify_candidate, self.config.repo.path, expected_repository=meta["repo_path"], strict_commit_mode=True)
        if actual.candidate_sha != request.candidate_sha:
            raise CandidateMutatedDuringReview("Local candidate changed")
        url, identity = await self._remote()
        if url != meta["remote_url"] or identity != meta["repository"]:
            raise GitHubPublishError("Publish destination changed", code="GITHUB_BINDING_MISMATCH")
        await self._git("merge-base", "--is-ancestor", request.base_sha, request.candidate_sha)
        blob = await self._spec_blob(request.candidate_sha, meta["task_spec_path"])
        if not meta["allow_spec_change"] and blob != meta["task_spec_blob"]:
            raise GitHubPublishError("Ordinary task requirements changed", code="TASK_SPEC_MUTATED")

    def _candidate(self, request, row):
        return PublishedCandidate(self.config.project_id, request.task_id, row["metadata"]["repository"],
            row["github_task_branch"], request.base_sha, request.candidate_sha, request.review_cycle,
            row["metadata"]["task_spec_path"], row.get("github_pr_url"), row.get("github_pr_number"),
            row["metadata"].get("project_name"), row["metadata"].get("project_repository_url"))

    async def _finish(self, request, row, *, allow_pr_creation=True):
        if self.config.github.mode == "pr":
            await self._ensure_pr(row, allow_creation=allow_pr_creation)
        await self._validate(request, row)
        if await self._remote_sha(row["metadata"]["remote_url"], row["github_task_branch"]) != request.candidate_sha:
            raise GitHubPublishError("Published candidate changed", code="REMOTE_CANDIDATE_MUTATED")
        row["github_publish_status"] = "READY_TO_NOTIFY_REVIEWER"
        self._save(row, "REVIEW_NOTIFICATION_READY")
        return self._candidate(request, row)

    async def _ensure_pr(self, row, *, allow_creation=True):
        repository, branch, base = row["metadata"]["repository"], row["github_task_branch"], row["github_base_branch"]
        found = await self.pr.find(repository, branch, base)
        if found is None:
            if row.get("github_pr_url") or row["metadata"].get("pr_status") in {"IN_FLIGHT", "AMBIGUOUS", "CONFIRMED"}:
                raise GitHubPublishError("Canonical PR cannot be reconciled; creation will not repeat", code="GITHUB_PR_AMBIGUOUS")
            if not allow_creation:
                raise GitHubPublishError("Read-only reconciliation found no canonical PR", code="GITHUB_PR_REQUIRED")
            row["metadata"]["pr_status"] = "PLANNED"
            self._save(row, "GITHUB_PR_PLANNED")
            row["metadata"]["pr_status"] = "IN_FLIGHT"
            self._save(row, "GITHUB_PR_STARTED")
            try:
                await self.pr.create(repository, branch, base, row["task_id"])
            except Exception:
                row["metadata"]["pr_status"] = "AMBIGUOUS"
                self._save(row, "GITHUB_PR_AMBIGUOUS")
                raise
            found = await self.pr.find(repository, branch, base)
            event = "PR_CREATED"
        else:
            event = "PR_REUSED"
        if (not found or not isinstance(found.get("number"), int) or isinstance(found.get("number"), bool)
                or found["number"] <= 0 or found.get("url") != f"https://github.com/{repository}/pull/{found['number']}"
                or found.get("headRefName") != branch or found.get("baseRefName") != base or found.get("state") != "OPEN"
                or (row.get("github_pr_url") and row["github_pr_url"] != found["url"])):
            raise GitHubPublishError("PR identity is not the canonical task PR", code="GITHUB_PR_AMBIGUOUS")
        if found.get("isCrossRepository") is not False or found.get("headRefOid") != row["github_last_local_sha"]:
            raise GitHubPublishError("PR HEAD is not the exact published task candidate", code="GITHUB_PR_HEAD_MISMATCH")
        row.update(github_pr_url=found["url"], github_pr_number=found["number"])
        row["metadata"]["pr_status"] = "CONFIRMED"
        self._save(row, event)

    async def publish(self, request: PublishRequest) -> PublishedCandidate:
        lock = self._lock(request.task_id)
        try:
            row = self.load(request.task_id)
            await self._validate(request, row)
            pending = row["github_publish_status"] in {"GITHUB_PUSH_IN_FLIGHT", "GITHUB_PUSH_AMBIGUOUS", "GITHUB_PUSH_CONFIRMED"}
            if pending:
                return await self._reconcile(request, row)
            if (row["github_publish_status"] == "GITHUB_PUSH_PLANNED"
                    and (row.get("github_last_local_sha") != request.candidate_sha or row["metadata"].get("review_cycle") != request.review_cycle)):
                raise GitHubPublishError("Planned push binding changed", code="GITHUB_BINDING_MISMATCH")
            remote = await self._remote_sha(row["metadata"]["remote_url"], row["github_task_branch"])
            if remote == request.candidate_sha:
                row.update(github_last_local_sha=request.candidate_sha, github_last_remote_sha=remote,
                           github_publish_status="REMOTE_SHA_VERIFIED", github_published_at=utc_now_iso())
                row["metadata"]["review_cycle"] = request.review_cycle
                self._save(row, "PUBLISH_ALREADY_CONFIRMED")
                return await self._finish(request, row)
            previous = row.get("github_last_remote_sha")
            if remote != previous:
                raise GitHubPublishError("Task branch changed outside this publisher", code="GITHUB_BRANCH_DIVERGED")
            if previous:
                try:
                    await self._git("merge-base", "--is-ancestor", previous, request.candidate_sha)
                except GitHubPublishError as exc:
                    raise GitHubPublishError("Task branch cannot advance without rewriting history", code="GITHUB_BRANCH_DIVERGED") from exc
            row.update(github_last_local_sha=request.candidate_sha, github_publish_status="LOCAL_CANDIDATE_READY")
            row["metadata"].update(review_cycle=request.review_cycle, previous_remote_sha=previous)
            self._save(row, "CANDIDATE_READY")
            row["github_publish_status"] = "GITHUB_PUSH_PLANNED"
            self._save(row, "GITHUB_PUSH_PLANNED")
            # Reverify immediately before dispatch; the refspec is the immutable SHA, never HEAD.
            await self._validate(request, row)
            row["github_publish_status"] = "GITHUB_PUSH_IN_FLIGHT"
            self._save(row, "GITHUB_PUSH_STARTED")
            try:
                await self._git("-c", "push.followTags=false", "-c", "push.recurseSubmodules=no", "push", "--porcelain",
                                row["metadata"]["remote_url"], f"{request.candidate_sha}:refs/heads/{row['github_task_branch']}")
            except Exception:
                row["github_publish_status"] = "GITHUB_PUSH_AMBIGUOUS"
                self._save(row, "GITHUB_PUSH_AMBIGUOUS")
                raise
            row["github_publish_status"] = "GITHUB_PUSH_CONFIRMED"
            self._save(row, "GITHUB_PUSH_CONFIRMED")
            return await self._reconcile(request, row)
        finally:
            lock.close()

    async def _reconcile(self, request, row, *, allow_pr_creation=True):
        if (row.get("github_last_local_sha") != request.candidate_sha
                or row["metadata"].get("review_cycle") != request.review_cycle):
            raise GitHubPublishError("An unresolved publication belongs to another candidate", code="GITHUB_PUSH_AMBIGUOUS")
        remote = await self._remote_sha(row["metadata"]["remote_url"], row["github_task_branch"])
        if remote != request.candidate_sha:
            code = "GITHUB_PUSH_AMBIGUOUS" if row["github_publish_status"] in {"GITHUB_PUSH_IN_FLIGHT", "GITHUB_PUSH_AMBIGUOUS"} else "REMOTE_CANDIDATE_MISMATCH"
            row["metadata"].update(error_code=code, observed_remote_sha=remote)
            self._save(row, code)
            raise GitHubPublishError("Remote SHA does not prove publication", code=code)
        row.update(github_last_remote_sha=remote, github_publish_status="REMOTE_SHA_VERIFIED", github_published_at=utc_now_iso())
        self._save(row, "REMOTE_SHA_VERIFIED")
        return await self._finish(request, row, allow_pr_creation=allow_pr_creation)

    async def reconcile(self, request: PublishRequest) -> PublishedCandidate:
        lock = self._lock(request.task_id)
        try:
            row = self.load(request.task_id)
            await self._validate(request, row)
            return await self._reconcile(request, row, allow_pr_creation=False)
        finally:
            lock.close()

    async def verify_current(self, candidate: PublishedCandidate) -> None:
        row = self.load(candidate.task_id)
        record = self.state.get(self.config.project_id, candidate.task_id)
        if record and (record.candidate_sha != candidate.head_sha or record.review_cycle != candidate.review_cycle):
            raise GitHubPublishError("Review belongs to an earlier candidate/cycle", code="STALE_REVIEW")
        request = PublishRequest(candidate.task_id, candidate.base_sha, candidate.head_sha, candidate.review_cycle)
        try:
            await self._validate(request, row)
        except CandidateInvalidDirtyWorktree:
            raise CandidateMutatedDuringReview("Local worktree changed during review") from None
        if row["github_publish_status"] != "READY_TO_NOTIFY_REVIEWER" or self._candidate(request, row) != candidate:
            raise GitHubPublishError("Notification candidate is not verified", code="GITHUB_BINDING_MISMATCH")
        if await self._remote_sha(row["metadata"]["remote_url"], row["github_task_branch"]) != candidate.head_sha:
            raise GitHubPublishError("Remote branch changed during review", code="REMOTE_CANDIDATE_MUTATED")

    def close(self):
        self.state.close()
