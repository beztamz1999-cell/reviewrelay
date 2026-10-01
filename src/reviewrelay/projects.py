"""Project identity and registry in the existing portable SQLite database."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .config import GitHubConfig, ProjectConfig, RepoConfig, validate_identifier
from .errors import ReviewRelayError
from .models import utc_now_iso
from .state import StateStore


class ProjectError(ReviewRelayError):
    code = "PROJECT_SETUP_FAILED"


class ConnectionStatus(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    NEEDS_OWNER = "NEEDS_OWNER"
    ERROR = "ERROR"
    SETTING_UP = "SETTING_UP"


class ProjectKind(str, Enum):
    NEW = "NEW"
    EXISTING = "EXISTING"


@dataclass(frozen=True)
class GitHubIdentity:
    owner: str
    name: str

    @property
    def key(self):
        return f"{self.owner}/{self.name}".lower()

    @property
    def url(self):
        return f"https://github.com/{self.owner}/{self.name}"


def github_identity(value: str) -> GitHubIdentity:
    """Validate web/HTTPS/SSH repository URLs; never persist userinfo or tokens."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ProjectError("Expected a GitHub repository URL", code="GITHUB_URL_INVALID")
    if value.startswith("git@github.com:"):
        path = value[len("git@github.com:"):]
    else:
        try:
            parts = urlsplit(value)
            valid = (parts.hostname == "github.com" and not parts.password and not parts.query and not parts.fragment
                and ((parts.scheme == "https" and not parts.username and parts.port is None)
                     or (parts.scheme == "ssh" and parts.username == "git" and parts.port in {None, 22})))
        except ValueError:
            valid = False
        if not valid:
            raise ProjectError("Expected a credential-free GitHub repository URL", code="GITHUB_URL_INVALID")
        path = parts.path.removeprefix("/").removesuffix("/")
    path = path.removesuffix(".git")
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_][A-Za-z0-9_.-]{0,99})", path)
    if not match or match[2] in {".", ".."}:
        raise ProjectError("Expected a GitHub owner and repository", code="GITHUB_URL_INVALID")
    return GitHubIdentity(match[1], match[2])


