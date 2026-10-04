from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from reviewrelay.acceptance_workspace import (
    acceptance_workspace_base,
    create_acceptance_workspace,
    publish_verified_onedir,
)
from reviewrelay.errors import StorageError
from reviewrelay.legacy_import import import_legacy_data
from reviewrelay.legacy_import import legacy_import_available
from reviewrelay.models import TaskRecord
from reviewrelay.projects import ProjectRegistry
from reviewrelay.state import StateStore
from reviewrelay.storage import PortableDataRoot, SelfManagedDataRoot, TaskStorage, application_root


def test_application_data_path_is_stable_across_cwd_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    first = SelfManagedDataRoot.for_application().path
    monkeypatch.chdir(tmp_path.parent)
    second = SelfManagedDataRoot.for_application().path
    assert first == second == application_root() / "data"


def test_frozen_application_root_is_derived_from_onedir_executable(tmp_path):
    executable = tmp_path / "dist" / "ReviewRelay" / "ReviewRelay.exe"
    assert application_root(executable=executable, frozen=True) == tmp_path.resolve()


def test_first_launch_writes_identity_and_creates_managed_directories(tmp_path):
    root = SelfManagedDataRoot(tmp_path / "install").create()
    marker = json.loads(root.marker_path.read_text(encoding="utf-8"))
    assert marker == {"application": "ReviewRelay", "marker_version": 1, "schema_version": 1}
    assert {"config", "browser-profile", "db", "active", "archive", "logs"} <= {
        item.name for item in root.path.iterdir() if item.is_dir()
    }


def test_marked_root_is_reused_and_missing_directories_are_recreated(tmp_path):
    first = SelfManagedDataRoot(tmp_path / "install").create()
    identity = first.marker_path.read_bytes()
    (first.path / "logs").rmdir()
    second = SelfManagedDataRoot(tmp_path / "install").create()
    assert second.path == first.path
    assert second.marker_path.read_bytes() == identity
    assert (second.path / "logs").is_dir()


def test_unmarked_directory_with_relay_db_is_rejected_without_adoption(tmp_path):
    app = tmp_path / "install"
    data = app / "data"
    (data / "db").mkdir(parents=True)
    (data / "db" / "relay.db").write_bytes(b"unrelated")
    with pytest.raises(StorageError, match="Không thể mở dữ liệu ReviewRelay"):
        SelfManagedDataRoot(app).create()
    assert not (data / ".reviewrelay-root").exists()
    assert (data / "db" / "relay.db").read_bytes() == b"unrelated"


def test_unreadable_canonical_root_fails_closed(tmp_path):
    application = tmp_path / "install"
    application.mkdir()
    (application / "data").write_text("not a directory", encoding="utf-8")
    with pytest.raises(StorageError):
        SelfManagedDataRoot(application).create()


def test_external_legacy_root_is_not_auto_adopted(tmp_path):
    legacy = PortableDataRoot(tmp_path / "old-location").create()
    with ProjectRegistry(legacy) as registry:
        registry.create("Legacy", str(tmp_path / "source"), "EXISTING")
    canonical = SelfManagedDataRoot(tmp_path / "new-install").create()
    with ProjectRegistry(canonical) as registry:
        assert registry.list() == ()
    assert (legacy.path / "db" / "relay.db").is_file()


def test_legacy_import_action_is_only_available_before_canonical_state_exists(tmp_path):
    root = SelfManagedDataRoot(tmp_path / "install").create()
    assert legacy_import_available(root)
    with ProjectRegistry(root) as registry:
        registry.create("Canonical", str(tmp_path / "source"), "EXISTING")
    assert not legacy_import_available(root)


def test_legacy_import_is_owner_initiated_preserves_source_and_is_one_time(tmp_path):
    legacy = PortableDataRoot(tmp_path / "legacy").create()
    with ProjectRegistry(legacy) as registry:
        project = registry.create("Legacy", str(tmp_path / "source"), "EXISTING")
    with StateStore(legacy) as state:
        state.save(TaskRecord(project.project_id, "1"))
    TaskStorage(legacy).create_task(project.project_id, "1")
    (legacy.path / "browser-profile" / "auth-not-migrated.txt").write_text("fixture-only", encoding="utf-8")

    canonical = SelfManagedDataRoot(tmp_path / "install").create()
    counts = import_legacy_data(legacy.path, canonical)
    assert counts["projects"] == 1 and counts["tasks"] == 1
    with ProjectRegistry(canonical) as registry:
        assert [item.project_id for item in registry.list()] == [project.project_id]
    with StateStore(canonical) as state:
        assert state.get(project.project_id, "1") is not None
    assert (canonical.path / "active" / project.project_id / "1" / "durable" / "task.json").is_file()
    assert not any((canonical.path / "browser-profile").iterdir())
    assert (legacy.path / "browser-profile" / "auth-not-migrated.txt").is_file()
    assert json.loads(canonical.marker_path.read_text(encoding="utf-8"))["legacy_imported"] is True
    with pytest.raises(StorageError, match="already been imported"):
        import_legacy_data(legacy.path, canonical)


def test_acceptance_workspace_is_under_os_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    workspace = create_acceptance_workspace("owner-ux-test")
    assert workspace.parent == acceptance_workspace_base()
    assert workspace.is_relative_to(tmp_path.resolve())
    assert workspace.name == "owner-ux-test"


def test_successful_onedir_acceptance_cleans_temp_and_keeps_canonical_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    workspace = create_acceptance_workspace("publish-test")
    built = workspace / "dist" / "ReviewRelay"
    built.mkdir(parents=True)
    (built / "ReviewRelay.exe").write_bytes(b"verified reviewrelay bundle")
    (built / "_internal").mkdir()
    canonical = tmp_path / "application" / "dist" / "ReviewRelay"
    canonical.mkdir(parents=True)
    (canonical / "ReviewRelay.exe").write_bytes(b"previous bundle")
    verifier_calls = []

    def verifier(executable):
        verifier_calls.append(Path(executable))
        return Path(executable).is_file() and Path(executable).read_bytes() == b"verified reviewrelay bundle"

    digest = publish_verified_onedir(workspace, built, canonical, launch_verifier=verifier)
    assert len(digest) == 64
    assert not workspace.exists()
    assert (canonical / "ReviewRelay.exe").read_bytes() == b"verified reviewrelay bundle"
    assert len(verifier_calls) == 2


def test_failed_onedir_verification_retains_diagnostics_and_previous_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    workspace = create_acceptance_workspace("failed-publish")
    built = workspace / "dist" / "ReviewRelay"
    built.mkdir(parents=True)
    (built / "ReviewRelay.exe").write_bytes(b"bad bundle")
    canonical = tmp_path / "application" / "dist" / "ReviewRelay"
    canonical.mkdir(parents=True)
    (canonical / "ReviewRelay.exe").write_bytes(b"previous bundle")
    with pytest.raises(StorageError, match="did not pass"):
        publish_verified_onedir(workspace, built, canonical, launch_verifier=lambda _: False)
    assert workspace.is_dir()
    assert (canonical / "ReviewRelay.exe").read_bytes() == b"previous bundle"
