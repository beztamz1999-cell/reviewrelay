from __future__ import annotations

from datetime import datetime, timedelta, timezone

from reviewrelay.gc import GarbageCollector, RetentionPolicy
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.storage import PortableDataRoot, TaskStorage


def test_gc_never_deletes_active_noncompleted_task(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    task_path = TaskStorage(root).create_task(
        "p", "active-task", TaskRecord("p", "active-task", TaskState.WORKER_RUNNING)
    )
    evidence = task_path / "scratch" / "patch" / "important.patch"
    evidence.write_text("keep", encoding="utf-8")
    result = GarbageCollector(root, RetentionPolicy(max_total_size_gb=1e-12)).run()
    assert evidence.exists()
    assert task_path.exists()
    assert result.storage_cap_exceeded is True


def test_completed_task_compaction_deletes_scratch_and_keeps_durable(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    task_path = TaskStorage(root).create_task("p", "done", TaskRecord("p", "done", TaskState.COMPLETE))
    evidence = task_path / "scratch" / "upload" / "cycle-3" / "changes.patch"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("disposable", encoding="utf-8")
    report = task_path / "durable" / "worker-report.md"
    report.write_text("audit text", encoding="utf-8")
    gc = GarbageCollector(root)
    assert gc.compact_completed_task("p", "done") is True
    assert not (task_path / "scratch").exists()
    assert report.read_text(encoding="utf-8") == "audit text"
    assert (task_path / "durable" / "final.patch").read_text(encoding="utf-8") == "disposable"
    assert gc.compact_completed_task("p", "done") is False


def test_completed_compaction_can_discard_final_patch_when_configured(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    task_path = TaskStorage(root).create_task("p", "done", TaskRecord("p", "done", TaskState.COMPLETE))
    patch = task_path / "scratch" / "patch" / "changes.patch"
    patch.write_text("temporary", encoding="utf-8")
    gc = GarbageCollector(root, RetentionPolicy(keep_final_patch=False))
    assert gc.compact_completed_task("p", "done") is True
    assert not (task_path / "durable" / "final.patch").exists()


def test_expired_archive_is_deleted_by_configured_retention(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    task = TaskRecord("p", "old", TaskState.COMPLETE, created_at=old, updated_at=old)
    task_path = TaskStorage(root).create_task("p", "old", task)
    (task_path / "durable" / "final.json").write_text("{}", encoding="utf-8")
    gc = GarbageCollector(root, RetentionPolicy(completed_retention_days=1))
    archived = gc.archive_task("p", "old")
    result = gc.run()
    assert not archived.exists()
    assert result.deleted_archives == ("archive/p/old",)


def test_gc_preserves_database_config_and_active_durable_files(tmp_path) -> None:
    root = PortableDataRoot(tmp_path / "data").create()
    config = root.safe_path("config/projects/p.yaml")
    config.parent.mkdir(parents=True)
    config.write_text("project_id: p", encoding="utf-8")
    database = root.safe_path("db/relay.db")
    database.write_bytes(b"db")
    task = TaskStorage(root).create_task("p", "running", TaskRecord("p", "running", TaskState.WAIT_REVIEW))
    active_durable = task / "durable" / "state.json"
    result = GarbageCollector(root, RetentionPolicy(max_total_size_gb=1e-12)).run()
    assert config.exists() and database.exists() and active_durable.exists()
    assert result.storage_cap_exceeded is True
