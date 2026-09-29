from __future__ import annotations

import pytest

from reviewrelay.errors import (
    BlockedDirtyBaseline,
    CandidateInvalidDirtyWorktree,
    CandidateMutatedDuringReview,
    RepositoryError,
)
from reviewrelay.git import GitClient


def test_clean_git_baseline_is_accepted_and_captured(git_repo) -> None:
    result = GitClient().precheck(git_repo, require_clean_baseline=True)
    assert result.base_sha == result.inspection.head_sha
    assert len(result.base_sha) == 40
    assert result.inspection.worktree_clean


def test_dirty_git_baseline_is_rejected(git_repo) -> None:
    (git_repo / "base.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(BlockedDirtyBaseline) as error:
        GitClient().precheck(git_repo)
    assert error.value.code == "BLOCKED_DIRTY_BASELINE"


def test_candidate_sha_comes_from_actual_head_and_detects_worker_mismatch(git_repo, commit_change) -> None:
    actual = commit_change(git_repo, "candidate.txt", "candidate\n")
    verified = GitClient().verify_candidate(git_repo, worker_reported_sha="f" * 40)
    assert verified.candidate_sha == actual
    assert verified.worker_reported_sha_mismatch is True
    assert verified.worktree_clean


def test_clean_candidate_is_accepted_without_worker_sha(git_repo, commit_change) -> None:
    actual = commit_change(git_repo, "candidate.txt", "candidate\n")
    verified = GitClient().verify_candidate(git_repo)
    assert verified.candidate_sha == actual
    assert verified.worker_reported_sha_mismatch is False


def test_dirty_candidate_is_rejected_in_strict_mode(git_repo) -> None:
    (git_repo / "base.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(CandidateInvalidDirtyWorktree) as error:
        GitClient().verify_candidate(git_repo, strict_commit_mode=True)
    assert error.value.code == "CANDIDATE_INVALID_DIRTY_WORKTREE"


def test_candidate_repository_root_must_match_expected(git_repo, tmp_path) -> None:
    wrong_root = tmp_path / "not-the-repo"
    wrong_root.mkdir()
    with pytest.raises(RepositoryError):
        GitClient().verify_candidate(git_repo, expected_repository=wrong_root)


def test_candidate_mutation_during_verification_fails_closed(git_repo) -> None:
    class MutatingGit(GitClient):
        inspections = 0

        def inspect(self, repository, expected_repository=None):
            self.inspections += 1
            if self.inspections == 2:
                (git_repo / "base.txt").write_text("changed during verification\n", encoding="utf-8")
            return super().inspect(repository, expected_repository)

    with pytest.raises(CandidateMutatedDuringReview):
        MutatingGit().verify_candidate(git_repo)
