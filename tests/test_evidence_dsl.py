from __future__ import annotations

import pytest

from reviewrelay.config import ProjectConfig, RepoConfig
from reviewrelay.errors import (
    InvalidEvidenceRequest,
    UnknownTestId,
    UnsafeEvidenceRequest,
)
from reviewrelay.evidence_dsl import (
    DEFAULT_EVIDENCE_LIMITS,
    DiffFileRequest,
    EvidenceLimits,
    GitLogRequest,
    GitShowRequest,
    GitStatusRequest,
    GrepRequest,
    ListDirRequest,
    ReadFileRequest,
    ReadRangeRequest,
    TestRequest,
    validate_evidence_request,
    validate_evidence_requests,
)


def config() -> ProjectConfig:
    return ProjectConfig(
        project_id="dsl-tests",
        repo=RepoConfig(path="G:/repo"),
        tests={"targeted_m4": "pytest tests/test_m4.py"},
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"kind": "read_file", "path": "src/Foo.cs"}, ReadFileRequest("src/Foo.cs")),
        ({"kind": "read_range", "path": "src/Foo.cs", "start_line": 20, "end_line": 80}, ReadRangeRequest("src/Foo.cs", 20, 80)),
        ({"kind": "grep", "pattern": "ActivityCompleted", "roots": ["src", "tests"]}, GrepRequest("ActivityCompleted", ("src", "tests"))),
        ({"kind": "git_show", "ref": "HEAD~1", "path": "src/Foo.cs"}, GitShowRequest("HEAD~1", "src/Foo.cs")),
        ({"kind": "diff_file", "path": "src/Foo.cs", "base_ref": "a" * 40, "head_ref": "HEAD"}, DiffFileRequest("src/Foo.cs", "a" * 40, "HEAD")),
        ({"kind": "list_dir", "path": "src/reviewrelay"}, ListDirRequest("src/reviewrelay")),
        ({"kind": "test", "test_id": "targeted_m4"}, TestRequest("targeted_m4")),
        ({"kind": "git_log", "max_entries": 20, "path": "src/Foo.cs"}, GitLogRequest(20, "src/Foo.cs")),
        ({"kind": "git_status"}, GitStatusRequest()),
    ],
)
def test_all_nine_evidence_operations_return_typed_requests(raw: dict[str, object], expected: object) -> None:
    actual = validate_evidence_request(raw, project_config=config())
    assert actual == expected


@pytest.mark.parametrize("path", [
    "../secret.txt",
    "../../secret.txt",
    r"..\secret.txt",
    r"C:\Users\Admin\secret.txt",
    r"C:secret.txt",
    r"\\server\share\secret.txt",
    "/root/file",
])
@pytest.mark.parametrize("operation", [
    "read_file", "read_range", "grep", "git_show", "diff_file", "list_dir", "git_log"
])
def test_all_path_bearing_operations_reject_absolute_and_traversal_paths(path: str, operation: str) -> None:
    request: dict[str, object] = {"kind": operation}
    if operation == "read_range":
        request.update(path=path, start_line=1, end_line=1)
    elif operation == "grep":
        request.update(pattern="needle", roots=[path])
    elif operation == "git_show":
        request.update(ref="HEAD", path=path)
    elif operation == "diff_file":
        request.update(path=path, base_ref="HEAD", head_ref="HEAD")
    elif operation == "git_log":
        request.update(max_entries=1, path=path)
    else:
        request["path"] = path
    with pytest.raises(UnsafeEvidenceRequest):
        validate_evidence_request(request)


def test_repo_relative_backslash_path_normalizes_through_phase1_validator() -> None:
    request = validate_evidence_request({"kind": "read_file", "path": r"src\Foo.cs"})
    assert request == ReadFileRequest("src/Foo.cs")


def test_empty_path_is_rejected_as_invalid_request() -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "read_file", "path": ""})


def test_read_range_inclusive_span_at_limit_is_allowed() -> None:
    request = validate_evidence_request({
        "kind": "read_range", "path": "src/Foo.cs", "start_line": 1,
        "end_line": DEFAULT_EVIDENCE_LIMITS.max_read_range_lines,
    })
    assert isinstance(request, ReadRangeRequest)


@pytest.mark.parametrize("start,end", [(0, 1), (4, 3), (1, 501), (True, 2), (1, False)])
def test_read_range_invalid_order_bounds_or_boolean_numbers_are_rejected(start: object, end: object) -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({
            "kind": "read_range", "path": "src/Foo.cs", "start_line": start, "end_line": end,
        })


def test_grep_keeps_shell_looking_text_as_literal_data() -> None:
    pattern = "literal ; $(whoami) | `echo nope`"
    request = validate_evidence_request({"kind": "grep", "pattern": pattern, "roots": ["src"]})
    assert isinstance(request, GrepRequest)
    assert request.pattern == pattern


