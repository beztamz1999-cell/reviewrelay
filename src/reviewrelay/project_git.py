"""Bounded local discovery, explicit initial snapshots and history checks."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import validate_branch_name
from .evidence_process import run_bounded
from .projects import ProjectError, github_identity


@dataclass(frozen=True)
class DetectedRemote:
    name: str
    git_url: str
    repository_url: str


@dataclass(frozen=True)
class LocalDiscovery:
    path: str
    is_git: bool
    head: str | None = None
    branch: str | None = None
    clean: bool = False
    remotes: tuple[DetectedRemote, ...] = ()


@dataclass(frozen=True)
class SnapshotPlan:
    project_id: str
    path: str
    files: tuple[str, ...]
    ignored: tuple[str, ...]
    digest: str


class ProjectGit:
    def __init__(self, *, runner=run_bounded, executable="git", timeout=30):
        if not 0 < timeout <= 300:
            raise ValueError("Git timeout must be bounded")
        self.runner, self.executable, self.timeout = runner, executable, timeout

    async def run(self, cwd, *args, allowed=(0,)):
        result = await self.runner((self.executable, *args), str(cwd), timeout=self.timeout,
            stdout_cap=1024 * 1024, stderr_cap=8192,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0"))
        if result.launch_error:
            raise ProjectError("Git is unavailable", code="GIT_TOOLING_REQUIRED")
        if result.timed_out:
            raise ProjectError("Git operation timed out", code="GITHUB_TIMEOUT")
        if result.exit_code not in allowed or result.stdout_truncated:
            auth = any(t in result.stderr.lower() for t in (b"authentication", b"could not read username", b"permission denied", b"http 401", b"http 403", b"returned error: 403"))
            identity = any(t in result.stderr.lower() for t in (b"author identity unknown", b"unable to auto-detect email"))
            code = "GITHUB_AUTH_REQUIRED" if auth else "GIT_IDENTITY_REQUIRED" if identity else "GIT_SETUP_FAILED"
            raise ProjectError("Git operation could not complete", code=code)
        return result.stdout.decode("utf-8", errors="strict"), result.exit_code

    async def text(self, cwd, *args):
        return (await self.run(cwd, *args))[0].strip()

    async def discover(self, path):
        path = Path(path).resolve()
        if not path.is_dir():
            return LocalDiscovery(str(path), False)
        root, code = await self.run(path, "rev-parse", "--show-toplevel", allowed=(0, 128))
        if code:
            return LocalDiscovery(str(path), False)
        if os.path.normcase(str(Path(root.strip()).resolve())) != os.path.normcase(str(path)):
            raise ProjectError("Select the Git repository root, not a nested folder", code="PROJECT_REPOSITORY_ROOT_REQUIRED")
        head, code = await self.run(path, "rev-parse", "--verify", "HEAD", allowed=(0, 128))
        branch_text, branch_code = await self.run(path, "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1))
        branch = branch_text.strip() if not branch_code else None
        # Detached HEAD is handled explicitly by the caller rather than inventing a branch.
        status = await self.text(path, "status", "--porcelain")
        remotes = []
        for name in (await self.text(path, "remote")).splitlines():
            try:
                urls = (await self.text(path, "remote", "get-url", "--all", name)).splitlines()
                push = (await self.text(path, "remote", "get-url", "--push", "--all", name)).splitlines()
                if len(urls) == len(push) == 1 and github_identity(urls[0]).key == github_identity(push[0]).key:
                    remotes.append(DetectedRemote(name, push[0], github_identity(push[0]).url))
            except ProjectError:
                continue
        return LocalDiscovery(str(path), True, head.strip() if not code else None, branch, not status, tuple(remotes))

    async def initialize(self, path, branch):
        validate_branch_name(branch)
        path = Path(path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        discovery = await self.discover(path)
        if not discovery.is_git:
            await self.text(path, "init", "--initial-branch=" + branch, "--", str(path))
        return await self.discover(path)

    async def snapshot_plan(self, project_id, path):
        discovery = await self.discover(path)
        if not discovery.is_git or discovery.head:
            raise ProjectError("Initial snapshot requires an initialized repository without HEAD", code="INITIAL_SNAPSHOT_NOT_APPLICABLE")
        if await self.text(path, "ls-files", "--cached"):
            raise ProjectError("Resolve the existing staged index before an initial snapshot", code="INITIAL_SNAPSHOT_INDEX_NOT_EMPTY")
        files = tuple(filter(None, (await self.run(path, "ls-files", "--others", "--exclude-standard", "-z"))[0].split("\0")))
        ignored = tuple(filter(None, (await self.run(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"))[0].split("\0")))
        if len(files) + len(ignored) > 10000:
            raise ProjectError("Initial snapshot inventory is too large", code="INITIAL_SNAPSHOT_LIMIT")
        return SnapshotPlan(project_id, str(Path(path).resolve()), files, ignored, self._snapshot_digest(path, files))

    def _snapshot_digest(self, path, files):
        digest = hashlib.sha256()
        root = Path(path).resolve()
        total = 0
        for name in sorted(files):
            source = root / name
            resolved = source.resolve()
            if not resolved.is_relative_to(root) or source.is_symlink() or not source.is_file():
                raise ProjectError("Snapshot contains an unsafe path", code="INITIAL_SNAPSHOT_UNSAFE_PATH")
            digest.update(json.dumps([name, source.stat().st_mode]).encode())
            if source.stat().st_size > 100 * 1024 * 1024:
                raise ProjectError("Snapshot file exceeds the inventory limit", code="INITIAL_SNAPSHOT_LIMIT")
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ProjectError("Snapshot inventory exceeds its byte limit", code="INITIAL_SNAPSHOT_LIMIT")
                    digest.update(chunk)
        return digest.hexdigest()

    async def commit_snapshot(self, plan, *, confirmed=False):
        if confirmed is not True:
            raise ProjectError("Review and confirm the initial snapshot", code="INITIAL_SNAPSHOT_CONFIRMATION_REQUIRED")
        current = await self.snapshot_plan(plan.project_id, plan.path)
        if current != plan:
            raise ProjectError("Files changed since the snapshot preview", code="INITIAL_SNAPSHOT_CHANGED")
        if plan.files:
            await self.text(plan.path, "add", "--all", "--", ".")
            staged = tuple(filter(None, (await self.run(plan.path, "ls-files", "--cached", "-z"))[0].split("\0")))
            if set(staged) != set(plan.files):
                raise ProjectError("Staged files differ from the confirmed preview", code="INITIAL_SNAPSHOT_CHANGED")
            _, changed = await self.run(plan.path, "diff", "--quiet", "--exit-code", allowed=(0, 1))
            if changed or self._snapshot_digest(plan.path, plan.files) != plan.digest:
                raise ProjectError("Snapshot contents changed during staging", code="INITIAL_SNAPSHOT_CHANGED")
        await self.text(plan.path, "commit", "--allow-empty", "-m", "Initialize ReviewRelay project")
        return await self.discover(plan.path)

    async def remote_sha(self, path, url, branch):
        import re
        ref = "refs/heads/" + validate_branch_name(branch)
        lines = (await self.text(path, "ls-remote", "--refs", url, ref)).splitlines()
        if not lines:
            return None
        if len(lines) != 1 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\s+" + re.escape(ref), lines[0]):
            raise ProjectError("Remote candidate response is invalid", code="REMOTE_CANDIDATE_MISMATCH")
        return lines[0].split()[0]

    async def relationship(self, path, url, branch, local_sha):
        remote = await self.remote_sha(path, url, branch)
        if remote is None:
            # A missing selected branch is not proof that the repository is empty.
            if await self.text(path, "ls-remote", "--heads", url):
                raise ProjectError("Remote default branch could not be verified", code="GITHUB_REMOTE_BRANCH_REQUIRED")
            return "REMOTE_EMPTY", None
        if remote == local_sha:
            return "MATCHING", remote
        await self.text(path, "fetch", "--no-tags", "--no-write-fetch-head", url, remote)
        _, code = await self.run(path, "merge-base", "--is-ancestor", remote, local_sha, allowed=(0, 1))
        if not code:
            return "LOCAL_AHEAD", remote
        _, code = await self.run(path, "merge-base", "--is-ancestor", local_sha, remote, allowed=(0, 1))
        if not code:
            return "REMOTE_AHEAD", remote
        _, code = await self.run(path, "merge-base", local_sha, remote, allowed=(0, 1))
        raise ProjectError("Resolve repository histories outside ReviewRelay", code="GITHUB_HISTORY_UNRELATED" if code else "GITHUB_HISTORY_DIVERGED")

    async def publication_risks(self, path, sha, known_paths=()):
        # Paths across reachable history matter: deleted credentials would also be pushed.
        names = (await self.run(path, "log", "--format=", "--name-only", "-z", sha))[0].split("\0")
        names += (await self.run(path, "ls-tree", "-r", "--name-only", "-z", sha))[0].split("\0")
        risks = set()
        known = {name.replace("\\", "/").lower() for name in known_paths}
        for name in names:
            name = name.lstrip("\n")
            if not name:
                continue
            normalized = name.replace("\\", "/").lower()
            base = normalized.rsplit("/", 1)[-1]
            if (base == ".env" or base.startswith(".env.") or base.endswith((".pem", ".key"))
                    or base in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json"}
                    or normalized in known):
                risks.add(name)
        return tuple(sorted(risks))
