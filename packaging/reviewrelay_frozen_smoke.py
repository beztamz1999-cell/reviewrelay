"""Explicit packaged acceptance seam. Never runs a model turn or sends a review.

UI fixtures require a marked disposable database copy. Live transport probes
perform only Git/GitHub reads, app-server initialize and restored-tab readiness.
Normal application launch never imports or invokes this module.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import replace

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QLabel

from reviewrelay.controller import TaskController
from reviewrelay.controller_store import ControllerState as S, ControllerStore
from reviewrelay.owner_recovery import RecoveryAction as R
from reviewrelay.project_git import ProjectGit
from reviewrelay.project_setup import GitHubRepositoryCLI, check_codex
from reviewrelay.projects import ProjectRegistry
from reviewrelay.reviewer.base import ChatGPTWebSettings
from reviewrelay.reviewer.chatgpt_web import ChatGPTWebAdapter
from reviewrelay.reviewer.chrome_cdp import chrome_listener_ready
from reviewrelay.reviewer.errors import LoginRequired
from reviewrelay.storage import PortableDataRoot
from reviewrelay.task_ui import TaskWindow
from reviewrelay.ui import ProjectHub
from reviewrelay.ui import CreateProjectDialog, GitHubSetupDialog, RuntimeDialog, ReviewerDialog
from reviewrelay.task_ui import NewTaskDialog
from reviewrelay.ui_text import recovery_text
from reviewrelay.worker.base import CodexWorkerSettings
from reviewrelay.worker.codex_app_server import build_app_server_command
from reviewrelay.worker.transport import AppServerTransport


COPY_MARKER = ".reviewrelay-packaged-smoke-copy"


def disposable_root(path):
    path = Path(path).resolve()
    if not (path / COPY_MARKER).is_file():
        raise ValueError("Packaged acceptance requires an explicitly marked disposable data copy")
    forbidden = [Path(sys.executable).resolve().parent]
    if hasattr(sys, "_MEIPASS"):
        forbidden.append(Path(sys._MEIPASS).resolve())
    if any(path == p or path.is_relative_to(p) for p in forbidden):
        raise ValueError("Portable smoke data must be outside the application bundle")
    return PortableDataRoot(path)


def durable_snapshot(root):
    """Hash persisted state without serializing reviewer bodies or authentication."""
    uri = (root.path / "db" / "relay.db").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        hashes = {}
        for table in ("projects", "tasks", "controller_tasks", "github_publications",
                "controller_events", "github_events", "controller_reviews", "controller_effects"):
            rows = db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            hashes[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return hashes


async def challenge_visible(page):
    """Read only challenge UI markers; never click or solve a challenge."""
    title = (await page.title()).strip().lower()
    if title.startswith(("just a moment", "attention required")):
        return True
    markers = page.locator('#challenge-running, #challenge-stage, form#challenge-form, '
        'iframe[src*="challenges.cloudflare.com"]')
    for index in range(await markers.count()):
        if await markers.nth(index).is_visible():
            return True
    return False


async def chatgpt_cdp_probe(config):
    """Opt-in disposable acceptance only: no inference, messages or JS overrides."""
    root = disposable_root(config["data_root"])
    before = durable_snapshot(root)
    with ProjectRegistry(root) as registry:
        project = registry.get(config["project_id"])
    settings = ChatGPTWebSettings.from_mapping({**project.reviewer_settings,
        "conversation_url": project.chatgpt_conversation_url})
    if settings.browser_backend.value != "google-chrome-cdp":
        raise ValueError("Read-only live probe requires the installed Chrome CDP backend")
    seconds = config.get("acceptance_seconds", 30)
    if type(seconds) is not int or not 10 <= seconds <= 60:
        raise ValueError("Read-only acceptance window must be 10..60 seconds")
    result = dict(status="FAIL", navigator_webdriver="NOT_RUN", chrome_version=None,
        nonzero_cdp_port=False, localhost_only=False, restored_tab=False, composer_ready=False,
        cloudflare_loop="NOT_RUN", challenge_observations=0, vpn_proxy="NOT_OBSERVED",
        codex_inference_turns=0, chatgpt_messages_sent=0)
    reviewer = ChatGPTWebAdapter(PortableDataRoot(config["browser_data_root"]), settings)
    navigation = None
    challenge_times = []
    loop = asyncio.get_running_loop()
    try:
        await reviewer.start()
        result["chrome_version"] = reviewer.browser_version
        result["nonzero_cdp_port"] = type(reviewer._cdp_port) is int and 1 <= reviewer._cdp_port <= 65535
        result["localhost_only"] = chrome_listener_ready(reviewer._chrome_process, reviewer._cdp_port)
        # The only diagnostic evaluate: record the boolean, never alter browser properties.
        value = await reviewer.page.evaluate("navigator.webdriver")
        if type(value) is not bool:
            raise ValueError("navigator.webdriver did not return a boolean")
        result["navigator_webdriver"] = value
        target = settings.resolve_conversation_url()
        result["restored_tab"] = sum(not p.is_closed() and reviewer._same_conversation(p.url, target)
            for p in reviewer._context.pages) == 1
        navigation = asyncio.create_task(reviewer.open_task_conversation(target))
        # Observe while the existing adapter waits for readiness; no goto/reload/retry.
        while not navigation.done():
            if await challenge_visible(reviewer.page):
                challenge_times.append(loop.time())
            await asyncio.wait({navigation}, timeout=1)
        await navigation
        if value is not False or not result["nonzero_cdp_port"] or not result["localhost_only"]:
            raise ValueError("Browser automation exposure/loopback assertion failed")
        end = loop.time() + seconds
        stable = True
        while loop.time() < end:
            if await challenge_visible(reviewer.page):
                challenge_times.append(loop.time())
                stable = False
            if not reviewer._same_conversation(reviewer.page.url, target) or await reviewer._find_composer() is None:
                stable = False
            await asyncio.sleep(min(1, max(0, end - loop.time())))
        result["composer_ready"] = stable
        if stable and result["restored_tab"]:
            result["status"] = "PASS"
    except Exception as error:
        result["error_code"] = getattr(error, "code", type(error).__name__)
    finally:
        if navigation is not None and not navigation.done():
            navigation.cancel()
            await asyncio.gather(navigation, return_exceptions=True)
        if result["navigator_webdriver"] != "NOT_RUN":
            result["cloudflare_loop"] = "YES" if len(challenge_times) >= 2 and challenge_times[-1] - challenge_times[0] >= 10 else "NO"
        result["challenge_observations"] = len(challenge_times)
        await reviewer.close()
    if before != durable_snapshot(root):
        result.update(status="FAIL", error_code="DIAGNOSTIC_DURABLE_STATE_CHANGED")
    return result


async def transport_probe(config):
    root = disposable_root(config["data_root"])
    with ProjectRegistry(root) as registry:
        project = registry.get(config["project_id"])
    result = {"codex_inference_turns": 0, "chatgpt_messages_sent": 0}
    local = await ProjectGit().discover(project.local_repo_path)
    if not local.is_git or not local.head:
        raise ValueError("Registered repository is unavailable")
    result["git"] = {"head": local.head, "branch": local.branch, "clean": local.clean,
        "executable": shutil.which("git")}
    github = await GitHubRepositoryCLI(root.path).inspect(f"{project.github_owner}/{project.github_repo_name}")
    if github is None or github.url != project.github_repo_url:
        raise ValueError("GitHub identity differs from registration")
    result["github"] = {"url": github.url, "visibility": github.visibility, "executable": shutil.which("gh")}
    with ControllerStore(root) as store:
        row = store.db.execute("SELECT * FROM github_publications WHERE project_id=? AND task_id=?",
            (project.project_id, config["task_id"])).fetchone()
    remote = await ProjectGit().remote_sha(project.local_repo_path, project.github_git_url, row["github_task_branch"])
    if remote != row["github_last_remote_sha"]:
        raise ValueError("Published Task branch SHA differs from durable metadata")
    result["github"].update(task_branch=row["github_task_branch"], remote_sha=remote)
    settings = CodexWorkerSettings.from_mapping(project.worker_settings)
    if not Path(settings.executable).is_absolute():
        raise ValueError("The accepted Codex runtime must be explicitly configured, not a PATH fallback")
    checked = await check_codex(project.local_repo_path, settings)
    if Path(checked.executable).resolve() != Path(settings.executable).resolve():
        raise ValueError("Configured Codex executable changed")
    command = build_app_server_command(checked)
    methods = []
    async def event(message):
        methods.append(message.get("method"))
    failures = []
    transport = AppServerTransport(command, project.local_repo_path, checked.timeouts, event, failures.append)
    try:
        await transport.start()
        initialized = await transport.request("initialize", {"clientInfo": {
            "name": "reviewrelay", "title": "ReviewRelay packaged read-only acceptance", "version": "0.1.0"}},
            checked.timeouts.initialize_seconds)
        if not initialized.get("userAgent"):
            raise ValueError("App-server initialize did not succeed")
        await transport.send({"method": "initialized", "params": {}})
        result["codex"] = {"command": command, "initialize": True, "requests": ["initialize", "initialized"]}
    finally:
        await transport.close()
    if failures or transport.process and transport.process.returncode is None:
        raise ValueError("App-server did not shut down cleanly")
    result["codex"]["shutdown"] = True
    if config.get("browser_data_root"):
        # Reuse the existing dedicated profile in place; never copy its contents.
        browser_root = PortableDataRoot(config["browser_data_root"])
        reviewer = ChatGPTWebAdapter(browser_root, ChatGPTWebSettings.from_mapping(project.to_config().chatgpt))
        try:
            await reviewer.start()
            url = await reviewer.open_task_conversation(project.chatgpt_conversation_url)
            if url != project.chatgpt_conversation_url:
                raise ValueError("Restored reviewer URL differs")
            result["chatgpt_cdp"] = {"status": "PASS", "url": url, "restored_tab": True,
                "composer_ready": bool(await reviewer._find_composer())}
            if not result["chatgpt_cdp"]["composer_ready"]:
                raise ValueError("Composer not ready")
        except LoginRequired:
            result["chatgpt_cdp"] = {"status": "AUTH_REQUIRED"}
        except Exception as error:
            result["chatgpt_cdp"] = {"status": "FAIL", "error": f"{type(error).__name__}: {error}"}
            if reviewer._page is not None:
                result["chatgpt_cdp"]["title"] = await reviewer.page.title()
                if config.get("diagnostics_dir"):
                    await reviewer.page.screenshot(path=str(Path(config["diagnostics_dir"]) / "chatgpt-readiness.png"))
        finally:
            await reviewer.close()
    else:
        result["chatgpt_cdp"] = {"status": "NOT_RUN"}
    return result


class UiProbe:
    def __init__(self, config, mode, chooser, output):
        self.config, self.mode, self.chooser, self.output = config, mode, chooser, output
        self.root = disposable_root(config["data_root"] if mode in {"ui", "owner"} else config["fixture_root"])
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.result = {"frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
            "cwd": os.getcwd(), "bundle": getattr(sys, "_MEIPASS", None), "mode": mode,
            "codex_inference_turns": 0, "chatgpt_messages_sent": 0}
        self.timer = QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.started = time.monotonic()
        self.step, self.window, self.calls, self.decisions = 0, None, [], []
        self.before = durable_snapshot(self.root)

    def finish(self, error=None):
        self.timer.stop()
        self.result.update(status="FAIL" if error else "PASS")
        if error:
            self.result["error"] = f"{type(error).__name__}: {error}"
        for widget in self.app.topLevelWidgets():
            widget.close()
        self.output.write_text(json.dumps(self.result, indent=2), encoding="utf-8")
        self.app.exit(1 if error else 0)

    def tick(self):
        try:
            if time.monotonic() - self.started > 45:
                raise TimeoutError("Packaged Qt smoke did not finish within its bound")
            getattr(self, "tick_" + self.mode)()
        except Exception as error:
            self.finish(error)

    def tick_ui(self):
        if self.window is None:
            hubs = [w for w in self.app.topLevelWidgets() if isinstance(w, ProjectHub) and w.isVisible()]
            if not hubs:
                return
            self.window = hubs[0]
            if self.window.root.path.resolve() != self.root.path.resolve():
                raise ValueError("UI selected a different data root")
            ids = [self.window.projects.item(i).data(256) for i in range(self.window.projects.count())]
            if self.config["project_id"] not in ids:
                raise ValueError("Project registration not rendered")
            self.window.projects.setCurrentRow(ids.index(self.config["project_id"]))
            self.window.open_tasks()
            if not self.window.task_windows:
                raise ValueError("Production Tasks window did not open")
            task_window = self.window.task_windows[-1]
            if (self.window.windowTitle() != "ReviewRelay — Dự án"
                    or self.window.create_button.text() != "+ Thêm dự án"
                    or task_window.windowTitle() != "ReviewRelay — Công việc"
                    or task_window.manual_button.text() != "Gửi chỉ dẫn thủ công"):
                raise ValueError("Vietnamese Hub/Task UI differs")
            project = self.window.selected()
            dialogs = ((CreateProjectDialog(self.window), "Thêm / Nhập dự án"),
                (GitHubSetupDialog(project, parent=self.window), "Thiết lập GitHub"),
                (RuntimeDialog(project, self.window), "Kết nối Codex Runtime"),
                (ReviewerDialog(project, self.window), "Kết nối ChatGPT Reviewer"),
                (NewTaskDialog(task_window), "Gửi yêu cầu"))
            for index, (dialog, title) in enumerate(dialogs):
                dialog.show()
                self.app.processEvents()
                if dialog.windowTitle() != title:
                    raise ValueError("Vietnamese setup dialog differs")
                dialog.grab().save(str(self.output.with_suffix(f".dialog-{index}.png")))
                dialog.reject()
                dialog.deleteLater()
            self.result["vietnamese_dialogs"] = [title for _, title in dialogs]
            if self.config["task_id"] not in task_window.summary.text():
                raise ValueError("Task state not rendered")
            if "WORKER_THREAD_ID=" not in task_window.worker_identity.text() or not task_window.timeline.toPlainText():
                raise ValueError("Worker identity/timeline not rendered")
            self.window.grab().save(str(self.output.with_suffix(".hub.png")))
            task_window.grab().save(str(self.output.with_suffix(".tasks.png")))
            self.result.update(data_root=str(self.root.path), projects=ids,
                task_summary=task_window.summary.text(), worker_identity=task_window.worker_identity.text(),
                timeline_present=True, recovery_hidden=not task_window.recovery_button.isVisible(),
                snapshot=self.before, snapshot_after=durable_snapshot(self.root))
            if self.result["snapshot_after"] != self.before:
                raise ValueError("UI read altered durable state")
            self.finish()

    def tick_owner(self):
        """Stored-worker Owner UI only; no app-server/model/reviewer effects."""
        if self.window is None:
            self.window = ProjectHub(self.root, auto_discover=False)
            self.window.show()
            ids = [self.window.projects.item(i).data(256) for i in range(self.window.projects.count())]
            self.window.projects.setCurrentRow(ids.index(self.config["project_id"]))
            self.window.open_tasks()
            if not self.window.task_windows:
                raise ValueError("Production TaskWindow did not open")
            return
        task_window = self.window.task_windows[-1]
        project = self.window.selected()
        if not project.codex_worker_thread_id or not project.codex_worker_title:
            raise ValueError("Fixture requires a stored canonical Project worker")
        visible = "\n".join(w.text() for w in task_window.findChildren(QLabel) if w.isVisible())
        if (project.codex_worker_thread_id in visible or "SHA:" in visible or "PROJECT_ID=" in visible
                or task_window.diagnostics.isVisible() or task_window.new_button.isVisible()
                or task_window.send_button.text() != "Gửi yêu cầu"
                or project.codex_worker_title not in task_window.worker_card.label.text()):
            raise ValueError("Owner view exposes technical fields or omits the stored Worker")
        dialog = NewTaskDialog(task_window)
        dialog.show()
        if hasattr(dialog, "task_id") or hasattr(dialog, "title") or dialog.fix_limit.isVisible():
            raise ValueError("Normal prompt flow still requires technical fields")
        dialog.spec.setPlainText("Read-only presentation check; never submitted")
        if not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
            raise ValueError("Prompt-only dialog is not usable")
        dialog.reject()
        task_window.grab().save(str(self.output.with_suffix(".owner.png")))
        task_window.diagnostics_button.click()
        if not task_window.diagnostics.isVisible() or project.codex_worker_thread_id not in task_window.project_identity.text():
            raise ValueError("Diagnostics identity is unavailable")
        task_window.grab().save(str(self.output.with_suffix(".diagnostics.png")))
        if durable_snapshot(self.root) != self.before:
            raise ValueError("Read-only Owner UI changed persisted state")
        self.result.update(project_opened=True, stored_worker_displayed=True, prompt_only=True,
            diagnostics_hidden_by_default=True, diagnostics_available=True, durable_unchanged=True)
        self.finish()

    def fixture(self, action=None):
        with ControllerStore(self.root) as store:
            original = store.get(self.config["project_id"], self.config["task_id"])
            record = store.state.get(original.project_id, original.task_id)
            store.control(original.project_id, original.task_id, "CLEAR")
            with store.db:
                store.db.execute("DELETE FROM controller_effects WHERE project_id=? AND task_id=?",
                    (original.project_id, original.task_id))
            turn = "packaged-fixture-turn"
            store.state.save(replace(record, worker_last_turn_id=turn,
                worker_last_turn_status="IN_PROGRESS" if action is None else "COMPLETED"))
            task = replace(original, state=S.WORKER_RUNNING if action is None else S.PAUSED_ERROR,
                worker_thread_id=record.worker_thread_id, worker_turn_id=turn, manual_pending={}, review_invalidated=False,
                pending={"worker_kind": "WORKER_INITIAL", "number": 42}, candidate_sha=None, published=None,
                review_cycle=0, ready_for_owner_review=False, resume_state=S.VERIFYING_CANDIDATE.value,
                error_code="WORKER_NO_NEW_COMMIT")
            controller = TaskController(self.root, task.project_id)
            try:
                if action in {None, R.CONTINUE_WORKER, R.CHECK_WORKER}:
                    kind = "WORKER_CONTINUATION" if action is R.CHECK_WORKER else "WORKER_INITIAL"
                    task = replace(task, pending={"worker_kind": kind, "number": 42},
                        resume_state=S.WORKER_RUNNING.value if action is R.CHECK_WORKER else task.resume_state)
                    key = controller._key(task, kind, 42)
                    status = "IN_FLIGHT" if action is None else "CONFIRMED" if action is R.CHECK_WORKER else "COMPLETED"
                    store.put_effect(task, key, kind, status, {"thread_id": record.worker_thread_id, "turn_id": turn})
                else:
                    prompt = "Harmless packaged UI fixture. Never send this prompt."
                    key = controller._key(task, "REVIEW_SEND", 42)
                    task = replace(task, candidate_sha=self.config["candidate_sha"], review_cycle=1,
                        published=self.config["published"], resume_state=S.WAITING_REVIEW.value,
                        error_code="MESSAGE_SEND_FAILED" if action is R.RETRY_REVIEW else "MESSAGE_SEND_AMBIGUOUS",
                        pending={"key": key, "message_kind": "REVIEW_SEND", "prompt": prompt,
                            "candidate_sha": self.config["candidate_sha"], "cycle": 1})
                    payload = {"candidate_sha": task.candidate_sha, "cycle": 1,
                        "conversation_url": self.config["conversation_url"],
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
                    if action is R.RECOVER_REVIEW:
                        with store.db:
                            store._event(task, "REVIEW_PRE_CLICK_FAILURE_RECONCILED", {"effect_key": key,
                                "proof_source": "typed-pre-click-error-and-exact-visible-draft", "draft_cleared": True,
                                "prompt_sha256": payload["prompt_sha256"], "conversation_url": payload["conversation_url"]})
                        store.put_effect(task, key, "REVIEW_SEND", "PLANNED", payload)
                        store.dispatch(task, key, "REVIEW_SEND", "review_messages")
                    store.put_effect(task, key, "REVIEW_SEND", "NOT_SENT" if action is R.RETRY_REVIEW else "AMBIGUOUS", payload)
                store.save(task, "PACKAGED_DISPOSABLE_UI_FIXTURE")
            finally:
                controller.close()
        return dict(project_id=task.project_id, task_id=task.task_id, thread_id=record.worker_thread_id,
            turn_id=turn, item_id="packaged-fixture-command", command="git status --short", cwd=record.worker_repo_path,
            environment_id=None)

    def tick_approval(self):
        choices = ("accept_once", "decline", "cancel", "close")
        expected = ("accept", "decline", "cancel", "cancel")
        if self.window is None:
            self.request = self.fixture()
            self.window = TaskWindow(self.root, self.config["project_id"])
            self.window.show()
        if self.step >= len(choices):
            if self.window.busy:
                return
            if self.decisions != list(expected):
                raise ValueError("Approval choices did not map to bounded decisions")
            self.result.update(decisions=self.decisions, gui_thread=True, background_request=True, no_session_approval=True)
            self.finish()
        elif not self.window.busy and len(self.decisions) == self.step:
            async def approve():
                if threading.get_ident() == self.gui_ident:
                    raise ValueError("Approval request was not on the background thread")
                self.decisions.append(await self.window.approvals.approve(self.request))
            self.gui_ident = threading.get_ident()
            self.window.start_job(lambda: asyncio.run(approve()), self.request["task_id"])
        elif self.window.approvals.dialog is not None:
            dialog = self.window.approvals.dialog
            if dialog.thread() != self.app.thread() or QThread.currentThread() != self.app.thread():
                raise ValueError("Approval dialog is not on the GUI thread")
            for label, key in (("Dự án", "project_id"), ("Công việc", "task_id"), ("Codex thread", "thread_id"),
                    ("Lượt", "turn_id"), ("cwd", "cwd")):
                if f"{label}: {self.request[key]}" not in dialog.metadata.text():
                    raise ValueError("Approval metadata differs")
            if dialog.command.toPlainText() != self.request["command"] or dialog.findChildren(QCheckBox):
                raise ValueError("Command/options differ")
            if (dialog.windowTitle() != "Cho phép chạy lệnh này?" or
                    [dialog.accept_once.text(), dialog.decline.text(), dialog.cancel.text()]
                    != ["Cho phép một lần", "Từ chối", "Hủy"]):
                raise ValueError("Vietnamese approval UI differs")
            if self.step == 0:
                dialog.grab().save(str(self.output.with_suffix(".approval.png")))
            choice = choices[self.step]
            dialog.close() if choice == "close" else getattr(dialog, choice).click()
            self.step += 1

    def tick_recovery(self):
        actions = (R.CONTINUE_WORKER, R.CHECK_WORKER, R.RETRY_REVIEW, R.RECOVER_REVIEW)
        methods = ("continue_incomplete_worker", "reconcile_interrupted_continuation", "reconcile_unsent_review", "reconcile_visible_review")
        if self.window is not None and self.window.busy:
            return
        if self.step >= len(actions):
            expected = [value for i, method in enumerate(methods) for value in
                ([method] if i == 1 else [method, "run"])]
            if self.calls != expected:
                raise ValueError(f"Recovery wiring differs: {self.calls}")
            self.result.update(actions=[a.value for a in actions], calls=self.calls,
                display_labels=[recovery_text(a) for a in actions])
            self.finish()
            return
        action = actions[self.step]
        request = self.fixture(action)
        calls = self.calls
        class ProbeController(TaskController):
            async def continue_incomplete_worker(self, identity, instruction):
                calls.append("continue_incomplete_worker")
            async def reconcile_interrupted_continuation(self, identity):
                calls.append("reconcile_interrupted_continuation")
                return self.store.get(identity.project_id, identity.task_id)
            async def reconcile_unsent_review(self, identity):
                calls.append("reconcile_unsent_review")
            async def reconcile_visible_review(self, identity):
                calls.append("reconcile_visible_review")
            async def run(self, task_id, **kwargs):
                calls.append("run")
                return self.store.get(self.project_id, task_id)
        if self.window is not None:
            self.window.close()
        self.window = TaskWindow(self.root, request["project_id"], controller_factory=lambda: ProbeController(self.root, request["project_id"]))
        self.window.show()
        if (self.window.recovery is not action or not self.window.recovery_button.isVisible()
                or self.window.recovery_button.text() != recovery_text(action)):
            raise ValueError("Contextual recovery action was not rendered")
        self.window.grab().save(str(self.output.with_suffix(f".recovery-{self.step}.png")))
        import reviewrelay.task_ui as task_ui
        previous = task_ui.owner_input
        try:
            task_ui.owner_input = lambda *a, **kw: ("Harmless fixture instruction; no worker runs", True)
            self.window.recovery_button.click()
        finally:
            task_ui.owner_input = previous
        self.step += 1

    def run(self):
        self.timer.start()
        if self.mode != "ui":
            return self.app.exec()
        import reviewrelay.ui as ui_module
        from reviewrelay.ui import main as ui_main
        original_hub = ui_module.ProjectHub
        original_args = sys.argv
        original_chooser = QFileDialog.getExistingDirectory
        def choose(parent, caption):
            dialog = QFileDialog(parent, caption, str(self.root.path))
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            def select():
                dialog.setDirectory(str(self.root.path))
                dialog.accept()
            QTimer.singleShot(500, select)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            self.result["chooser_used"] = accepted
            return dialog.selectedFiles()[0] if accepted else ""
        try:
            # This acceptance mode reads a marked fixture; it must not refresh
            # operational worker metadata or leave background discovery alive.
            ui_module.ProjectHub = lambda root: original_hub(root, auto_discover=False)
            sys.argv = [sys.executable] + ([] if self.chooser else ["--data-root", str(self.root.path)])
            if self.chooser:
                QFileDialog.getExistingDirectory = choose
            return ui_main()
        finally:
            ui_module.ProjectHub = original_hub
            sys.argv = original_args
            QFileDialog.getExistingDirectory = original_chooser


def main():
    parser = argparse.ArgumentParser(description="Explicit read-only packaged acceptance")
    parser.add_argument("--packaged-smoke", action="store_true", required=True)
    parser.add_argument("--mode", choices=("ui", "owner", "approval", "recovery", "transports", "chatgpt"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chooser", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    output = args.output.resolve()
    if not output.parent.is_dir():
        raise ValueError("Acceptance output directory must explicitly exist")
    try:
        if args.mode not in {"transports", "chatgpt"}:
            return UiProbe(config, args.mode, args.chooser, output).run()
        config["diagnostics_dir"] = str(output.parent)
        result = asyncio.run(chatgpt_cdp_probe(config) if args.mode == "chatgpt" else transport_probe(config))
        passed = result["status"] == "PASS" if args.mode == "chatgpt" else result["chatgpt_cdp"]["status"] != "FAIL"
        result.update(status="PASS" if passed else "FAIL", frozen=bool(getattr(sys, "frozen", False)), executable=sys.executable, cwd=os.getcwd())
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0 if passed else 1
    except Exception as error:
        output.write_text(json.dumps({"status": "FAIL", "error": f"{type(error).__name__}: {error}"}, indent=2), encoding="utf-8")
        return 1
