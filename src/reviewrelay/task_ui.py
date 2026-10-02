"""Minimal responsive Task controls; background controller owns the loop."""
from __future__ import annotations

import asyncio
import json

from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from .controller import TaskController
from .approval_ui import CommandApprovalBridge
from .controller_store import ControllerState as S, ControllerStore
from .github_publish import task_branch_name, task_spec_path
from .ui import Job, dialog_buttons, owner_input, explanation
from .worker_management import worker_view, same_path
from .owner_recovery import RecoveryAction, recovery_action
from .ui_text import recovery_text, state_text, detail_text


class NewTaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tạo công việc mới")
        self.resize(720, 680)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.task_id, self.title = QLineEdit(), QLineEdit()
        self.fix_limit, self.evidence_limit = QSpinBox(), QSpinBox()
        for widget, value in ((self.fix_limit, 3), (self.evidence_limit, 5)):
            widget.setRange(0, 100)
            widget.setValue(value)
        form.addRow("Mã công việc", self.task_id)
        form.addRow("Tiêu đề", self.title)
        form.addRow("Số vòng sửa tối đa", self.fix_limit)
        form.addRow("Số vòng kiểm chứng tối đa", self.evidence_limit)
        layout.addLayout(form)
        self.spec = QTextEdit()
        self.spec.setPlaceholderText("Nhập yêu cầu của Owner. ReviewRelay sẽ lưu .reviewrelay/tasks/<TASK_ID>.md thành commit riêng trước khi Codex bắt đầu.")
        layout.addWidget(self.spec)
        self.require_changes = QCheckBox("Yêu cầu commit code mới")
        self.require_changes.setChecked(True)
        self.allow_spec_change = QCheckBox("Cho phép công việc này thay đổi đặc tả gốc")
        layout.addWidget(self.require_changes)
        layout.addWidget(self.allow_spec_change)
        self.tests = QTextEdit()
        self.tests.setMaximumHeight(80)
        self.tests.setPlaceholderText('Cấu hình test bổ sung của Owner (JSON), ví dụ {"unit": ["python", "-m", "pytest", "-q"]}')
        layout.addWidget(self.tests)
        layout.addWidget(explanation("ReviewRelay sẽ lưu yêu cầu công việc thành một commit riêng trước khi Codex bắt đầu. Cần xử lý các thay đổi local khác trước khi tạo. Chọn Bắt đầu để chạy tự động."))
        self.buttons = dialog_buttons(self, "Tạo công việc")
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
            raise ValueError("Cấu hình test của Owner phải là một đối tượng JSON")
        tests = {key: tuple(value) if isinstance(value, list) else value for key, value in tests.items()}
        return dict(task_id=self.task_id.text().strip(), title=self.title.text().strip(), spec=self.spec.toPlainText(),
            require_changes=self.require_changes.isChecked(), allow_spec_change=self.allow_spec_change.isChecked(),
            max_fix_cycles=self.fix_limit.value(), max_evidence_cycles=self.evidence_limit.value(), tests=tests)


