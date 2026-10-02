"""Minimal responsive Task controls; background controller owns the loop."""
from __future__ import annotations

import asyncio
import json

from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from .controller import TaskController
from .controller_store import ControllerState as S, ControllerStore
from .github_publish import task_branch_name, task_spec_path
from .ui import Job, dialog_buttons
from .worker_management import worker_view, WorkerStatus


class NewTaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Task — commit the canonical Owner specification")
        self.resize(720, 680)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.task_id, self.title = QLineEdit(), QLineEdit()
        self.fix_limit, self.evidence_limit = QSpinBox(), QSpinBox()
        for widget, value in ((self.fix_limit, 3), (self.evidence_limit, 5)):
            widget.setRange(0, 100)
            widget.setValue(value)
        form.addRow("Task ID", self.task_id)
        form.addRow("Title", self.title)
        form.addRow("Max fix cycles", self.fix_limit)
        form.addRow("Max evidence cycles", self.evidence_limit)
        layout.addLayout(form)
        self.spec = QTextEdit()
        self.spec.setPlaceholderText("Paste the Owner requirement. ReviewRelay will commit only .reviewrelay/tasks/<TASK_ID>.md before the worker starts.")
        layout.addWidget(self.spec)
        self.require_changes = QCheckBox("Require a new implementation commit")
        self.require_changes.setChecked(True)
        self.allow_spec_change = QCheckBox("Explicitly authorize this Task to change its canonical specification")
        layout.addWidget(self.require_changes)
        layout.addWidget(self.allow_spec_change)
        self.tests = QTextEdit()
        self.tests.setMaximumHeight(80)
        self.tests.setPlaceholderText('Optional Owner test registry JSON, e.g. {"unit": ["python", "-m", "pytest", "-q"]}')
        layout.addWidget(self.tests)
        layout.addWidget(QLabel("Create intentionally writes and commits the Task specification. Other local changes block creation. Start performs the automated loop."))
        self.buttons = dialog_buttons(self, "Create and Commit Task Specification")
        layout.addWidget(self.buttons)
        for edit in (self.task_id, self.title):
            edit.textChanged.connect(self.validate)
        self.spec.textChanged.connect(self.validate)
        self.validate()

    def validate(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(
            self.task_id.text().strip() and self.title.text().strip() and self.spec.toPlainText().strip()))

    def values(self):
        tests = json.loads(self.tests.toPlainText()) if self.tests.toPlainText().strip() else {}
        if not isinstance(tests, dict):
            raise ValueError("Owner test registry must be a JSON object")
        tests = {key: tuple(value) if isinstance(value, list) else value for key, value in tests.items()}
        return dict(task_id=self.task_id.text().strip(), title=self.title.text().strip(), spec=self.spec.toPlainText(),
            require_changes=self.require_changes.isChecked(), allow_spec_change=self.allow_spec_change.isChecked(),
            max_fix_cycles=self.fix_limit.value(), max_evidence_cycles=self.evidence_limit.value(), tests=tests)


