"""Small task-start operation that captures and persists the Git baseline."""

from __future__ import annotations

from .config import ProjectConfig, validate_identifier
from .errors import StorageError
from .git import GitClient
from .models import TaskRecord, TaskState, utc_now_iso
from .state import StateStore
from .storage import PortableDataRoot, TaskStorage


def begin_task(
    config: ProjectConfig,
    task_id: str,
    data_root: PortableDataRoot,
    *,
    git: GitClient | None = None,
) -> TaskRecord:
    """Precheck a repository and persist actual HEAD as BASE_SHA before worker execution."""
    task_id = validate_identifier(task_id, "task_id")
    data_root.create()
    task_storage = TaskStorage(data_root)
    task_path = task_storage.task_root(config.project_id, task_id)
    if task_path.exists():
        raise StorageError(f"Task storage already exists: {config.project_id}/{task_id}")

    with StateStore(data_root) as state:
        registered = state._connection.execute("SELECT record_json FROM projects WHERE project_id=?", (config.project_id,)).fetchone()
        if registered:
            from .projects import Project, ProjectError
            project = Project.from_json(registered[0])
            expected = project.to_config()
            if (expected.repo != config.repo or expected.github != config.github
                    or expected.chatgpt != config.chatgpt or expected.worker != config.worker):
                raise ProjectError("Task configuration differs from its registered Project", code="PROJECT_TASK_BINDING_MISMATCH")
        if state.get(config.project_id, task_id) is not None:
            raise StorageError(f"Task identity already exists in the state database: {config.project_id}/{task_id}")
        precheck = (git or GitClient()).precheck(
            config.repo.path,
            require_clean_baseline=config.repo.require_clean_baseline,
            expected_repository=config.repo.path,
        )
        timestamp = utc_now_iso()
        record = TaskRecord(
            project_id=config.project_id,
            task_id=task_id,
            task_state=TaskState.PRECHECK,
            base_sha=precheck.base_sha,
            created_at=timestamp,
            updated_at=timestamp,
        )
        task_storage.create_task(config.project_id, task_id, record)
        state.save(record)
    return record
