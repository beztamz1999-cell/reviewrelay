"""Git-derived review-pack and changed-source evidence generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .config import validate_identifier
from .errors import CandidateInvalidDirtyWorktree, CandidateMutatedDuringReview, PathSafetyError, RepositoryError
from .git import GitClient
from .storage import PortableDataRoot, TaskStorage


_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_EXCLUDED_DIRS = {".git", "node_modules", "bin", "obj", "dist", "build", "vendor", ".venv", "venv", "__pycache__", "target"}
_EXCLUDED_NAMES = {"cookies", "cookie", "credentials", "secrets"}


@dataclass(frozen=True)
class ReviewPackResult:
    pack_path: Path
    manifest_path: Path
    manifest: dict[str, Any]
    artifact_hashes: dict[str, str]
    changed_paths: tuple[str, ...]
    snapshotted_paths: tuple[str, ...]


def normalize_repo_relative_path(value: str) -> str:
    """Normalize a repo-relative path and reject traversal/absolute forms."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PathSafetyError("Repository-relative path must be non-empty text")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PathSafetyError(f"Absolute repository path is not allowed: {value!r}")
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise PathSafetyError(f"Path traversal is not allowed: {value!r}")
    clean = [part for part in parts if part not in ("", ".")]
    if not clean or any(":" in part for part in clean):
        raise PathSafetyError(f"Invalid repository-relative path: {value!r}")
    return PurePosixPath(*clean).as_posix()


