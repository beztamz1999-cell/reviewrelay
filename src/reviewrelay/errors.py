"""Typed failures shared by ReviewRelay Phase 1."""

from __future__ import annotations


class ReviewRelayError(Exception):
    """Base exception with a stable, machine-readable error code."""

    code = "REVIEWRELAY_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigError(ReviewRelayError):
    code = "INVALID_PROJECT_CONFIG"


class StorageError(ReviewRelayError):
    code = "STORAGE_ERROR"


class PathSafetyError(ReviewRelayError):
    code = "UNSAFE_PATH"


class SchemaVersionError(ReviewRelayError):
    code = "UNSUPPORTED_SCHEMA_VERSION"


class GitError(ReviewRelayError):
    code = "GIT_ERROR"


class GitCommandError(GitError):
    code = "GIT_COMMAND_FAILED"

    def __init__(
        self,
        args: tuple[str, ...],
        cwd: str,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.args_run = args
        self.cwd = cwd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        rendered = " ".join(args)
        detail = stderr.strip() or stdout.strip() or "no diagnostic output"
        super().__init__(f"Git command failed ({returncode}): git {rendered}: {detail}")


class RepositoryError(GitError):
    code = "INVALID_REPOSITORY"


class BlockedDirtyBaseline(GitError):
    code = "BLOCKED_DIRTY_BASELINE"


class CandidateInvalidDirtyWorktree(GitError):
    code = "CANDIDATE_INVALID_DIRTY_WORKTREE"


class CandidateMutatedDuringReview(GitError):
    code = "CANDIDATE_MUTATED_DURING_REVIEW"
