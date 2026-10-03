"""Minimal responsive Task controls; background controller owns the loop."""
from __future__ import annotations

import asyncio
import json

from PySide6.QtCore import QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget, QScrollArea)

from .controller import TaskController
from .approval_ui import CommandApprovalBridge
from .controller_store import ControllerState as S, ControllerStore
from .github_publish import task_branch_name, task_spec_path
from .ui import Job, dialog_buttons, owner_input, explanation
from .worker_management import worker_view, same_path
from .owner_recovery import RecoveryAction, recovery_action
from .ui_text import recovery_text, state_text, detail_text
from .projects import ProjectRegistry
from .worker_ui import ProjectWorkerCard


class NewTaskDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gửi yêu cầu")
        self.resize(720, 680)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.fix_limit, self.evidence_limit = QSpinBox(), QSpinBox()
        for widget, value in ((self.fix_limit, 3), (self.evidence_limit, 5)):
            widget.setRange(0, 100)
            widget.setValue(value)
        form.addRow("Số vòng sửa tối đa", self.fix_limit)
        form.addRow("Số vòng kiểm chứng tối đa", self.evidence_limit)
        self.advanced_button = QPushButton("Tuỳ chọn nâng cao")
        self.advanced_button.setCheckable(True)
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.addLayout(form)
        advanced.hide()
        self.advanced_button.toggled.connect(advanced.setVisible)
        layout.addWidget(QLabel("Bạn muốn Codex làm gì?"))
        self.spec = QTextEdit()
        self.spec.setPlaceholderText("Nhập yêu cầu của bạn…")
        layout.addWidget(self.spec)
        self.require_changes = QCheckBox("Yêu cầu commit code mới")
        self.require_changes.setChecked(True)
        self.allow_spec_change = QCheckBox("Cho phép công việc này thay đổi đặc tả gốc")
        advanced_layout.addWidget(self.require_changes)
        advanced_layout.addWidget(self.allow_spec_change)
        self.tests = QTextEdit()
        self.tests.setMaximumHeight(80)
        self.tests.setPlaceholderText('Cấu hình test bổ sung của Owner (JSON), ví dụ {"unit": ["python", "-m", "pytest", "-q"]}')
        advanced_layout.addWidget(self.tests)
        layout.addWidget(self.advanced_button)
        layout.addWidget(advanced)
        self.buttons = dialog_buttons(self, "Gửi yêu cầu")
        layout.addWidget(self.buttons)
        self.spec.textChanged.connect(self.validate)
        self.validate()

    def validate(self):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(
            self.spec.toPlainText().strip()))

    def values(self):
        tests = json.loads(self.tests.toPlainText()) if self.tests.toPlainText().strip() else {}
        if not isinstance(tests, dict):
            raise ValueError("Cấu hình test của Owner phải là một đối tượng JSON")
        tests = {key: tuple(value) if isinstance(value, list) else value for key, value in tests.items()}
        return dict(prompt=self.spec.toPlainText(),
            require_changes=self.require_changes.isChecked(), allow_spec_change=self.allow_spec_change.isChecked(),
            max_fix_cycles=self.fix_limit.value(), max_evidence_cycles=self.evidence_limit.value(), tests=tests)