def build_review_pack(
    repository: str | Path,
    data_root: PortableDataRoot,
    *,
    pack_relative_path: str | Path,
    project_id: str,
    task_id: str,
    cycle: int,
    base_sha: str,
    head_sha: str,
    full_changed_files_limit: int = 12,
    strict_commit_mode: bool = True,
    task_text: str | None = None,
    git: GitClient | None = None,
) -> ReviewPackResult:
    project_id = validate_identifier(project_id, "project_id")
    task_id = validate_identifier(task_id, "task_id")
    if cycle < 1:
        raise ValueError("cycle must be >= 1")
    if full_changed_files_limit < 0:
        raise ValueError("full_changed_files_limit must be >= 0")
    _require_sha(base_sha, "base_sha")
    _require_sha(head_sha, "head_sha")
    configured_repository = Path(repository)
    client = git or GitClient()
    inspection = client.inspect(configured_repository, expected_repository=configured_repository)
    repository_path = Path(inspection.repository_root)
    if inspection.head_sha != head_sha.lower():
        raise RepositoryError(f"Requested HEAD {head_sha} is not current Git HEAD {inspection.head_sha}")
    if strict_commit_mode and not inspection.worktree_clean:
        raise CandidateInvalidDirtyWorktree("Cannot build a review pack from a dirty candidate worktree")
    pack_path = data_root.safe_path(pack_relative_path)
    if _path_is_inside(pack_path, repository_path):
        raise PathSafetyError("Review-pack output must stay outside the configured repository")
    pack_path.mkdir(parents=True, exist_ok=True)
    data_root.assert_managed_path(pack_path)
    _clear_previous_pack_outputs(pack_path, data_root)

    diff_range = f"{base_sha.lower()}..{head_sha.lower()}"
    patch = client.run(repository_path, "diff", "--no-ext-diff", "--no-color", diff_range, "--")
    diff_stat = client.run(repository_path, "diff", "--no-ext-diff", "--no-color", "--stat", diff_range, "--")
    changed_text = client.run(repository_path, "diff", "--no-ext-diff", "--no-color", "--name-status", diff_range, "--")
    status_text = client.run(repository_path, "status", "--porcelain")
    name_status_z = client.run(repository_path, "diff", "--no-ext-diff", "--no-color", "--name-status", "-z", diff_range, "--")
    numstat_z = client.run(repository_path, "diff", "--no-ext-diff", "--no-color", "--numstat", "-z", diff_range, "--")

    files = _parse_name_status_z(name_status_z)
    insertions, deletions = _parse_numstat_z(numstat_z)
    files = tuple(files)
    paths_written: dict[str, Path] = {}
    for name, content in (
        ("changes.patch", patch),
        ("diff-stat.txt", diff_stat),
        ("changed-files.txt", changed_text),
        ("git-status.txt", status_text),
    ):
        destination = data_root.safe_path(pack_path.relative_to(data_root.path) / name)
        _write_text(destination, content)
        paths_written[name] = destination

    snapshot_paths: list[str] = []
    if len(files) <= full_changed_files_limit:
        for status, repo_path in files:
            if status == "D" or _excluded_source(repo_path):
                continue
            source = _resolve_repo_file(repository_path, repo_path)
            if not source.is_file() and not source.is_symlink():
                continue
            relative = normalize_repo_relative_path(repo_path)
            destination = data_root.safe_path(pack_path.relative_to(data_root.path) / "source" / Path(*relative.split("/")))
            destination.parent.mkdir(parents=True, exist_ok=True)
            data_root.assert_managed_path(destination)
            if source.is_symlink():
                destination.write_text(os.readlink(source), encoding="utf-8", newline="\n")
            else:
                shutil.copyfile(source, destination)
            snapshot_paths.append(relative)
            paths_written[f"source/{relative}"] = destination
    snapshot_included = len(files) <= full_changed_files_limit

    task_storage = TaskStorage(data_root)
    task_root = task_storage.task_root(project_id, task_id)
    worker_report = task_root / "durable" / "worker-report.md"
    if worker_report.is_file():
        data_root.assert_managed_path(worker_report)
        destination = data_root.safe_path(pack_path.relative_to(data_root.path) / "worker-report.md")
        shutil.copyfile(worker_report, destination)
        paths_written["worker-report.md"] = destination
    if task_text is not None:
        destination = data_root.safe_path(pack_path.relative_to(data_root.path) / "task.md")
        _write_text(destination, task_text)
        paths_written["task.md"] = destination

    final_inspection = client.inspect(repository_path, expected_repository=repository_path)
    if (
        final_inspection.head_sha != inspection.head_sha
        or final_inspection.status_porcelain != inspection.status_porcelain
    ):
        raise CandidateMutatedDuringReview(
            "Repository HEAD or worktree status changed while ReviewRelay was collecting evidence"
        )
    if strict_commit_mode and not final_inspection.worktree_clean:
        raise CandidateMutatedDuringReview("Repository became dirty while ReviewRelay was collecting evidence")

    artifact_hashes = {
        name.replace(os.sep, "/"): _sha256(path)
        for name, path in sorted(paths_written.items())
        if path.is_file()
    }
    manifest = {
        "protocol": "review-relay/1",
        "reviewrelay_version": __version__,
        "project_id": project_id,
        "task_id": task_id,
        "cycle": cycle,
        "base_sha": base_sha.lower(),
        "head_sha": head_sha.lower(),
        "worktree_clean": inspection.worktree_clean,
        "files_changed": len(files),
        "insertions": insertions,
        "deletions": deletions,
        "created_at_utc": _utc_timestamp(),
        "source_snapshot_included": snapshot_included,
        "source_snapshot_files": sorted(snapshot_paths),
        "artifact_sha256": artifact_hashes,
    }
    manifest_path = data_root.safe_path(pack_path.relative_to(data_root.path) / "manifest.json")
    _write_text(manifest_path, json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n")
    return ReviewPackResult(
        pack_path=pack_path,
        manifest_path=manifest_path,
        manifest=manifest,
        artifact_hashes=artifact_hashes,
        changed_paths=tuple(path for _, path in files),
        snapshotted_paths=tuple(sorted(snapshot_paths)),
    )


def _parse_name_status_z(output: str) -> list[tuple[str, str]]:
    tokens = output.split("\x00")
    if tokens and tokens[-1] == "":
        tokens.pop()
    result: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"}:
            if i + 1 >= len(tokens):
                raise RepositoryError("Git returned malformed rename/copy name-status evidence")
            _old_path = normalize_repo_relative_path(tokens[i])
            new_path = normalize_repo_relative_path(tokens[i + 1])
            i += 2
            result.append((code, new_path))
        else:
            if i >= len(tokens):
                raise RepositoryError("Git returned malformed name-status evidence")
            result.append((code, normalize_repo_relative_path(tokens[i])))
            i += 1
    return result


def _clear_previous_pack_outputs(pack_path: Path, data_root: PortableDataRoot) -> None:
    """Remove only known generated outputs so reruns cannot retain stale evidence."""
    for name in ("changes.patch", "diff-stat.txt", "changed-files.txt", "git-status.txt", "manifest.json", "worker-report.md", "task.md"):
        path = pack_path / name
        if path.is_symlink():
            raise PathSafetyError(f"Refusing to overwrite symlinked pack artifact: {path}")
        if path.exists():
            if not path.is_file():
                raise PathSafetyError(f"Expected a file at generated artifact path: {path}")
            data_root.assert_managed_path(path)
            path.unlink()
    source = pack_path / "source"
    if source.is_symlink():
        raise PathSafetyError(f"Refusing to overwrite symlinked source snapshot directory: {source}")
    if source.exists():
        if not source.is_dir():
            raise PathSafetyError(f"Expected a directory at source snapshot path: {source}")
        data_root.assert_managed_path(source)
        shutil.rmtree(source)


def _parse_numstat_z(output: str) -> tuple[int, int]:
    insertions = deletions = 0
    tokens = output.split("\x00")
    if tokens and tokens[-1] == "":
        tokens.pop()
    i = 0
    while i < len(tokens):
        record = tokens[i]
        fields = record.split("\t", 2)
        if len(fields) != 3:
            raise RepositoryError("Git returned malformed numstat evidence")
        added, removed, name = fields
        if name == "":
            # Rename records with tabs/newlines use an extra NUL-delimited path.
            if i + 2 >= len(tokens):
                raise RepositoryError("Git returned malformed rename numstat evidence")
            i += 3
        else:
            i += 1
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def _resolve_repo_file(repository: Path, relative: str) -> Path:
    normalized = normalize_repo_relative_path(relative)
    root = repository.resolve(strict=True)
    lexical = root / Path(*normalized.split("/"))
    resolved = lexical.resolve(strict=False)
    if not _path_is_inside(resolved, root) or resolved == root:
        raise PathSafetyError(f"Changed path resolves outside repository: {relative!r}")
    return lexical


def _excluded_source(relative: str) -> bool:
    normalized = normalize_repo_relative_path(relative)
    parts = normalized.split("/")
    if any(part.lower() in _EXCLUDED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].lower()
    if name in _EXCLUDED_NAMES or name.startswith(".env"):
        return True
    if name.endswith((".pem", ".key")) or name.startswith(("credentials", "secrets")):
        return True
    return False


def _path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _require_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full Git SHA")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
