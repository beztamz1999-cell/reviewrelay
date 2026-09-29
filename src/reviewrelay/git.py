"""Deterministic, shell-free Git inspection and candidate verification."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .errors import (
    BlockedDirtyBaseline,
    CandidateInvalidDirtyWorktree,
    CandidateMutatedDuringReview,
    GitCommandError,
    RepositoryError,
)
from .models import CandidateVerification, GitInspection, PrecheckResult


_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class GitClient:
    def __init__(self, executable: str = "git") -> None:
        self.executable = executable

    def run(self, repository: str | Path, *args: str) -> str:
        cwd = Path(repository)
        if not cwd.exists() or not cwd.is_dir():
            raise RepositoryError(f"Repository path does not exist: {cwd}")
        argv = [self.executable, *args]
        completed = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise GitCommandError(tuple(argv[1:]), str(cwd), completed.returncode, completed.stdout, completed.stderr)
        return completed.stdout

    def inspect(self, repository: str | Path, expected_repository: str | Path | None = None) -> GitInspection:
        path = Path(repository)
        if not path.exists() or not path.is_dir():
            raise RepositoryError(f"Repository path does not exist: {path}")
        try:
            top = self.run(path, "rev-parse", "--show-toplevel").strip()
            head = self.run(path, "rev-parse", "HEAD").strip()
            status = self.run(path, "status", "--porcelain")
        except GitCommandError as exc:
            raise RepositoryError(f"Not a usable Git repository: {path}: {exc}") from exc
        if not _SHA_RE.fullmatch(head):
            raise RepositoryError(f"Git returned an invalid HEAD SHA: {head!r}")
        try:
            resolved_top = Path(top).resolve(strict=True)
            if expected_repository is not None:
                expected = Path(expected_repository).resolve(strict=True)
                if not _same_path(resolved_top, expected):
                    raise RepositoryError(f"Git root {resolved_top} does not match expected repository {expected}")
        except OSError as exc:
            raise RepositoryError(f"Could not resolve configured repository path: {exc}") from exc
        return GitInspection(str(resolved_top), head.lower(), status)

    def precheck(
        self,
        repository: str | Path,
        *,
        require_clean_baseline: bool = True,
        expected_repository: str | Path | None = None,
    ) -> PrecheckResult:
        inspection = self.inspect(repository, expected_repository)
        if require_clean_baseline and not inspection.worktree_clean:
            raise BlockedDirtyBaseline(
                f"Baseline worktree is dirty; task cannot start. git status --porcelain: {inspection.status_porcelain!r}"
            )
        return PrecheckResult(inspection=inspection, base_sha=inspection.head_sha)

    def verify_candidate(
        self,
        repository: str | Path,
        *,
        expected_repository: str | Path | None = None,
        strict_commit_mode: bool = True,
        worker_reported_sha: str | None = None,
    ) -> CandidateVerification:
        inspection = self.inspect(repository, expected_repository)
        self.run(repository, "cat-file", "-e", f"{inspection.head_sha}^{{commit}}")
        final_inspection = self.inspect(repository, expected_repository)
        if (
            final_inspection.head_sha != inspection.head_sha
            or final_inspection.status_porcelain != inspection.status_porcelain
        ):
            raise CandidateMutatedDuringReview("Repository changed while ReviewRelay was verifying the candidate")
        inspection = final_inspection
        if strict_commit_mode and not inspection.worktree_clean:
            raise CandidateInvalidDirtyWorktree(
                "Candidate worktree is dirty; review is blocked. "
                f"git status --porcelain: {inspection.status_porcelain!r}"
            )
        mismatch = worker_reported_sha is not None and worker_reported_sha.strip().lower() != inspection.head_sha
        return CandidateVerification(
            repository_root=inspection.repository_root,
            candidate_sha=inspection.head_sha,
            worktree_clean=inspection.worktree_clean,
            worker_reported_sha_mismatch=mismatch,
        )


def _same_path(left: Path, right: Path) -> bool:
    import os

    return os.path.normcase(str(left)) == os.path.normcase(str(right))
