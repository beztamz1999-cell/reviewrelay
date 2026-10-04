"""Owner flow contracts on disposable roots; no live model/reviewer calls."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QTextBrowser

from reviewrelay.controller_store import ControllerState as S, ControllerStore, ControllerTask
from reviewrelay.models import TaskRecord
from reviewrelay.owner_ui import OwnerMainWindow, ProjectChatWindow, ProjectSetupWizard
from reviewrelay.owner_view import chat_history, recency, task_message
from reviewrelay.projects import ConnectionStatus as C, ProjectRegistry
from reviewrelay.storage import PortableDataRoot, TaskStorage
from test_controller import h
from test_project_ui import app
from test_projects import setup
from test_project_worker import service, thread
from test_task_ui import spin


THREAD = "01a0fe44-0000-7773-b02c-48bfb6969100"


@pytest.fixture
def data(tmp_path):
    root = PortableDataRoot(tmp_path / "data").create()
    with ProjectRegistry(root) as registry:
        project = registry.create("Project minh họa", str(tmp_path / "repo"), "EXISTING")
        project = registry.save(replace(project, local_status=C.READY, github_status=C.READY,
            github_repo_url="https://github.com/owner/example", github_owner="owner", github_repo_name="example",
            github_last_verified_at="fixture", chatgpt_status=C.READY,
            chatgpt_conversation_url="https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc",
            codex_status=C.READY, worker_settings={"executable": "codex"},
            codex_worker_thread_id=THREAD, codex_worker_repo_path=project.local_repo_path,
            codex_worker_verified_at="fixture", codex_worker_title="Thiết kế provenance",
            setup={"local_inspection": {"classification": "CLEAN_READY", "head": "a" * 40,
                "branch": "main", "remotes": [{"name": "origin", "url": "https://github.com/owner/example"}]}}), event="PROJECT_WORKER_BOUND")
    return root, project


def add_job(root, project, task_id="job-one", *, state=S.COMPLETE, created="2026-10-05T01:00:00+00:00", prompt="Yêu cầu đầu tiên"):
    task = ControllerTask(project.project_id, task_id, prompt, prompt, json.dumps(project.to_config().to_mapping()), "a" * 40, "b" * 64,
        state=state, candidate_sha="c" * 40, base_sha="a" * 40, worker_thread_id=THREAD,
        ready_for_owner_review=state is S.COMPLETE, created_at=created)
    with ControllerStore(root) as store:
        record = TaskRecord(project.project_id, task_id, worker_thread_id=THREAD,
            worker_repo_path=project.local_repo_path, worker_last_turn_status="COMPLETED")
        store.state.save(record)
        TaskStorage(root).create_task(project.project_id, task_id, record)
        store.save(task, "OWNER_UX_DISPOSABLE_FIXTURE")
    return task


def visible_text(window):
    texts = []
    for kind in (QLabel, QPushButton):
        for widget in window.findChildren(kind):
            if widget.isVisible():
                texts.append(widget.text())
    for widget in window.findChildren(QTextBrowser):
        if widget.isVisible():
            texts.append(widget.toPlainText())
    return "\n".join(texts)


def test_landing_has_only_project_cards_and_existing_first_folder_flow(app, data, monkeypatch):
    root, project = data
    main = OwnerMainWindow(root, auto_discover=False)
    main.show()
    app.processEvents()
    assert main.projects.item(0).text() == project.project_name + "\n● Sẵn sàng"
    text = visible_text(main)
    assert THREAD not in text and "SHA" not in text and "Khởi tạo" not in text
    calls = []
    monkeypatch.setattr("reviewrelay.owner_ui.QFileDialog.getExistingDirectory", lambda owner, label: calls.append(label) or "")
    main.create_button.click()
    assert main.existing_button.isVisible()
    main.existing_button.click()
    assert calls == ["Chọn thư mục project"]
    main.close()


def test_global_settings_keeps_data_path_and_tools_under_advanced(app, data):
    root, _ = data
    main = OwnerMainWindow(root, auto_discover=False)
    main.open_settings()
    dialog = main.windows[-1]
    app.processEvents()
    assert str(root.path) not in visible_text(dialog)
    advanced = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Chi tiết nâng cao")
    advanced.click()
    assert str(root.path) in visible_text(dialog)
    main.close()


def test_owner_light_surfaces_remain_readable_with_a_dark_system_palette(app, data):
    root, project = data
    original = app.palette()
    dark = QPalette(original)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        dark.setColor(role, QColor("white"))
    dark.setColor(QPalette.ColorRole.Base, QColor("#202020"))
    app.setPalette(dark)
    try:
        main = OwnerMainWindow(root, auto_discover=False)
        main.show()
        chat = ProjectChatWindow(root, project.project_id, auto_discover=False)
        chat.show()
        app.processEvents()
        assert main.projects.palette().color(QPalette.ColorRole.Text) != main.projects.palette().color(QPalette.ColorRole.Base)
        assert main.create_button.palette().color(QPalette.ColorRole.ButtonText) != main.create_button.palette().color(QPalette.ColorRole.Button)
        assert chat.prompt.palette().color(QPalette.ColorRole.PlaceholderText) != chat.prompt.palette().color(QPalette.ColorRole.Base)
        chat.close()
        main.close()
    finally:
        app.setPalette(original)


def test_existing_folder_auto_recognizes_git_and_proposes_remote_without_publish(app, setup, monkeypatch):
    from test_projects import git
    root, repo, _, _, recorder, _, make = setup
    git(repo, "remote", "add", "upstream", "https://github.com/owner/already-there.git")
    main = OwnerMainWindow(root, service_factory=make, auto_discover=False)
    monkeypatch.setattr("reviewrelay.owner_ui.QFileDialog.getExistingDirectory", lambda *_: str(repo))
    main.show()
    main.add_existing()
    spin(app, lambda: not main.busy, timeout=30)
    wizard = main.windows[-1]
    assert isinstance(wizard, ProjectSetupWizard) and wizard.step == "github"
    assert wizard.repo_url.text() == "https://github.com/owner/already-there"
    assert "Đã tìm thấy" in wizard.description.text()
    assert not any("push" in call or "init" in call for call in recorder.calls)
    assert "upstream" not in visible_text(wizard)
    wizard.select_step("project")
    assert "Đã nhận diện" in wizard.description.text()
    main.close()


@pytest.mark.parametrize("remote", [True, False])
def test_github_normal_form_is_only_repository_url(app, data, remote):
    root, project = data
    with ProjectRegistry(root) as registry:
        inspection = dict(project.setup["local_inspection"])
        if not remote:
            inspection["remotes"] = []
        registry.save(replace(project, github_status=C.NOT_CONFIGURED, github_repo_url=None,
            github_last_verified_at=None, setup={"local_inspection": inspection}))
    wizard = ProjectSetupWizard(root, project.project_id, auto_discover=False)
    wizard.show()
    app.processEvents()
    assert wizard.repo_url.isVisible() and not wizard.conversation_url.isVisible()
    assert wizard.repo_url.text() == ("https://github.com/owner/example" if remote else "")
    assert "remote" not in visible_text(wizard).lower()
    assert not hasattr(wizard, "remote_name")
    wizard.close()


def test_chatgpt_setup_only_shows_conversation_url_and_manual_login(app, data):
    root, project = data
    wizard = ProjectSetupWizard(root, project.project_id, auto_discover=False, settings=True)
    wizard.select_step("chatgpt")
    wizard.show()
    app.processEvents()
    assert wizard.conversation_url.isVisible() and wizard.auth_button.isVisible()
    assert not wizard.repo_url.isVisible() and not wizard.folder.isVisible()
    text = visible_text(wizard)
    assert all(word not in text for word in ("CDP", "profile", "port", "UUID"))
    wizard.close()


def test_inline_worker_chooser_binds_visual_selection_through_existing_verifier(app, h):
    worker, protocol = service(h, [thread(h.repo, "one"), {**thread(h.repo, "two"), "name": "M4 validation"}])
    wizard = ProjectSetupWizard(h.root, h.project.project_id, worker_service_factory=lambda: worker, auto_discover=False)
    wizard.select_step("worker")
    wizard.show()
    wizard.worker_card.scan()
    spin(app, lambda: not wizard.busy, timeout=30)
    choices = wizard.worker_card.choices
    assert choices.isVisible() and choices.count() == 2
    assert "one" not in choices.item(0).text() and "two" not in choices.item(1).text()
    choices.setCurrentRow(1)
    wizard.worker_card.select_button.click()
    spin(app, lambda: not wizard.busy, timeout=30)
    with ProjectRegistry(h.root) as registry:
        assert registry.get(h.project.project_id).codex_worker_thread_id == "two"
    assert protocol.calls[-1] == ("thread/read", {"threadId": "two", "includeTurns": True})
    assert protocol.started == 0 and wizard.step == "done"
    wizard.close()


def test_setup_completion_opens_chat_and_reuses_existing_window(app, data):
    root, project = data
    main = OwnerMainWindow(root, auto_discover=False)
    main.show()
    wizard = ProjectSetupWizard(root, project.project_id, main, auto_discover=False)
    main.windows.append(wizard)
    wizard.opened.connect(lambda identity: main.setup_done(wizard, identity))
    wizard.show()
    wizard.next_button.click()
    app.processEvents()
    chat = main.windows[-1]
    assert isinstance(chat, ProjectChatWindow) and chat.isVisible() and not wizard.isVisible()
    assert main.open_project_id(project.project_id) is chat
    main.close()


def test_chat_hides_ids_shas_create_start_and_preserves_advanced_details(app, data):
    root, project = data
    task = add_job(root, project)
    chat = ProjectChatWindow(root, project.project_id, auto_discover=False)
    chat.show()
    app.processEvents()
    text = visible_text(chat)
    assert "Yêu cầu đầu tiên" in text and "ChatGPT đã PASS" in text
    assert all(token not in text for token in (THREAD, task.task_id, "a" * 40, "c" * 40, "SHA", "WORKER_RUNNING", "Bắt đầu"))
    assert not chat.start_button.isVisible() and not chat.new_button.isVisible()
    chat.open_settings()
    settings = chat.settings_windows[-1]
    assert chat.diagnostics_button.isVisible()
    chat.diagnostics_button.click()
    app.processEvents()
    assert chat.diagnostic_dialog.isVisible()
    assert THREAD in chat.project_identity.text() and str(root.path) in chat.project_identity.text()
    assert "Candidate SHA:" in chat.summary.text()
    chat.diagnostic_dialog.close()
    settings.close()
    chat.close()


def test_read_only_diagnostics_never_start_background_worker_discovery(app, data):
    root, project = data
    class ForbiddenDiscovery:
        async def discover(self, *_args, **_kwargs):
            pytest.fail("Opening Diagnostics started worker discovery")
    chat = ProjectChatWindow(root, project.project_id, worker_service_factory=ForbiddenDiscovery)
    assert not chat.worker_card.auto_discover
    chat.show()
    chat.open_diagnostics()
    app.processEvents()
    assert not chat.busy
    chat.diagnostic_dialog.close()
    chat.close()


@pytest.mark.parametrize("state", [S.WORKER_RUNNING, S.WAITING_REVIEW, S.PAUSED_OWNER, S.PAUSED_ERROR])
def test_unfinished_durable_project_job_disables_new_send(app, data, state):
    root, project = data
    task = add_job(root, project, state=state)
    chat = ProjectChatWindow(root, project.project_id, auto_discover=False)
    chat.show()
    chat.prompt.setPlainText("Another request")
    assert not chat.send_button.isEnabled()
    assert "yêu cầu hiện tại" in chat.input_hint.text()
    chat.close()


def test_chat_submit_runs_existing_controller_without_create_start_or_copy_paste(app, h):
    chat = ProjectChatWindow(h.root, h.project.project_id, controller_factory=h.make, auto_discover=False)
    chat.show()
    chat.prompt.setPlainText("Create a harmless fixture result file.")
    assert chat.send_button.isEnabled()
    chat.send_button.click()
    assert not chat.send_button.isEnabled()
    spin(app, lambda: not chat.busy, timeout=120)
    with ControllerStore(h.root) as store:
        task = store.list(h.project.project_id)[0]
    assert task.state is S.COMPLETE and task.ready_for_owner_review
    assert "ChatGPT đã PASS" in chat.chatlog.toPlainText()
    assert not chat.prompt.toPlainText()
    assert h.initial == h.sends == 1 and h.thread_starts == 0
    assert task.task_id not in visible_text(chat)
    chat.close()


def test_chat_composer_enter_is_multiline_and_ctrl_enter_sends(app, data):
    root, project = data
    chat = ProjectChatWindow(root, project.project_id, auto_discover=False)
    calls = []
    chat.prompt.send_requested.connect(lambda: calls.append(1))
    chat.prompt.send_requested.disconnect(chat.submit_prompt)
    QTest.keyClick(chat.prompt, Qt.Key.Key_Return)
    assert chat.prompt.toPlainText() == "\n"
    QTest.keyClick(chat.prompt, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert calls == [1]
    chat.close()


@pytest.mark.parametrize("state,words", [(S.WORKER_RUNNING, "Codex đang làm"), (S.VERIFYING_CANDIDATE, "kiểm tra thay đổi"),
    (S.PUBLISHING, "đồng bộ"), (S.WAITING_REVIEW, "ChatGPT đang review"), (S.COLLECTING_EVIDENCE, "thu thập"),
    (S.PAUSED_OWNER, "quyết định"), (S.COMPLETE, "ChatGPT đã PASS")])
def test_friendly_controller_state_mapping(data, state, words):
    root, project = data
    task = add_job(root, project, state=state)
    assert words in task_message(task) and state.value not in task_message(task)


def test_history_is_chronological_and_fix_routing_is_presented_without_raw_journals(data):
    root, project = data
    first = add_job(root, project, "z-first", prompt="First request")
    second = add_job(root, project, "a-second", created="2026-10-05T02:00:00+00:00", prompt="Second request", state=S.WORKER_RUNNING)
    with ControllerStore(root) as store:
        second = replace(second, pending={"worker_kind": "WORKER_FIX"})
        store.save(second, "REVIEW_FIX_REQUIRED")
        messages = chat_history(store.list(project.project_id), lambda task_id: store.events(project.project_id, task_id))
    assert [text for who, text in messages if who == "Bạn"] == ["First request", "Second request"]
    assert "Đã gửi lại cho Codex" in messages[-1][1]
    assert all("candidate_sha" not in text and "WORKER_FIX" not in text for _, text in messages)
    assert "PASS" not in task_message(replace(first, ready_for_owner_review=False))


def test_recency_is_human_language():
    assert recency("2026-10-05T01:00:00+00:00", now=datetime(2026, 10, 5, 1, 8, tzinfo=timezone.utc)) == "Hoạt động 8 phút trước"


def test_packaged_owner_acceptance_seam_is_read_only_and_captures_all_views(app, tmp_path, monkeypatch):
    from pathlib import Path
    import sys
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "packaging"))
    from reviewrelay_owner_smoke import prepare_fixture, run_owner_smoke
    from reviewrelay.controller import TaskController
    from reviewrelay.worker.discovery import WorkerProtocolClient
    def forbidden(*args, **kwargs):
        pytest.fail("Read-only UI smoke attempted a controller/worker effect")
    monkeypatch.setattr(TaskController, "submit_request", forbidden)
    monkeypatch.setattr(WorkerProtocolClient, "request", forbidden)
    config = prepare_fixture(tmp_path)
    output = tmp_path / "owner.json"
    assert run_owner_smoke(config, output) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "PASS" and result["durable_unchanged"]
    assert result["codex_inference_turns"] == result["chatgpt_messages_sent"] == 0
    assert result["technical_fields_hidden"] and result["data_root_dialog"] == "ABSENT"
    assert set(result["screenshots"]) == {"project-list", "setup", "worker-chooser", "chat", "diagnostics"}
    assert all(Path(path).is_file() for path in result["screenshots"].values())