class TaskWindow(QMainWindow):
    def __init__(self, root, project_id, parent=None, *, controller_factory=None):
        super().__init__(parent)
        self.root, self.project_id = root, project_id
        self.approvals = CommandApprovalBridge(self, self.valid_approval)
        self.factory = controller_factory or (lambda: TaskController(root, project_id,
            command_approval=self.approvals.approve))
        self.setWindowTitle("ReviewRelay — Công việc")
        self.resize(1100, 780)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.busy, self._job, self.running_task_id = False, None, None
        content = QWidget()
        layout = QVBoxLayout(content)
        self.new_button = QPushButton("+ Tạo công việc")
        self.new_button.clicked.connect(self.new_task)
        layout.addWidget(self.new_button)
        body = QHBoxLayout()
        self.tasks = QListWidget()
        self.tasks.setMaximumWidth(260)
        self.tasks.currentItemChanged.connect(self.show_task)
        workers = QVBoxLayout()
        workers.addWidget(QLabel("Codex Worker — dự án hiện tại"))
        workers.addWidget(self.tasks)
        body.addLayout(workers)
        detail = QVBoxLayout()
        self.summary = QLabel("Chọn một công việc")
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
            QPushButton(label) for label in ("Bắt đầu", "Tạm dừng", "Tiếp tục", "Dừng", "Quyết định của Owner"))
        for button, callback in ((self.start_button, lambda: self.start(False)), (self.pause_button, self.pause),
            (self.resume_button, lambda: self.start(True)), (self.stop_button, self.stop), (self.owner_button, self.owner_decision)):
            button.clicked.connect(callback)
            controls.addWidget(button)
        detail.addLayout(controls)
        worker_controls = QHBoxLayout()
        self.open_worker_button, self.pause_auto_button, self.manual_button, self.resume_auto_button = (
            QPushButton(label) for label in ("Xem Worker", "Tạm dừng tự động", "Gửi chỉ dẫn thủ công", "Tiếp tục tự động"))
        for button, callback in ((self.open_worker_button, self.open_worker), (self.pause_auto_button, self.pause_auto),
                (self.manual_button, self.manual_instruction), (self.resume_auto_button, self.resume_auto)):
            button.clicked.connect(callback)
            worker_controls.addWidget(button)
        detail.addLayout(worker_controls)
        self.recovery_button = QPushButton()
        self.recovery_button.clicked.connect(self.recover)
        self.recovery_button.hide()
        detail.addWidget(self.recovery_button)
        self.recovery = None
        self.worker_identity = QLabel()
        self.worker_identity.setTextFormat(Qt.TextFormat.PlainText)
        self.worker_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.worker_identity.setWordWrap(True)
        detail.addWidget(self.worker_identity)
        self.timeline = QTextEdit()
        self.timeline.setReadOnly(True)
        detail.addWidget(QLabel("Nhật ký hoạt động"))
        detail.addWidget(self.timeline)
        body.addLayout(detail, 1)
        layout.addLayout(body)
        self.message = QLabel("Bắt đầu chạy công việc tự động. Tạm dừng chờ điểm dừng an toàn; Dừng yêu cầu ngắt công việc. PASS nghĩa là sẵn sàng để Owner duyệt.")
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
            item = QListWidgetItem(f"{task.task_id} — {state_text(views[task.task_id].status)}")
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
                f"WORKER_THREAD_ID={identity.worker_thread_id or 'Chưa bắt đầu'}\nREPOSITORY={identity.repository}\n"
                f"TASK_BRANCH={identity.task_branch}\nTrạng thái Worker: {state_text(view.status)}")
            counters = task.counters
            self.summary.setText(f"Công việc: {task.task_id} — {task.title}\nTrạng thái: {state_text(task.state)}\nĐặc tả: {task_spec_path(task.task_id)}\n"
                f"Nhánh: {task_branch_name(task.task_id)}\nBase SHA: {task.base_sha or 'Chưa commit'}\nCandidate SHA: {task.candidate_sha or 'Không có'}\n"
                f"Remote SHA: {publication['github_last_remote_sha'] if publication else 'Không có'}\n"
                f"Codex: {state_text(record.worker_last_turn_status) or 'Chưa bắt đầu'} | thread: {record.worker_thread_id or 'Không có'} | lượt: {counters['worker_initial_turns'] + counters['worker_fix_turns'] + counters.get('worker_continuation_turns', 0) + counters.get('worker_manual_turns', 0)}\n"
                f"GitHub: {state_text(publication['github_publish_status']) if publication else 'Chưa đồng bộ'} | PR: {(publication['github_pr_url'] or 'Không có') if publication else 'Không có'}\n"
                f"ChatGPT: {state_text(task.state)} | tin nhắn review: {counters['review_messages']} | tin nhắn kiểm chứng: {counters['evidence_messages']}\n"
                f"Vòng review: {task.review_cycle} | Số vòng sửa: {task.fix_cycles} | Số vòng kiểm chứng: {task.evidence_cycles} | Lượt kiểm chứng local: {counters['local_evidence_batches']}\n"
                f"SẴN SÀNG ĐỂ OWNER DUYỆT: {'CÓ' if task.ready_for_owner_review else 'CHƯA'}\n"
                f"{task.error_code or ''} {detail_text(task.reason) or ''}\n{task.context or ''}")
            self.spec.setPlainText(task.spec)
            self.timeline.setPlainText("\n".join(f"{e['created_at']} {e['kind']} {e['payload_json']}" for e in events[-1000:]))
        else:
            self.summary.setText("Chưa có công việc. Tạo công việc để lưu đặc tả gốc thành commit.")
            self.spec.clear()
            self.timeline.clear()
            self.worker_identity.clear()
        self.start_button.setEnabled(bool(task and not self.busy and task.state is S.READY))
        self.resume_button.setEnabled(bool(task and not task.review_invalidated and not self.busy and task.state not in {S.READY, S.COMPLETE, S.STOPPED, S.PAUSED_OWNER, S.PAUSED_OWNER_STEER, S.PAUSED_ERROR}))
        self.pause_button.setEnabled(bool(task and self.busy and self.running_task_id == task.task_id and task.state is not S.PAUSED_OWNER_STEER))
        self.stop_button.setEnabled(bool(task and task.state not in {S.COMPLETE, S.STOPPED}))
        self.owner_button.setEnabled(bool(task and not self.busy and task.state is S.PAUSED_OWNER and task.error_code != "OWNER_ESCALATION_REQUIRED"))
        self.open_worker_button.setEnabled(bool(task))
        self.pause_auto_button.setEnabled(bool(task and not task.review_invalidated and task.state not in {S.STOPPED, S.PAUSED_OWNER_STEER}))
        self.manual_button.setEnabled(bool(task and identity.worker_thread_id and not self.busy and not task.manual_pending
            and not task.review_invalidated and task.state is S.PAUSED_OWNER_STEER))
        self.resume_auto_button.setEnabled(bool(task and not self.busy and not task.review_invalidated and task.state is S.PAUSED_OWNER_STEER))
        self.recovery = None
        if task and task.state is S.PAUSED_ERROR and not self.busy:
            controller = self.factory()
            try:
                self.recovery = recovery_action(controller, identity)
            finally:
                controller.close()
        self.recovery_button.setVisible(self.recovery is not None)
        self.recovery_button.setEnabled(self.recovery is not None and not self.busy)
        self.recovery_button.setText(recovery_text(self.recovery) if self.recovery else "")

    def valid_approval(self, request):
        """GUI-side revalidation before showing AND returning an Owner decision."""
        if (not self.busy or request.get("project_id") != self.project_id
                or request.get("task_id") != self.running_task_id
                or request.get("environment_id") not in {None, "local"}
                or not isinstance(request.get("command"), str) or not request["command"].strip()
                or len(request["command"]) > 16384 or "\x00" in request["command"]
                or not request.get("item_id")):
            return False
        controller = self.factory()
        try:
            identity = worker_view(controller.store, self.project_id, self.running_task_id).identity
            task, _ = controller._worker_binding(identity, require_thread=False)
            record = controller.store.state.get(self.project_id, self.running_task_id)
            if (request.get("thread_id") != identity.worker_thread_id or not identity.worker_thread_id
                    or (task.worker_thread_id and task.worker_thread_id != request.get("thread_id"))
                    or request.get("turn_id") != record.worker_last_turn_id
                    or record.worker_last_turn_status != "IN_PROGRESS" or not same_path(request.get("cwd"), identity.repository)
                    or controller.store.control(self.project_id, self.running_task_id) == "STOP"):
                return False
            if task.state is S.WORKER_RUNNING:
                kind = task.pending.get("worker_kind")
                key = controller._key(task, kind, task.pending.get("number", -1))
            elif task.state is S.PAUSED_OWNER_STEER and task.manual_pending:
                kind, key = "WORKER_MANUAL", task.manual_pending.get("key")
            else:
                return False
            effect = controller.store.effect(key)
            return bool(effect and effect["project_id"] == self.project_id and effect["task_id"] == self.running_task_id
                and kind in {"WORKER_INITIAL", "WORKER_FIX", "WORKER_CONTINUATION", "WORKER_MANUAL"}
                and effect["kind"] == kind and effect["status"] in {"IN_FLIGHT", "CONFIRMED"}
                and effect["payload"].get("thread_id") in {None, request["thread_id"]}
                and (effect["status"] == "IN_FLIGHT" or effect["payload"].get("turn_id") == request["turn_id"]
                    and effect["payload"].get("thread_id") == request["thread_id"]))
        finally:
            controller.close()

    def recover(self):
        if self.busy or self.recovery is None:
            return
        identity, action = self.selected_worker(), self.recovery
        instruction = None
        if action is RecoveryAction.CONTINUE_WORKER:
            instruction, accepted = owner_input(self, recovery_text(action),
                f"Chỉ dẫn cho {identity.task_id}, giữ nguyên Codex thread {identity.worker_thread_id}.")
            if not accepted:
                return
        def work():
            controller = self.factory()
            async def execute():
                if recovery_action(controller, identity) is not action:
                    raise ValueError("Không còn đủ điều kiện khôi phục; công việc vẫn tạm dừng.")
                if action is RecoveryAction.CHECK_WORKER:
                    return await controller.reconcile_interrupted_continuation(identity)
                if action is RecoveryAction.CONTINUE_WORKER:
                    await controller.continue_incomplete_worker(identity, instruction)
                elif action is RecoveryAction.RETRY_REVIEW:
                    await controller.reconcile_unsent_review(identity)
                elif action is RecoveryAction.RECOVER_REVIEW:
                    await controller.reconcile_visible_review(identity)
                return await controller.run(identity.task_id)
            try:
                return asyncio.run(execute())
            finally:
                controller.close()
        self.start_job(work, identity.task_id)

    def selected_worker(self):
        with ControllerStore(self.root) as store:
            return worker_view(store, self.project_id, self.task_id).identity

    def open_worker(self):
        controller = self.factory()
        try:
            view = controller.open_worker(self.task_id)
            self.message.setText(f"Đã mở Worker {view.identity.task_id}: {view.identity.worker_thread_id or 'Chưa bắt đầu'}. Xem hoạt động bên dưới.")
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
            self.message.setText(f"{identity.task_id}: {state_text(view.status)}")
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', 'OWNER_STEER_NOT_SAFE')}: {exc}")
        finally:
            controller.close()
        self.refresh()

    def manual_instruction(self):
        identity = self.selected_worker()  # Capture selection before the dialog/job.
        text, accepted = owner_input(self, "Gửi chỉ dẫn thủ công",
            f"Chỉ dẫn của Owner cho {identity.task_id}, đúng thread {identity.worker_thread_id}. Tự động vẫn tạm dừng.")
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
            f"Trạng thái công việc: {state_text(getattr(result, 'state', 'Đã cập nhật'))}. {getattr(result, 'error_code', None) or ''}")
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
        self.message.setText("Đã yêu cầu tạm dừng. Thao tác hiện tại có thể hoàn tất; thao tác tiếp theo sẽ chờ tại điểm dừng an toàn.")

    def stop(self):
        with ControllerStore(self.root) as store:
            store.control(self.project_id, self.task_id, "STOP")
        if not self.busy:
            self.start(True)
        self.message.setText("Đã yêu cầu dừng. Thao tác đã bắt đầu sẽ không bị hoàn tác hoặc tự động lặp lại.")

    def owner_decision(self):
        text, accepted = owner_input(self, "Quyết định của Owner", "Ý kiến của Owner được gửi cho Reviewer về cùng candidate này, không gửi trực tiếp thành lệnh cho Worker.")
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
        self.approvals.cancel_pending()
        if self.busy:
            self.message.setText("Dừng hoặc tạm dừng công việc, rồi chờ thao tác hiện tại hoàn tất trước khi đóng.")
            event.ignore()
        else:
            self.timer.stop()
            event.accept()
