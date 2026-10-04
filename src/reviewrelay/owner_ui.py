"""Owner landing, guided setup and chat presentation of existing services."""
from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
import shutil

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QKeyEvent, QPalette
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QTextBrowser,
    QTextEdit, QVBoxLayout, QWidget)

from .controller_store import ControllerState as S, ControllerStore
from .owner_view import chat_history, friendly_error, local_blocker, project_ready, recency, setup_step
from .project_setup import ProjectSetupService
from .projects import ConnectionStatus as C, ProjectKind, ProjectRegistry
from .task_ui import TaskWindow
from .ui import Job, ProjectHub, confirm_question, explanation
from .worker_ui import ProjectWorkerCard


OWNER_STYLE = """
QWidget { color: #243145; }
QMainWindow, QDialog { background: #f7f8fa; }
QLabel { color: #243145; font-size: 14px; }
QPushButton { color: #243145; padding: 9px 16px; border: 1px solid #d8dee8; border-radius: 8px; background: white; }
QPushButton:disabled { color: #8a94a4; background: #eef0f4; }
QPushButton#primary { background: #2458d3; color: white; border: none; }
QListWidget, QTextBrowser, QTextEdit, QLineEdit { color: #243145; background: white; border: 1px solid #d8dee8; border-radius: 8px; padding: 10px; }
QTextEdit, QLineEdit { placeholder-text-color: #748097; }
QScrollArea, QScrollArea QWidget { background: #f7f8fa; }
QListWidget::item { padding: 14px; border-bottom: 1px solid #edf0f5; }
QListWidget::item:selected { background: #e9f0ff; color: #243145; }
"""


