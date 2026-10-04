from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QMessageBox

from reviewrelay.project_git import DetectedRemote
from reviewrelay.projects import ConnectionStatus as Status, ProjectError, ProjectRegistry
from reviewrelay.storage import PortableDataRoot
from reviewrelay.storage import SelfManagedDataRoot
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
    assert window.create_button.text() == "+ Thêm dự án"
    assert not window.reviewer_button.isEnabled() and not window.codex_button.isEnabled()
    assert "Chưa thiết lập" in window.heading.text()
    with ProjectRegistry(root) as registry:
        verified = registry.save(replace(project, local_status=Status.READY, github_status=Status.READY,
            github_repo_url="https://github.com/owner/project", github_last_verified_at="fixture-check"))
    window.refresh_projects()
    assert window.reviewer_button.isEnabled() and window.codex_button.isEnabled()
    assert "https://github.com/owner/project" not in window.github_label.text()
    assert "Kết nối ChatGPT" in window.primary_action.text()
    assert "Sẵn sàng" not in window.heading.text()
    with ProjectRegistry(root) as registry:
        registry.save(replace(verified, chatgpt_status=Status.READY, codex_status=Status.READY,
            chatgpt_conversation_url="https://chatgpt.com/c/ui-fixture", worker_settings={"executable": "fixture.exe"}))
    window.refresh_projects()
    assert "Sẵn sàng" in window.heading.text()


def _set_local_inspection(root, project, classification):
    metadata = {"classification": classification, "is_git": classification != "NOT_GIT",
        "head": "a" * 40 if classification in {"CLEAN_READY", "DIRTY_WORKTREE", "DETACHED_HEAD"} else None,
        "branch": "main" if classification != "DETACHED_HEAD" else None,
        "clean": classification != "DIRTY_WORKTREE"}
    with ProjectRegistry(root) as registry:
        return registry.save(replace(project, local_status=Status.READY if classification == "CLEAN_READY" else Status.NEEDS_OWNER,
            last_error_code=None if classification == "CLEAN_READY" else classification,
            setup={**project.setup, "local_inspection": metadata}))


def test_existing_git_repository_does_not_offer_git_init_and_uses_progressive_next_step(hub, app):
    window, root, project = hub
    _set_local_inspection(root, project, "CLEAN_READY")
    window.refresh_projects()
    assert window.primary_action.text() == "Kết nối GitHub"
    assert "Git init" not in window.primary_action.text()
    window.advanced_toggle.setChecked(True)
    app.processEvents()
    assert not window.initialize_button.isVisible()
    assert window.refresh_button.isVisible()
    assert window.advanced_panel.isVisible()


@pytest.mark.parametrize("classification,primary,visible_button,guidance", [
    ("NOT_GIT", "Khởi tạo repository Git", "initialize_button", "chưa phải repository Git"),
    ("NO_HEAD", "Xem trước commit ban đầu", "snapshot_button", "chưa có commit đầu tiên"),
    ("DIRTY_WORKTREE", "Xem thay đổi", "view_changes_button", "thay đổi chưa hoàn tất"),
    ("DETACHED_HEAD", "Chuyển về branch làm việc", None, "không đứng trên branch"),
    ("WRONG_REPOSITORY_ROOT", "Chọn lại thư mục gốc Git", None, "thư mục con"),
])
def test_local_repository_blockers_have_owner_language_and_safe_next_step(hub, app, classification, primary, visible_button, guidance):
    window, root, project = hub
    _set_local_inspection(root, project, classification)
    window.refresh_projects()
    assert window.primary_action.text() == primary
    assert guidance.lower() in window.owner_guidance.text().lower()
    window.advanced_toggle.setChecked(True)
    app.processEvents()
    if visible_button:
        assert getattr(window, visible_button).isVisible()
    assert not window.initialize_button.isVisible() if classification != "NOT_GIT" else window.initialize_button.isVisible()


