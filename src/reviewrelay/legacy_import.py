"""Explicit one-time import of non-browser ReviewRelay data."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .errors import StorageError
from .state import SCHEMA_VERSION, StateStore
from .storage import PortableDataRoot, SelfManagedDataRoot


_IMPORT_MARKER = "legacy_import.json"
_COPY_DIRS = ("config", "active", "archive", "logs")


def legacy_import_available(target: SelfManagedDataRoot) -> bool:
    if not isinstance(target, SelfManagedDataRoot):
        return False
    try:
        marker = target._read_marker(target.marker_path)
        return not marker.get("legacy_imported") and _target_is_empty(target)
    except (OSError, StorageError):
        return False


def import_legacy_data(source: str | Path, target: SelfManagedDataRoot) -> dict[str, int]:
    """Import an Owner-selected legacy root once, preserving the source.

    The browser profile is intentionally excluded: authentication material is
    never copied as part of data-root migration. The Owner can authenticate in
    the new canonical profile through the normal manual Auth Mode flow.
    """
    target.create()
    source_path = Path(source).expanduser().resolve(strict=True)
    target_path = target.path.resolve(strict=True)
    if not source_path.is_dir() or source_path == target_path or source_path in target_path.parents or target_path in source_path.parents:
        raise StorageError("Choose a separate ReviewRelay legacy data folder")
    marker = target._read_marker(target.marker_path)
    if marker.get("legacy_imported"):
        raise StorageError("Legacy ReviewRelay data has already been imported once")
    if not _target_is_empty(target):
        raise StorageError("Import is available only before canonical ReviewRelay data is created")
    source_db = source_path / "db" / "relay.db"
    if not source_db.is_file() or source_db.is_symlink():
        raise StorageError("The selected folder does not contain a ReviewRelay database")

    stage_parent = Path(tempfile.mkdtemp(prefix=".reviewrelay-import-", dir=target_path.parent))
    stage_data = stage_parent / "data"
    try:
        staged_root = PortableDataRoot(stage_data).create()
        for name in _COPY_DIRS:
            candidate = source_path / name
            if candidate.exists():
                _copy_tree_without_links(candidate, staged_root.safe_path(name), skip_project_locks=name == "config")
        staged_db = staged_root.safe_path(Path("db") / "relay.db")
        _sqlite_backup(source_db, staged_db)
        with StateStore(staged_root) as imported_state:
            version = int(imported_state._connection.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise StorageError("The imported database could not be upgraded to the supported schema")
            counts = {table: int(imported_state._connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in _user_tables(imported_state._connection)}
        import_marker = {"application": "ReviewRelay", "schema_version": 1, "legacy_imported": True}
        staged_marker = {"application": "ReviewRelay", "marker_version": 1, "schema_version": 1,
            "legacy_imported": True}
        (stage_data / ".reviewrelay-root").write_text(json.dumps(staged_marker, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (stage_data / _IMPORT_MARKER).write_text(json.dumps(import_marker, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        _replace_empty_target(stage_data, target_path)
        return counts
    finally:
        if stage_parent.exists():
            shutil.rmtree(stage_parent)


def _target_is_empty(target: SelfManagedDataRoot) -> bool:
    for child in target.path.iterdir():
        if child.name == ".reviewrelay-root":
            continue
        if child.is_dir() and not child.is_symlink() and not any(child.iterdir()):
            continue
        if child.name == "db" and child.is_dir() and not child.is_symlink():
            db = child / "relay.db"
            if db.is_file() and not db.is_symlink() and _database_is_empty(db):
                other = [item for item in child.iterdir() if item != db]
                if not other:
                    continue
        return False
    return True


def _database_is_empty(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            for table in _user_tables(db):
                if db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                    return False
            return True
    except sqlite3.Error:
        return False


def _user_tables(db: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))


def _sqlite_backup(source: Path, destination: Path) -> None:
    try:
        source_uri = "file:" + source.as_posix() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_db:
            check = source_db.execute("PRAGMA quick_check").fetchone()
            version = int(source_db.execute("PRAGMA user_version").fetchone()[0])
            if check is None or check[0] != "ok" or version <= 0 or version > SCHEMA_VERSION:
                raise StorageError("The selected ReviewRelay database is invalid or newer than this version")
            tables = {row[0] for row in source_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not {"tasks", "projects"} <= tables:
                raise StorageError("The selected database does not contain ReviewRelay Project and Task state")
            with closing(sqlite3.connect(destination)) as destination_db:
                source_db.backup(destination_db)
    except sqlite3.Error as exc:
        raise StorageError("Could not read the selected ReviewRelay database safely") from exc


def _copy_tree_without_links(source: Path, destination: Path, *, skip_project_locks: bool = False) -> None:
    if source.is_symlink() or not source.is_dir():
        raise StorageError(f"Unsafe legacy data folder: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if skip_project_locks and item.name == "project-locks":
            continue
        if item.name.lower().endswith(".lock"):
            continue
        if item.is_symlink():
            raise StorageError(f"Legacy data contains a symbolic link: {item}")
        target = destination / item.name
        if item.is_dir():
            _copy_tree_without_links(item, target, skip_project_locks=False)
        elif item.is_file():
            shutil.copy2(item, target)


def _replace_empty_target(stage: Path, target: Path) -> None:
    backup = target.parent / f".reviewrelay-empty-data-{uuid4().hex}"
    os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup)
