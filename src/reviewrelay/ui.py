"""Minimal Project Hub and setup UI with a separate Task control window."""
from __future__ import annotations

import argparse
import asyncio

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QInputDialog, QMainWindow, QMessageBox, QPushButton, QRadioButton, QTextEdit, QVBoxLayout, QWidget)

from .project_setup import ProjectSetupService
from .projects import ConnectionStatus, ProjectKind, ProjectRegistry
from .storage import PortableDataRoot
from .ui_text import state_text


class JobSignals(QObject):
    finished = Signal(object, object)


class Job(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.work, self.signals = work, JobSignals()

    @Slot()
    def run(self):
        try:
            self.signals.finished.emit(self.work(), None)
        except Exception as exc:
            self.signals.finished.emit(None, exc)


def dialog_buttons(dialog, action="Lưu"):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(action)
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Hủy")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


def explanation(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def owner_input(parent, title, label):
    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setOption(QInputDialog.InputDialogOption.UsePlainTextEditForTextInput)
    dialog.setOkButtonText("Gửi")
    dialog.setCancelButtonText("Hủy")
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    return dialog.textValue(), accepted


def confirm_question(parent, title, text):
    box = QMessageBox(QMessageBox.Icon.Question, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.button(QMessageBox.StandardButton.Yes).setText("Có")
    box.button(QMessageBox.StandardButton.No).setText("Không")
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec()


class CreateProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Thêm / Nhập dự án")
        self.resize(580, 370)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Chọn loại dự án:"))
        self.new = QRadioButton("Dự án mới — thư mục local trống")
        self.existing = QRadioButton("Dự án có sẵn — chọn thư mục local")
        self.kind_group = QButtonGroup(self)
        self.kind_group.addButton(self.new)
        self.kind_group.addButton(self.existing)
        layout.addWidget(self.new)
        layout.addWidget(self.existing)
        form = QFormLayout()
        self.name, self.folder, self.branch = QLineEdit(), QLineEdit(), QLineEdit("main")
        row = QWidget()
        folder_layout = QHBoxLayout(row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Chọn…")
        browse.clicked.connect(self.browse)
        folder_layout.addWidget(self.folder)
        folder_layout.addWidget(browse)
        form.addRow("Tên dự án", self.name)
        form.addRow("Thư mục dự án", row)
        form.addRow("Nhánh khởi tạo", self.branch)
        layout.addLayout(form)
        self.github_choice = QComboBox()
        self.github_choice.addItems(["Chọn cách kết nối GitHub…", "Tạo repository GitHub mới", "Liên kết repository GitHub có sẵn"])
        layout.addWidget(self.github_choice)
        layout.addWidget(explanation("Thư mục có sẵn được kiểm tra trước. Tạo commit ban đầu và thao tác GitHub cần xác nhận riêng."))
        self.buttons = dialog_buttons(self, "Tiếp tục")
        layout.addWidget(self.buttons)
        for edit in (self.name, self.folder):
            edit.textChanged.connect(self.validate)
        for radio in (self.new, self.existing):
            radio.toggled.connect(self.validate)
        self.github_choice.currentIndexChanged.connect(self.validate)
        self.validate()

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục dự án")
        if folder:
            self.folder.setText(folder)

    def validate(self):
        allowed = bool(self.name.text().strip() and self.folder.text().strip() and (self.new.isChecked() or self.existing.isChecked()))
        if self.new.isChecked():
            allowed = allowed and self.github_choice.currentIndex() > 0
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(allowed)

    def values(self):
        return self.name.text().strip(), self.folder.text().strip(), ProjectKind.NEW if self.new.isChecked() else ProjectKind.EXISTING, self.branch.text().strip()


class GitHubSetupDialog(QDialog):
    def __init__(self, project, remotes=(), parent=None, initial_choice=0):
        super().__init__(parent)
        self.setWindowTitle("Thiết lập GitHub")
        self.resize(650, 490)
        layout = QVBoxLayout(self)
        self.detected = QComboBox()
        self.detected.addItem("Chọn repository khác", None)
        for remote in remotes:
            self.detected.addItem(f"Đã phát hiện: {remote.name} — {remote.repository_url}", remote)
        layout.addWidget(QLabel("ReviewRelay đã phát hiện remote. Chỉ liên kết sau khi bạn xác nhận."))
        layout.addWidget(self.detected)
        self.action = QComboBox()
        self.action.addItems(["Liên kết repository GitHub có sẵn", "Tạo repository GitHub mới"])
        self.action.setCurrentIndex(1 if initial_choice == 1 else 0)
        self.url = QLineEdit(project.github_repo_url or "")
        self.url.setPlaceholderText("https://github.com/owner/repository")
        self.remote = QLineEdit(project.github_remote_name)
        self.visibility = QComboBox()
        self.visibility.addItems(["Chọn quyền riêng tư…", "PRIVATE", "PUBLIC"])
        self.mode = QComboBox()
        self.mode.addItems(["branch", "pr"])
        self.mode.setCurrentText(project.review_mode)
        self.sensitive = QTextEdit("\n".join(project.credential_paths))
        self.sensitive.setMaximumHeight(70)
        form = QFormLayout()
        form.addRow("Thao tác", self.action)
        form.addRow("URL repository", self.url)
        form.addRow("Tên remote", self.remote)
        form.addRow("Quyền riêng tư repository mới", self.visibility)
        form.addRow("Chế độ review", self.mode)
        form.addRow("Đường dẫn nhạy cảm đã biết (mỗi dòng một đường dẫn)", self.sensitive)
        layout.addLayout(form)
        warning = QLabel("Thiết lập sẽ kiểm tra lịch sử Git và có thể đồng bộ đúng commit local ban đầu. Không force push, merge, rebase hoặc reset.\nKiểm tra tên file cơ bản không thay thế việc rà soát toàn bộ dữ liệu bí mật.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.buttons = dialog_buttons(self, "Kiểm tra và liên kết")
        layout.addWidget(self.buttons)
        self.detected.currentIndexChanged.connect(self.use_detected)
        self.action.currentIndexChanged.connect(self.validate)
        self.visibility.currentIndexChanged.connect(self.validate)
        self.url.textChanged.connect(self.validate)
        self.remote.textChanged.connect(self.validate)
        if remotes:
            self.detected.setCurrentIndex(1)
        self.validate()

    def use_detected(self):
        remote = self.detected.currentData()
        if remote:
            self.url.setText(remote.repository_url)
            self.remote.setText(remote.name)
            self.action.setCurrentIndex(0)
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Dùng repository này")
        else:
            self.remote.setText("reviewrelay")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Kiểm tra và liên kết")

    def validate(self):
        create = self.action.currentIndex() == 1
        self.visibility.setEnabled(create)
        allowed = bool(self.url.text().strip() and self.remote.text().strip() and (not create or self.visibility.currentIndex() > 0))
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(allowed)

    def values(self):
        return dict(repository_url=self.url.text().strip(), action="create" if self.action.currentIndex() else "link",
            visibility=self.visibility.currentText() if self.action.currentIndex() else None,
            remote_name=self.remote.text().strip(), review_mode=self.mode.currentText(),
            credential_paths=tuple(line.strip() for line in self.sensitive.toPlainText().splitlines() if line.strip()))


class RuntimeDialog(QDialog):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kết nối Codex Runtime")
        layout = QVBoxLayout(self)
        self.executable = QLineEdit(project.worker_settings.get("executable", ""))
        self.model = QLineEdit(project.worker_settings.get("model") or "")
        self.effort = QComboBox()
        self.effort.addItems(["Theo cấu hình Codex", "low", "medium", "high", "xhigh", "max", "ultra"])
        if project.worker_settings.get("reasoning_effort"):
            self.effort.setCurrentText(project.worker_settings["reasoning_effort"])
        browse = QPushButton("Chọn file Codex…")
        browse.clicked.connect(self.browse)
        form = QFormLayout()
        form.addRow("File Codex executable", self.executable)
        form.addRow(browse)
        form.addRow("Model mặc định (không bắt buộc)", self.model)
        form.addRow("Mức suy luận", self.effort)
        layout.addLayout(form)
        layout.addWidget(explanation("Kiểm tra file Codex, khả năng kết nối và trạng thái đăng nhập. Không tạo thread hoặc lượt suy luận."))
        layout.addWidget(dialog_buttons(self, "Kiểm tra Codex"))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Chọn file Codex")
        if path:
            self.executable.setText(path)

    def values(self):
        return dict(executable=self.executable.text().strip(), model=self.model.text().strip() or None,
            reasoning_effort=None if self.effort.currentIndex() == 0 else self.effort.currentText())


class ReviewerDialog(QDialog):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kết nối ChatGPT Reviewer")
        layout = QVBoxLayout(self)
        self.url = QLineEdit(project.chatgpt_conversation_url or "")
        self.profile = QLineEdit(project.reviewer_settings.get("browser_profile", "reviewer-chrome"))
        form = QFormLayout()
        form.addRow("URL cuộc trò chuyện ChatGPT", self.url)
        form.addRow("Profile Chrome riêng cho ReviewRelay", self.profile)
        layout.addLayout(form)
        label = QLabel("ReviewRelay dùng profile Chrome riêng. Bạn đăng nhập thủ công, mở đúng cuộc trò chuyện Reviewer rồi thoát Chrome bằng menu → Exit để lưu tab. ReviewRelay sẽ tự kết nối lại để kiểm tra, không gửi tin nhắn.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.auth = QPushButton("Mở Chrome để đăng nhập")
        layout.addWidget(self.auth)
        self.buttons = dialog_buttons(self, "Kiểm tra Reviewer")
        layout.addWidget(self.buttons)


class ProjectHub(QMainWindow):
    def __init__(self, root, *, service_factory=None):
        super().__init__()
        self.root = root.create()
        self.service_factory = service_factory or (lambda: ProjectSetupService(root))
        self.setWindowTitle("ReviewRelay — Dự án")
        self.resize(1020, 640)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.busy = False
        self.task_windows = []
        self._job = None
        self._callback = None
        widget = QWidget()
        outer = QVBoxLayout(widget)
        header = QHBoxLayout()
        header.addWidget(QLabel("ReviewRelay — Quản lý dự án"))
        header.addStretch()
        self.create_button = QPushButton("+ Thêm dự án")
        self.create_button.clicked.connect(self.create_project)
        header.addWidget(self.create_button)
        outer.addLayout(header)
        body = QHBoxLayout()
        self.projects = QListWidget()
        self.projects.setMaximumWidth(270)
        self.projects.currentItemChanged.connect(self.show_selected)
        body.addWidget(self.projects)
        detail = QWidget()
        details = QVBoxLayout(detail)
        self.heading = QLabel("Chọn hoặc thêm dự án")
        details.addWidget(self.heading)
        self.name = QLineEdit()
        self.rename_button = QPushButton("Đổi tên dự án")
        self.rename_button.clicked.connect(self.rename_project)
        name_row = QHBoxLayout()
        name_row.addWidget(self.name)
        name_row.addWidget(self.rename_button)
        details.addLayout(name_row)
        self.local_label, self.github_label, self.reviewer_label, self.codex_label = (QLabel() for _ in range(4))
        for label in (self.local_label, self.github_label, self.reviewer_label, self.codex_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.addWidget(label)
        self.initialize_button = QPushButton("Khởi tạo repository Git")
        self.snapshot_button = QPushButton("Xem trước commit Git ban đầu")
        self.github_button = QPushButton("Kết nối GitHub")
        self.reviewer_button = QPushButton("Kết nối ChatGPT Reviewer")
        self.codex_button = QPushButton("Kết nối Codex Worker")
        self.refresh_button = QPushButton("Kiểm tra repository local")
        self.unregister_button = QPushButton("Gỡ dự án khỏi ReviewRelay")
        self.tasks_button = QPushButton("Công việc / + Tạo công việc")
        for button, callback in ((self.initialize_button, self.initialize_git), (self.snapshot_button, self.preview_snapshot),
            (self.github_button, self.configure_github), (self.reviewer_button, self.configure_reviewer),
            (self.codex_button, self.configure_codex), (self.refresh_button, self.inspect_project), (self.unregister_button, self.unregister_project), (self.tasks_button, self.open_tasks)):
            button.clicked.connect(callback)
            details.addWidget(button)
        details.addStretch()
        body.addWidget(detail, 1)
        outer.addLayout(body)
        self.message = QLabel("Thiết lập repository trước khi kết nối Reviewer và Worker.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.setCentralWidget(widget)
        self.controls = [self.create_button, self.rename_button, self.initialize_button, self.snapshot_button,
            self.github_button, self.reviewer_button, self.codex_button, self.refresh_button, self.unregister_button, self.tasks_button]
        self.refresh_projects()

    @property
    def project_id(self):
        item = self.projects.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selected(self):
        if not self.project_id:
            return None
        with ProjectRegistry(self.root) as registry:
            return registry.get(self.project_id)

    def refresh_projects(self, select=None):
        selected = select or self.project_id
        with ProjectRegistry(self.root) as registry:
            records = registry.list()
        self.projects.blockSignals(True)
        self.projects.clear()
        for record in sorted(records, key=lambda p: p.project_name.lower()):
            item = QListWidgetItem(record.project_name)
            item.setData(Qt.ItemDataRole.UserRole, record.project_id)
            self.projects.addItem(item)
            if record.project_id == selected:
                self.projects.setCurrentItem(item)
        if self.projects.currentItem() is None and self.projects.count():
            self.projects.setCurrentRow(0)
        self.projects.blockSignals(False)
        self.show_selected()

    def show_selected(self, *_):
        project = self.selected()
        for button in self.controls:
            button.setEnabled(not self.busy and (project is not None or button is self.create_button))
        self.projects.setEnabled(not self.busy)
        self.name.setEnabled(not self.busy and project is not None)
        if not project:
            self.heading.setText("Chọn hoặc thêm dự án")
            self.name.clear()
            for label in (self.local_label, self.github_label, self.reviewer_label, self.codex_label):
                label.clear()
            return
        self.heading.setText("Trạng thái dự án: " + state_text(project.status))
        self.name.setText(project.project_name)
        self.local_label.setText(f"Repository local: {state_text(project.local_status)}\n{project.local_repo_path}")
        relationship = state_text(project.setup.get("relationship", "Chưa kiểm tra"))
        self.github_label.setText(f"GitHub: {state_text(project.github_status)}\n{project.github_repo_url or 'Chưa thiết lập'}\nQuan hệ lịch sử Git: {relationship}")
        self.reviewer_label.setText(f"ChatGPT Reviewer: {state_text(project.chatgpt_status)}\n{project.chatgpt_conversation_url or 'Chưa thiết lập'}")
        self.codex_label.setText(f"Codex Worker: {state_text(project.codex_status)}\n{project.worker_settings.get('executable') or 'Chưa thiết lập'}")
        self.reviewer_button.setEnabled(not self.busy and project.repository_ready)
        self.codex_button.setEnabled(not self.busy and project.repository_ready)
        self.tasks_button.setEnabled(not self.busy and project.ready)
        self.github_button.setEnabled(not self.busy and project.local_status is ConnectionStatus.READY)
        self.github_button.setText("Kiểm tra liên kết GitHub" if project.github_last_verified_at else "Kết nối GitHub")
        self.initialize_button.setEnabled(not self.busy and project.local_status is not ConnectionStatus.READY)
        self.snapshot_button.setEnabled(not self.busy and project.last_error_code == "INITIAL_SNAPSHOT_CONFIRMATION_REQUIRED")
        if not self.busy and project.last_error_code:
            self.message.setText(project.last_error_code + ": Hoàn tất hoặc xử lý bước thiết lập được chỉ ra.")

    def start_job(self, label, work, callback=None):
        if self.busy:
            return False
        self.busy = True
        self._callback = callback
        self.message.setText(label + "…")
        self.show_selected()
        self._job = Job(work)
        self._job.signals.finished.connect(self.finished)
        self.pool.start(self._job)
        return True

    @Slot(object, object)
    def finished(self, result, error):
        callback = self._callback
        self.busy, self._job, self._callback = False, None, None
        self.refresh_projects(getattr(result, "project_id", None))
        if error:
            self.message.setText(f"{getattr(error, 'code', 'PROJECT_SETUP_FAILED')}: {error}")
        else:
            self.message.setText("Đã hoàn tất bước thiết lập. " + state_text(getattr(result, "status", "") or ""))
        if callback:
            callback(result, error)

    def call_service(self, method, *args, **kwargs):
        def work():
            with self.service_factory() as service:
                return asyncio.run(getattr(service, method)(*args, **kwargs))
        return work

    def create_project(self):
        dialog = CreateProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, path, kind, branch = dialog.values()
        choice = dialog.github_choice.currentIndex()
        def next_step(result, error):
            if not error and result.local_status is ConnectionStatus.READY:
                self.configure_github(initial_choice=choice)
        self.start_job("Đang chuẩn bị dự án local", self.call_service("register", name, path, kind, branch=branch), next_step)

    def inspect_project(self):
        self.start_job("Đang kiểm tra repository local", self.call_service("inspect_local", self.project_id))

    def initialize_git(self):
        if confirm_question(self, "Khởi tạo Git", "Khởi tạo Git trong thư mục đã chọn? Thao tác này chưa đưa file vào commit.") != QMessageBox.StandardButton.Yes:
            return
        self.start_job("Đang khởi tạo Git", self.call_service("initialize_local", self.project_id, confirmed=True))

    def preview_snapshot(self):
        project_id = self.project_id
        def previewed(plan, error):
            if error:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Kiểm tra commit Git ban đầu")
            dialog.resize(650, 500)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(f"File đã tìm thấy: {len(plan.files) + len(plan.ignored)}\nFile bỏ qua: {len(plan.ignored)}\nFile sẽ commit: {len(plan.files)}"))
            listing = QTextEdit()
            listing.setReadOnly(True)
            listing.setPlainText("FILE SẼ COMMIT\n" + "\n".join(plan.files) + "\n\nFILE BỎ QUA\n" + "\n".join(plan.ignored))
            layout.addWidget(listing)
            layout.addWidget(dialog_buttons(dialog, "Tạo commit Git ban đầu"))
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.start_job("Đang tạo commit ban đầu đã xác nhận", self.call_service("create_snapshot", plan, confirmed=True))
        self.start_job("Đang chuẩn bị bản xem trước", self.call_service("preview_snapshot", project_id), previewed)

    def configure_github(self, *, initial_choice=0):
        project = self.selected()
        if project is None:
            return
        if project.github_last_verified_at:
            self.start_job("Đang kiểm tra liên kết GitHub, không đồng bộ code", self.call_service("verify_github", project.project_id))
            return
        def detected(remotes, error):
            if error:
                return
            dialog = GitHubSetupDialog(project, remotes, self, initial_choice)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            values = dialog.values()
            def confirm_public():
                box = QMessageBox(self)
                box.setWindowTitle("Xác nhận repository công khai")
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText("Repository và code đã commit sẽ có thể được xem công khai.")
                confirm = box.addButton("Xác nhận repository công khai", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel).setText("Hủy")
                box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                return box.clickedButton() is confirm
            if values["action"] == "create" and values["visibility"] == "PUBLIC":
                if not confirm_public():
                    return
                values["public_confirmed"] = True
            def bound(result, error):
                if error and getattr(error, "code", "") == "PUBLIC_CONFIRMATION_REQUIRED" and confirm_public():
                    values["public_confirmed"] = True
                    self.start_job("Đang liên kết repository công khai đã xác nhận", self.call_service("bind_github", project.project_id, **values))
            self.start_job("Đang kiểm tra repository GitHub", self.call_service("bind_github", project.project_id, **values), bound)
        self.start_job("Đang tìm remote GitHub đã cấu hình", self.call_service("detect_remotes", project.project_id), detected)

    def configure_reviewer(self):
        project = self.selected()
        dialog = ReviewerDialog(project, self)
        def auth():
            dialog.auth.setEnabled(False)
            dialog.buttons.setEnabled(False)
            def closed(result, error):
                dialog.auth.setEnabled(True)
                dialog.buttons.setEnabled(True)
            self.start_job("Đăng nhập thủ công, kiểm tra đúng tab rồi chọn menu Chrome → Exit",
                self.call_service("open_reviewer_auth", project.project_id, dialog.url.text().strip(),
                    browser_profile=dialog.profile.text().strip()), closed)
        dialog.auth.clicked.connect(auth)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_job("Đang kiểm tra Reviewer, không gửi tin nhắn", self.call_service("connect_reviewer", project.project_id,
                dialog.url.text().strip(), browser_profile=dialog.profile.text().strip()))

    def configure_codex(self):
        project = self.selected()
        dialog = RuntimeDialog(project, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_job("Đang kiểm tra Codex đã cấu hình", self.call_service("connect_codex", project.project_id, **dialog.values()))

    def rename_project(self):
        project_id, name = self.project_id, self.name.text()
        def work():
            with ProjectRegistry(self.root) as registry:
                return registry.rename(project_id, name)
        self.start_job("Đang đổi tên dự án", work)

    def unregister_project(self):
        project_id = self.project_id
        if confirm_question(self, "Gỡ dự án khỏi ReviewRelay", "Chỉ gỡ đăng ký dự án khỏi ReviewRelay? File local, lịch sử .git, repository GitHub và lịch sử công việc vẫn được giữ lại.") != QMessageBox.StandardButton.Yes:
            return
        def work():
            with ProjectRegistry(self.root) as registry:
                return registry.unregister(project_id, confirmed=True)
        self.start_job("Đang gỡ dự án", work)

    def open_tasks(self):
        from .task_ui import TaskWindow
        project = self.selected()
        if project is None or not project.ready:
            return
        for window in self.task_windows:
            if window.project_id == project.project_id:
                window.show()
                window.raise_()
                window.activateWindow()
                return
        window = TaskWindow(self.root, project.project_id, self)
        self.task_windows.append(window)
        window.show()

    def closeEvent(self, event):
        if self.busy or any(window.busy for window in self.task_windows):
            self.message.setText("Chờ thao tác thiết lập hiện tại hoàn tất trước khi đóng.")
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="ReviewRelay Project Hub")
    parser.add_argument("--data-root", help="Explicit portable ReviewRelay data folder (otherwise choose it in the UI)")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    folder = args.data_root or QFileDialog.getExistingDirectory(None, "Chọn thư mục dữ liệu ReviewRelay")
    if not folder:
        return 0
    window = ProjectHub(PortableDataRoot(folder))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
