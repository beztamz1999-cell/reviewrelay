from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from reviewrelay.config import ProjectConfig, RepoConfig
from reviewrelay.evidence_dsl import (
    DiffFileRequest, GitLogRequest, GitShowRequest, GitStatusRequest, GrepRequest,
    ListDirRequest, ReadFileRequest, ReadRangeRequest, TestRequest,
)
from reviewrelay.evidence_executor import (
    EvidenceExecutionContext, ExecutionLimits, LocalEvidenceExecutor,
)
from reviewrelay.evidence_process import legacy_test_argv
from reviewrelay.models import TaskRecord, TaskState
from reviewrelay.state import StateStore
from reviewrelay.storage import PortableDataRoot, TaskStorage


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def context(git_repo, tmp_path) -> EvidenceExecutionContext:
    head = git(git_repo, "rev-parse", "HEAD")
    data = PortableDataRoot(tmp_path / "data").create()
    storage = TaskStorage(data)
    record = TaskRecord("local", "t1", TaskState.COLLECT_EVIDENCE,
                        base_sha=head, candidate_sha=head, review_cycle=1)
    storage.create_task("local", "t1", record)
    with StateStore(data) as state:
        state.save(record)
    config = ProjectConfig("local", RepoConfig(str(git_repo)),
                           tests={"pass": (sys.executable, "-c", "print('ok')"),
                                  "fail": (sys.executable, "-c", "raise SystemExit(3)"),
                                  "timeout": (sys.executable, "-c", "import time; time.sleep(5)"),
                                  "literal": (sys.executable, "-c", "import sys; print(sys.argv[1])", "; $(whoami)"),
                                  "mutate": (sys.executable, "-c",
                                             "import subprocess,pathlib; pathlib.Path('new.txt').write_text('new'); "
                                             "subprocess.run(['git','add','new.txt'],check=True); "
                                             "subprocess.run(['git','commit','-m','mutated'],check=True)"),
                                  "legacy": 'python -c "print(42)"'})
    return EvidenceExecutionContext("local", "t1", git_repo, head, head, 1, config, storage)


def run(context, *requests, limits=None):
    return asyncio.run(LocalEvidenceExecutor(limits=limits).execute_batch(tuple(requests), context))


def fresh(context, cycle):
    with StateStore(context.task_storage.data_root) as state:
        record = state.get(context.project_id, context.task_id)
        record = replace(record, review_cycle=cycle)
        state.save(record)
    context.task_storage.persist_task_record(record)
    return replace(context, review_cycle=cycle)


def set_candidate(context, head):
    with StateStore(context.task_storage.data_root) as state:
        record = replace(state.get(context.project_id, context.task_id), candidate_sha=head)
        state.save(record)
    context.task_storage.persist_task_record(record)
    return replace(context, candidate_sha=head)


def test_complete_batch_manifest_hashes_order_and_local_only(context):
    result = run(context, ReadFileRequest("base.txt"), GrepRequest("base", ("base.txt",)),
                 GitStatusRequest(), TestRequest("pass"))
    assert result.complete and result.status == "COMPLETE"
    assert [p.name for p in result.upload_artifacts] == [
        "evidence-manifest.json", "request-001-read_file.md", "request-002-grep.md",
        "request-003-git_status.txt", "request-004-test.txt"]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["batch_complete"] and manifest["request_count"] == 4
    assert manifest["head_before"] == manifest["head_after"] == context.candidate_sha
    for artifact in result.upload_artifacts[1:]:
        assert manifest["artifact_sha256"][artifact.name] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert artifact.is_relative_to(context.task_storage.task_root("local", "t1") / "scratch" / "evidence")
    assert result.results[-1].status == "TEST_PASS"
    assert "import reviewrelay.worker" not in Path("src/reviewrelay/evidence_executor.py").read_text()


def test_wrong_type_identity_and_candidate_rejected(context):
    with pytest.raises(Exception):
        run(context, {"kind": "git_status"})
    with pytest.raises(Exception):
        run(replace(context, task_id="another"), GitStatusRequest())
    with pytest.raises(Exception):
        run(replace(context, candidate_sha="0" * 40), GitStatusRequest())
    with pytest.raises(Exception):
        run(replace(context, repo_path=context.task_storage.data_root.path), GitStatusRequest())


@pytest.mark.parametrize("evidence_request,expected", [
    (ReadFileRequest("missing.txt"), "EVIDENCE_EXECUTION_ERROR"),
    (ReadFileRequest("."), "INVALID_EVIDENCE_REQUEST"),
])
def test_invalid_paths_fail(context, evidence_request, expected):
    if evidence_request.path == ".":
        with pytest.raises(Exception):
            run(context, evidence_request)
    else:
        result = run(context, evidence_request)
        assert not result.complete and result.upload_artifacts == ()
        assert result.results[0].status == expected