def local_identity(path: str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


@dataclass(frozen=True)
class Project:
    project_id: str
    project_name: str
    local_repo_path: str
    kind: ProjectKind
    initial_branch: str = "main"
    local_status: ConnectionStatus = ConnectionStatus.NOT_CONFIGURED
    github_status: ConnectionStatus = ConnectionStatus.NOT_CONFIGURED
    github_remote_name: str = "origin"
    github_owner: str | None = None
    github_repo_name: str | None = None
    github_repo_url: str | None = None
    github_git_url: str | None = None
    github_visibility: str | None = None
    github_default_branch: str | None = None
    github_last_verified_at: str | None = None
    review_mode: str = "branch"
    chatgpt_conversation_url: str | None = None
    chatgpt_status: ConnectionStatus = ConnectionStatus.NOT_CONFIGURED
    reviewer_settings: dict = field(default_factory=dict)
    codex_status: ConnectionStatus = ConnectionStatus.NOT_CONFIGURED
    worker_settings: dict = field(default_factory=dict)
    credential_paths: tuple[str, ...] = ()
    setup: dict = field(default_factory=dict)
    last_error_code: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def repository_ready(self):
        return (self.local_status is ConnectionStatus.READY and self.github_status is ConnectionStatus.READY
            and bool(self.github_repo_url and self.github_last_verified_at))

    @property
    def ready(self):
        return (self.repository_ready and self.chatgpt_status is ConnectionStatus.READY and self.codex_status is ConnectionStatus.READY
            and bool(self.chatgpt_conversation_url and self.worker_settings.get("executable")))

    @property
    def status(self):
        if self.ready:
            return "PROJECT_READY"
        if ConnectionStatus.NEEDS_OWNER in (self.local_status, self.github_status, self.chatgpt_status, self.codex_status):
            return "NEEDS_OWNER"
        if ConnectionStatus.ERROR in (self.local_status, self.github_status, self.chatgpt_status, self.codex_status):
            return "ERROR"
        return "SETUP_REQUIRED"

    def to_config(self, *, require_ready=True):
        if require_ready and not self.ready:
            raise ProjectError("Finish repository, reviewer and runtime setup first", code="PROJECT_NOT_READY")
        return ProjectConfig(self.project_id, RepoConfig(self.local_repo_path),
            chatgpt={**self.reviewer_settings, "conversation_url": self.chatgpt_conversation_url},
            worker=self.worker_settings, github=GitHubConfig(self.repository_ready, self.github_remote_name,
            self.github_default_branch or self.initial_branch, self.review_mode))

    @classmethod
    def from_json(cls, text):
        values = json.loads(text)
        values["kind"] = ProjectKind(values["kind"])
        for name in ("local_status", "github_status", "chatgpt_status", "codex_status"):
            values[name] = ConnectionStatus(values[name])
        values["credential_paths"] = tuple(values["credential_paths"])
        return cls(**values)


class ProjectRegistry:
    def __init__(self, root):
        self.root = root
        self.state = StateStore(root)

    def get(self, project_id):
        validate_identifier(project_id, "project_id")
        row = self.state._connection.execute("SELECT record_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise ProjectError("Project registration is missing", code="PROJECT_NOT_FOUND")
        return Project.from_json(row[0])

    def list(self):
        return tuple(Project.from_json(r[0]) for r in self.state._connection.execute("SELECT record_json FROM projects ORDER BY project_id"))

    def save(self, project, *, event="PROJECT_UPDATED"):
        from .reviewer.base import ChatGPTWebSettings
        from .worker.base import CodexWorkerSettings
        validate_identifier(project.project_id, "project_id")
        try:
            ChatGPTWebSettings.from_mapping({**project.reviewer_settings, "conversation_url": project.chatgpt_conversation_url})
            CodexWorkerSettings.from_mapping(project.worker_settings)
        except (TypeError, ValueError, ReviewRelayError):
            raise ProjectError("Invalid Project reviewer/runtime configuration", code="PROJECT_CONFIGURATION_INVALID") from None
        if not isinstance(project.project_name, str) or not project.project_name.strip() or len(project.project_name) > 128:
            raise ProjectError("A project name of 1–128 characters is required", code="PROJECT_NAME_INVALID")
        if any(ord(char) < 32 for char in project.project_name):
            raise ProjectError("Invalid project name", code="PROJECT_NAME_INVALID")
        old = self.state._connection.execute("SELECT local_identity FROM projects WHERE project_id=?", (project.project_id,)).fetchone()
        identity = local_identity(project.local_repo_path)
        if old and old[0] != identity:
            raise ProjectError("A registered repository cannot silently change", code="PROJECT_IDENTITY_IMMUTABLE")
        github = github_identity(project.github_repo_url) if project.github_repo_url else None
        github_key = github.key if github else None
        updated = replace(project, project_name=project.project_name.strip(),
            github_repo_url=github.url if github else None, updated_at=utc_now_iso())
        try:
            with self.state._connection as db:
                db.execute("""INSERT INTO projects VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                    github_identity=excluded.github_identity, record_json=excluded.record_json""",
                    (updated.project_id, identity, github_key, json.dumps(asdict(updated), sort_keys=True)))
                db.execute("INSERT INTO project_events(project_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                    (updated.project_id, event, json.dumps({"status": updated.status, "error_code": updated.last_error_code,
                     "github_url": updated.github_repo_url}), utc_now_iso()))
        except sqlite3.IntegrityError:
            duplicate = self.state._connection.execute("SELECT project_id FROM projects WHERE local_identity=?", (identity,)).fetchone()
            code = "PROJECT_REPO_ALREADY_REGISTERED" if duplicate and duplicate[0] != project.project_id else "GITHUB_REPO_ALREADY_REGISTERED"
            raise ProjectError("Repository already belongs to another registered Project", code=code) from None
        return updated

    def create(self, name, path, kind, *, branch="main"):
        from .config import validate_branch_name
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ProjectError("Select a local folder", code="PROJECT_PATH_INVALID")
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_absolute() or (resolved.exists() and not resolved.is_dir()):
            raise ProjectError("Select a local directory", code="PROJECT_PATH_INVALID")
        data = self.root.path.resolve()
        if resolved == data or resolved.is_relative_to(data) or data.is_relative_to(resolved):
            raise ProjectError("Source and portable data folders must be separate", code="PROJECT_DATA_ROOT_OVERLAP")
        return self.save(Project("project-" + uuid4().hex, name, str(resolved), ProjectKind(kind), validate_branch_name(branch)), event="PROJECT_REGISTERED")

    def rename(self, project_id, name):
        from .worker.lock import WorkerTaskLock
        lock = WorkerTaskLock(self.root.safe_path(Path("config/project-locks") / (validate_identifier(project_id) + ".lock")))
        try:
            return self.save(replace(self.get(project_id), project_name=name), event="PROJECT_RENAMED")
        finally:
            lock.close()

    def unregister(self, project_id, *, confirmed=False):
        if confirmed is not True:
            raise ProjectError("Confirm unregistering; source and GitHub will be preserved", code="PROJECT_UNREGISTER_CONFIRMATION_REQUIRED")
        from .worker.lock import WorkerTaskLock
        lock = WorkerTaskLock(self.root.safe_path(Path("config/project-locks") / (validate_identifier(project_id) + ".lock")))
        try:
            self.get(project_id)
            with self.state._connection as db:
                db.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
                db.execute("INSERT INTO project_events(project_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                    (project_id, "PROJECT_UNREGISTERED", "{}", utc_now_iso()))
        finally:
            lock.close()

    def close(self):
        self.state.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
