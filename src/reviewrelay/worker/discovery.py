"""Project worker discovery through the public app-server, without model turns."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__
from ..git import GitClient
from ..models import utc_now_iso
from ..projects import ProjectError, ProjectRegistry, local_identity
from .base import CodexWorkerSettings
from .errors import WorkerBindingMismatch, WorkerProtocolError, WorkerTurnAlreadyActive, WorkerRequestFailed
from .lock import WorkerTaskLock
from .transport import AppServerTransport


INTERACTIVE_SOURCES = ("cli", "vscode", "appServer")


@dataclass(frozen=True)
class WorkerCandidate:
    thread_id: str
    repository: str
    title: str
    source: str
    last_activity: str


@dataclass(frozen=True)
class DiscoveryResult:
    status: str  # CONNECTED, CHOOSE, NOT_FOUND, STALE
    candidates: tuple[WorkerCandidate, ...] = ()


def verify_thread(thread, repository, expected_id, *, idle=True):
    """Require protocol identity, persisted interactive origin and terminal turns."""
    if (not isinstance(thread, dict) or thread.get("id") != expected_id
            or not isinstance(expected_id, str) or not expected_id or len(expected_id) > 512
            or not isinstance(thread.get("cwd"), str)
            or local_identity(thread["cwd"]) != local_identity(repository)
            or thread.get("source") not in INTERACTIVE_SOURCES
            or thread.get("ephemeral") is not False or thread.get("parentThreadId")
            or thread.get("agentRole") or thread.get("agentNickname")):
        raise WorkerBindingMismatch("Worker is not a persisted interactive thread for this Project")
    turns = thread.get("turns")
    if not isinstance(turns, list) or any(not isinstance(t, dict) or not isinstance(t.get("id"), str)
            or t.get("status") not in {"completed", "failed", "interrupted", "inProgress"} for t in turns):
        raise WorkerProtocolError("Worker turn history is not authoritative")
    status = thread.get("status")
    if not isinstance(status, dict) or status.get("type") not in {"idle", "notLoaded", "active"}:
        raise WorkerProtocolError("Worker runtime status is not authoritative")
    if idle and (any(t["status"] == "inProgress" for t in turns)
                 or isinstance(status, dict) and status.get("type") == "active"):
        raise WorkerTurnAlreadyActive("Worker has an unresolved active turn")
    title = thread.get("name") or thread.get("preview") or "Worker Codex"
    title = " ".join(str(title).split())[:80]
    # Never fall back to the UUID as an Owner-facing title.
    if expected_id in title:
        title = "Worker Codex"
    timestamp = thread.get("recencyAt") or thread.get("updatedAt")
    try:
        activity = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else "Chưa có thông tin"
    except (ValueError, OverflowError, OSError):
        activity = "Chưa có thông tin"
    return WorkerCandidate(expected_id, str(Path(repository).resolve()), title, thread["source"], activity)


class WorkerProtocolClient:
    def __init__(self, project, *, process_command=None):
        self.project = project
        self.settings = CodexWorkerSettings.from_mapping(project.worker_settings)
        self.transport = AppServerTransport(process_command or (self.settings.executable, "app-server", "--listen", "stdio://"),
            project.local_repo_path, self.settings.timeouts, self.on_event, lambda error: None)

    async def on_event(self, message):
        if "id" in message:
            # Discovery never authorizes commands, credentials, or model operations.
            await self.transport.send({"id": message["id"], "error": {"code": -32601, "message": "Read-only worker discovery"}})

    async def __aenter__(self):
        try:
            await self.transport.start()
            result = await self.transport.request("initialize", {"clientInfo": {"name": "reviewrelay", "title": "ReviewRelay", "version": __version__}}, self.settings.timeouts.initialize_seconds)
            if not isinstance(result.get("userAgent"), str) or not result["userAgent"]:
                raise WorkerProtocolError("Invalid initialize response")
            await self.transport.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            await self.transport.close()
            raise

    async def request(self, method, params):
        return await self.transport.request(method, params)

    async def __aexit__(self, *_):
        await self.transport.close()


class ProjectWorkerService:
    def __init__(self, root, *, client_factory=WorkerProtocolClient):
        self.root, self.client_factory = root, client_factory

    def _lock(self, project_id):
        return WorkerTaskLock(self.root.safe_path(Path("config/project-locks") / (project_id + ".lock")))

    def _project(self, registry, project_id):
        project = registry.get(project_id)
        actual = GitClient().inspect(project.local_repo_path, expected_repository=project.local_repo_path)
        if local_identity(actual.repository_root) != local_identity(project.local_repo_path):
            raise WorkerBindingMismatch("Registered repository is not its Git root")
        return project

    async def _read(self, client, project, thread_id, *, idle=True):
        result = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        return verify_thread(result.get("thread"), project.local_repo_path, thread_id, idle=idle)

    def _bind(self, registry, project, candidate):
        registry.state.assert_project_available(project.project_id)
        return registry.save(replace(project, codex_worker_thread_id=candidate.thread_id,
            codex_worker_repo_path=candidate.repository, codex_worker_title=candidate.title,
            codex_worker_source=candidate.source, codex_worker_last_activity=candidate.last_activity,
            codex_worker_verified_at=utc_now_iso()), event="PROJECT_WORKER_BOUND")

    async def discover(self, project_id, *, change=False):
        lock = self._lock(project_id)
        try:
            with ProjectRegistry(self.root) as registry:
                project = self._project(registry, project_id)
                if change:
                    registry.state.assert_project_available(project_id)
                async with self.client_factory(project) as client:
                    stale = False
                    if project.codex_worker_thread_id and not change:
                        try:
                            candidate = await self._read(client, project, project.codex_worker_thread_id, idle=False)
                            registry.save(replace(project, codex_worker_title=candidate.title,
                                codex_worker_last_activity=candidate.last_activity, codex_worker_verified_at=utc_now_iso()), event="PROJECT_WORKER_VERIFIED")
                            return DiscoveryResult("CONNECTED", (candidate,))
                        except (WorkerBindingMismatch, WorkerProtocolError, WorkerRequestFailed):
                            stale = True
                    candidates, cursor, seen = {}, None, set()
                    for _ in range(20):  # Bounded pagination; never auto-bind an incomplete result set.
                        params = {"cwd": str(Path(project.local_repo_path).resolve()), "archived": False,
                            "sortKey": "recency_at", "sortDirection": "desc", "sourceKinds": list(INTERACTIVE_SOURCES), "limit": 100}
                        if cursor:
                            params["cursor"] = cursor
                        result = await client.request("thread/list", params)
                        if not isinstance(result.get("data"), list):
                            raise WorkerProtocolError("Invalid worker list")
                        for item in result["data"]:
                            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                                raise WorkerProtocolError("Invalid listed identity")
                            try:
                                candidate = await self._read(client, project, item["id"])
                            except (WorkerBindingMismatch, WorkerTurnAlreadyActive):
                                continue
                            owner = registry.state._connection.execute("SELECT project_id FROM worker_owners WHERE thread_id=?", (candidate.thread_id,)).fetchone()
                            if not owner or owner[0] == project_id:
                                candidates[candidate.thread_id] = candidate
                        cursor = result.get("nextCursor")
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or not cursor or cursor in seen:
                            raise WorkerProtocolError("Invalid worker pagination")
                        seen.add(cursor)
                    else:
                        raise WorkerProtocolError("Worker discovery exceeded its bounded pagination")
                    choices = tuple(candidates.values())
                    if stale:
                        return DiscoveryResult("STALE", choices)
                    if len(choices) == 1 and not change:
                        self._bind(registry, project, choices[0])
                        return DiscoveryResult("CONNECTED", choices)
                    return DiscoveryResult("CHOOSE" if choices else "NOT_FOUND", choices)
        finally:
            lock.close()

    async def select(self, project_id, thread_id):
        lock = self._lock(project_id)
        try:
            with ProjectRegistry(self.root) as registry:
                project = self._project(registry, project_id)
                registry.state.assert_project_available(project_id)
                async with self.client_factory(project) as client:
                    candidate = await self._read(client, project, thread_id)
                    self._bind(registry, project, candidate)
                    return candidate
        finally:
            lock.close()

    async def create(self, project_id):
        """Only the explicit Owner button calls this; ambiguous creation stays closed."""
        lock = self._lock(project_id)
        try:
            with ProjectRegistry(self.root) as registry:
                project = self._project(registry, project_id)
                registry.state.assert_project_available(project_id)
                if project.codex_worker_thread_id or project.setup.get("worker_creation"):
                    raise ProjectError("Rescan or reconcile the existing worker first", code="PROJECT_WORKER_CREATE_UNRESOLVED")
                project = registry.save(replace(project, setup={**project.setup, "worker_creation": "IN_FLIGHT"}), event="PROJECT_WORKER_CREATE_PLANNED")
                async with self.client_factory(project) as client:
                    params = {"cwd": project.local_repo_path, "approvalPolicy": "never", "sandbox": CodexWorkerSettings.from_mapping(project.worker_settings).sandbox, "ephemeral": False}
                    if project.worker_settings.get("model"):
                        params["model"] = project.worker_settings["model"]
                    result = await client.request("thread/start", params)
                    thread = result.get("thread", {})
                    verify_thread(thread, project.local_repo_path, thread.get("id"))
                    candidate = await self._read(client, project, thread["id"])
                    project = replace(project, setup={**project.setup, "worker_creation": "CONFIRMED"})
                    self._bind(registry, project, candidate)
                    return candidate
        finally:
            lock.close()
