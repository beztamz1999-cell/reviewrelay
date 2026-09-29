from __future__ import annotations

import pytest

from reviewrelay.errors import PathSafetyError, StorageError
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.storage import PortableDataRoot, TaskStorage


def test_portable_data_root_creates_required_directories(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "ReviewRelayData").create()
    assert root.path.is_dir()
    assert {"config", "browser-profile", "db", "active", "archive", "logs"} <= {
        path.name for path in root.path.iterdir() if path.is_dir()
    }


@pytest.mark.parametrize("relative", ["../outside", "..\\outside", "active/../../outside", "C:/outside", "/outside"])
def test_data_root_rejects_paths_outside_root(tmp_path, relative: str) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    with pytest.raises(PathSafetyError):
        root.safe_path(relative)


def test_task_storage_separates_durable_and_reproducible_scratch(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    storage = TaskStorage(root)
    task = storage.create_task("gacha-v4", "task-2", TaskRecord("gacha-v4", "task-2", TaskState.WORKER_RUNNING))
    assert (task / "durable" / "task.json").is_file()
    assert (task / "durable" / "state.json").is_file()
    assert {"patch", "source", "grep", "tests", "upload"} <= {path.name for path in (task / "scratch").iterdir()}


def test_worker_report_is_persisted_verbatim_as_untrusted_text(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    storage = TaskStorage(root)
    storage.create_task("gacha-v4", "task-3")
    report = "RELAY_WORKER_DONE\nHEAD_SHA=abc\n"
    saved = storage.persist_worker_report("gacha-v4", "task-3", report)
    assert saved.read_text(encoding="utf-8") == report
    assert saved.name == "worker-report.md"


def test_task_ids_cannot_escape_active_storage(tmp_path) -> None:
    storage = TaskStorage(PortableDataRoot(tmp_path / "data").create())
    with pytest.raises(PathSafetyError):
        storage.create_task("..", "task")
    with pytest.raises(PathSafetyError):
        storage.task_root("project", "..\\outside")


def test_report_cannot_be_written_for_unknown_task(tmp_path) -> None:
    storage = TaskStorage(PortableDataRoot(tmp_path / "data").create())
    with pytest.raises(StorageError):
        storage.persist_worker_report("project", "missing", "text")