@pytest.mark.parametrize("pattern", ["", "  ", "x" * 257, "\x00"])
def test_grep_rejects_empty_or_oversized_patterns(pattern: str) -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "grep", "pattern": pattern, "roots": ["src"]})


def test_grep_rejects_too_many_roots() -> None:
    roots = [f"src/{index}" for index in range(DEFAULT_EVIDENCE_LIMITS.max_grep_roots + 1)]
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "grep", "pattern": "needle", "roots": roots})


@pytest.mark.parametrize("ref", ["", "-c", "--help", "HEAD;whoami", "HEAD..main", "HEAD with-space", "x" * 129])
def test_git_show_rejects_option_like_or_invalid_refs(ref: str) -> None:
    with pytest.raises((InvalidEvidenceRequest, UnsafeEvidenceRequest)):
        validate_evidence_request({"kind": "git_show", "ref": ref, "path": "src/Foo.cs"})


def test_git_revision_expression_is_validated_as_data() -> None:
    request = validate_evidence_request({
        "kind": "git_show", "ref": "refs/heads/feature-1~2", "path": "src/Foo.cs",
    })
    assert request == GitShowRequest("refs/heads/feature-1~2", "src/Foo.cs")


@pytest.mark.parametrize("ref", ["", "x" * 129, "--output=x", "HEAD;whoami"])
def test_diff_file_requires_bounded_non_option_refs(ref: str) -> None:
    with pytest.raises((InvalidEvidenceRequest, UnsafeEvidenceRequest)):
        validate_evidence_request({
            "kind": "diff_file", "path": "src/Foo.cs", "base_ref": ref, "head_ref": "HEAD",
        })


def test_test_operation_only_accepts_configured_id_and_never_command_text() -> None:
    assert validate_evidence_request(
        {"kind": "test", "test_id": "targeted_m4"}, project_config=config()
    ) == TestRequest("targeted_m4")
    with pytest.raises(UnknownTestId) as error:
        validate_evidence_request({"kind": "test", "test_id": "arbitrary"}, project_config=config())
    assert error.value.code == "UNKNOWN_TEST_ID"
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({
            "kind": "test", "test_id": "targeted_m4", "command": "pytest anything",
        }, project_config=config())


def test_test_id_is_syntactically_valid_when_registry_is_not_supplied() -> None:
    assert validate_evidence_request({"kind": "test", "test_id": "future_registry_id"}) == TestRequest("future_registry_id")


def test_git_log_accepts_default_range_and_optional_path() -> None:
    assert validate_evidence_request({"kind": "git_log", "max_entries": 1}) == GitLogRequest(1)
    assert validate_evidence_request({"kind": "git_log", "max_entries": 100}) == GitLogRequest(100)


@pytest.mark.parametrize("count", [0, 101, True, "20"])
def test_git_log_rejects_invalid_or_excessive_entry_counts(count: object) -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "git_log", "max_entries": count})


def test_git_status_rejects_any_additional_parameters() -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "git_status", "path": "src"})


@pytest.mark.parametrize("kind", ["shell", "bash", "cmd", "powershell", "python", "exec", "run", "curl", "wget", "delete", "deploy"])
def test_forbidden_command_kinds_are_explicitly_rejected(kind: str) -> None:
    with pytest.raises(UnsafeEvidenceRequest) as error:
        validate_evidence_request({"kind": kind, "command": "anything"})
    assert error.value.code == "UNSAFE_EVIDENCE_REQUEST"


def test_unknown_operation_is_rejected() -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "custom_tool", "args": []})


def test_evidence_request_limit_is_enforced_without_truncation() -> None:
    requests = [{"kind": "git_status"}] * (DEFAULT_EVIDENCE_LIMITS.max_requests_per_response + 1)
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_requests(requests)


def test_custom_evidence_request_limit_is_supported() -> None:
    limits = EvidenceLimits(max_requests_per_response=2)
    assert len(validate_evidence_requests([{"kind": "git_status"}] * 2, limits=limits)) == 2
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_requests([{"kind": "git_status"}] * 3, limits=limits)


def test_path_length_limit_is_enforced() -> None:
    with pytest.raises(UnsafeEvidenceRequest):
        validate_evidence_request({"kind": "read_file", "path": "a" * 513})


def test_request_schemas_reject_unknown_flags() -> None:
    with pytest.raises(InvalidEvidenceRequest):
        validate_evidence_request({"kind": "grep", "pattern": "x", "roots": ["src"], "recursive": True})


def test_mixed_request_set_fails_as_a_whole() -> None:
    with pytest.raises(UnsafeEvidenceRequest):
        validate_evidence_requests([
            {"kind": "git_status"},
            {"kind": "powershell", "script": "Get-ChildItem"},
        ])
