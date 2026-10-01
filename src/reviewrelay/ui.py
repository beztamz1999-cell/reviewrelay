"""Minimal Project Hub and setup UI; no task execution or embedded browser."""
from __future__ import annotations

import argparse
import asyncio

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QTextEdit, QVBoxLayout, QWidget)

from .project_setup import ProjectSetupService
from .projects import ConnectionStatus, ProjectKind, ProjectRegistry
from .storage import PortableDataRoot


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


def dialog_buttons(dialog, action="Save"):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(action)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


class CreateProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create / Import Project")
        self.resize(580, 370)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("First choose a project type:"))
        self.new = QRadioButton("New Project — empty local folder")
        self.existing = QRadioButton("Existing Project — inspect a local folder")
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
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        folder_layout.addWidget(self.folder)
        folder_layout.addWidget(browse)
        form.addRow("Project name", self.name)
        form.addRow("Local folder", row)
        form.addRow("Initial branch (when initializing)", self.branch)
        layout.addLayout(form)
        self.github_choice = QComboBox()
        self.github_choice.addItems(["Choose GitHub setup…", "Create New GitHub Repository", "Link Existing GitHub Repository"])
        layout.addWidget(self.github_choice)
        layout.addWidget(QLabel("Existing folders are inspected first. Snapshot and GitHub actions require separate confirmation."))
        self.buttons = dialog_buttons(self, "Continue")
        layout.addWidget(self.buttons)
        for edit in (self.name, self.folder):
            edit.textChanged.connect(self.validate)
        for radio in (self.new, self.existing):
            radio.toggled.connect(self.validate)
        self.github_choice.currentIndexChanged.connect(self.validate)
        self.validate()

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select local project folder")
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
        self.setWindowTitle("GitHub Repository Setup")
        self.resize(650, 490)
        layout = QVBoxLayout(self)
        self.detected = QComboBox()
        self.detected.addItem("Choose another repository", None)
        for remote in remotes:
            self.detected.addItem(f"Detected: {remote.name} — {remote.repository_url}", remote)
        layout.addWidget(QLabel("Detected remotes are proposals. Nothing is bound until you confirm."))
        layout.addWidget(self.detected)
        self.action = QComboBox()
        self.action.addItems(["Link Existing GitHub Repository", "Create New GitHub Repository"])
        self.action.setCurrentIndex(1 if initial_choice == 1 else 0)
        self.url = QLineEdit(project.github_repo_url or "")
        self.url.setPlaceholderText("https://github.com/owner/repository")
        self.remote = QLineEdit(project.github_remote_name)
        self.visibility = QComboBox()
        self.visibility.addItems(["Select visibility…", "PRIVATE", "PUBLIC"])
        self.mode = QComboBox()
        self.mode.addItems(["branch", "pr"])
        self.mode.setCurrentText(project.review_mode)
        self.sensitive = QTextEdit("\n".join(project.credential_paths))
        self.sensitive.setMaximumHeight(70)
        form = QFormLayout()
        form.addRow("Action", self.action)
        form.addRow("Repository URL", self.url)
        form.addRow("Remote name", self.remote)
        form.addRow("New repository visibility", self.visibility)
        form.addRow("Review mode", self.mode)
        form.addRow("Known sensitive paths (one per line)", self.sensitive)
        layout.addLayout(form)
        warning = QLabel("Setup verifies history and may publish the exact initial local commit. It never force pushes, merges, rebases or resets.\nThe basic filename guard is not comprehensive secret scanning.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.buttons = dialog_buttons(self, "Verify and Bind Repository")
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
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use This Repository")
        else:
            self.remote.setText("reviewrelay")
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Verify and Bind Repository")

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
        self.setWindowTitle("Connect Codex Runtime")
        layout = QVBoxLayout(self)
        self.executable = QLineEdit(project.worker_settings.get("executable", ""))
        self.model = QLineEdit(project.worker_settings.get("model") or "")
        self.effort = QComboBox()
        self.effort.addItems(["Configured default", "low", "medium", "high", "xhigh", "max", "ultra"])
        if project.worker_settings.get("reasoning_effort"):
            self.effort.setCurrentText(project.worker_settings["reasoning_effort"])
        browse = QPushButton("Select executable…")
        browse.clicked.connect(self.browse)
        form = QFormLayout()
        form.addRow("Tested/configured Codex executable", self.executable)
        form.addRow(browse)
        form.addRow("Default model (optional)", self.model)
        form.addRow("Reasoning effort", self.effort)
        layout.addLayout(form)
        layout.addWidget(QLabel("Checks executable, app-server capability and supported login status. No thread or inference is created."))
        layout.addWidget(dialog_buttons(self, "Check Runtime"))

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Codex executable")
        if path:
            self.executable.setText(path)

    def values(self):
        return dict(executable=self.executable.text().strip(), model=self.model.text().strip() or None,
            reasoning_effort=None if self.effort.currentIndex() == 0 else self.effort.currentText())


class ReviewerDialog(QDialog):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect ChatGPT Reviewer")
        layout = QVBoxLayout(self)
        self.url = QLineEdit(project.chatgpt_conversation_url or "")
        self.profile = QLineEdit(project.reviewer_settings.get("browser_profile", "reviewer-chrome"))
        form = QFormLayout()
        form.addRow("Existing ChatGPT conversation URL", self.url)
        form.addRow("Dedicated ReviewRelay profile", self.profile)
        layout.addLayout(form)
        label = QLabel("Use the existing Phase 3 manual Auth Mode to sign in and save this exact tab, then close Chrome cleanly. The connection check attaches to the dedicated profile and verifies readiness without sending a message.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.auth = QPushButton("Open Manual Auth Mode")
        layout.addWidget(self.auth)
        self.buttons = dialog_buttons(self, "Check Reviewer")
        layout.addWidget(self.buttons)


class ProjectHub(QMainWindow):
    def __init__(self, root, *, service_factory=None):
        super().__init__()
        self.root = root.create()
        self.service_factory = service_factory or (lambda: ProjectSetupService(root))
        self.setWindowTitle("ReviewRelay — Projects")
        self.resize(1020, 640)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.busy = False
        self._job = None
        self._callback = None
        widget = QWidget()
        outer = QVBoxLayout(widget)
        header = QHBoxLayout()
        header.addWidget(QLabel("ReviewRelay — Project Hub"))
        header.addStretch()
        self.create_button = QPushButton("+ Create Project")
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
        self.heading = QLabel("Select or create a Project")
        details.addWidget(self.heading)
        self.name = QLineEdit()
        self.rename_button = QPushButton("Rename Project")
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
        self.initialize_button = QPushButton("Initialize Git Repository")
        self.snapshot_button = QPushButton("Preview Initial Git Snapshot")
        self.github_button = QPushButton("Detect / Create / Link GitHub")
        self.reviewer_button = QPushButton("Connect Reviewer")
        self.codex_button = QPushButton("Connect Codex Worker Runtime")
        self.refresh_button = QPushButton("Inspect Local Repository")
        self.unregister_button = QPushButton("Unregister Project")
        for button, callback in ((self.initialize_button, self.initialize_git), (self.snapshot_button, self.preview_snapshot),
            (self.github_button, self.configure_github), (self.reviewer_button, self.configure_reviewer),
            (self.codex_button, self.configure_codex), (self.refresh_button, self.inspect_project), (self.unregister_button, self.unregister_project)):
            button.clicked.connect(callback)
            details.addWidget(button)
        details.addStretch()
        body.addWidget(detail, 1)
        outer.addLayout(body)
        self.message = QLabel("Repository setup comes before reviewer and worker connection.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.setCentralWidget(widget)
        self.controls = [self.create_button, self.rename_button, self.initialize_button, self.snapshot_button,
            self.github_button, self.reviewer_button, self.codex_button, self.refresh_button, self.unregister_button]
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
            self.heading.setText("Select or create a Project")
            self.name.clear()
            for label in (self.local_label, self.github_label, self.reviewer_label, self.codex_label):
                label.clear()
            return
        self.heading.setText("Project Status: " + project.status)
        self.name.setText(project.project_name)
        self.local_label.setText(f"Local Repository: {project.local_status.value}\n{project.local_repo_path}")
        relationship = project.setup.get("relationship", "Not yet verified")
        self.github_label.setText(f"GitHub: {project.github_status.value}\n{project.github_repo_url or 'Not configured'}\nHistory relationship: {relationship}")
        self.reviewer_label.setText(f"ChatGPT Reviewer: {project.chatgpt_status.value}\n{project.chatgpt_conversation_url or 'Not configured'}")
        self.codex_label.setText(f"Codex Worker: {project.codex_status.value}\n{project.worker_settings.get('executable') or 'Not configured'}")
        self.reviewer_button.setEnabled(not self.busy and project.repository_ready)
        self.codex_button.setEnabled(not self.busy and project.repository_ready)
        self.github_button.setEnabled(not self.busy and project.local_status is ConnectionStatus.READY)
        self.github_button.setText("Verify GitHub Binding" if project.github_last_verified_at else "Detect / Create / Link GitHub")
        self.initialize_button.setEnabled(not self.busy and project.local_status is not ConnectionStatus.READY)
        self.snapshot_button.setEnabled(not self.busy and project.last_error_code == "INITIAL_SNAPSHOT_CONFIRMATION_REQUIRED")
        if not self.busy and project.last_error_code:
            self.message.setText(project.last_error_code + ": Finish or resolve the indicated setup step.")

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
            self.message.setText("Setup step completed. " + (getattr(result, "status", "") or ""))
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
        self.start_job("Preparing local Project", self.call_service("register", name, path, kind, branch=branch), next_step)

    def inspect_project(self):
        self.start_job("Inspecting local repository", self.call_service("inspect_local", self.project_id))

    def initialize_git(self):
        if QMessageBox.question(self, "Initialize Git", "Initialize Git in this selected folder? No populated snapshot is staged or committed by this action.") != QMessageBox.StandardButton.Yes:
            return
        self.start_job("Initializing Git", self.call_service("initialize_local", self.project_id, confirmed=True))

    def preview_snapshot(self):
        project_id = self.project_id
        def previewed(plan, error):
            if error:
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Review Initial Git Snapshot")
            dialog.resize(650, 500)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(f"Files discovered: {len(plan.files) + len(plan.ignored)}\nIgnored files: {len(plan.ignored)}\nFiles to be committed: {len(plan.files)}"))
            listing = QTextEdit()
            listing.setReadOnly(True)
            listing.setPlainText("FILES TO COMMIT\n" + "\n".join(plan.files) + "\n\nIGNORED\n" + "\n".join(plan.ignored))
            layout.addWidget(listing)
            layout.addWidget(dialog_buttons(dialog, "Create Initial Git Snapshot"))
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.start_job("Creating confirmed initial snapshot", self.call_service("create_snapshot", plan, confirmed=True))
        self.start_job("Preparing snapshot preview", self.call_service("preview_snapshot", project_id), previewed)

    def configure_github(self, *, initial_choice=0):
        project = self.selected()
        if project is None:
            return
        if project.github_last_verified_at:
            self.start_job("Verifying GitHub binding without publication", self.call_service("verify_github", project.project_id))
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
                box.setWindowTitle("Public repository exposure")
                box.setIcon(QMessageBox.Icon.Warning)
                box.setText("This repository and its committed source will be publicly accessible.")
                confirm = box.addButton("Confirm Public Repository", QMessageBox.ButtonRole.AcceptRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
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
                    self.start_job("Binding confirmed public repository", self.call_service("bind_github", project.project_id, **values))
            self.start_job("Verifying GitHub repository", self.call_service("bind_github", project.project_id, **values), bound)
        self.start_job("Detecting configured GitHub remotes", self.call_service("detect_remotes", project.project_id), detected)

    def configure_reviewer(self):
        project = self.selected()
        dialog = ReviewerDialog(project, self)
        def auth():
            dialog.auth.setEnabled(False)
            dialog.buttons.setEnabled(False)
            def closed(result, error):
                dialog.auth.setEnabled(True)
                dialog.buttons.setEnabled(True)
            self.start_job("Sign in manually, verify the exact tab, then use Chrome menu → Exit",
                self.call_service("open_reviewer_auth", project.project_id, dialog.url.text().strip(),
                    browser_profile=dialog.profile.text().strip()), closed)
        dialog.auth.clicked.connect(auth)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_job("Checking reviewer without sending", self.call_service("connect_reviewer", project.project_id,
                dialog.url.text().strip(), browser_profile=dialog.profile.text().strip()))

    def configure_codex(self):
        project = self.selected()
        dialog = RuntimeDialog(project, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_job("Checking configured Codex runtime", self.call_service("connect_codex", project.project_id, **dialog.values()))

    def rename_project(self):
        project_id, name = self.project_id, self.name.text()
        def work():
            with ProjectRegistry(self.root) as registry:
                return registry.rename(project_id, name)
        self.start_job("Renaming Project", work)

    def unregister_project(self):
        project_id = self.project_id
        if QMessageBox.question(self, "Unregister Project", "Remove this ReviewRelay registration only? Local files, .git history, GitHub repository and task history will remain.") != QMessageBox.StandardButton.Yes:
            return
        def work():
            with ProjectRegistry(self.root) as registry:
                return registry.unregister(project_id, confirmed=True)
        self.start_job("Unregistering Project", work)

    def closeEvent(self, event):
        if self.busy:
            self.message.setText("Wait for the current setup operation to finish before closing.")
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="ReviewRelay Project Hub")
    parser.add_argument("--data-root", help="Explicit portable ReviewRelay data folder (otherwise choose it in the UI)")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    folder = args.data_root or QFileDialog.getExistingDirectory(None, "Select portable ReviewRelay data folder")
    if not folder:
        return 0
    window = ProjectHub(PortableDataRoot(folder))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
