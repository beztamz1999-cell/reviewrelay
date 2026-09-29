"""Explicit, conservative task-storage cleanup operations."""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ProjectConfig, validate_identifier
from .errors import StorageError
from .models import TaskState
from .storage import PortableDataRoot


@dataclass(frozen=True)
class RetentionPolicy:
    completed_retention_days: int = 30
    failed_retention_days: int = 14
    aborted_retention_days: int = 7
    max_total_size_gb: float = 5.0
    keep_final_patch: bool = True


@dataclass(frozen=True)
class GCResult:
    compacted_tasks: tuple[str, ...]
    deleted_archives: tuple[str, ...]
    size_before_bytes: int
    size_after_bytes: int
    storage_cap_exceeded: bool


class GarbageCollector:
    def __init__(self, data_root: PortableDataRoot, policy: RetentionPolicy | None = None) -> None:
        self.data_root = data_root
        self.policy = policy or RetentionPolicy()
        for value in (
            self.policy.completed_retention_days,
            self.policy.failed_retention_days,
            self.policy.aborted_retention_days,
        ):
            if value < 0:
                raise ValueError("retention days must be non-negative")
        if not math.isfinite(self.policy.max_total_size_gb) or self.policy.max_total_size_gb <= 0:
            raise ValueError("max_total_size_gb must be positive")

    @classmethod
    def for_project(cls, data_root: PortableDataRoot, config: ProjectConfig) -> "GarbageCollector":
        """Build GC policy from a validated ProjectConfig without executing test settings."""
        policy = RetentionPolicy(
            completed_retention_days=config.storage.completed_retention_days,
            failed_retention_days=config.storage.failed_retention_days,
            aborted_retention_days=config.storage.aborted_retention_days,
            max_total_size_gb=config.storage.max_total_size_gb,
            keep_final_patch=config.review.keep_final_patch,
        )
        return cls(data_root, policy)

    def compact_completed_task(self, project_id: str, task_id: str) -> bool:
        path = self.data_root.safe_path(Path("active") / validate_identifier(project_id, "project_id") / validate_identifier(task_id, "task_id"))
        if not path.exists():
            return False
        return self._compact_if_complete(path)

    def compact_completed_tasks(self) -> tuple[str, ...]:
        compacted: list[str] = []
        for area in ("active", "archive"):
            area_root = self.data_root.safe_path(area)
            if not area_root.exists():
                continue
            for task_root in sorted(area_root.glob("*/*")):
                if task_root.is_dir() and self._compact_if_complete(task_root):
                    compacted.append(f"{area}/{task_root.parent.name}/{task_root.name}")
        return tuple(compacted)

    def archive_task(self, project_id: str, task_id: str) -> Path:
        project_id = validate_identifier(project_id, "project_id")
        task_id = validate_identifier(task_id, "task_id")
        source = self.data_root.safe_path(Path("active") / project_id / task_id)
        state = self._read_state(source)
        if state not in {TaskState.COMPLETE, TaskState.PAUSED_ERROR, TaskState.ABORTED}:
            raise StorageError(f"Only complete, failed, or aborted tasks may be archived (state={state.value})")
        destination = self.data_root.safe_path(Path("archive") / project_id / task_id)
        if destination.exists():
            raise StorageError(f"Archive already exists: {project_id}/{task_id}")
        if state is TaskState.COMPLETE:
            self._compact_if_complete(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.data_root.assert_managed_path(source)
        self.data_root.assert_managed_path(destination)
        shutil.move(str(source), str(destination))
        return destination

    def managed_size_bytes(self) -> int:
        total = 0
        root = self.data_root.path.resolve(strict=True)
        for path in root.rglob("*"):
            try:
                if path.is_symlink():
                    continue
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def run(self, *, now: datetime | None = None) -> GCResult:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        before = self.managed_size_bytes()
        compacted = self.compact_completed_tasks()
        deleted: list[str] = []
        archives = self._archive_records()
        expired = [entry for entry in archives if entry[2] <= now]
        for path, _kind, _expires in sorted(expired, key=lambda entry: (entry[2], str(entry[0]))):
            self._delete_archive(path)
            deleted.append(path.relative_to(self.data_root.path).as_posix())
        size_after = self.managed_size_bytes()
        cap = int(self.policy.max_total_size_gb * 1024**3)
        cap_exceeded = size_after > cap
        return GCResult(compacted, tuple(deleted), before, size_after, cap_exceeded)

    def _compact_if_complete(self, task_root: Path) -> bool:
        if self._read_state(task_root) is not TaskState.COMPLETE:
            return False
        scratch = task_root / "scratch"
        self.data_root.assert_managed_path(task_root)
        if scratch.is_symlink():
            raise StorageError(f"Refusing to remove symlinked scratch directory: {scratch}")
        if scratch.exists():
            if self.policy.keep_final_patch:
                patches = [
                    patch for patch in scratch.rglob("changes.patch")
                    if patch.is_file() and not patch.is_symlink()
                ]
                if patches:
                    def patch_order(path: Path) -> tuple[int, int, str]:
                        cycle = -1
                        for part in path.parts:
                            match = re.fullmatch(r"cycle-(\d+)", part)
                            if match:
                                cycle = int(match.group(1))
                        self.data_root.assert_managed_path(path)
                        return cycle, path.stat().st_mtime_ns, str(path)

                    patch = max(patches, key=patch_order)
                    final_patch = task_root / "durable" / "final.patch"
                    self.data_root.assert_managed_path(final_patch)
                    shutil.copyfile(patch, final_patch)
            shutil.rmtree(scratch)
            return True
        return False

    def _read_state(self, task_root: Path) -> TaskState:
        path = self.data_root.safe_path(task_root.relative_to(self.data_root.path) / "durable" / "state.json")
        if not path.is_file():
            raise StorageError(f"Task state metadata is missing: {path}")
        try:
            state_text = json.loads(path.read_text(encoding="utf-8"))["task_state"]
            return TaskState(state_text)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StorageError(f"Invalid task state metadata: {path}") from exc

    def _archive_records(self) -> list[tuple[Path, str, datetime]]:
        root = self.data_root.safe_path("archive")
        records: list[tuple[Path, str, datetime]] = []
        if not root.exists():
            return records
        for task_root in sorted(root.glob("*/*")):
            if not task_root.is_dir():
                continue
            state = self._read_state(task_root)
            kind, days = self._retention_for(state)
            if days is None:
                continue
            metadata_path = task_root / "durable" / "task.json"
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                timestamp = data.get("updated_at") or data.get("created_at")
                recorded = _parse_timestamp(timestamp)
            except (OSError, ValueError, TypeError) as exc:
                raise StorageError(f"Invalid archive timestamp metadata: {metadata_path}") from exc
            records.append((task_root, kind, recorded + timedelta(days=days)))
        return records

    def _retention_for(self, state: TaskState) -> tuple[str, int | None]:
        if state is TaskState.COMPLETE:
            return "completed", self.policy.completed_retention_days
        if state is TaskState.PAUSED_ERROR:
            return "failed", self.policy.failed_retention_days
        if state is TaskState.ABORTED:
            return "aborted", self.policy.aborted_retention_days
        return "other", None

    def _delete_archive(self, path: Path) -> None:
        archive_root = self.data_root.safe_path("archive")
        self.data_root.assert_managed_path(path)
        if not path.resolve(strict=False).is_relative_to(archive_root.resolve(strict=False)):
            raise StorageError(f"Refusing to delete non-archive path: {path}")
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
