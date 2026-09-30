import os
import stat

import pytest

from reviewrelay.dev.codex_smoke import cleanup_disposable_repo
from reviewrelay.storage import PortableDataRoot


def test_disposable_git_readonly_files_are_cleaned_with_checked_target(tmp_path):
    root = PortableDataRoot(tmp_path / "portable").create()
    repo = root.safe_path("logs/codex-smoke-test/repo")
    objects = repo / ".git" / "objects"
    objects.mkdir(parents=True)
    object_file = objects / "readonly-object"
    object_file.write_bytes(b"fixture")
    os.chmod(object_file, stat.S_IREAD)
    cleanup_disposable_repo(root, repo, "codex-smoke-test")
    assert not repo.exists()


def test_cleanup_rejects_wrong_task_target_before_deletion(tmp_path):
    root = PortableDataRoot(tmp_path / "portable").create()
    repo = root.safe_path("logs/other-task/repo")
    repo.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="TARGET_INVALID"):
        cleanup_disposable_repo(root, repo, "codex-smoke-test")
    assert repo.exists()
