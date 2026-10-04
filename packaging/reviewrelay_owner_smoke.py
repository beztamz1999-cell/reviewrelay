"""Opt-in read-only Owner UI acceptance using a synthetic disposable database."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QPushButton

from reviewrelay.controller_store import ControllerState as S, ControllerStore, ControllerTask
from reviewrelay.models import TaskRecord
from reviewrelay.owner_ui import OwnerMainWindow, ProjectChatWindow, ProjectSetupWizard
from reviewrelay.projects import ConnectionStatus as C, ProjectRegistry
from reviewrelay.storage import PortableDataRoot, SelfManagedDataRoot, application_root
from reviewrelay.worker.discovery import DiscoveryResult, WorkerCandidate


def prepare_fixture(workspace):
    """Explicit acceptance setup, permitted only in a new OS temporary folder."""
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("Owner UI fixtures must stay under OS temp")
    path = workspace / "owner-ui-fixture"
    if path.exists():
        raise ValueError("Refuse to overwrite an existing acceptance fixture")
    root = PortableDataRoot(path).create()
    thread = "f7cb1a68-0000-4000-9000-123456789abc"
    with ProjectRegistry(root) as registry:
        ready = registry.create("Project minh họa", str(workspace / "source-preview"), "EXISTING")
        ready = registry.save(replace(ready, local_status=C.READY, github_status=C.READY,
            github_repo_url="https://github.com/example/owner-ui-preview", github_owner="example", github_repo_name="owner-ui-preview",
            github_last_verified_at="disposable-fixture", chatgpt_status=C.READY,
            chatgpt_conversation_url="https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc",
            codex_status=C.READY, worker_settings={"executable": "fixture-never-executed.exe"},
            codex_worker_thread_id=thread, codex_worker_repo_path=ready.local_repo_path,
            codex_worker_title="Thiết kế provenance", codex_worker_verified_at="disposable-fixture",
            setup={"local_inspection": {"classification": "CLEAN_READY", "head": "a" * 40,
                "branch": "main", "remotes": [{"name": "origin", "url": "https://github.com/example/owner-ui-preview"}]}}), event="PROJECT_WORKER_BOUND")
        incomplete = registry.create("Project cần thiết lập", str(workspace / "setup-preview"), "EXISTING")
        incomplete = registry.save(replace(incomplete, local_status=C.READY,
            setup={"local_inspection": {"classification": "CLEAN_READY", "remotes": [{"name": "origin", "url": "https://github.com/example/setup-preview"}]}}))
        worker_project = registry.create("Project cần chọn Worker", str(workspace / "worker-preview"), "EXISTING")
        worker_project = registry.save(replace(worker_project, local_status=C.READY, github_status=C.READY,
            github_repo_url="https://github.com/example/worker-preview", github_owner="example", github_repo_name="worker-preview",
            github_last_verified_at="disposable-fixture", chatgpt_status=C.READY,
            chatgpt_conversation_url=ready.chatgpt_conversation_url, codex_status=C.READY,
            worker_settings={"executable": "fixture-never-executed.exe"}))
    with ControllerStore(root) as store:
        prompt = "Sửa phần hiển thị theo yêu cầu và giữ nguyên phạm vi đã duyệt."
        task = ControllerTask(ready.project_id, "preview-internal-job", "Yêu cầu minh họa", prompt,
            json.dumps(ready.to_config().to_mapping()), "a" * 40, "b" * 64,
            state=S.COMPLETE, candidate_sha="c" * 40, base_sha="a" * 40,
            worker_thread_id=thread, ready_for_owner_review=True)
        record = TaskRecord(ready.project_id, task.task_id, worker_thread_id=thread,
            worker_repo_path=ready.local_repo_path, worker_last_turn_status="COMPLETED")
        store.state.save(record)
        store.storage.create_task(ready.project_id, task.task_id, record)
        for kind in ("WORKER_INITIAL_IN_FLIGHT", "WORKER_TURN_COMPLETED", "GITHUB_PUSH_STARTED",
                     "REVIEW_SEND_COMPLETED", "REVIEW_FIX_REQUIRED", "WORKER_TURN_COMPLETED", "TASK_COMPLETED"):
            store.save(task, kind)
    (path / ".reviewrelay-packaged-smoke-copy").write_text("synthetic Owner UI fixture", encoding="utf-8")
    return dict(data_root=str(path), project_id=ready.project_id, setup_project_id=incomplete.project_id,
        worker_project_id=worker_project.project_id, task_id=task.task_id)


def run_owner_smoke(config, output):
    from reviewrelay_frozen_smoke import disposable_root, durable_snapshot
    import reviewrelay.ui as ui
    import reviewrelay.owner_ui as owner_ui
    root = disposable_root(config["data_root"])
    if not root.path.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("Owner UI acceptance must use OS temp")
    before = durable_snapshot(root)
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    result = dict(status="FAIL", frozen=bool(getattr(sys, "frozen", False)),
        executable=sys.executable, application_root=str(application_root()), data_root=str(root.path),
        codex_inference_turns=0, chatgpt_messages_sent=0, fixture="SYNTHETIC_DISPOSABLE")
    started = time.monotonic()
    windows = []
    timer = QTimer()
    timer.setInterval(80)
    saved = {}
    def capture(window, name):
        app.processEvents()
        path = output.parent / ("owner-ux-" + name + ".png")
        if not window.grab().save(str(path)):
            raise ValueError("Screenshot capture failed")
        saved[name] = str(path)
    def tick():
        try:
            if time.monotonic() - started > 30:
                raise TimeoutError("Owner UI did not open within the acceptance bound")
            mains = [w for w in app.topLevelWidgets() if isinstance(w, OwnerMainWindow) and w.isVisible()
                and w.root.path.resolve() == root.path.resolve()]
            if not mains:
                return
            timer.stop()
            main = mains[0]
            windows.append(main)
            if main.root.path != root.path or main.projects.count() != 3:
                raise ValueError("Production landing did not render the fixture Project list")
            if (main.projects.palette().color(QPalette.ColorRole.Text) == main.projects.palette().color(QPalette.ColorRole.Base)
                    or main.create_button.palette().color(QPalette.ColorRole.ButtonText) == main.create_button.palette().color(QPalette.ColorRole.Button)):
                raise ValueError("Owner text disappeared into the OS theme background")
            capture(main, "project-list")
            wizard = ProjectSetupWizard(root, config["setup_project_id"], auto_discover=False)
            windows.append(wizard)
            wizard.show()
            wizard.select_step("project")
            capture(wizard, "setup")
            wizard.close()
            chooser = ProjectSetupWizard(root, config["worker_project_id"], auto_discover=False)
            windows.append(chooser)
            chooser.show()
            with ProjectRegistry(root) as registry:
                project = registry.get(config["worker_project_id"])
            candidates = tuple(WorkerCandidate("preview-worker-" + str(i), project.local_repo_path, label,
                "appServer", "2026-10-05T01:00:00+00:00") for i, label in enumerate(("AUTONOMY provenance", "M4 validation")))
            chooser.worker_card.discovered(project.project_id, DiscoveryResult("CHOOSE", candidates), None)
            capture(chooser, "worker-chooser")
            if not chooser.worker_card.choices.isVisible() or chooser.worker_card.choices.count() != 2:
                raise ValueError("Visual worker chooser not available")
            chooser.close()
            chat = main.open_project_id(config["project_id"])
            if not isinstance(chat, ProjectChatWindow):
                raise ValueError("Ready Project did not open Chat Workspace")
            app.processEvents()
            capture(chat, "chat")
            with ProjectRegistry(root) as registry:
                project = registry.get(config["project_id"])
            visible = "\n".join(w.text() for kind in (QLabel, QPushButton) for w in chat.findChildren(kind) if w.isVisible()) + chat.chatlog.toPlainText()
            if any(token in visible for token in (project.codex_worker_thread_id, config["task_id"], "SHA:", "PROJECT_ID=", "Bắt đầu")):
                raise ValueError("Technical identity escaped into normal Owner UI")
            if (chat.start_button.isVisible() or chat.new_button.isVisible()
                    or "ChatGPT đã PASS" not in chat.chatlog.toPlainText()
                    or "Đã gửi lại cho Codex" not in chat.chatlog.toPlainText()):
                raise ValueError("Prompt-only automatic review presentation differs")
            chat.prompt.setPlainText("Presentation only; never submitted")
            if not chat.send_button.isEnabled():
                raise ValueError("Prompt-only composer is unavailable")
            chat.prompt.clear()
            if chat.prompt.palette().color(QPalette.ColorRole.PlaceholderText) == chat.prompt.palette().color(QPalette.ColorRole.Base):
                raise ValueError("Composer placeholder disappeared into the OS theme background")
            chat.open_settings()
            chat.diagnostics_button.click()
            capture(chat.diagnostic_dialog, "diagnostics")
            if project.codex_worker_thread_id not in chat.project_identity.text():
                raise ValueError("Advanced identity details are unavailable")
            if durable_snapshot(root) != before:
                raise ValueError("Read-only presentation altered durable state")
            result.update(status="PASS", project_list=True, setup_wizard=True, worker_chooser=True,
                chat_workspace=True, prompt_only=True, technical_fields_hidden=True,
                advanced_diagnostics=True, automatic_review_history=True, data_root_dialog="ABSENT",
                durable_unchanged=True, screenshots=saved)
            finish()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            finish()
    def finish():
        timer.stop()
        for window in windows:
            window.close()
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        app.exit(0 if result["status"] == "PASS" else 1)
    original_factory = SelfManagedDataRoot.__dict__["for_application"]
    original_main = owner_ui.OwnerMainWindow
    original_args = sys.argv
    original_chooser = QFileDialog.getExistingDirectory
    try:
        SelfManagedDataRoot.for_application = classmethod(lambda cls: root)
        owner_ui.OwnerMainWindow = lambda data: original_main(data, auto_discover=False)
        QFileDialog.getExistingDirectory = lambda *_: (_ for _ in ()).throw(ValueError("Unexpected startup data-root chooser"))
        sys.argv = [sys.executable]
        timer.timeout.connect(tick)
        timer.start()
        return ui.main()
    finally:
        timer.stop()
        SelfManagedDataRoot.for_application = original_factory
        owner_ui.OwnerMainWindow = original_main
        QFileDialog.getExistingDirectory = original_chooser
        sys.argv = original_args
