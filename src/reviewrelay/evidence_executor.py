"""Phase 5 local execution of validated reviewer evidence requests."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import ProjectConfig, validate_identifier
from .errors import CandidateMutatedDuringReview, InvalidEvidenceRequest, PathSafetyError, ReviewRelayError
from .evidence_dsl import (
    DiffFileRequest, EvidenceRequest, GitLogRequest, GitShowRequest, GitStatusRequest,
    GrepRequest, ListDirRequest, ReadFileRequest, ReadRangeRequest, TestRequest,
    validate_evidence_request,
)
from .evidence_process import legacy_test_argv, run_bounded
from .models import utc_now_iso
from .state import StateStore
from .storage import PortableDataRoot, TaskStorage


@dataclass(frozen=True)
class ExecutionLimits:
    read_file_bytes: int = 256 * 1024
    read_range_output_bytes: int = 128 * 1024
    grep_scanned_files: int = 2000
    grep_scanned_bytes: int = 16 * 1024 * 1024
    grep_matches: int = 300
    grep_output_bytes: int = 128 * 1024
    git_output_bytes: int = 256 * 1024
    directory_entries: int = 300
    test_stdout_bytes: int = 256 * 1024
    test_stderr_bytes: int = 128 * 1024
    batch_total_bytes: int = 2 * 1024 * 1024
    test_timeout_seconds: float = 120
    git_timeout_seconds: float = 20
    max_text_file_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
               or (isinstance(value, float) and not math.isfinite(value))
               for value in vars(self).values()):
            raise ValueError("Evidence limits must be positive numbers")


@dataclass(frozen=True)
class EvidenceExecutionContext:
    project_id: str
    task_id: str
    repo_path: str | Path
    base_sha: str
    candidate_sha: str
    review_cycle: int
    project_config: ProjectConfig
    task_storage: TaskStorage


@dataclass(frozen=True)
class EvidenceResult:
    request_index: int
    kind: str
    status: str
    summary: str
    artifact_paths: tuple[Path, ...] = ()
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceBatchResult:
    project_id: str
    task_id: str
    candidate_sha: str
    review_cycle: int
    head_before: str
    head_after: str
    complete: bool
    status: str
    results: tuple[EvidenceResult, ...]
    manifest_path: Path | None
    upload_artifacts: tuple[Path, ...]


class EvidenceExecutor(Protocol):
    async def execute(self, request: EvidenceRequest, context: EvidenceExecutionContext) -> EvidenceBatchResult: ...

    async def execute_batch(self, requests: tuple[EvidenceRequest, ...], context: EvidenceExecutionContext) -> EvidenceBatchResult: ...


class _OperationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


_REQUEST_TYPES = (ReadFileRequest, ReadRangeRequest, GrepRequest, GitShowRequest,
                  DiffFileRequest, ListDirRequest, TestRequest, GitLogRequest, GitStatusRequest)


class LocalEvidenceExecutor:
    """One bounded local batch; no worker or browser dependencies."""

    def __init__(self, *, limits: ExecutionLimits | None = None, git_executable: str = "git") -> None:
        self.limits = limits or ExecutionLimits()
        self.git_executable = git_executable
        self._running = False

    async def execute(self, request: EvidenceRequest, context: EvidenceExecutionContext) -> EvidenceBatchResult:
        return await self.execute_batch((request,), context)

    async def execute_batch(self, requests: tuple[EvidenceRequest, ...], context: EvidenceExecutionContext) -> EvidenceBatchResult:
        if self._running:
            raise InvalidEvidenceRequest("An evidence batch is already active")
        if not isinstance(requests, tuple) or not requests or len(requests) > 10:
            raise InvalidEvidenceRequest("Evidence batch needs 1 to 10 typed requests")
        for request in requests:
            if type(request) not in _REQUEST_TYPES:
                raise InvalidEvidenceRequest("Executor accepts only exact Phase 2 typed requests")
            raw = asdict(request)
            if isinstance(request, GrepRequest):
                raw["roots"] = list(request.roots)
            checked = validate_evidence_request(raw, project_config=context.project_config)
            if checked != request:
                raise InvalidEvidenceRequest("Typed evidence request did not revalidate")
        self._running = True
        lock = None
        owned_lock = False
        try:
            if not isinstance(context.task_storage, TaskStorage):
                raise InvalidEvidenceRequest("Expected managed task storage")
            if context.task_storage.data_root.path.resolve().is_relative_to(Path(context.repo_path).resolve()):
                raise PathSafetyError("Portable data root must be outside the task repository")
            task_root = context.task_storage.task_root(context.project_id, context.task_id)
            if not (task_root / "durable" / "task.json").is_file():
                raise InvalidEvidenceRequest("Managed task record is missing")
            lock_dir = context.task_storage.data_root.safe_path(
                task_root.relative_to(context.task_storage.data_root.path) / "scratch" / "evidence")
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock = context.task_storage.data_root.safe_path(
                lock_dir.relative_to(context.task_storage.data_root.path) / ".execution.lock")
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise InvalidEvidenceRequest("Another evidence batch owns this task") from exc
            owned_lock = True
            os.close(fd)
            return await self._execute_checked(requests, context)
        finally:
            if owned_lock and lock is not None:
                lock.unlink(missing_ok=True)
            self._running = False

    async def _execute_checked(self, requests, context):
        root, before_status = await self._bind(context)
        if before_status and any(not isinstance(request, GitStatusRequest) for request in requests):
            raise _OperationError("CANDIDATE_INVALID_DIRTY_WORKTREE", "Candidate worktree is not clean")
        cycle_dir = context.task_storage.data_root.safe_path(
            Path("active") / context.project_id / context.task_id / "scratch" / "evidence" /
            f"cycle-{context.review_cycle:02d}")
        if cycle_dir.is_relative_to(root):
            raise PathSafetyError("Evidence storage must be outside the task repository")
        cycle_dir.mkdir(parents=True, exist_ok=True)
        base = context.task_storage.data_root.safe_path(
            cycle_dir.relative_to(context.task_storage.data_root.path) / f"batch-{uuid.uuid4().hex}")
        base.mkdir(exist_ok=False)
        context.task_storage.data_root.assert_managed_path(base)
        results: list[EvidenceResult] = []
        artifacts: list[Path] = []
        hashes: dict[str, str] = {}
        total = 0
        failed = bool(before_status)
        for index, request in enumerate(requests, 1):
            # Rebind before every operation; a prior test may have changed the candidate.
            if await self._changed(context, before_status):
                failed = True
                results.extend(EvidenceResult(i, r.kind, "NOT_EXECUTED", "Candidate changed")
                               for i, r in enumerate(requests[index - 1:], index))
                break
            try:
                content, suffix, truncated, metadata, status = await self._run_one(request, root, context)
                encoded = content.encode("utf-8")
                if total + len(encoded) > self.limits.batch_total_bytes:
                    raise _OperationError("EVIDENCE_BATCH_TOO_LARGE", "Evidence batch total size exceeded")
                name = f"request-{index:03d}-{request.kind}.{suffix}"
                path = context.task_storage.data_root.safe_path(base.relative_to(context.task_storage.data_root.path) / name)
                with path.open("xb") as stream:
                    stream.write(encoded)
                total += len(encoded)
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                artifacts.append(path)
                if status not in {"OK", "TEST_PASS", "TEST_FAIL"}:
                    failed = True
                results.append(EvidenceResult(index, request.kind, status, status, (path,), truncated, metadata))
            except (_OperationError, ReviewRelayError, OSError, UnicodeError) as exc:
                failed = True
                code = getattr(exc, "code", "EVIDENCE_EXECUTION_ERROR")
                results.append(EvidenceResult(index, request.kind, code, str(exc)))
                if isinstance(exc, PathSafetyError) or code in {"UNSAFE_PATH", "CANDIDATE_MUTATED_DURING_REVIEW"}:
                    results.extend(EvidenceResult(i, r.kind, "NOT_EXECUTED", "Batch stopped on unsafe request")
                                   for i, r in enumerate(requests[index:], index + 1))
                    break
        after_head, after_status = await self._inspect(root)
        mutated = after_head != context.candidate_sha.lower() or after_status != before_status
        status = ("CANDIDATE_MUTATED_DURING_REVIEW" if mutated else
                  "CANDIDATE_INVALID_DIRTY_WORKTREE" if before_status else
                  "INCOMPLETE" if failed else "COMPLETE")
        complete = not mutated and not failed
        manifest = {
            "protocol": "reviewrelay/evidence/1", "project_id": context.project_id,
            "task_id": context.task_id, "review_cycle": context.review_cycle,
            "candidate_sha": context.candidate_sha.lower(), "head_before": context.candidate_sha.lower(),
            "head_after": after_head, "created_at": utc_now_iso(),
            "batch_complete": complete, "status": status, "request_count": len(requests),
            "results": [{"index": r.request_index, "kind": r.kind, "status": r.status,
                         "truncated": r.truncated, "artifact": [p.name for p in r.artifact_paths],
                         "metadata": r.metadata} for r in results], "artifact_sha256": hashes,
        }
        manifest_path = context.task_storage.data_root.safe_path(
            base.relative_to(context.task_storage.data_root.path) / "evidence-manifest.json")
        serialized = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if total + len(serialized) > self.limits.batch_total_bytes:
            return EvidenceBatchResult(context.project_id, context.task_id, context.candidate_sha.lower(),
                                       context.review_cycle, context.candidate_sha.lower(), after_head,
                                       False, "EVIDENCE_BATCH_TOO_LARGE", tuple(results), None, ())
        with manifest_path.open("xb") as stream:
            stream.write(serialized)
        return EvidenceBatchResult(context.project_id, context.task_id, context.candidate_sha.lower(),
                                   context.review_cycle, context.candidate_sha.lower(), after_head,
                                   complete, status, tuple(results), manifest_path,
                                   (manifest_path, *artifacts) if complete else ())

    async def _bind(self, context: EvidenceExecutionContext) -> tuple[Path, str]:
        validate_identifier(context.project_id, "project_id")
        validate_identifier(context.task_id, "task_id")
        if context.project_config.project_id != context.project_id:
            raise InvalidEvidenceRequest("Project identity mismatch")
        if isinstance(context.review_cycle, bool) or not isinstance(context.review_cycle, int) or context.review_cycle < 1:
            raise InvalidEvidenceRequest("Invalid review cycle")
        root = Path(context.repo_path).resolve(strict=True)
        configured = Path(context.project_config.repo.path).resolve(strict=True)
        if os.path.normcase(str(root)) != os.path.normcase(str(configured)):
            raise InvalidEvidenceRequest("Configured repository mismatch")
        if not isinstance(context.task_storage, TaskStorage):
            raise InvalidEvidenceRequest("Expected managed task storage")
        task_root = context.task_storage.task_root(context.project_id, context.task_id)
        if not (task_root / "durable" / "task.json").is_file():
            raise InvalidEvidenceRequest("Managed task record is missing")
        try:
            durable = json.loads((task_root / "durable" / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InvalidEvidenceRequest("Managed task metadata is invalid") from exc
        if any(durable.get(name) != value for name, value in (
            ("project_id", context.project_id), ("task_id", context.task_id),
            ("base_sha", context.base_sha), ("candidate_sha", context.candidate_sha),
            ("review_cycle", context.review_cycle))):
            raise InvalidEvidenceRequest("Durable task/candidate binding mismatch")
        with StateStore(context.task_storage.data_root) as state:
            task = state.get(context.project_id, context.task_id)
        if task is None or (task.base_sha, task.candidate_sha, task.review_cycle) != (
                context.base_sha, context.candidate_sha, context.review_cycle):
            raise InvalidEvidenceRequest("Task/candidate/review cycle binding mismatch")
        head, status = await self._inspect(root)
        if head != context.candidate_sha.lower():
            raise CandidateMutatedDuringReview("Candidate HEAD differs before evidence collection")
        return root, status

    async def _inspect(self, root: Path) -> tuple[str, str]:
        top, _ = await self._git(root, "rev-parse", "--show-toplevel", cap=4096)
        if os.path.normcase(str(Path(top.strip()).resolve(strict=True))) != os.path.normcase(str(root)):
            raise InvalidEvidenceRequest("Git root does not match configured repository")
        head, _ = await self._git(root, "rev-parse", "HEAD", cap=256)
        if len(head.strip()) not in (40, 64) or any(c not in "0123456789abcdefABCDEF" for c in head.strip()):
            raise InvalidEvidenceRequest("Invalid Git HEAD")
        status, _ = await self._git(root, "status", "--porcelain=v1", "--untracked-files=all")
        return head.strip().lower(), status

    async def _changed(self, context, initial_status: str) -> bool:
        head, status = await self._inspect(Path(context.repo_path))
        return head != context.candidate_sha.lower() or status != initial_status

    @staticmethod
    def _path(root: Path, relative: str, *, required: bool = True) -> Path:
        # Phase 2 lexical validation is repeated at the executor boundary.
        from .evidence import normalize_repo_relative_path
        clean = normalize_repo_relative_path(relative)
        path = root.joinpath(*clean.split("/"))
        resolved = path.resolve(strict=required)
        if not resolved.is_relative_to(root):
            raise PathSafetyError(f"Resolved path escapes repository: {relative}")
        return resolved

    @staticmethod
    def _read_text(path: Path, max_bytes: int) -> str:
        if not path.is_file():
            raise _OperationError("EVIDENCE_NOT_REGULAR_FILE", "Expected regular file")
        if path.stat().st_size > max_bytes:
            raise _OperationError("EVIDENCE_FILE_TOO_LARGE", "File exceeds configured byte limit")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise _OperationError("EVIDENCE_FILE_TOO_LARGE", "File grew beyond configured limit")
        if b"\x00" in data:
            raise _OperationError("EVIDENCE_BINARY_FILE", "Binary file cannot be rendered as text")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _OperationError("EVIDENCE_DECODE_FAILED", "File is not valid UTF-8") from exc

    async def _git(self, root: Path, *args: str, cap: int | None = None, allow_truncate: bool = False):
        limit = cap or self.limits.git_output_bytes
        result = await run_bounded((self.git_executable, "--no-pager", *args), str(root),
                                   timeout=self.limits.git_timeout_seconds,
                                   stdout_cap=limit, stderr_cap=4096,
                                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"})
        if result.timed_out:
            raise _OperationError("EVIDENCE_GIT_TIMEOUT", "Git evidence timed out")
        if result.launch_error or result.exit_code != 0:
            raise _OperationError("EVIDENCE_GIT_FAILED", result.launch_error or result.stderr.decode("utf-8", "replace")[:1000])
        if result.stdout_truncated and not allow_truncate:
            raise _OperationError("EVIDENCE_OUTPUT_TOO_LARGE", "Git output exceeded configured limit")
        try:
            return result.stdout.decode("utf-8"), result.stdout_truncated
        except UnicodeDecodeError as exc:
            raise _OperationError("EVIDENCE_DECODE_FAILED", "Git output is not UTF-8") from exc

    async def _run_one(self, request, root, context):
        limits = self.limits
        if isinstance(request, ReadFileRequest):
            path = self._path(root, request.path)
            text = self._read_text(path, limits.read_file_bytes)
            return text, "md", False, {"path": request.path, "bytes": path.stat().st_size}, "OK"
        if isinstance(request, ReadRangeRequest):
            path = self._path(root, request.path)
            # Decode by lines without retaining the whole file; byte cap protects huge line/files.
            text = self._read_text(path, limits.max_text_file_bytes)
            lines = text.splitlines(keepends=True)
            selected = lines[request.start_line - 1:request.end_line]
            output = "".join(f"{i}: {line}" for i, line in enumerate(selected, request.start_line))
            if len(output.encode("utf-8")) > limits.read_range_output_bytes:
                raise _OperationError("EVIDENCE_OUTPUT_TOO_LARGE", "Range output exceeded limit")
            return output, "md", False, {"requested_start": request.start_line,
                                          "requested_end": request.end_line,
                                          "available_end": min(request.end_line, len(lines))}, "OK"
        if isinstance(request, GrepRequest):
            def paths():
                for relative in request.roots:
                    source = self._path(root, relative)
                    if source.is_file():
                        yield source
                    elif source.is_dir():
                        yield from self._walk_safe(root, source)
                    else:
                        raise _OperationError("EVIDENCE_INVALID_ROOT", "Grep root is not a file or directory")
            output, files, scanned, matches, binary, capped = [], 0, 0, 0, 0, False
            seen: set[Path] = set()
            output_bytes = 0
            for path in paths():
                if files >= limits.grep_scanned_files or scanned >= limits.grep_scanned_bytes:
                    capped = True
                    break
                if path in seen:
                    continue
                seen.add(path)
                files += 1
                size = path.stat().st_size
                if size > limits.grep_scanned_bytes - scanned:
                    capped = True
                    break
                scanned += size
                try:
                    text = self._read_text(path, limits.max_text_file_bytes)
                except _OperationError as exc:
                    if exc.code in {"EVIDENCE_BINARY_FILE", "EVIDENCE_DECODE_FAILED"}:
                        binary += 1
                        continue
                    if exc.code == "EVIDENCE_FILE_TOO_LARGE":
                        capped = True
                        continue
                    raise
                for number, line in enumerate(text.splitlines(), 1):
                    if request.pattern not in line:
                        continue
                    entry = f"{path.relative_to(root).as_posix()}:{number}:{line}\n"
                    entry_bytes = len(entry.encode("utf-8"))
                    if matches >= limits.grep_matches or output_bytes + entry_bytes > limits.grep_output_bytes:
                        capped = True
                        break
                    output.append(entry)
                    output_bytes += entry_bytes
                    matches += 1
                if capped:
                    break
            return "".join(output), "md", capped, {"scanned_files": files, "scanned_bytes": scanned,
                                                       "matches": matches, "binary_skipped": binary}, "OK"
        if isinstance(request, GitShowRequest):
            # Historical Git blob lookup uses Git's path namespace, not filesystem traversal.
            self._path(root, request.path, required=False)
            text, capped = await self._git(root, "show", f"{request.ref}:{request.path}")
            return text, "md", capped, {"ref": request.ref, "path": request.path}, "OK"
        if isinstance(request, DiffFileRequest):
            self._path(root, request.path, required=False)
            text, capped = await self._git(root, "diff", "--no-ext-diff", "--no-color", request.base_ref,
                                           request.head_ref, "--", request.path)
            return text, "md", capped, {"base_ref": request.base_ref, "head_ref": request.head_ref}, "OK"
        if isinstance(request, ListDirRequest):
            source = self._path(root, request.path)
            if not source.is_dir():
                raise _OperationError("EVIDENCE_NOT_DIRECTORY", "Expected directory")
            # Stable first N entries with bounded memory, regardless of directory size.
            total_entries = 0
            def names():
                nonlocal total_entries
                for entry in source.iterdir():
                    total_entries += 1
                    yield entry
            entries = heapq.nsmallest(limits.directory_entries + 1, names(), key=lambda p: p.name)
            shown = []
            for entry in entries[:limits.directory_entries]:
                target = entry.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise PathSafetyError(f"Directory entry escapes repository: {entry.name}")
                shown.append({"name": entry.name, "type": "directory" if target.is_dir() else "file",
                              "path": entry.relative_to(root).as_posix()})
            return (json.dumps(shown, ensure_ascii=False, indent=2) + "\n", "json",
                    total_entries > len(shown),
                    {"shown_entries": len(shown), "total_entries": total_entries}, "OK")
        if isinstance(request, TestRequest):
            definition = context.project_config.tests.get(request.test_id)
            if definition is None:
                raise _OperationError("UNKNOWN_TEST_ID", "Test ID is not configured")
            argv = legacy_test_argv(definition) if isinstance(definition, str) else tuple(definition)
            if not argv or not argv[0] or any("\x00" in x for x in argv):
                raise _OperationError("EVIDENCE_INVALID_TEST_COMMAND", "Configured test argv is invalid")
            # Windows invokes batch files through cmd even with shell=False.
            if Path(argv[0]).suffix.lower() in {".bat", ".cmd"} or Path(argv[0]).name.lower() in {
                "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "bash.exe", "sh"}:
                raise _OperationError("EVIDENCE_INVALID_TEST_COMMAND", "Shell interpreters are not supported")
            result = await run_bounded(argv, str(root), timeout=limits.test_timeout_seconds,
                                       stdout_cap=limits.test_stdout_bytes, stderr_cap=limits.test_stderr_bytes)
            status = ("TEST_EXECUTION_ERROR" if result.launch_error else "TEST_TIMEOUT" if result.timed_out
                      else "TEST_PASS" if result.exit_code == 0 else "TEST_FAIL")
            text = (f"test_id={request.test_id}\nargv={json.dumps(argv)}\nexit_code={result.exit_code}\n"
                    f"duration_seconds={result.duration:.3f}\ntimeout={result.timed_out}\n"
                    f"stdout_truncated={result.stdout_truncated}\nstderr_truncated={result.stderr_truncated}\n"
                    f"launch_error={result.launch_error or ''}\n\nSTDOUT\n"
                    + result.stdout.decode("utf-8", "replace") + "\nSTDERR\n"
                    + result.stderr.decode("utf-8", "replace"))
            return (text, "txt", result.stdout_truncated or result.stderr_truncated,
                    {"test_id": request.test_id, "argv": list(argv), "exit_code": result.exit_code,
                     "duration_seconds": round(result.duration, 3), "timed_out": result.timed_out}, status)
        if isinstance(request, GitLogRequest):
            args = ["log", f"-n{request.max_entries}", "--format=%H%x09%aI%x09%s"]
            if request.path:
                self._path(root, request.path, required=False)
                args += ["--", request.path]
            text, capped = await self._git(root, *args)
            return text, "txt", capped, {"max_entries": request.max_entries}, "OK"
        if isinstance(request, GitStatusRequest):
            head, status = await self._inspect(root)
            text, _ = await self._git(root, "status", "--porcelain=v1", "-b", "--untracked-files=all")
            return (f"HEAD={head}\nCLEAN={not bool(status)}\n{text}", "txt", False,
                    {"head_sha": head, "clean": not bool(status)}, "OK")
        raise InvalidEvidenceRequest("Unsupported typed evidence request")

    def _walk_safe(self, root: Path, directory: Path):
        # Do not traverse .git internals or any directory link. Outside links fail closed.
        stack = [directory]
        while stack:
            current = stack.pop()
            for path in sorted(current.iterdir(), reverse=True):
                if path.name == ".git":
                    continue
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise PathSafetyError(f"Grep path escapes repository: {path.relative_to(root)}")
                if path.is_symlink():
                    continue
                if target.is_dir():
                    stack.append(target)
                elif target.is_file():
                    yield target
