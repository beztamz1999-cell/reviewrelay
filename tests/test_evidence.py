from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from reviewrelay.errors import CandidateMutatedDuringReview, PathSafetyError
from reviewrelay.evidence import build_review_pack, normalize_repo_relative_path
from reviewrelay.git import GitClient
from reviewrelay.storage import PortableDataRoot, TaskStorage


def _git(repo, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, shell=False, text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def test_generates_patch_stat_name_status_and_status_artifacts(git_repo, commit_change, tmp_path) -> None:
    client = GitClient()
    base = client.inspect(git_repo).head_sha
    head = commit_change(git_repo, "src/change.py", "print('changed')\n")
    data = PortableDataRoot(tmp_path / "data").create()
    result = build_review_pack(
        git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
        cycle=1, base_sha=base, head_sha=head,
    )
    assert "diff --git" in (result.pack_path / "changes.patch").read_text(encoding="utf-8")
    assert "src/change.py" in (result.pack_path / "changed-files.txt").read_text(encoding="utf-8")
    assert (result.pack_path / "diff-stat.txt").is_file()
    assert (result.pack_path / "git-status.txt").read_text(encoding="utf-8") == ""


def test_changed_source_snapshot_tracks_new_and_renamed_files_and_omits_deletions(git_repo, tmp_path) -> None:
    (git_repo / "old-name.txt").write_text("same data\n", encoding="utf-8")
    (git_repo / "deleted.txt").write_text("will disappear\n", encoding="utf-8")
    _git(git_repo, "add", "old-name.txt", "deleted.txt")
    _git(git_repo, "commit", "-m", "add files")
    base = GitClient().inspect(git_repo).head_sha

    (git_repo / "old-name.txt").rename(git_repo / "new-name.txt")
    (git_repo / "deleted.txt").unlink()
    (git_repo / "base.txt").write_text("changed\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "rename delete and modify")
    head = GitClient().inspect(git_repo).head_sha

    data = PortableDataRoot(tmp_path / "data").create()
    result = build_review_pack(
        git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
        cycle=1, base_sha=base, head_sha=head,
    )
    assert "R" in (result.pack_path / "changed-files.txt").read_text(encoding="utf-8")
    assert (result.pack_path / "source" / "new-name.txt").read_text(encoding="utf-8") == "same data\n"
    assert not (result.pack_path / "source" / "deleted.txt").exists()
    assert "new-name.txt" in result.snapshotted_paths


def test_manifest_counts_changes_and_hashes_actual_artifacts(git_repo, commit_change, tmp_path) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "new.txt", "one\ntwo\n")
    data = PortableDataRoot(tmp_path / "data").create()
    result = build_review_pack(
        git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
        cycle=1, base_sha=base, head_sha=head,
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["protocol"] == "review-relay/1"
    assert manifest["files_changed"] == 1
    assert manifest["insertions"] == 2
    assert manifest["deletions"] == 0
    assert manifest["worktree_clean"] is True
    assert manifest["reviewrelay_version"]
    for relative, expected in manifest["artifact_sha256"].items():
        assert hashlib.sha256((result.pack_path / relative).read_bytes()).hexdigest() == expected


def test_review_pack_includes_worker_report_and_task_text_when_present(git_repo, commit_change, tmp_path) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "file.txt", "new\n")
    data = PortableDataRoot(tmp_path / "data").create()
    storage = TaskStorage(data)
    storage.create_task("p", "t")
    storage.persist_worker_report("p", "t", "worker says this is done\n")
    result = build_review_pack(
        git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
        cycle=1, base_sha=base, head_sha=head, task_text="Approved task\n",
    )
    assert (result.pack_path / "worker-report.md").read_text(encoding="utf-8") == "worker says this is done\n"
    assert (result.pack_path / "task.md").read_text(encoding="utf-8") == "Approved task\n"


def test_source_snapshot_limit_skips_large_full_file_collection(git_repo, commit_change, tmp_path) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "new.txt", "new\n")
    data = PortableDataRoot(tmp_path / "data").create()
    result = build_review_pack(
        git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
        cycle=1, base_sha=base, head_sha=head, full_changed_files_limit=0,
    )
    assert result.manifest["source_snapshot_included"] is False
    assert result.snapshotted_paths == ()


@pytest.mark.parametrize("path", ["..\\..\\secret.txt", "../secret.txt", "C:\\secret.txt", "/secret.txt"])
def test_rejects_repo_path_traversal_and_absolute_paths(path: str) -> None:
    with pytest.raises(PathSafetyError):
        normalize_repo_relative_path(path)


def test_normalizes_safe_repo_relative_paths() -> None:
    assert normalize_repo_relative_path("src\\game\\file.cs") == "src/game/file.cs"


def test_review_pack_path_must_remain_inside_data_root(git_repo, commit_change, tmp_path) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "x.txt", "x\n")
    data = PortableDataRoot(tmp_path / "data").create()
    with pytest.raises(PathSafetyError):
        build_review_pack(
            git_repo, data, pack_relative_path="../../outside", project_id="p", task_id="t",
            cycle=1, base_sha=base, head_sha=head,
        )


def test_review_pack_cannot_be_written_inside_repository(git_repo, commit_change) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "x.txt", "x\n")
    data = PortableDataRoot(git_repo / ".reviewrelay-data").create()
    with pytest.raises(PathSafetyError):
        build_review_pack(
            git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
            cycle=1, base_sha=base, head_sha=head, strict_commit_mode=False,
        )


def test_rebuilding_same_pack_removes_stale_optional_artifacts(git_repo, commit_change, tmp_path) -> None:
    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "new.txt", "new\n")
    data = PortableDataRoot(tmp_path / "data").create()
    arguments = dict(
        repository=git_repo, data_root=data, pack_relative_path="active/p/t/scratch/upload/cycle-1",
        project_id="p", task_id="t", cycle=1, base_sha=base, head_sha=head,
    )
    build_review_pack(**arguments, task_text="old task content")
    result = build_review_pack(**arguments)
    assert not (result.pack_path / "task.md").exists()


def test_candidate_mutation_during_collection_fails_closed(git_repo, commit_change, tmp_path) -> None:
    class MutatingGit(GitClient):
        inspections = 0

        def inspect(self, repository, expected_repository=None):
            self.inspections += 1
            if self.inspections == 2:
                (git_repo / "base.txt").write_text("changed during evidence collection\n", encoding="utf-8")
            return super().inspect(repository, expected_repository)

    base = GitClient().inspect(git_repo).head_sha
    head = commit_change(git_repo, "new.txt", "new\n")
    data = PortableDataRoot(tmp_path / "data").create()
    with pytest.raises(CandidateMutatedDuringReview):
        build_review_pack(
            git_repo, data, pack_relative_path="active/p/t/scratch/upload/cycle-1", project_id="p", task_id="t",
            cycle=1, base_sha=base, head_sha=head, git=MutatingGit(),
        )
