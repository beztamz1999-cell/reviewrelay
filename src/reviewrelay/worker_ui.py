"""Small Project worker card and explicit human-readable selection."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QPushButton, QHBoxLayout, QVBoxLayout, QWidget

from .projects import ConnectionStatus, ProjectRegistry
from .worker.discovery import ProjectWorkerService


class WorkerChooser(QDialog):
    def __init__(self, project, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Chọn Worker cho " + project.project_name)
        self.resize(660, 360)
        layout = QVBoxLayout(self)
        self.choices = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(f"{candidate.title}\n{candidate.last_activity} · {candidate.source}")
            item.setData(Qt.ItemDataRole.UserRole, candidate.thread_id)
            self.choices.addItem(item)
        layout.addWidget(self.choices)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Chọn Worker")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Hủy")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.choices.currentItemChanged.connect(lambda item: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(item is not None))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_id(self):
        item = self.choices.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class ProjectWorkerCard(QWidget):
    changed = Signal()

    def __init__(self, root, selected_project, launch, parent=None, *, service_factory=None, auto_discover=True):
        super().__init__(parent)
        self.root, self.selected_project, self.launch = root, selected_project, launch
        self.factory = service_factory or (lambda: ProjectWorkerService(root))
        self.auto_discover, self.searched, self.connected = auto_discover, set(), None
        self.allow_create = False
        layout = QVBoxLayout(self)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        row = QHBoxLayout()
        self.change_button, self.scan_button, self.create_button = (QPushButton(t) for t in ("Đổi Worker", "Quét lại", "Tạo Worker mới"))
        self.change_button.clicked.connect(lambda: self.scan(change=True))
        self.scan_button.clicked.connect(self.scan)
        self.create_button.clicked.connect(self.create)
        for button in (self.change_button, self.scan_button, self.create_button):
            row.addWidget(button)
        layout.addLayout(row)

    def refresh(self, *, busy=False):
        project_id = self.selected_project()
        project = None
        available = False
        if project_id:
            with ProjectRegistry(self.root) as registry:
                project = registry.get(project_id)
                try:
                    registry.state.assert_project_available(project_id)
                    available = True
                except Exception:
                    pass
        if not project:
            self.label.setText("Worker Codex: Chọn một dự án")
        elif project.codex_worker_thread_id:
            self.label.setText(f"Worker Codex: {'Đã kết nối' if self.connected == project_id else 'Đã lưu; cần kiểm tra'}\n"
                f"{project.codex_worker_title or 'Worker Codex'}\nProject: {project.local_repo_path}\n"
                f"Hoạt động gần nhất: {project.codex_worker_last_activity or 'Chưa có thông tin'}")
        else:
            self.label.setText("Worker Codex: Chưa kết nối" if project.codex_status is not ConnectionStatus.READY
                else "Không tìm thấy Worker Codex đang dùng cho Project này.")
        enabled = bool(project and project.codex_status is ConnectionStatus.READY and not busy)
        self.scan_button.setEnabled(enabled)
        self.change_button.setEnabled(enabled and available)
        self.create_button.setVisible(bool(project and not project.codex_worker_thread_id and self.allow_create))
        self.create_button.setEnabled(enabled and available and self.allow_create)
        if enabled and self.isVisible() and self.auto_discover and project_id not in self.searched:
            self.searched.add(project_id)
            QTimer.singleShot(0, lambda: self.auto_scan(project_id))

    def showEvent(self, event):
        self.refresh(busy=getattr(self.parent(), "busy", False))
        super().showEvent(event)

    def auto_scan(self, project_id):
        if (self.isVisible() and self.selected_project() == project_id
                and not getattr(self.parent(), "busy", False)):
            self.scan()
        else:
            self.searched.discard(project_id)

    def scan(self, checked=False, *, change=False):
        project_id = self.selected_project()
        if not project_id:
            return
        self.connected = None
        self.launch(lambda: asyncio.run(self.factory().discover(project_id, change=change)),
                    lambda result, error: self.discovered(project_id, result, error))

    def discovered(self, project_id, result, error):
        if self.selected_project() != project_id:
            return
        self.allow_create = False
        if error:
            self.label.setText("Không thể kiểm tra Worker. Quét lại sau khi xử lý lỗi.")
            self.changed.emit()
            return
        self.refresh()
        if result.status == "CONNECTED":
            self.connected = project_id
            self.refresh()
        elif result.candidates:
            with ProjectRegistry(self.root) as registry:
                project = registry.get(project_id)
            chooser = WorkerChooser(project, result.candidates, self)
            if chooser.exec() == QDialog.DialogCode.Accepted and chooser.selected_id:
                thread_id = chooser.selected_id
                self.launch(lambda: asyncio.run(self.factory().select(project_id, thread_id)),
                    lambda value, failure: self.selected(project_id, value, failure))
        elif result.status == "NOT_FOUND":
            self.allow_create = True
            self.refresh()
        else:
            self.label.setText("Worker đã lưu không còn khớp dự án. Chọn Đổi Worker để kiểm tra lại.")
        self.changed.emit()

    def selected(self, project_id, result, error):
        self.connected = project_id if not error else None
        self.allow_create = False
        self.refresh()
        if error:
            self.label.setText("Worker chưa được kết nối. Dự án được giữ nguyên.")
        self.changed.emit()

    def create(self):
        project_id = self.selected_project()
        if not project_id or not self.allow_create or not self.create_button.isEnabled():
            return
        self.launch(lambda: asyncio.run(self.factory().create(project_id)),
            lambda result, error: self.selected(project_id, result, error))
