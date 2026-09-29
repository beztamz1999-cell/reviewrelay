from __future__ import annotations

import pytest

from reviewrelay.config import ProjectConfig
from reviewrelay.errors import BlockedDirtyBaseline
from reviewrelay.models import TaskState
from reviewrelay.state import StateStore
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task import begin_task


def _config(repo) -> ProjectConfig:
    return ProjectConfig.from_yaml(f"project_id: test-project\nrepo:\n  path: '{repo.as_posix()}'\n")


def test_begin_task_persists_actual_head_before_worker_execution(git_repo, tmp_path) -> None:
    data = PortableDataRoot(tmp_path / "portable")
    record = begin_task(_config(git_repo), "task-1", data)
    assert record.task_state is TaskState.PRECHECK
    assert record.base_sha
    with StateStore(data) as state:
        persisted = state.get("test-project", "task-1")
    assert persisted == record
    assert (data.safe_path("active/test-project/task-1/durable/state.json")).is_file()


def test_begin_task_rejects_dirty_baseline_before_creating_task(git_repo, tmp_path) -> None:
    (git_repo / "base.txt").write_text("dirty\n", encoding="utf-8")
    data = PortableDataRoot(tmp_path / "portable")
    with pytest.raises(BlockedDirtyBaseline):
        begin_task(_config(git_repo), "task-1", data)
    assert not data.safe_path("active/test-project/task-1").exists()
    with StateStore(data) as state:
        assert state.get("test-project", "task-1") is None
