"""One-command Owner decisions, delivered exclusively on the Qt GUI thread."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout


class CommandApprovalDialog(QDialog):
    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.decision = "cancel"
        self.setWindowTitle("Cho phép chạy lệnh này?")
        self.resize(720, 400)
        layout = QVBoxLayout(self)
        self.metadata = QLabel("\n".join(f"{label}: {request[key]}" for label, key in (
            ("Dự án", "project_id"), ("Công việc", "task_id"), ("Codex thread", "thread_id"),
            ("Lượt", "turn_id"), ("cwd", "cwd"))))
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metadata.setWordWrap(True)
        layout.addWidget(self.metadata)
        layout.addWidget(QLabel("Lệnh cần chạy (chỉ áp dụng một lần):"))
        self.command = QTextEdit()
        self.command.setReadOnly(True)
        self.command.setPlainText(request["command"])
        layout.addWidget(self.command)
        buttons = QHBoxLayout()
        self.accept_once, self.decline, self.cancel = (QPushButton(label) for label in
            ("Cho phép một lần", "Từ chối", "Hủy"))
        for button, decision in ((self.accept_once, "accept"), (self.decline, "decline"), (self.cancel, "cancel")):
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, value=decision: self.choose(value))
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def choose(self, decision):
        self.decision = decision
        self.done(QDialog.DialogCode.Accepted if decision == "accept" else QDialog.DialogCode.Rejected)


@dataclass(eq=False)
class _Request:
    metadata: dict
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future


class CommandApprovalBridge(QObject):
    requested = Signal(object)
    released = Signal(object)

    def __init__(self, parent, validate):
        super().__init__(parent)
        self.validate = validate
        self.pending, self.active, self.dialog = [], None, None
        self.closed = False
        self.requested.connect(self.receive, Qt.ConnectionType.QueuedConnection)
        self.released.connect(self.release, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.check)
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    async def approve(self, metadata):
        # Called by the adapter's asyncio loop on the background thread. No widgets.
        loop = asyncio.get_running_loop()
        request = _Request(dict(metadata), loop, loop.create_future())
        self.requested.emit(request)
        try:
            return await request.future
        finally:
            self.released.emit(request)  # Also dismiss the dialog on overall timeout.

    def valid(self, request):
        try:
            return not self.closed and not request.future.done() and bool(self.validate(request.metadata))
        except Exception:
            return False

    @staticmethod
    def resolve(request, decision):
        def deliver():
            if not request.future.done():
                request.future.set_result(decision)
        if not request.loop.is_closed():
            try:
                request.loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                pass  # The overall timeout may already have closed the worker loop.

    @Slot(object)
    def receive(self, request):
        if not self.valid(request):
            self.resolve(request, "cancel")
            return
        self.pending.append(request)
        self.timer.start()
        self.show_next()

    def show_next(self):
        while self.active is None and self.pending:
            request = self.pending.pop(0)
            if not self.valid(request):
                self.resolve(request, "cancel")
                continue
            self.active = request
            self.dialog = CommandApprovalDialog(request.metadata, self.parent())
            self.dialog.finished.connect(self.decided)
            self.dialog.open()
        if self.active is None and not self.pending:
            self.timer.stop()

    @Slot(int)
    def decided(self, _result):
        request, dialog = self.active, self.dialog
        self.active, self.dialog = None, None
        if request is not None:
            decision = dialog.decision if self.valid(request) else "cancel"
            self.resolve(request, decision)
            dialog.deleteLater()
        self.show_next()

    @Slot(object)
    def release(self, request):
        self.pending = [r for r in self.pending if r is not request]
        if self.active is request:
            self.dialog.reject()

    @Slot()
    def check(self):
        for request in tuple(self.pending):
            if not self.valid(request):
                self.resolve(request, "cancel")
                self.release(request)
        if self.active is not None and not self.valid(self.active):
            self.dialog.reject()

    @Slot()
    def cancel_pending(self):
        pending, self.pending = self.pending, []
        for request in pending:
            self.resolve(request, "cancel")
        if self.dialog is not None:
            self.dialog.reject()

    @Slot()
    def shutdown(self):
        self.closed = True
        self.cancel_pending()
        self.timer.stop()
