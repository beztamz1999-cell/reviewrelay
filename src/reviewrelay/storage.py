"""Portable data-root and per-task durable/scratch storage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePath
from typing import Any

from .config import validate_identifier
from .errors import PathSafetyError, StorageError
from .models import TaskRecord


_DATA_DIRS = ("config", "browser-profile", "db", "active", "archive", "logs")
_SCRATCH_DIRS = ("patch", "source", "grep", "tests", "upload")
_ROOT_MARKER = ".reviewrelay-root"
_ROOT_MARKER_VERSION = 1


def application_root(*, executable: str | Path | None = None, frozen: bool | None = None) -> Path:
    """Resolve the installation root independently of cwd.

    The supported ONEDIR layout is ``<application root>/dist/ReviewRelay``;
    source launches use the repository/package root. Rebuilding the bundle in
    place therefore keeps the same data location.
    """
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        binary = Path(executable or sys.executable).expanduser().resolve()
        if len(binary.parents) < 3:
            raise StorageError("Cannot resolve the ReviewRelay application directory")
        return binary.parents[2]
    return Path(__file__).resolve().parents[2]


class PortableDataRoot:
    """An explicit, user-selected root for all ReviewRelay-managed files."""

    def __init__(self, path: str | Path) -> None:
        if path is None or not str(path).strip():
            raise PathSafetyError("A portable data root must be explicitly supplied")
        self.path = Path(path).expanduser().absolute()

    def create(self) -> "PortableDataRoot":
        self.path.mkdir(parents=True, exist_ok=True)
        self.path = self.path.resolve(strict=True)
        for name in _DATA_DIRS:
            self.safe_path(name).mkdir(parents=True, exist_ok=True)
        return self

    def safe_path(self, relative: str | Path) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or PurePath(str(relative)).anchor:
            raise PathSafetyError(f"Expected a relative managed path: {relative!r}")
        root = self.path.resolve(strict=False)
        result = (root / candidate).resolve(strict=False)
        if not _is_relative_to(result, root) or result == root:
            raise PathSafetyError(f"Managed path escapes the portable data root: {relative!r}")
        return result

    def assert_managed_path(self, path: str | Path) -> Path:
        root = self.path.resolve(strict=False)
        resolved = Path(path).resolve(strict=False)
        if not _is_relative_to(resolved, root) or resolved == root:
            raise PathSafetyError(f"Path is outside the portable data root: {path}")
        return resolved

    def store_project_config(self, config: Any) -> Path:
        from .config import store_project_config

        return store_project_config(config, self)


class SelfManagedDataRoot(PortableDataRoot):
    """Application-relative production data root with a persistent identity."""

    def __init__(self, app_root: str | Path | None = None) -> None:
        self.app_root = Path(app_root).expanduser().absolute() if app_root is not None else application_root()
        super().__init__(self.app_root / "data")

    @classmethod
    def for_application(cls) -> "SelfManagedDataRoot":
        return cls()

    @property
    def marker_path(self) -> Path:
        return self.path / _ROOT_MARKER

    def create(self) -> "SelfManagedDataRoot":
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self.path = self.path.resolve(strict=True)
            marker = self.marker_path
            if marker.is_symlink():
                raise StorageError("ReviewRelay data identity marker must not be a symbolic link")
            if marker.exists():
                self._read_marker(marker)
            else:
                # Never adopt an existing relay.db or arbitrary directory. An
                # interrupted first run is recoverable only after the marker
                # has been written, before creating any managed subdirectories.
                if any(self.path.iterdir()):
                    raise StorageError(
                        f"Không thể mở dữ liệu ReviewRelay tại thư mục ứng dụng: {self.path}"
                    )
                self._write_marker(marker, {"application": "ReviewRelay", "marker_version": _ROOT_MARKER_VERSION,
                    "schema_version": 1})
            for name in _DATA_DIRS:
                self.safe_path(name).mkdir(parents=True, exist_ok=True)
            return self
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(
                f"Không thể mở dữ liệu ReviewRelay tại thư mục ứng dụng: {self.path}"
            ) from exc

    def _read_marker(self, path: Path) -> dict[str, Any]:
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError(f"Invalid ReviewRelay data identity marker: {path}") from exc
        if (not isinstance(marker, dict) or marker.get("application") != "ReviewRelay"
                or marker.get("marker_version") != _ROOT_MARKER_VERSION
                or marker.get("schema_version") != 1):
            raise StorageError(f"Unsupported ReviewRelay data identity marker: {path}")
        return marker

    def _write_marker(self, path: Path, values: dict[str, Any]) -> None:
        rendered = json.dumps(values, sort_keys=True, indent=2) + "\n"
        _atomic_write(path, rendered.encode("utf-8"))


class TaskStorage:
    """Creates task directories and persists durable task metadata/report text."""

    def __init__(self, data_root: PortableDataRoot) -> None:
        self.data_root = data_root

    def create_task(self, project_id: str, task_id: str, record: TaskRecord | None = None) -> Path:
        project_id = validate_identifier(project_id, "project_id")
        task_id = validate_identifier(task_id, "task_id")
        task_root = self.data_root.safe_path(Path("active") / project_id / task_id)
        if task_root.exists():
            raise StorageError(f"Task storage already exists: {project_id}/{task_id}")
        durable = task_root / "durable"
        scratch = task_root / "scratch"
        durable.mkdir(parents=True)
        for name in _SCRATCH_DIRS:
            (scratch / name).mkdir(parents=True, exist_ok=True)
        self.data_root.assert_managed_path(task_root)
        if record is None:
            record = TaskRecord(project_id=project_id, task_id=task_id)
        if record.project_id != project_id or record.task_id != task_id:
            raise StorageError("Task record identity does not match the requested storage path")
        self.persist_task_record(record)
        return task_root

    def task_root(self, project_id: str, task_id: str) -> Path:
        project_id = validate_identifier(project_id, "project_id")
        task_id = validate_identifier(task_id, "task_id")
        return self.data_root.safe_path(Path("active") / project_id / task_id)

    def persist_task_record(self, record: TaskRecord) -> Path:
        validate_identifier(record.project_id, "project_id")
        validate_identifier(record.task_id, "task_id")
        task_root = self.task_root(record.project_id, record.task_id)
        durable = task_root / "durable"
        if not durable.is_dir():
            raise StorageError(f"Task durable directory does not exist: {durable}")
        self.data_root.assert_managed_path(durable)
        self._write_json(durable / "state.json", {
            "task_state": record.task_state.value,
            "updated_at": record.updated_at,
            "base_sha": record.base_sha,
            "candidate_sha": record.candidate_sha,
            "review_cycle": record.review_cycle,
            "worker_reported_sha_mismatch": record.worker_reported_sha_mismatch,
        })
        return self._write_json(durable / "task.json", asdict(record) | {"task_state": record.task_state.value})

    def persist_worker_report(self, project_id: str, task_id: str, final_worker_text: str) -> Path:
        """Persist worker output verbatim; its contents are untrusted narrative, not evidence."""
        if not isinstance(final_worker_text, str):
            raise StorageError("Worker report must be text")
        task_root = self.task_root(project_id, task_id)
        durable = task_root / "durable"
        if not durable.is_dir():
            raise StorageError(f"Task durable directory does not exist: {durable}")
        destination = durable / "worker-report.md"
        self.data_root.assert_managed_path(destination)
        _atomic_write(destination, final_worker_text.encode("utf-8"))
        return destination

    def _write_json(self, path: Path, mapping: dict[str, Any]) -> Path:
        self.data_root.assert_managed_path(path)
        rendered = json.dumps(mapping, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        _atomic_write(path, rendered.encode("utf-8"))
        return path


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