def test_setup_primary_action_advances_github_chatgpt_codex_then_tasks(hub, app):
    window, root, project = hub
    local = _set_local_inspection(root, project, "CLEAN_READY")
    window.refresh_projects()
    assert window.primary_action.text() == "Kết nối GitHub"
    with ProjectRegistry(root) as registry:
        github = registry.save(replace(local, github_status=Status.READY,
            github_repo_url="https://github.com/owner/project", github_last_verified_at="fixture"))
    window.refresh_projects()
    assert window.primary_action.text() == "Kết nối ChatGPT"
    with ProjectRegistry(root) as registry:
        reviewer = registry.save(replace(github, chatgpt_status=Status.READY,
            chatgpt_conversation_url="https://chatgpt.com/c/fixture"))
    window.refresh_projects()
    assert window.primary_action.text() == "Kết nối Codex"
    with ProjectRegistry(root) as registry:
        ready = registry.save(replace(reviewer, codex_status=Status.READY,
            worker_settings={"executable": "codex.exe"}))
    window.refresh_projects()
    assert ready.ready and window.primary_action.text() == "Mở dự án — Gửi yêu cầu"
    assert window.worker_card.isVisible()


def test_main_uses_self_managed_root_without_showing_a_data_root_chooser(app, tmp_path, monkeypatch):
    import sys
    import reviewrelay.ui as ui
    root = SelfManagedDataRoot(tmp_path / "install")
    seen = []
    class Window:
        def show(self):
            seen.append("shown")
    monkeypatch.setattr(sys, "argv", ["ReviewRelay"])
    monkeypatch.setattr(ui.SelfManagedDataRoot, "for_application", classmethod(lambda cls: root))
    monkeypatch.setattr(ui.QFileDialog, "getExistingDirectory", lambda *args: pytest.fail("normal startup opened a chooser"))
    monkeypatch.setattr("reviewrelay.owner_ui.OwnerMainWindow", lambda selected: (seen.append(selected.path), Window())[1])
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    assert ui.main() == 0
    assert root.path in [entry for entry in seen if isinstance(entry, Path)]
    assert "shown" in seen


def test_main_shows_owner_readable_error_and_does_not_fallback(app, tmp_path, monkeypatch):
    import sys
    import reviewrelay.ui as ui
    root = SelfManagedDataRoot(tmp_path / "unavailable")
    monkeypatch.setattr(sys, "argv", ["ReviewRelay"])
    monkeypatch.setattr(ui.SelfManagedDataRoot, "for_application", classmethod(lambda cls: root))
    monkeypatch.setattr(root, "create", lambda: (_ for _ in ()).throw(OSError("blocked")))
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: shown.append((self.text(), self.detailedText())) or 0)
    assert ui.main() == 2
    assert "Không thể mở dữ liệu ReviewRelay" in shown[0][0]
    assert "blocked" in shown[0][1]


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
    assert "Đã phát hiện" in dialog.detected.currentText()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "Dùng repository này"
    assert dialog.values()["remote_name"] == "upstream" and dialog.values()["action"] == "link"
    dialog.detected.setCurrentIndex(0)
    dialog.action.setCurrentIndex(1)
    assert dialog.remote.text() == "reviewrelay"
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.visibility.setCurrentText("PUBLIC")
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert "không thay thế việc rà soát toàn bộ dữ liệu bí mật" in " ".join(label.text() for label in dialog.findChildren(QLabel))
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
    monkeypatch.setattr("reviewrelay.ui.confirm_question", decline)
    window.unregister_project()
    assert not window.busy
    assert "Chỉ gỡ đăng ký" in warnings[0] and "repository GitHub" in warnings[0]
    monkeypatch.setattr("reviewrelay.ui.confirm_question", lambda *args: QMessageBox.StandardButton.Yes)
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
    assert reviewer.auth.text() == "Mở Chrome để đăng nhập"
    runtime.close()
    reviewer.close()