class TaskWindow(QMainWindow):
    def __init__(self, root, project_id, parent=None, *, controller_factory=None):
        super().__init__(parent)
        self.root, self.project_id = root, project_id
        self.factory = controller_factory or (lambda: TaskController(root, project_id))
        self.setWindowTitle("ReviewRelay — Tasks")
        self.resize(1100, 780)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.busy, self._job, self.running_task_id = False, None, None
        content = QWidget()
        layout = QVBoxLayout(content)
        self.new_button = QPushButton("+ New Task")
        self.new_button.clicked.connect(self.new_task)
        layout.addWidget(self.new_button)
        body = QHBoxLayout()
        self.tasks = QListWidget()
        self.tasks.setMaximumWidth(260)
        self.tasks.currentItemChanged.connect(self.show_task)
        workers = QVBoxLayout()
        workers.addWidget(QLabel("Codex Workers — selected Project"))
        workers.addWidget(self.tasks)
        body.addLayout(workers)
        detail = QVBoxLayout()
        self.summary = QLabel("Select a Task")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        detail.addWidget(self.summary)
        self.spec = QTextEdit()
        self.spec.setReadOnly(True)
        self.spec.setMaximumHeight(160)
        detail.addWidget(self.spec)
        controls = QHBoxLayout()
        self.start_button, self.pause_button, self.resume_button, self.stop_button, self.owner_button = (
            QPushButton(label) for label in ("Start", "Pause", "Resume", "Stop", "Owner Decision"))
        for button, callback in ((self.start_button, lambda: self.start(False)), (self.pause_button, self.pause),
            (self.resume_button, lambda: self.start(True)), (self.stop_button, self.stop), (self.owner_button, self.owner_decision)):
            button.clicked.connect(callback)
            controls.addWidget(button)
        detail.addLayout(controls)
        worker_controls = QHBoxLayout()
        self.open_worker_button, self.pause_auto_button, self.manual_button, self.resume_auto_button = (
            QPushButton(label) for label in ("Open Worker", "Pause Auto Relay", "Send Manual Instruction", "Resume Auto Relay"))
        for button, callback in ((self.open_worker_button, self.open_worker), (self.pause_auto_button, self.pause_auto),
                (self.manual_button, self.manual_instruction), (self.resume_auto_button, self.resume_auto)):
            button.clicked.connect(callback)
            worker_controls.addWidget(button)
        detail.addLayout(worker_controls)
        self.worker_identity = QLabel()
        self.worker_identity.setTextFormat(Qt.TextFormat.PlainText)
        self.worker_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.worker_identity.setWordWrap(True)
        detail.addWidget(self.worker_identity)
        self.timeline = QTextEdit()
        self.timeline.setReadOnly(True)
        detail.addWidget(self.timeline)
        body.addLayout(detail, 1)
        layout.addLayout(body)
        self.message = QLabel("Start runs the Task loop. Pause waits for a safe checkpoint; Stop requests interruption. PASS is ready for Owner review.")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.setCentralWidget(content)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    @property
    def task_id(self):
        item = self.tasks.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def showEvent(self, event):
        self.timer.start()
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        selected = self.task_id
        with ControllerStore(self.root) as store:
            tasks = store.list(self.project_id)
            views = {t.task_id: worker_view(store, self.project_id, t.task_id) for t in tasks}
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for task in tasks:
            item = QListWidgetItem(f"{task.task_id} — {views[task.task_id].status.value}")
            item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            self.tasks.addItem(item)
            if task.task_id == selected:
                self.tasks.setCurrentItem(item)
        if self.tasks.currentItem() is None and self.tasks.count():
            self.tasks.setCurrentRow(0)
        self.tasks.blockSignals(False)
        self.show_task()

    def show_task(self, *_):
        self.new_button.setEnabled(not self.busy)
        task = None
        if self.task_id:
            with ControllerStore(self.root) as store:
                task = store.get(self.project_id, self.task_id)
                record = store.state.get(self.project_id, self.task_id)
                publication = store.db.execute("SELECT * FROM github_publications WHERE project_id=? AND task_id=?", (self.project_id, self.task_id)).fetchone()
                events = store.events(self.project_id, self.task_id)
                view = worker_view(store, self.project_id, self.task_id)
            identity = view.identity
            self.worker_identity.setText(f"PROJECT_ID={identity.project_id}\nTASK_ID={identity.task_id}\n"
                f"WORKER_THREAD_ID={identity.worker_thread_id or 'Not started'}\nREPOSITORY={identity.repository}\n"
                f"TASK_BRANCH={identity.task_branch}\nWORKER_STATUS={view.status.value}")
            counters = task.counters
            self.summary.setText(f"Task: {task.task_id} — {task.title}\nState: {task.state.value}\nSpec: {task_spec_path(task.task_id)}\n"
                f"Branch: {task_branch_name(task.task_id)}\nBase SHA: {task.base_sha or 'Not committed'}\nCandidate SHA: {task.candidate_sha or 'None'}\n"
                f"Remote SHA: {publication['github_last_remote_sha'] if publication else 'None'}\n"
                f"Codex: {record.worker_last_turn_status or 'Not started'} | thread: {record.worker_thread_id or 'None'} | turns: {counters['worker_initial_turns'] + counters['worker_fix_turns'] + counters.get('worker_continuation_turns', 0) + counters.get('worker_manual_turns', 0)}\n"
                f"GitHub: {publication['github_publish_status'] if publication else 'Not published'} | PR: {publication['github_pr_url'] if publication else 'None'}\n"
                f"ChatGPT: {task.state.value} | review messages: {counters['review_messages']} | evidence messages: {counters['evidence_messages']}\n"
                f"Review cycle: {task.review_cycle} | Fix cycles: {task.fix_cycles} | Evidence cycles: {task.evidence_cycles} | Local batches: {counters['local_evidence_batches']}\n"
                f"READY FOR OWNER REVIEW: {'YES' if task.ready_for_owner_review else 'NO'}\n"
                f"{task.error_code or ''} {task.reason or ''}\n{task.context or ''}")
            self.spec.setPlainText(task.spec)
            self.timeline.setPlainText("\n".join(f"{e['created_at']} {e['kind']} {e['payload_json']}" for e in events[-1000:]))
        else:
            self.summary.setText("No Tasks. Create one to commit its canonical specification.")
            self.spec.clear()
            self.timeline.clear()
            self.worker_identity.clear()
        self.start_button.setEnabled(bool(task and not self.busy and task.state is S.READY))
        self.resume_button.setEnabled(bool(task and not task.review_invalidated and not self.busy and task.state not in {S.READY, S.COMPLETE, S.STOPPED, S.PAUSED_OWNER, S.PAUSED_OWNER_STEER}))
        self.pause_button.setEnabled(bool(task and self.busy and self.running_task_id == task.task_id and task.state is not S.PAUSED_OWNER_STEER))
        self.stop_button.setEnabled(bool(task and task.state not in {S.COMPLETE, S.STOPPED}))
        self.owner_button.setEnabled(bool(task and not self.busy and task.state is S.PAUSED_OWNER and task.error_code != "OWNER_ESCALATION_REQUIRED"))
        self.open_worker_button.setEnabled(bool(task))
        self.pause_auto_button.setEnabled(bool(task and not task.review_invalidated and task.state not in {S.STOPPED, S.PAUSED_OWNER_STEER}))
        self.manual_button.setEnabled(bool(task and identity.worker_thread_id and not self.busy and not task.manual_pending
            and not task.review_invalidated and task.state is S.PAUSED_OWNER_STEER))
        self.resume_auto_button.setEnabled(bool(task and not self.busy and not task.review_invalidated and task.state is S.PAUSED_OWNER_STEER))

    def selected_worker(self):
        with ControllerStore(self.root) as store:
            return worker_view(store, self.project_id, self.task_id).identity

    def open_worker(self):
        controller = self.factory()
        try:
            view = controller.open_worker(self.task_id)
            self.message.setText(f"Opened worker {view.identity.task_id}: {view.identity.worker_thread_id or 'Not started'}. Activity is shown below.")
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', 'WORKER_SELECTION_FAILED')}: {exc}")
        finally:
            controller.close()
        self.show_task()

    def pause_auto(self):
        identity = self.selected_worker()
        controller = self.factory()
        try:
            view = controller.request_worker_pause(identity)
            self.message.setText(f"{identity.task_id}: {view.status.value}")
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', 'OWNER_STEER_NOT_SAFE')}: {exc}")
        finally:
            controller.close()
        self.refresh()

    def manual_instruction(self):
        identity = self.selected_worker()  # Capture selection before the dialog/job.
        text, accepted = QInputDialog.getMultiLineText(self, "Send Manual Instruction",
            f"Owner instruction to {identity.task_id}, exact thread {identity.worker_thread_id}. Auto Relay stays paused.")
        if not accepted:
            return
        def work():
            controller = self.factory()
            try:
                return asyncio.run(controller.send_manual_instruction(identity, text))
            finally:
                controller.close()
        self.start_job(work, identity.task_id)

    def resume_auto(self):
        identity = self.selected_worker()
        def work():
            controller = self.factory()
            try:
                asyncio.run(controller.resume_auto_relay(identity))
                return asyncio.run(controller.run(identity.task_id, resume=True))
            finally:
                controller.close()
        self.start_job(work, identity.task_id)

    def start_job(self, work, task_id=None):
        if self.busy:
            return False
        self.busy, self.running_task_id = True, task_id
        self._job = Job(work)
        self._job.signals.finished.connect(self.finished)
        self.pool.start(self._job)
        self.show_task()
        return True

    def finished(self, result, error):
        self.busy, self._job, self.running_task_id = False, None, None
        self.message.setText(f"{getattr(error, 'code', 'TASK_SETUP_FAILED')}: {error}" if error else
            f"Task state: {getattr(result, 'state', 'Updated')}. {getattr(result, 'error_code', None) or ''}")
        self.refresh()

    def new_task(self):
        dialog = NewTaskDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.values()
        except (ValueError, TypeError) as exc:
            self.message.setText("TASK_CONFIGURATION_INVALID: " + str(exc))
            return
        def work():
            controller = self.factory()
            try:
                return asyncio.run(controller.create_task(**values))
            finally:
                controller.close()
        self.start_job(work)

    def start(self, resume=False):
        task_id = self.task_id
        def work():
            controller = self.factory()
            try:
                return asyncio.run(controller.run(task_id, resume=resume))
            finally:
                controller.close()
        self.start_job(work, task_id)

    def pause(self):
        with ControllerStore(self.root) as store:
            store.control(self.project_id, self.task_id, "PAUSE")
        self.message.setText("Pause requested. The current effect may finish; no next effect starts after its safe checkpoint.")

    def stop(self):
        with ControllerStore(self.root) as store:
            store.control(self.project_id, self.task_id, "STOP")
        if not self.busy:
            self.start(True)
        self.message.setText("Stop requested. A dispatched effect is never undone or automatically repeated.")

    def owner_decision(self):
        text, accepted = QInputDialog.getMultiLineText(self, "Explicit Owner Decision", "Owner input is sent to the reviewer for this same candidate; it is not a direct worker command.")
        if not accepted:
            return
        task_id = self.task_id
        def work():
            controller = self.factory()
            try:
                controller.resume_with_owner_decision(task_id, text)
                return asyncio.run(controller.run(task_id))
            finally:
                controller.close()
        self.start_job(work, task_id)

    def closeEvent(self, event):
        if self.busy:
            self.message.setText("Stop or pause the Task and wait for its current operation before closing.")
            event.ignore()
        else:
            self.timer.stop()
            event.accept()