def test_file_range_binary_and_oversize(context, commit_change):
    repo = Path(context.repo_path)
    (repo / "lines.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"a\x00b")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "files")
    head = git(repo, "rev-parse", "HEAD")
    context = set_candidate(context, head)
    result = run(context, ReadRangeRequest("lines.txt", 2, 5), ReadFileRequest("binary.bin"))
    assert not result.complete and result.results[0].metadata["available_end"] == 3
    assert "2: two\n3: three\n" == result.results[0].artifact_paths[0].read_text()
    assert result.results[1].status == "EVIDENCE_BINARY_FILE"
    context = fresh(context, 2)
    limits = ExecutionLimits(read_file_bytes=2)
    result = run(context, ReadFileRequest("lines.txt"), limits=limits)
    assert result.results[0].status == "EVIDENCE_FILE_TOO_LARGE"


def test_grep_literal_binary_skip_and_caps(context):
    repo = Path(context.repo_path)
    (repo / "src").mkdir()
    (repo / "src" / "a.txt").write_text("needle\nneedle $(whoami)\n", encoding="utf-8")
    (repo / "src" / "b.bin").write_bytes(b"needle\x00x")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "grep")
    head = git(repo, "rev-parse", "HEAD")
    context = set_candidate(context, head)
    result = run(context, GrepRequest("$(whoami)", ("src", "base.txt")))
    assert result.complete and result.results[0].metadata["binary_skipped"] == 1
    assert "src/a.txt:2:" in result.results[0].artifact_paths[0].read_text()
    context = fresh(context, 2)
    result = run(context, GrepRequest("needle", ("src",)), limits=ExecutionLimits(grep_matches=1))
    assert result.complete and result.results[0].truncated


def test_git_operations_valid(context, commit_change):
    repo = Path(context.repo_path)
    old = context.candidate_sha
    new = commit_change(repo, "src/a.txt", "changed\n")
    context = set_candidate(context, new)
    result = run(context, GitShowRequest(old, "base.txt"), DiffFileRequest("src/a.txt", old, new),
                 GitLogRequest(2, "src/a.txt"), ListDirRequest("src"))
    assert result.complete
    assert result.results[0].artifact_paths[0].read_text() == "base\n"
    assert "+changed" in result.results[1].artifact_paths[0].read_text()
    assert new in result.results[2].artifact_paths[0].read_text()
    assert "a.txt" in result.results[3].artifact_paths[0].read_text()


def test_test_runner_fail_timeout_literal_and_legacy(context):
    result = run(context, TestRequest("fail"), TestRequest("literal"), TestRequest("legacy"),
                 TestRequest("timeout"), limits=ExecutionLimits(test_timeout_seconds=0.5))
    assert [r.status for r in result.results] == ["TEST_FAIL", "TEST_PASS", "TEST_PASS", "TEST_TIMEOUT"]
    assert not result.complete and result.upload_artifacts == ()
    assert "$(whoami)" in result.results[1].artifact_paths[0].read_text()
    assert legacy_test_argv('python -m pytest "tests/with space.py"') == (
        "python", "-m", "pytest", "tests/with space.py")
    assert legacy_test_argv('python -c "print(42)"') == ("python", "-c", "print(42)")


def test_failed_test_is_valid_evidence_and_output_caps_are_marked(context):
    result = run(context, TestRequest("fail"))
    assert result.complete and result.results[0].status == "TEST_FAIL"
    context = fresh(context, 2)
    config = replace(context.project_config, tests={**context.project_config.tests,
        "loud": (sys.executable, "-c", "import sys; print('x'*200); print('y'*200,file=sys.stderr)")})
    result = run(replace(context, project_config=config), TestRequest("loud"),
                 limits=ExecutionLimits(test_stdout_bytes=30, test_stderr_bytes=20))
    assert result.complete and result.results[0].truncated
    text = result.results[0].artifact_paths[0].read_text()
    assert "stdout_truncated=True" in text and "stderr_truncated=True" in text


def test_unknown_test_and_launch_failure_are_incomplete(context):
    with pytest.raises(Exception):
        run(context, TestRequest("not-configured"))
    config = replace(context.project_config, tests={**context.project_config.tests,
        "missing-exe": ("reviewrelay-does-not-exist-4057",)})
    result = run(replace(context, project_config=config), TestRequest("missing-exe"))
    assert not result.complete and result.results[0].status == "TEST_EXECUTION_ERROR"
    assert result.upload_artifacts == ()


def test_git_failure_and_output_limit_are_incomplete(context):
    result = run(context, GitShowRequest("HEAD", "not-there.txt"))
    assert not result.complete and result.results[0].status == "EVIDENCE_GIT_FAILED"
    context = fresh(context, 2)
    result = run(context, GitShowRequest("HEAD", "base.txt"),
                 limits=ExecutionLimits(git_output_bytes=2))
    assert not result.complete and result.results[0].status == "EVIDENCE_OUTPUT_TOO_LARGE"


def test_directory_cap_and_batch_total_cap(context, commit_change):
    repo = Path(context.repo_path)
    new = commit_change(repo, "folder/a.txt", "a")
    new = commit_change(repo, "folder/b.txt", "b")
    context = set_candidate(context, new)
    result = run(context, ListDirRequest("folder"), limits=ExecutionLimits(directory_entries=1))
    assert result.complete and result.results[0].truncated
    assert result.results[0].metadata == {"shown_entries": 1, "total_entries": 2}
    context = fresh(context, 2)
    result = run(context, ReadFileRequest("base.txt"),
                 limits=ExecutionLimits(batch_total_bytes=40))
    assert not result.complete and result.upload_artifacts == ()
    assert result.status == "EVIDENCE_BATCH_TOO_LARGE"


