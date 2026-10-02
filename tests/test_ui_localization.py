"""Display contracts and unchanged domain bindings, without external effects."""
from pathlib import Path
import hashlib

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QInputDialog, QLabel, QMessageBox

from reviewrelay.approval_ui import CommandApprovalDialog
from reviewrelay.controller_store import ControllerState
from reviewrelay.owner_recovery import RecoveryAction
from reviewrelay.projects import ConnectionStatus, ProjectKind, ProjectRegistry
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task_ui import NewTaskDialog
from reviewrelay.ui import (CreateProjectDialog, GitHubSetupDialog, ProjectHub, ReviewerDialog,
    RuntimeDialog, confirm_question, owner_input)
from reviewrelay.ui_text import recovery_text, state_text
from reviewrelay.github_publish import PublishedCandidate
from reviewrelay.github_review import review_notification
from reviewrelay.protocol import PROTOCOL_VERSION
from test_project_ui import app


@pytest.fixture
def project(tmp_path):
    root = PortableDataRoot(tmp_path / "data").create()
    with ProjectRegistry(root) as registry:
        record = registry.create("Owner project", str(tmp_path / "repo"), "EXISTING")
    return root, record


def test_hub_and_all_setup_dialogs_are_vietnamese(app, project):
    root, record = project
    hub = ProjectHub(root)
    assert hub.windowTitle() == "ReviewRelay — Dự án"
    for attribute, expected in (("create_button", "+ Thêm dự án"), ("github_button", "Kết nối GitHub"),
            ("reviewer_button", "Kết nối ChatGPT Reviewer"), ("codex_button", "Kết nối Codex Worker"),
            ("tasks_button", "Công việc / + Tạo công việc")):
        assert getattr(hub, attribute).text() == expected
    dialogs = ((CreateProjectDialog(), "Thêm / Nhập dự án", "Tên dự án"),
        (GitHubSetupDialog(record), "Thiết lập GitHub", "Chế độ review"),
        (ReviewerDialog(record), "Kết nối ChatGPT Reviewer", "URL cuộc trò chuyện ChatGPT"),
        (RuntimeDialog(record), "Kết nối Codex Runtime", "Mức suy luận"),
        (NewTaskDialog(), "Tạo công việc mới", "Mã công việc"))
    for dialog, title, label in dialogs:
        assert dialog.windowTitle() == title
        assert label in [w.text() for w in dialog.findChildren(QLabel)]
        cancel = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Cancel)
        assert cancel.text() == "Hủy"
        dialog.reject()
    hub.close()


def test_localized_combos_preserve_github_and_codex_values(app, project):
    _, record = project
    github = GitHubSetupDialog(record)
    github.url.setText("https://github.com/owner/project")
    for mode in ("branch", "pr"):
        github.mode.setCurrentText(mode)
        assert github.values()["review_mode"] == mode
    github.action.setCurrentIndex(1)
    for visibility in ("PRIVATE", "PUBLIC"):
        github.visibility.setCurrentText(visibility)
        assert github.values()["visibility"] == visibility
        assert github.values()["action"] == "create"
    runtime = RuntimeDialog(record)
    assert runtime.effort.itemText(0) == "Theo cấu hình Codex"
    assert runtime.values()["reasoning_effort"] is None
    runtime.model.setText("owner-model")
    for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
        runtime.effort.setCurrentText(effort)
        assert runtime.values()["reasoning_effort"] == effort
        assert runtime.values()["model"] == "owner-model"
    github.reject()
    runtime.reject()


def test_domain_and_recovery_values_remain_internal():
    assert [a.value for a in RecoveryAction] == ["Continue Same Worker", "Check Worker Completion",
        "Retry Reviewer Send", "Recover Reviewer Response"]
    assert [recovery_text(a) for a in RecoveryAction] == ["Tiếp tục Worker hiện tại", "Kiểm tra Worker",
        "Gửi lại cho Reviewer", "Khôi phục phản hồi Reviewer"]
    for enum in (ControllerState, ConnectionStatus, ProjectKind):
        for value in enum:
            assert value.value == value.name
    for value in ControllerState:
        assert state_text(value) != value.value
    assert state_text("MESSAGE_SEND_AMBIGUOUS") == "MESSAGE_SEND_AMBIGUOUS"
    assert ControllerState.COMPLETE.value == "COMPLETE"


@pytest.mark.parametrize("button,decision", [("accept_once", "accept"), ("decline", "decline"),
    ("cancel", "cancel"), ("close", "cancel")])
def test_vietnamese_approval_still_returns_exact_decisions(app, button, decision):
    request = dict(project_id="PROJECT-X", task_id="TASK-X", thread_id="exact-thread",
        turn_id="exact-turn", cwd="G:/owner/repo", command="git status --short")
    dialog = CommandApprovalDialog(request)
    assert dialog.windowTitle() == "Cho phép chạy lệnh này?"
    assert [dialog.accept_once.text(), dialog.decline.text(), dialog.cancel.text()] == [
        "Cho phép một lần", "Từ chối", "Hủy"]
    assert dialog.command.toPlainText() == request["command"]
    assert "Dự án: PROJECT-X" in dialog.metadata.text()
    assert "Công việc: TASK-X" in dialog.metadata.text()
    assert "Codex thread: exact-thread" in dialog.metadata.text()
    dialog.close() if button == "close" else getattr(dialog, button).click()
    assert dialog.decision == decision


@pytest.mark.parametrize("accept", [True, False])
def test_owner_input_vietnamese_buttons_preserve_accept_cancel(app, accept):
    def respond():
        dialog = app.activeModalWidget()
        assert isinstance(dialog, QInputDialog)
        assert dialog.okButtonText() == "Gửi" and dialog.cancelButtonText() == "Hủy"
        dialog.setTextValue("Owner text unchanged")
        dialog.accept() if accept else dialog.reject()
    QTimer.singleShot(0, respond)
    assert owner_input(None, "Chỉ dẫn của Owner", "Đúng thread") == ("Owner text unchanged", accept)


def test_confirmation_buttons_keep_no_as_default_and_close_is_not_yes(app):
    def respond():
        box = app.activeModalWidget()
        assert isinstance(box, QMessageBox)
        assert box.button(QMessageBox.StandardButton.Yes).text() == "Có"
        assert box.button(QMessageBox.StandardButton.No).text() == "Không"
        assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
        box.close()
    QTimer.singleShot(0, respond)
    assert confirm_question(None, "Xác nhận", "Nội dung") != QMessageBox.StandardButton.Yes


def test_presentation_helper_is_not_used_by_backend_or_protocol():
    source = Path(__file__).resolve().parents[1] / "src/reviewrelay"
    for path in source.rglob("*.py"):
        if path.name not in {"ui_text.py", "ui.py", "task_ui.py"}:
            assert "ui_text" not in path.read_text(encoding="utf-8")


def test_review_protocol_output_is_byte_for_byte_unchanged():
    candidate = PublishedCandidate("PROJECT-X", "TASK-X", "owner/repo", "reviewrelay/TASK-X",
        "a" * 40, "b" * 40, 3, ".reviewrelay/tasks/TASK-X.md")
    prompt = review_notification(candidate)
    assert PROTOCOL_VERSION == "rr.v1"
    assert hashlib.sha256(prompt.encode()).hexdigest() == "85646f0ada5984dd4dbaee48141bf11fade6d5d471992efda4cd6e8be2ae8752"
    assert "REVIEWRELAY_REVIEW_REQUEST" in prompt and "<RELAY_CONTROL>" in prompt