def title(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setStyleSheet("font-size: 24px; font-weight: 600; padding: 12px 0;")
    return label


def apply_owner_theme(window):
    """Keep the light Owner surface readable even with a dark OS palette."""
    palette = window.palette()
    for role, color in ((QPalette.ColorRole.Window, "#f7f8fa"), (QPalette.ColorRole.Base, "#ffffff"),
            (QPalette.ColorRole.Button, "#ffffff"), (QPalette.ColorRole.Text, "#243145"),
            (QPalette.ColorRole.WindowText, "#243145"), (QPalette.ColorRole.ButtonText, "#243145"),
            (QPalette.ColorRole.PlaceholderText, "#748097")):
        palette.setColor(role, QColor(color))
    window.setPalette(palette)
    window.setStyleSheet(OWNER_STYLE)


def window_busy(window):
    return bool(getattr(window, "busy", False) or any(window_busy(child)
        for name in ("settings_windows", "advanced_windows", "task_windows", "windows")
        for child in getattr(window, name, ())))


class WorkerSelectionPanel(ProjectWorkerCard):
    """Inline visual chooser; identity verification stays in ProjectWorkerService."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.choices = QListWidget()
        self.choices.hide()
        self.select_button = QPushButton("Chọn Worker")
        self.select_button.setObjectName("primary")
        self.select_button.hide()
        self.select_button.setEnabled(False)
        self.choices.currentItemChanged.connect(lambda item: self.select_button.setEnabled(item is not None))
        self.select_button.clicked.connect(self.choose)
        self.layout().addWidget(self.choices)
        self.layout().addWidget(self.select_button)

    def refresh(self, *, busy=False):
        super().refresh(busy=busy)
        project_id = self.selected_project()
        if not project_id:
            return
        with ProjectRegistry(self.root) as registry:
            project = registry.get(project_id)
        if project.codex_worker_thread_id:
            self.label.setText(f"✓ {project.codex_worker_title or 'Worker Codex'}\n{recency(project.codex_worker_last_activity)}")
        elif self.allow_create:
            self.label.setText("Không tìm thấy Worker Codex phù hợp.")
        else:
            self.label.setText("Chọn Worker Codex cho " + project.project_name)
        if hasattr(self, "choices"):
            self.choices.setEnabled(not busy)
            self.select_button.setEnabled(not busy and self.choices.currentItem() is not None)

    def discovered(self, project_id, result, error):
        if self.selected_project() != project_id:
            return
        self.choices.clear()
        self.choices.hide()
        self.select_button.hide()
        if not error and result.status == "CHOOSE" and result.candidates:
            self.allow_create = False
            self.refresh()
            for candidate in result.candidates:
                item = QListWidgetItem(f"{candidate.title}\n{recency(candidate.last_activity)}")
                item.setData(Qt.ItemDataRole.UserRole, candidate.thread_id)
                self.choices.addItem(item)
            self.choices.show()
            self.select_button.show()
            self.changed.emit()
        else:
            super().discovered(project_id, result, error)

    def choose(self):
        item = self.choices.currentItem()
        if item is None or not self.select_button.isEnabled():
            return
        project_id, thread_id = self.selected_project(), item.data(Qt.ItemDataRole.UserRole)
        self.launch(lambda: asyncio.run(self.factory().select(project_id, thread_id)),
            lambda value, error: self.selected(project_id, value, error))

    def selected(self, project_id, result, error):
        super().selected(project_id, result, error)
        if not error:
            self.choices.hide()
            self.select_button.hide()


class ProjectSetupWizard(QMainWindow):
    opened = Signal(str)
    changed = Signal()

    def __init__(self, root, project_id, parent=None, *, service_factory=None,
                 worker_service_factory=None, auto_discover=True, settings=False):
        super().__init__(parent)
        self.root, self.project_id, self.busy = root, project_id, False
        self.service_factory = service_factory or (lambda: ProjectSetupService(root))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._job = self._callback = None
        self.force_step = None
        self.runtime_started = False
        self.auto_discover = auto_discover
        self.advanced_windows = []
        apply_owner_theme(self)
        self.resize(760, 650)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 20, 32, 24)
        self.heading = title("")
        layout.addWidget(self.heading)
        self.steps = QHBoxLayout()
        self.step_buttons = {}
        for key, text in (("project", "Project"), ("github", "Git / GitHub"), ("chatgpt", "ChatGPT"), ("worker", "Codex Worker")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, selected=key: self.select_step(selected))
            self.steps.addWidget(button)
            self.step_buttons[key] = button
        layout.addLayout(self.steps)
        self.description = explanation("")
        layout.addWidget(self.description)
        self.folder = QLineEdit()
        self.folder.setReadOnly(True)
        layout.addWidget(self.folder)
        self.repo_url = QLineEdit()
        self.repo_url.setPlaceholderText("https://github.com/owner/repository")
        layout.addWidget(self.repo_url)
        self.conversation_url = QLineEdit()
        self.conversation_url.setPlaceholderText("https://chatgpt.com/c/…")
        layout.addWidget(self.conversation_url)
        self.auth_button = QPushButton("Đăng nhập ChatGPT")
        self.auth_button.clicked.connect(self.open_auth)
        layout.addWidget(self.auth_button)
        self.worker_card = WorkerSelectionPanel(root, lambda: self.project_id,
            lambda work, callback: self.start_job("Đang kiểm tra Worker…", work, callback), self,
            service_factory=worker_service_factory, auto_discover=auto_discover)
        self.worker_card.changed.connect(self.refresh)
        layout.addWidget(self.worker_card)
        self.next_button = QPushButton()
        self.next_button.setObjectName("primary")
        self.next_button.clicked.connect(self.advance)
        layout.addWidget(self.next_button)
        self.message = explanation("")
        layout.addWidget(self.message)
        layout.addStretch()
        self.advanced_button = QPushButton("Cài đặt nâng cao")
        self.advanced_button.clicked.connect(self.open_advanced)
        layout.addWidget(self.advanced_button)
        self.setCentralWidget(content)
        self.settings_mode = settings
        with ProjectRegistry(root) as registry:
            project = registry.get(project_id)
        self.repo_url.setText(project.github_repo_url or self.detected_url(project))
        self.conversation_url.setText(project.chatgpt_conversation_url or "")
        self.refresh()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(800)
        self.refresh_timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.refresh_timer.start()

    @staticmethod
    def detected_url(project):
        remotes = project.setup.get("local_inspection", {}).get("remotes", [])
        return remotes[0]["url"] if remotes else ""

    def select_step(self, key):
        if not self.busy:
            self.force_step = key
            self.refresh()

    def showEvent(self, event):
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.start()
            self.refresh()
        super().showEvent(event)

    def refresh(self):
        with ProjectRegistry(self.root) as registry:
            project = registry.get(self.project_id)
            try:
                registry.state.assert_project_available(self.project_id)
                available = True
            except Exception:
                available = False
        self.setWindowTitle("Cài đặt " + project.project_name if self.settings_mode else "Thiết lập " + project.project_name)
        self.heading.setText(self.windowTitle())
        self.folder.setText(project.local_repo_path)
        self.step = self.force_step or setup_step(project)
        ready = {"project": project.local_status is C.READY, "github": project.repository_ready,
            "chatgpt": project.chatgpt_status is C.READY, "worker": project_ready(project)}
        for key, button in self.step_buttons.items():
            label = {"project": "Project", "github": "Git / GitHub", "chatgpt": "ChatGPT", "worker": "Codex Worker"}[key]
            button.setText(("✓ " if ready[key] else "● " if key == self.step else "○ ") + label)
            button.setEnabled(not self.busy)
        self.folder.setVisible(self.step == "project")
        self.repo_url.setVisible(self.step == "github")
        self.conversation_url.setVisible(self.step == "chatgpt")
        self.auth_button.setVisible(self.step == "chatgpt")
        self.auth_button.setEnabled(not self.busy and available and project.repository_ready)
        self.worker_card.setVisible(self.step == "worker")
        self.worker_card.refresh(busy=self.busy or not available)
        self.repo_url.setEnabled(not self.busy and available)
        self.conversation_url.setEnabled(not self.busy and available)
        self.advanced_button.setEnabled(not self.busy)
        self.description.setText({
            "project": "Git ✓ Đã nhận diện" if project.local_status is C.READY else local_blocker(project),
            "github": "GitHub ✓ Đã tìm thấy\n" + self.detected_url(project) if self.detected_url(project) else "Repository GitHub của project",
            "chatgpt": "Link cuộc trò chuyện ChatGPT dùng để review\n" + ("✓ Đã kết nối" if project.chatgpt_status is C.READY else "! Cần đăng nhập" if project.last_error_code in {"LOGIN_REQUIRED", "REVIEWER_LOGIN_REQUIRED"} else "○ Chưa kết nối"),
            "worker": "Chọn Worker Codex cho " + project.project_name,
            "done": "✓ " + project.project_name + " đã sẵn sàng",
        }[self.step])
        self.next_button.setVisible(self.step != "worker" or project.codex_status is not C.READY)
        self.next_button.setText({"project": "Kiểm tra lại project", "github": "Dùng repository này" if self.detected_url(project) else "Kết nối GitHub",
            "chatgpt": "Kết nối", "worker": "Kiểm tra Codex", "done": "Mở project"}[self.step])
        self.next_button.setEnabled(not self.busy and (available or self.step == "done")
            and (self.step in {"project", "done"} or project.local_status is C.READY
                and (self.step == "github" or project.repository_ready)))
        if (self.step == "worker" and project.codex_status is not C.READY and not self.runtime_started
                and not self.busy and available and project.repository_ready and self.auto_discover):
            self.runtime_started = True
            QTimer.singleShot(0, self.connect_codex)

    def service_call(self, method, *args, **kwargs):
        def work():
            with self.service_factory() as service:
                return asyncio.run(getattr(service, method)(*args, **kwargs))
        return work

    def start_job(self, label, work, callback=None):
        if self.busy:
            return False
        self.busy = True
        self.message.setText(label)
        self._callback = callback
        self._job = Job(work)
        self._job.signals.finished.connect(self.finished)
        self.pool.start(self._job)
        self.refresh()
        return True

    @Slot(object, object)
    def finished(self, result, error):
        callback, self._callback = self._callback, None
        self.busy, self._job = False, None
        self.message.setText(friendly_error(getattr(error, "code", None)) if error else "Đã hoàn tất bước thiết lập.")
        if not error:
            self.force_step = None
        self.refresh()
        self.changed.emit()
        if callback:
            callback(result, error)

    def advance(self):
        if not self.next_button.isEnabled():
            return
        if self.step == "done":
            self.opened.emit(self.project_id)
        elif self.step == "project":
            self.start_job("Đang kiểm tra project…", self.service_call("inspect_local", self.project_id))
        elif self.step == "github":
            self.bind_github()
        elif self.step == "chatgpt":
            with ProjectRegistry(self.root) as registry:
                profile = registry.get(self.project_id).reviewer_settings.get("browser_profile", "reviewer-chrome")
            self.start_job("● Đang kiểm tra ChatGPT…", self.service_call("connect_reviewer", self.project_id,
                self.conversation_url.text().strip(), browser_profile=profile))
        elif self.step == "worker":
            self.connect_codex()

    def bind_github(self):
        with ProjectRegistry(self.root) as registry:
            project = registry.get(self.project_id)
        url = self.repo_url.text().strip()
        remote = next((r["name"] for r in project.setup.get("local_inspection", {}).get("remotes", []) if r["url"] == url), project.github_remote_name)
        values = dict(repository_url=url, remote_name=remote, review_mode=project.review_mode,
            credential_paths=project.credential_paths)
        def bound(result, error):
            if error and getattr(error, "code", None) == "PUBLIC_CONFIRMATION_REQUIRED":
                if confirm_question(self, "Xác nhận đồng bộ GitHub", "Code đã commit sẽ được đồng bộ lên repository công khai này. Bạn xác nhận đã kiểm tra dữ liệu trước khi công khai?") == QMessageBox.StandardButton.Yes:
                    self.start_job("Đang kết nối GitHub…", self.service_call("bind_github", self.project_id, **values, public_confirmed=True))
        self.start_job("Đang kiểm tra GitHub…", self.service_call("bind_github", self.project_id, **values), bound)

    def open_auth(self):
        with ProjectRegistry(self.root) as registry:
            profile = registry.get(self.project_id).reviewer_settings.get("browser_profile", "reviewer-chrome")
        self.start_job("Đăng nhập thủ công, mở đúng hội thoại rồi chọn Chrome → Exit.",
            self.service_call("open_reviewer_auth", self.project_id, self.conversation_url.text().strip(), browser_profile=profile))

    def connect_codex(self):
        if self.busy:
            return
        with ProjectRegistry(self.root) as registry:
            settings = registry.get(self.project_id).worker_settings
        self.start_job("Đang kiểm tra Codex…", self.service_call("connect_codex", self.project_id,
            executable=settings.get("executable") or shutil.which("codex") or "codex",
            model=settings.get("model"), reasoning_effort=settings.get("reasoning_effort")))

    def open_advanced(self):
        window = ProjectHub(self.root, service_factory=self.service_factory, auto_discover=False)
        window.refresh_projects(self.project_id)
        window.advanced_toggle.setChecked(True)
        window.show()
        self.advanced_windows.append(window)

    def closeEvent(self, event):
        if window_busy(self):
            self.message.setText("Chờ thao tác hiện tại hoàn tất trước khi đóng.")
            event.ignore()
        else:
            self.refresh_timer.stop()
            for window in self.advanced_windows:
                window.close()
            self.changed.emit()
            event.accept()


class ChatComposer(QTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            self.send_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ProjectChatWindow(TaskWindow):
    back_requested = Signal()

    def __init__(self, root, project_id, parent=None, **kwargs):
        # TaskWindow keeps the production command approval, recovery, submit and
        # background lifecycle. This subclass replaces presentation only.
        super().__init__(root, project_id, parent, **kwargs)
        # Worker discovery belongs to guided setup/settings, not opening the
        # read-only technical view retained from TaskWindow.
        self.worker_card.auto_discover = False
        self.legacy_view = self.takeCentralWidget()
        self.legacy_view.setParent(self)
        self.legacy_view.hide()
        self.setWindowTitle(self.heading.text() + " — ReviewRelay")
        apply_owner_theme(self)
        self.resize(920, 780)
        self.settings_windows = []
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 18, 24, 18)
        header = QHBoxLayout()
        self.back_button = QPushButton("← Dự án")
        self.back_button.clicked.connect(self.back)
        header.addWidget(self.back_button)
        header.addWidget(self.heading, 1)
        self.settings_button = QPushButton("⚙ Cài đặt")
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)
        layout.addLayout(header)
        layout.addWidget(self.connections)
        self.chatlog = QTextBrowser()
        self.chatlog.setOpenExternalLinks(False)
        layout.addWidget(self.chatlog, 1)
        actions = QHBoxLayout()
        for button in (self.owner_button, self.recovery_button, self.pause_auto_button, self.manual_button, self.resume_auto_button):
            actions.addWidget(button)
        self.pause_auto_button.setText("Tạm dừng")
        self.manual_button.setText("Gửi chỉ dẫn")
        self.resume_auto_button.setText("Tiếp tục")
        layout.addLayout(actions)
        self.input_hint = explanation("Enter xuống dòng · Ctrl+Enter gửi")
        layout.addWidget(self.input_hint)
        self.prompt = ChatComposer()
        self.prompt.setPlaceholderText("Nhập yêu cầu cho Codex…")
        self.prompt.setMaximumHeight(130)
        self.prompt.textChanged.connect(self.show_task)
        self.prompt.send_requested.connect(self.submit_prompt)
        composer = QHBoxLayout()
        composer.addWidget(self.prompt, 1)
        self.send_button.setText("Gửi")
        self.send_button.setObjectName("primary")
        composer.addWidget(self.send_button)
        layout.addLayout(composer)
        layout.addWidget(self.message)
        self.setCentralWidget(content)
        self._history_key = None
        self._submission_in_progress = False
        self.refresh()

    def refresh(self):
        super().refresh()
        if hasattr(self, "chatlog"):
            with ControllerStore(self.root) as store:
                tasks = store.list(self.project_id)
            active = sorted((t for t in tasks if t.state not in {S.COMPLETE, S.STOPPED}), key=lambda t: t.created_at)
            latest = sorted(tasks, key=lambda t: t.created_at)
            target = self.running_task_id or (active[-1].task_id if active else latest[-1].task_id if latest else None)
            if target and self.task_id != target:
                for i in range(self.tasks.count()):
                    if self.tasks.item(i).data(Qt.ItemDataRole.UserRole) == target:
                        self.tasks.setCurrentRow(i)
                        break
            self.show_task()

    def show_task(self, *_):
        super().show_task()
        if not hasattr(self, "chatlog"):
            return
        with ProjectRegistry(self.root) as registry:
            project = registry.get(self.project_id)
            try:
                registry.state.assert_project_available(self.project_id)
                available = True
            except Exception:
                available = False
        self.connections.setText(f"{'●' if project.codex_worker_thread_id else '○'} Codex     {'●' if project.github_status is C.READY else '○'} GitHub     {'●' if project.chatgpt_status is C.READY else '○'} ChatGPT")
        self.project_identity.setText(self.project_identity.text() + f"\nData ReviewRelay: {self.root.path}")
        self.send_button.setEnabled(self.send_button.isEnabled() and project_ready(project))
        self.input_hint.setText("Codex đang xử lý yêu cầu hiện tại." if self.busy or not available else "Enter xuống dòng · Ctrl+Enter gửi")
        with ControllerStore(self.root) as store:
            tasks = store.list(self.project_id)
            messages = chat_history(tasks, lambda task_id: store.events(self.project_id, task_id))
            selected = store.get(self.project_id, self.task_id) if self.task_id else None
        self.pause_auto_button.setVisible(bool(selected and selected.state not in {S.COMPLETE, S.STOPPED, S.PAUSED_OWNER_STEER, S.PAUSED_ERROR}))
        self.manual_button.setVisible(self.manual_button.isEnabled())
        self.resume_auto_button.setVisible(self.resume_auto_button.isEnabled())
        self.owner_button.setVisible(self.owner_button.isEnabled())
        if messages != self._history_key:
            scroll = self.chatlog.verticalScrollBar()
            at_bottom = scroll.value() >= scroll.maximum() - 8
            old_value = scroll.value()
            self.chatlog.setHtml("".join(f'<p style="margin-top:20px;color:{"#2458d3" if who == "Bạn" else "#243145"}"><b>{who}</b><br>{escape(text).replace(chr(10), "<br>")}</p>' for who, text in messages)
                or "<p>Nhập yêu cầu để bắt đầu. Codex và ChatGPT sẽ làm việc tự động.</p>")
            scroll.setValue(scroll.maximum() if at_bottom else old_value)
            self._history_key = messages

    def submit_values(self, values):
        if self.busy:
            return
        self._submission_in_progress = True
        super().submit_values(values)

    @Slot(str)
    def created(self, task_id):
        super().created(task_id)
        if hasattr(self, "chatlog") and self._submission_in_progress:
            self.prompt.clear()
            self._submission_in_progress = False

    def finished(self, result, error):
        super().finished(result, error)
        self._submission_in_progress = False
        if not error:
            self.message.setText("")

    def show_error(self, error):
        super().show_error(error)
        self.message.setText(friendly_error(getattr(error, "code", None)))

    def open_settings(self):
        for existing in self.settings_windows:
            if existing.isVisible():
                existing.raise_()
                return
        wizard = ProjectSetupWizard(self.root, self.project_id, self, auto_discover=False, settings=True)
        wizard.changed.connect(self.refresh)
        wizard.opened.connect(lambda _: wizard.close())
        wizard.show()
        self.settings_windows.append(wizard)
        self.diagnostics_button.setParent(wizard.centralWidget())
        wizard.centralWidget().layout().addWidget(self.diagnostics_button)
        self.diagnostics_button.show()
        try:
            self.diagnostics_button.toggled.disconnect()
        except RuntimeError:
            pass
        self.diagnostics_button.setCheckable(False)
        if not getattr(self, "_diagnostic_button_connected", False):
            self.diagnostics_button.clicked.connect(self.open_diagnostics)
            self._diagnostic_button_connected = True

    def open_diagnostics(self):
        if getattr(self, "diagnostic_dialog", None) is not None and self.diagnostic_dialog.isVisible():
            self.diagnostic_dialog.raise_()
            return
        self.diagnostic_dialog = QDialog(self)
        self.diagnostic_dialog.setWindowTitle("Chi tiết kỹ thuật")
        self.diagnostic_dialog.resize(1060, 800)
        layout = QVBoxLayout(self.diagnostic_dialog)
        layout.addWidget(self.legacy_view)
        self.legacy_view.show()
        self.diagnostics.show()
        self.diagnostic_scroll.show()
        self.diagnostic_dialog.finished.connect(self.hide_diagnostics)
        self.diagnostic_dialog.show()

    def hide_diagnostics(self, *_):
        self.legacy_view.hide()
        self.legacy_view.setParent(self)

    def back(self):
        self.close()
        if not self.isVisible():
            self.back_requested.emit()

    def closeEvent(self, event):
        if hasattr(self, "settings_windows") and any(window_busy(w) for w in self.settings_windows):
            self.message.setText("Chờ thao tác Cài đặt hoàn tất trước khi đóng.")
            event.ignore()
            return
        super().closeEvent(event)
        if event.isAccepted() and hasattr(self, "settings_windows"):
            for window in self.settings_windows:
                window.close()
            if getattr(self, "diagnostic_dialog", None) is not None:
                self.diagnostic_dialog.close()


class OwnerMainWindow(QMainWindow):
    def __init__(self, root, *, service_factory=None, worker_service_factory=None, auto_discover=True):
        super().__init__()
        self.root = root.create()
        self.service_factory = service_factory or (lambda: ProjectSetupService(root))
        self.worker_service_factory, self.auto_discover = worker_service_factory, auto_discover
        self.busy = False
        self.windows = []
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.setWindowTitle("ReviewRelay — Dự án")
        self.resize(760, 570)
        apply_owner_theme(self)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 24, 32, 24)
        header = QHBoxLayout()
        header.addWidget(title("ReviewRelay"), 1)
        settings = QPushButton("⚙ Cài đặt")
        settings.clicked.connect(self.open_settings)
        header.addWidget(settings)
        layout.addLayout(header)
        layout.addWidget(QLabel("Dự án của bạn"))
        self.projects = QListWidget()
        self.projects.itemClicked.connect(self.open_project)
        self.projects.itemActivated.connect(self.open_project)
        layout.addWidget(self.projects, 1)
        self.add_choices = QWidget()
        choices = QHBoxLayout(self.add_choices)
        self.existing_button = QPushButton("Project đã có")
        self.existing_button.setObjectName("primary")
        self.existing_button.clicked.connect(self.add_existing)
        choices.addWidget(self.existing_button)
        new = QPushButton("Dự án mới — Cài đặt nâng cao")
        new.clicked.connect(self.open_advanced)
        choices.addWidget(new)
        self.add_choices.hide()
        layout.addWidget(self.add_choices)
        row = QHBoxLayout()
        row.addStretch()
        self.create_button = QPushButton("+ Thêm dự án")
        self.create_button.clicked.connect(lambda: self.add_choices.setVisible(not self.add_choices.isVisible()))
        row.addWidget(self.create_button)
        layout.addLayout(row)
        self.message = explanation("")
        layout.addWidget(self.message)
        self.setCentralWidget(content)
        self.refresh_projects()

    def refresh_projects(self):
        with ProjectRegistry(self.root) as registry:
            projects = registry.list()
        self.projects.clear()
        for project in sorted(projects, key=lambda p: p.project_name.lower()):
            item = QListWidgetItem(project.project_name + "\n" + ("● Sẵn sàng" if project_ready(project) else "○ Cần hoàn tất thiết lập"))
            item.setData(Qt.ItemDataRole.UserRole, project.project_id)
            self.projects.addItem(item)

    def add_existing(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục project")
        if not folder:
            return
        self.busy = True
        self.projects.setEnabled(False)
        self.create_button.setEnabled(False)
        self.existing_button.setEnabled(False)
        self.message.setText("Đang nhận diện project…")
        def work():
            with self.service_factory() as service:
                return asyncio.run(service.register(Path(folder).name, folder, ProjectKind.EXISTING))
        self._job = Job(work)
        self._job.signals.finished.connect(self.registered)
        self.pool.start(self._job)

    @Slot(object, object)
    def registered(self, project, error):
        self.busy, self._job = False, None
        self.projects.setEnabled(True)
        self.create_button.setEnabled(True)
        self.existing_button.setEnabled(True)
        self.refresh_projects()
        self.message.setText("Chưa thêm được project. Kiểm tra thư mục hoặc Cài đặt nâng cao." if error else "")
        if not error:
            self.add_choices.hide()
            self.open_project_id(project.project_id)

    def open_project(self, item):
        self.open_project_id(item.data(Qt.ItemDataRole.UserRole))

    def open_project_id(self, project_id):
        if self.busy:
            return None
        for window in self.windows:
            if getattr(window, "project_id", None) == project_id and window.isVisible():
                window.raise_()
                return window
        with ProjectRegistry(self.root) as registry:
            project = registry.get(project_id)
        expected = ProjectChatWindow if project_ready(project) else ProjectSetupWizard
        for window in self.windows:
            if isinstance(window, expected) and window.project_id == project_id:
                window.show()
                window.raise_()
                return window
        if project_ready(project):
            window = ProjectChatWindow(self.root, project_id, self, worker_service_factory=self.worker_service_factory, auto_discover=self.auto_discover)
            window.back_requested.connect(self.refresh_projects)
        else:
            window = ProjectSetupWizard(self.root, project_id, self, service_factory=self.service_factory,
                worker_service_factory=self.worker_service_factory, auto_discover=self.auto_discover)
            window.opened.connect(lambda identity: self.setup_done(window, identity))
            window.changed.connect(self.refresh_projects)
        self.windows.append(window)
        window.show()
        return window

    def setup_done(self, wizard, project_id):
        wizard.close()
        self.refresh_projects()
        self.open_project_id(project_id)

    def open_advanced(self):
        if self.busy:
            return
        window = ProjectHub(self.root, service_factory=self.service_factory, auto_discover=False)
        window.advanced_toggle.setChecked(True)
        window.show()
        self.windows.append(window)

    def open_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Cài đặt ReviewRelay")
        dialog.resize(560, 270)
        layout = QVBoxLayout(dialog)
        layout.addWidget(explanation("Chọn dự án để cập nhật GitHub, ChatGPT và Worker Codex."))
        advanced = QPushButton("Chi tiết nâng cao")
        advanced.setCheckable(True)
        layout.addWidget(advanced)
        panel = QWidget()
        details = QVBoxLayout(panel)
        details.addWidget(explanation(f"Data ReviewRelay: {self.root.path}"))
        tools = QPushButton("Mở công cụ thiết lập / khôi phục dữ liệu cũ")
        tools.clicked.connect(self.open_advanced)
        details.addWidget(tools)
        panel.hide()
        advanced.toggled.connect(panel.setVisible)
        layout.addWidget(panel)
        self.windows.append(dialog)
        dialog.show()

    def closeEvent(self, event):
        if window_busy(self):
            self.message.setText("Chờ thao tác hiện tại hoàn tất trước khi đóng.")
            event.ignore()
        else:
            for window in self.windows:
                window.close()
            event.accept()