def test_executor_lock_and_repeat_batch_directory(context):
    first = run(context, GitStatusRequest())
    second = run(context, GitStatusRequest())
    assert first.complete and second.complete
    assert first.manifest_path != second.manifest_path
    lock = context.task_storage.task_root("local", "t1") / "scratch" / "evidence" / ".execution.lock"
    lock.write_text("busy")
    with pytest.raises(Exception, match="Another evidence batch"):
        run(context, GitStatusRequest())
    lock.unlink()


def test_git_status_records_dirty_candidate_without_upload(context):
    (Path(context.repo_path) / "untracked.txt").write_text("dirty", encoding="utf-8")
    result = run(context, GitStatusRequest())
    assert result.results[0].status == "OK"
    assert result.results[0].metadata["clean"] is False
    assert result.status == "CANDIDATE_INVALID_DIRTY_WORKTREE"
    assert not result.complete and result.upload_artifacts == ()


def test_grep_scanned_file_cap_is_explicit(context, commit_change):
    repo = Path(context.repo_path)
    head = commit_change(repo, "src/a.txt", "needle\n")
    head = commit_change(repo, "src/b.txt", "needle\n")
    context = set_candidate(context, head)
    result = run(context, GrepRequest("needle", ("src",)),
                 limits=ExecutionLimits(grep_scanned_files=1))
    assert result.complete and result.results[0].truncated
    assert result.results[0].metadata["scanned_files"] == 1


@pytest.mark.parametrize("path", ["../outside.txt", "C:/outside.txt", r"\\server\share\x"])
def test_typed_request_cannot_smuggle_lexical_escape(context, path):
    with pytest.raises(Exception):
        run(context, ReadFileRequest(path))


def test_candidate_mutation_invalidates_all_uploads(context):
    result = run(context, ReadFileRequest("base.txt"), TestRequest("mutate"), GitStatusRequest())
    assert result.status == "CANDIDATE_MUTATED_DURING_REVIEW"
    assert not result.complete and result.upload_artifacts == ()
    assert result.head_after != result.head_before
    assert result.results[-1].status == "NOT_EXECUTED"


def test_symlink_escape_and_inside(context, tmp_path):
    repo = Path(context.repo_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = repo / "outside-link"
    inside = repo / "inside-link"
    try:
        link.symlink_to(outside)
        inside.symlink_to(repo / "base.txt")
    except OSError:
        pytest.skip("Symlink creation is unavailable on this host")
    # Links are committed so strict candidate mode remains clean.
    git(repo, "add", "outside-link", "inside-link")
    git(repo, "commit", "-m", "links")
    head = git(repo, "rev-parse", "HEAD")
    context = set_candidate(context, head)
    result = run(context, ReadFileRequest("inside-link"), ReadFileRequest("outside-link"))
    assert result.results[0].status == "OK"
    assert result.results[1].status == "UNSAFE_PATH"
    assert not result.complete and result.upload_artifacts == ()


def test_windows_junction_escape(context, tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction only")
    repo = Path(context.repo_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    junction = repo / "junction"
    subprocess.run(("cmd", "/c", "mklink", "/J", str(junction), str(outside)),
                   capture_output=True, check=True)
    # A junction left untracked dirties the worktree: obtain a clean committed candidate.
    git(repo, "add", "junction")
    git(repo, "commit", "-m", "junction")
    head = git(repo, "rev-parse", "HEAD")
    context = set_candidate(context, head)
    for request in (
        ReadFileRequest("junction/secret.txt"), ReadRangeRequest("junction/secret.txt", 1, 1),
        GrepRequest("private", ("junction",)), ListDirRequest("junction"),
        GitShowRequest("HEAD", "junction/secret.txt"),
        DiffFileRequest("junction/secret.txt", "HEAD", "HEAD"),
        GitLogRequest(1, "junction/secret.txt"),
    ):
        result = run(context, request)
        assert not result.complete and result.results[0].status == "UNSAFE_PATH"
        assert result.upload_artifacts == ()


def test_gc_removes_evidence_scratch_preserves_durable(context):
    from reviewrelay.gc import GarbageCollector
    result = run(context, ReadFileRequest("base.txt"))
    task_root = context.task_storage.task_root("local", "t1")
    with StateStore(context.task_storage.data_root) as state:
        state.save(replace(state.get("local", "t1"), task_state=TaskState.COMPLETE))
    context.task_storage.persist_task_record(TaskRecord("local", "t1", TaskState.COMPLETE))
    report = task_root / "durable" / "worker-report.md"
    report.write_text("durable", encoding="utf-8")
    assert GarbageCollector(context.task_storage.data_root).compact_completed_task("local", "t1")
    assert not result.manifest_path.exists() and report.read_text() == "durable"
