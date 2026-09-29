"""Typed validation for Phase 2 evidence requests; this module never executes them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeAlias

from .config import ProjectConfig
from .errors import (
    InvalidEvidenceRequest,
    PathSafetyError,
    UnknownTestId,
    UnsafeEvidenceRequest,
)
from .evidence import normalize_repo_relative_path


MAX_EVIDENCE_REQUESTS_PER_RESPONSE = 10
MAX_READ_RANGE_LINES = 500
MAX_GREP_PATTERN_LENGTH = 256
MAX_GREP_ROOTS = 8
MAX_PATH_LENGTH = 512
MAX_GIT_REF_LENGTH = 128
MAX_GIT_LOG_ENTRIES = 100

_FORBIDDEN_KINDS = {
    "shell", "bash", "cmd", "powershell", "python", "exec", "run", "curl", "wget",
    "delete", "deploy",
}
_ALLOWED_KINDS = {
    "read_file", "read_range", "grep", "git_show", "diff_file", "list_dir", "test", "git_log", "git_status",
}
_TEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_REF_CHARS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./~^@{}+-]*$")


@dataclass(frozen=True)
class EvidenceLimits:
    max_requests_per_response: int = MAX_EVIDENCE_REQUESTS_PER_RESPONSE
    max_read_range_lines: int = MAX_READ_RANGE_LINES
    max_grep_pattern_length: int = MAX_GREP_PATTERN_LENGTH
    max_grep_roots: int = MAX_GREP_ROOTS
    max_path_length: int = MAX_PATH_LENGTH
    max_git_ref_length: int = MAX_GIT_REF_LENGTH
    max_git_log_entries: int = MAX_GIT_LOG_ENTRIES


DEFAULT_EVIDENCE_LIMITS = EvidenceLimits()


@dataclass(frozen=True)
class ReadFileRequest:
    path: str
    kind: str = "read_file"


@dataclass(frozen=True)
class ReadRangeRequest:
    path: str
    start_line: int
    end_line: int
    kind: str = "read_range"


@dataclass(frozen=True)
class GrepRequest:
    pattern: str
    roots: tuple[str, ...]
    kind: str = "grep"


@dataclass(frozen=True)
class GitShowRequest:
    ref: str
    path: str
    kind: str = "git_show"


@dataclass(frozen=True)
class DiffFileRequest:
    path: str
    base_ref: str
    head_ref: str
    kind: str = "diff_file"


@dataclass(frozen=True)
class ListDirRequest:
    path: str
    kind: str = "list_dir"


@dataclass(frozen=True)
class TestRequest:
    __test__ = False  # Prevent pytest from treating this public DSL type as a test class.

    test_id: str
    kind: str = "test"


@dataclass(frozen=True)
class GitLogRequest:
    max_entries: int
    path: str | None = None
    kind: str = "git_log"


@dataclass(frozen=True)
class GitStatusRequest:
    kind: str = "git_status"


EvidenceRequest: TypeAlias = (
    ReadFileRequest
    | ReadRangeRequest
    | GrepRequest
    | GitShowRequest
    | DiffFileRequest
    | ListDirRequest
    | TestRequest
    | GitLogRequest
    | GitStatusRequest
)


def validate_evidence_requests(
    values: object,
    *,
    project_config: ProjectConfig | None = None,
    limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
) -> tuple[EvidenceRequest, ...]:
    """Validate a complete non-empty request set atomically, with no partial result."""
    if not isinstance(values, list) or not values:
        raise InvalidEvidenceRequest("evidence_requests must be a non-empty array")
    if len(values) > limits.max_requests_per_response:
        raise InvalidEvidenceRequest(
            f"Too many evidence requests: maximum is {limits.max_requests_per_response}"
        )
    # Build only in local memory and return after every request validates. No request is executed here.
    validated = tuple(
        validate_evidence_request(value, project_config=project_config, limits=limits)
        for value in values
    )
    return validated


def validate_evidence_request(
    value: object,
    *,
    project_config: ProjectConfig | None = None,
    limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
) -> EvidenceRequest:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidEvidenceRequest("Each evidence request must be a JSON object with string keys")
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise InvalidEvidenceRequest("Evidence request kind must be a string")
    if kind in _FORBIDDEN_KINDS:
        raise UnsafeEvidenceRequest(f"Evidence operation {kind!r} is forbidden")
    if kind not in _ALLOWED_KINDS:
        raise InvalidEvidenceRequest(f"Unknown evidence operation {kind!r}")

    if kind == "read_file":
        _require_keys(value, {"kind", "path"})
        return ReadFileRequest(_path(value["path"], limits))
    if kind == "read_range":
        _require_keys(value, {"kind", "path", "start_line", "end_line"})
        path = _path(value["path"], limits)
        start = _integer(value["start_line"], "start_line", minimum=1)
        end = _integer(value["end_line"], "end_line", minimum=1)
        if end < start:
            raise InvalidEvidenceRequest("end_line must be greater than or equal to start_line")
        if end - start + 1 > limits.max_read_range_lines:
            raise InvalidEvidenceRequest(
                f"Requested range exceeds the {limits.max_read_range_lines}-line maximum"
            )
        return ReadRangeRequest(path, start, end)
    if kind == "grep":
        _require_keys(value, {"kind", "pattern", "roots"})
        pattern = value["pattern"]
        if not isinstance(pattern, str) or not pattern.strip() or "\x00" in pattern:
            raise InvalidEvidenceRequest("grep pattern must be non-empty text without NUL")
        if len(pattern) > limits.max_grep_pattern_length:
            raise InvalidEvidenceRequest(
                f"grep pattern exceeds the {limits.max_grep_pattern_length}-character maximum"
            )
        roots = value["roots"]
        if not isinstance(roots, list) or not roots or any(not isinstance(root, str) for root in roots):
            raise InvalidEvidenceRequest("grep roots must be a non-empty array of repository-relative paths")
        if len(roots) > limits.max_grep_roots:
            raise InvalidEvidenceRequest(f"grep supports at most {limits.max_grep_roots} roots")
        return GrepRequest(pattern, tuple(_path(root, limits) for root in roots))
    if kind == "git_show":
        _require_keys(value, {"kind", "ref", "path"})
        ref = _git_ref(value["ref"], "ref", limits)
        return GitShowRequest(ref, _path(value["path"], limits))
    if kind == "diff_file":
        _require_keys(value, {"kind", "path", "base_ref", "head_ref"})
        path = _path(value["path"], limits)
        base_ref = _git_ref(value["base_ref"], "base_ref", limits)
        head_ref = _git_ref(value["head_ref"], "head_ref", limits)
        return DiffFileRequest(path, base_ref, head_ref)
    if kind == "list_dir":
        _require_keys(value, {"kind", "path"})
        return ListDirRequest(_path(value["path"], limits))
    if kind == "test":
        _require_keys(value, {"kind", "test_id"})
        test_id = value["test_id"]
        if not isinstance(test_id, str) or not _TEST_ID.fullmatch(test_id):
            raise InvalidEvidenceRequest("test_id must match the configured identifier format")
        if project_config is not None and test_id not in project_config.tests:
            raise UnknownTestId(f"Test id {test_id!r} is not in the configured test registry")
        return TestRequest(test_id)
    if kind == "git_log":
        _require_keys(value, {"kind", "max_entries"}, optional={"path"})
        max_entries = _integer(value["max_entries"], "max_entries", minimum=1)
        if max_entries > limits.max_git_log_entries:
            raise InvalidEvidenceRequest(
                f"git_log max_entries exceeds the {limits.max_git_log_entries}-entry maximum"
            )
        path = _path(value["path"], limits) if "path" in value else None
        return GitLogRequest(max_entries, path)
    if kind == "git_status":
        _require_keys(value, {"kind"})
        return GitStatusRequest()
    raise AssertionError("_ALLOWED_KINDS and validators are out of sync")


def _require_keys(value: dict[str, object], required: set[str], optional: set[str] | None = None) -> None:
    allowed = required | (optional or set())
    missing = required - value.keys()
    extra = value.keys() - allowed
    if missing or extra:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(sorted(missing)))
        if extra:
            parts.append("unexpected " + ", ".join(sorted(extra)))
        raise InvalidEvidenceRequest("Evidence request has " + " and ".join(parts))


def _path(value: object, limits: EvidenceLimits) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidEvidenceRequest("path must be a non-empty repository-relative string")
    if len(value) > limits.max_path_length:
        raise UnsafeEvidenceRequest(f"path exceeds the {limits.max_path_length}-character maximum")
    try:
        return normalize_repo_relative_path(value)
    except PathSafetyError as exc:
        raise UnsafeEvidenceRequest(str(exc)) from exc


def _git_ref(value: object, label: str, limits: EvidenceLimits) -> str:
    if not isinstance(value, str) or not value or len(value) > limits.max_git_ref_length:
        raise InvalidEvidenceRequest(
            f"{label} must be non-empty text no longer than {limits.max_git_ref_length} characters"
        )
    # Refs remain data passed later through a fixed argument array. Exclude option-like,
    # whitespace, pathspec, and control syntax while allowing common revision expressions.
    if (
        value.startswith("-")
        or not _GIT_REF_CHARS.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith("/")
    ):
        raise UnsafeEvidenceRequest(f"{label} is not an allowed Git ref expression")
    return value


def _integer(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise InvalidEvidenceRequest(f"{label} must be an integer >= {minimum}")
    return value
