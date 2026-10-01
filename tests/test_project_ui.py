from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox

from reviewrelay.project_git import DetectedRemote
from reviewrelay.projects import ConnectionStatus as Status, ProjectError, ProjectRegistry
from reviewrelay.storage import PortableDataRoot
from reviewrelay.ui import CreateProjectDialog, GitHubSetupDialog, ProjectHub, ReviewerDialog, RuntimeDialog


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def spin(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        QTest.qWait(10)
        assert time.monotonic() < deadline, "Qt operation did not finish"
    app.processEvents()


@pytest.fixture
def hub(app, tmp_path):
    root = PortableDataRoot(tmp_path / "data").create()
    with ProjectRegistry(root) as registry:
        project = registry.create("Example", str(tmp_path / "source"), "EXISTING")
    window = ProjectHub(root)
    window.show()
    app.processEvents()
    yield window, root, project
    spin(app, lambda: not window.busy)
    window.close()
    app.processEvents()


def test_project_hub_connect_controls_follow_typed_repository_readiness(hub, app):
    window, root, project = hub
    assert window.create_button.text() == "+ Create Project"
    assert not window.reviewer_button.isEnabled() and not window.codex_button.isEnabled()
    assert "SETUP_REQUIRED" in window.heading.text()
    with ProjectRegistry(root) as registry:
        verified = registry.save(replace(project, local_status=Status.READY, github_status=Status.READY,
            github_repo_url="https://github.com/owner/project", github_last_verified_at="fixture-check"))
    window.refresh_projects()
    assert window.reviewer_button.isEnabled() and window.codex_button.isEnabled()
    assert "https://github.com/owner/project" in window.github_label.text()
    assert "PROJECT_READY" not in window.heading.text()
    with ProjectRegistry(root) as registry:
        registry.save(replace(verified, chatgpt_status=Status.READY, codex_status=Status.READY,
            chatgpt_conversation_url="https://chatgpt.com/c/ui-fixture", worker_settings={"executable": "fixture.exe"}))
    window.refresh_projects()
    assert "PROJECT_READY" in window.heading.text()


def test_create_dialog_requires_explicit_new_existing_and_new_github_choice(app):
    dialog = CreateProjectDialog()
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    dialog.name.setText("Example")
    dialog.folder.setText("example-folder")
    assert not dialog.new.isChecked() and not dialog.existing.isChecked() and not ok.isEnabled()
    dialog.new.setChecked(True)
    assert not ok.isEnabled()
    dialog.github_choice.setCurrentIndex(1)
    assert ok.isEnabled() and dialog.values()[2].value == "NEW"
    dialog.github_choice.setCurrentIndex(0)
    dialog.existing.setChecked(True)
    assert ok.isEnabled() and dialog.values()[2].value == "EXISTING"
    dialog.close()


def test_detected_github_is_explicit_option_create_and_link_visibility(hub, app):
    _, _, project = hub
    remote = DetectedRemote("upstream", "git@github.com:owner/project.git", "https://github.com/owner/project")
    dialog = GitHubSetupDialog(project, (remote,))
    assert "Detected" in dialog.detected.currentText()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "Use This Repository"
    assert dialog.values()["remote_name"] == "upstream" and dialog.values()["action"] == "link"
    dialog.detected.setCurrentIndex(0)
    dialog.action.setCurrentIndex(1)
    assert dialog.remote.text() == "reviewrelay"
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.visibility.setCurrentText("PUBLIC")
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert "not comprehensive secret scanning" in " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "public_confirmed" not in dialog.values()  # The separate explicit confirmation has not happened.
    dialog.close()


def test_busy_jobs_do_not_block_qt_events_or_duplicate_effects(hub, app):
    window, _, _ = hub
    gate = threading.Event()
    calls, ticks = [], []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    def long_operation():
        calls.append(1)
        gate.wait(2)
    timer.start()
    try:
        assert window.start_job("Mock GitHub operation", long_operation)
        assert not window.create_button.isEnabled() and not window.projects.isEnabled()
        assert not window.start_job("Duplicate", long_operation)
        spin(app, lambda: len(ticks) >= 4)
        assert window.busy and calls == [1]
        gate.set()
        spin(app, lambda: not window.busy)
        assert window.create_button.isEnabled()
    finally:
        gate.set()
        timer.stop()


def test_typed_backend_errors_are_visible_without_stack_trace(hub, app):
    window, _, _ = hub
    def fail():
        raise ProjectError("Authenticate supported tooling manually", code="GITHUB_AUTH_REQUIRED")
    window.start_job("Connecting", fail)
    spin(app, lambda: not window.busy)
    assert "GITHUB_AUTH_REQUIRED" in window.message.text() and "Traceback" not in window.message.text()


def test_unregister_warning_keeps_source_and_history(hub, app, monkeypatch):
    window, root, project = hub
    from pathlib import Path
    source = Path(project.local_repo_path)
    source.mkdir()
    (source / "keep.txt").write_text("Owner source")
    warnings = []
    def decline(parent, title, text):
        warnings.append(text)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", decline)
    window.unregister_project()
    assert not window.busy
    assert "registration only" in warnings[0] and "GitHub repository" in warnings[0]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window.unregister_project()
    spin(app, lambda: not window.busy)
    with ProjectRegistry(root) as registry:
        assert not registry.list()
    assert (source / "keep.txt").read_text() == "Owner source"


def test_runtime_and_reviewer_dialogs_do_not_define_task_thread_fields(hub, app):
    _, _, project = hub
    runtime = RuntimeDialog(project)
    assert not runtime.executable.text()  # Never silently choose the possibly incompatible PATH binary.
    assert "thread" not in runtime.values()
    reviewer = ReviewerDialog(project)
    assert reviewer.profile.text() == "reviewer-chrome"
    assert reviewer.auth.text() == "Open Manual Auth Mode"
    runtime.close()
    reviewer.close()