class TaskWindow(QMainWindow):
    task_created = Signal(str)

    def __init__(self, root, project_id, parent=None, *, controller_factory=None, worker_service_factory=None, auto_discover=None):
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
        self._callback = None
        self.task_created.connect(self.created)
        content = QWidget()
        layout = QVBoxLayout(content)
        with ProjectRegistry(root) as registry:
            project = registry.get(project_id)
        self.heading = QLabel(project.project_name)
        layout.addWidget(self.heading)
        self.connections = QLabel()
        layout.addWidget(self.connections)
        self.worker_card = ProjectWorkerCard(root, lambda: self.project_id,
            lambda work, callback: self.start_job(work, callback=callback), self,
            service_factory=worker_service_factory, auto_discover=(controller_factory is None or worker_service_factory is not None)
            if auto_discover is None else auto_discover)
        layout.addWidget(self.worker_card)
        layout.addWidget(QLabel("Bạn muốn Codex làm gì?"))
        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText("Nhập yêu cầu của bạn…")
        self.prompt.setMaximumHeight(150)
        layout.addWidget(self.prompt)
        self.send_button = QPushButton("Gửi yêu cầu")
        self.send_button.clicked.connect(self.submit_prompt)
        self.prompt.textChanged.connect(self.show_task)
        layout.addWidget(self.send_button)
        self.new_button = QPushButton("Tuỳ chọn yêu cầu nâng cao")
        self.new_button.clicked.connect(self.new_task)
        body = QHBoxLayout()
        self.tasks = QListWidget()
        self.tasks.setMaximumWidth(260)
        self.tasks.currentItemChanged.connect(self.show_task)
        workers = QVBoxLayout()
        workers.addWidget(QLabel("Lịch sử công việc"))
        workers.addWidget(self.tasks)
        body.addLayout(workers)
        detail = QVBoxLayout()
        self.owner_summary = QLabel("Nhập yêu cầu để bắt đầu")
        self.owner_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.owner_summary.setWordWrap(True)
        detail.addWidget(self.owner_summary)
        self.diagnostics_button = QPushButton("Chi tiết kỹ thuật")
        self.diagnostics_button.setCheckable(True)
        self.diagnostics = QWidget()
        diagnostic_layout = QVBoxLayout(self.diagnostics)
        self.diagnostics.hide()
        self.diagnostics_button.toggled.connect(self.diagnostics.setVisible)
        diagnostic_layout.addWidget(self.new_button)
        self.project_identity = QLabel()
        self.project_identity.setTextFormat(Qt.TextFormat.PlainText)
        self.project_identity.setWordWrap(True)
        diagnostic_layout.addWidget(self.project_identity)
        self.summary = QLabel("Chọn một công việc")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary.setWordWrap(True)
        diagnostic_layout.addWidget(self.summary)
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
            if button is self.start_button:
                diagnostic_layout.addWidget(button)
            else:
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
        diagnostic_layout.addWidget(self.worker_identity)
        self.timeline = QTextEdit()
        self.timeline.setReadOnly(True)
        self.diagnostic_error = QLabel()
        self.diagnostic_error.setTextFormat(Qt.TextFormat.PlainText)
        self.diagnostic_error.setWordWrap(True)
        diagnostic_layout.addWidget(self.diagnostic_error)
        diagnostic_layout.addWidget(QLabel("Nhật ký hoạt động"))
        diagnostic_layout.addWidget(self.timeline)
        detail.addWidget(self.diagnostics_button)
        self.diagnostic_scroll = QScrollArea()
        self.diagnostic_scroll.setWidgetResizable(True)
        self.diagnostic_scroll.setMaximumHeight(300)
        self.diagnostic_scroll.setWidget(self.diagnostics)
        self.diagnostic_scroll.hide()
        self.diagnostics_button.toggled.connect(self.diagnostic_scroll.setVisible)
        detail.addWidget(self.diagnostic_scroll)
        body.addLayout(detail, 1)
        layout.addLayout(body)
        self.message = QLabel("Gửi yêu cầu để Codex làm việc và ChatGPT review tự động.")
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
            group = "Hoàn tất" if task.state is S.COMPLETE else "Lỗi" if task.state is S.PAUSED_ERROR else "Đã dừng" if task.state is S.STOPPED else "Đang làm"
            status = state_text(views[task.task_id].status)
            item = QListWidgetItem(f"{group}{' · ' + status if status != group else ''}\n{task.title}")
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
        with ProjectRegistry(self.root) as registry:
            project = registry.get(self.project_id)
            try:
                registry.state.assert_project_available(self.project_id)
                available = True
            except Exception:
                available = False
        self.worker_card.refresh(busy=self.busy)
        self.project_identity.setText(f"PROJECT_ID={self.project_id}\nPROJECT_WORKER_THREAD_ID={project.codex_worker_thread_id or 'NONE'}\n"
            f"PROJECT_WORKER_REPOSITORY={project.codex_worker_repo_path or 'NONE'}\n"
            f"PROJECT_WORKER_VERIFIED_AT={project.codex_worker_verified_at or 'NONE'}")
        self.connections.setText(f"Worker Codex: {'Đã kết nối' if self.worker_card.connected == self.project_id else 'Cần kiểm tra'}   "
            f"ChatGPT Reviewer: {state_text(project.chatgpt_status)}   GitHub: {state_text(project.github_status)}")
        self.send_button.setEnabled(bool(not self.busy and available and self.prompt.toPlainText().strip()
                                        and project.codex_worker_thread_id))
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
            status = "Hoàn tất — sẵn sàng để bạn duyệt" if task.ready_for_owner_review else (
                "Codex đang sửa theo review" if task.state is S.WORKER_RUNNING and task.pending.get("worker_kind") == "WORKER_FIX"
                else "Có lỗi cần xử lý" if task.state is S.PAUSED_ERROR else state_text(task.state))
            self.owner_summary.setText(f"{task.title}\n● {status}")
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
            self.owner_summary.setText("Nhập yêu cầu để bắt đầu")
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
                "Chỉ dẫn bổ sung cho công việc đang chọn, dùng đúng Worker hiện tại.")
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
            self.message.setText("Đã chọn Worker của công việc này. Danh tính đầy đủ nằm trong Chi tiết kỹ thuật.")
        except Exception as exc:
            self.show_error(exc)
        finally:
            controller.close()
        self.show_task()

    def pause_auto(self):
        identity = self.selected_worker()
        controller = self.factory()
        try:
            view = controller.request_worker_pause(identity)
            self.message.setText(state_text(view.status))
        except Exception as exc:
            self.show_error(exc)
        finally:
            controller.close()
        self.refresh()

    def manual_instruction(self):
        identity = self.selected_worker()  # Capture selection before the dialog/job.
        text, accepted = owner_input(self, "Gửi chỉ dẫn thủ công",
            "Chỉ dẫn cho công việc đang chọn. Worker được giữ nguyên; tự động vẫn tạm dừng.")
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

    def start_job(self, work, task_id=None, *, callback=None):
        if self.busy:
            return False
        self.busy, self.running_task_id = True, task_id
        self._callback = callback
        self._job = Job(work)
        self._job.signals.finished.connect(self.finished)
        self.pool.start(self._job)
        self.show_task()
        return True

    def finished(self, result, error):
        callback, self._callback = self._callback, None
        self.busy, self._job, self.running_task_id = False, None, None
        if error:
            self.show_error(error)
        else:
            self.message.setText(f"Trạng thái công việc: {state_text(getattr(result, 'state', 'Đã cập nhật'))}.")
        self.refresh()
        if callback:
            callback(result, error)

    def show_error(self, error):
        self.diagnostic_error.setText(f"{getattr(error, 'code', 'TASK_SETUP_FAILED')}: {error}")
        self.message.setText("Có lỗi cần xử lý. Xem Chi tiết kỹ thuật để biết nguyên nhân.")

    @Slot(str)
    def created(self, task_id):
        self.running_task_id = task_id
        self.refresh()
        for index in range(self.tasks.count()):
            if self.tasks.item(index).data(Qt.ItemDataRole.UserRole) == task_id:
                self.tasks.setCurrentRow(index)
                break

    def submit_prompt(self):
        if not self.send_button.isEnabled():
            return
        self.submit_values(dict(prompt=self.prompt.toPlainText()))

    def submit_values(self, values):
        def work():
            controller = self.factory()
            try:
                return asyncio.run(controller.submit_request(**values, on_created=self.task_created.emit))
            finally:
                controller.close()
        self.start_job(work)

    def new_task(self):
        dialog = NewTaskDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            values = dialog.values()
        except (ValueError, TypeError) as exc:
            self.show_error(exc)
            return
        self.submit_values(values)

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
