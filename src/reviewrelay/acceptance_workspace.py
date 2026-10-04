"""Bounded temporary ONEDIR acceptance workspace lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from .errors import StorageError


_MARKER = ".reviewrelay-acceptance-workspace"
_BUNDLE_EXE = "ReviewRelay.exe"


def acceptance_workspace_base() -> Path:
    return (Path(tempfile.gettempdir()) / "ReviewRelay" / "Acceptance").resolve()


def create_acceptance_workspace(workspace_id: str | None = None) -> Path:
    """Create a uniquely named disposable workspace beneath the OS temp root."""
    workspace_id = workspace_id or uuid4().hex
    if not workspace_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in workspace_id):
        raise ValueError("Acceptance workspace ID must be a simple path component")
    base = acceptance_workspace_base()
    path = base / workspace_id
    base.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    (path / _MARKER).write_text(json.dumps({"application": "ReviewRelay", "workspace_id": workspace_id},
        sort_keys=True) + "\n", encoding="utf-8")
    return path.resolve()


def publish_verified_onedir(
    workspace: str | Path,
    built_onedir: str | Path,
    canonical_onedir: str | Path,
    *,
    launch_verifier,
) -> str:
    """Copy, launch-verify and publish an accepted ONEDIR bundle.

    The existing canonical bundle is retained as a sibling backup until the
    newly published executable has launched successfully. The acceptance
    workspace is removed only after that final verification.
    """
    workspace = _validate_workspace(workspace)
    built_onedir = Path(built_onedir).resolve(strict=True)
    canonical_onedir = Path(canonical_onedir).resolve(strict=False)
    if not built_onedir.is_relative_to(workspace):
        raise StorageError("The accepted ONEDIR build must come from this temporary acceptance workspace")
    if canonical_onedir == workspace or workspace in canonical_onedir.parents:
        raise StorageError("The canonical application bundle cannot be inside the temporary workspace")
    built_exe = built_onedir / _BUNDLE_EXE
    if not built_exe.is_file():
        raise StorageError(f"Accepted ONEDIR executable is missing: {built_exe}")
    canonical_onedir.parent.mkdir(parents=True, exist_ok=True)
    suffix = uuid4().hex
    staging = canonical_onedir.parent / f".{canonical_onedir.name}.candidate-{suffix}"
    backup = canonical_onedir.parent / f".{canonical_onedir.name}.backup-{suffix}"
    moved_old = False
    published_new = False
    try:
        shutil.copytree(built_onedir, staging)
        staged_exe = staging / _BUNDLE_EXE
        if not staged_exe.is_file() or _sha256(staged_exe) != _sha256(built_exe):
            raise StorageError("The staged ONEDIR executable does not match the accepted build")
        if not launch_verifier(staged_exe):
            raise StorageError("The staged ONEDIR executable did not pass its launch check")
        if canonical_onedir.exists():
            os.replace(canonical_onedir, backup)
            moved_old = True
        os.replace(staging, canonical_onedir)
        published_new = True
        canonical_exe = canonical_onedir / _BUNDLE_EXE
        if not launch_verifier(canonical_exe):
            raise StorageError("The canonical ONEDIR executable did not pass its launch check")
        digest = _sha256(canonical_exe)
        if moved_old:
            shutil.rmtree(backup)
            moved_old = False
        shutil.rmtree(workspace)
        return digest
    except Exception:
        if canonical_onedir.exists() and published_new:
            shutil.rmtree(canonical_onedir)
        if moved_old and backup.exists():
            os.replace(backup, canonical_onedir)
            moved_old = False
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _validate_workspace(workspace: str | Path) -> Path:
    path = Path(workspace).resolve(strict=True)
    base = acceptance_workspace_base()
    if path.is_symlink() or path.parent != base or not path.is_dir():
        raise StorageError("Acceptance workspace is outside the bounded OS temporary location")
    marker = path / _MARKER
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError("Acceptance workspace marker is missing or invalid") from exc
    if value != {"application": "ReviewRelay", "workspace_id": path.name}:
        raise StorageError("Acceptance workspace marker does not match its directory")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
