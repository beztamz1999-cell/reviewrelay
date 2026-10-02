from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from reviewrelay.protocol import ReviewerAction, validate_review_response
from reviewrelay.storage import PortableDataRoot
from reviewrelay.reviewer import (
    AttachmentLimitExceeded,
    AttachmentNotAllowed,
    AttachmentNotFound,
    AttachmentUploadFailed,
    AmbiguousResponse,
    ChatGPTTimeouts,
    BrowserBackend,
    ChatGPTWebAdapter,
    ChatGPTWebSettings,
    ComposerNotFound,
    ConversationNavigationFailed,
    ConversationNotReady,
    LoginRequired,
    MessageSendAmbiguous,
    MessageSendFailed,
    ResponseTimeout,
    ReviewKeyConflict,
    ReviewerConversationChanged,
    ReviewerConfigurationError,
    SendDisposition,
)


FIXTURE = Path(__file__).parent / "fixtures" / "chatgpt_ui.html"


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        mode = parse_qs(urlsplit(self.path).query).get("mode", [""])[0]
        if mode in ("nav-error", "login-error"):
            self.send_response(403 if mode == "login-error" else 503)
            if mode == "login-error":
                body = b'<html><body><button>Log in</button></body></html>'
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "login-error":
                self.wfile.write(body)
            return
        body = FIXTURE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture(scope="session")
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _settings(base_url: str, mode: str = "normal", *, stability: float = 0.03) -> ChatGPTWebSettings:
    return ChatGPTWebSettings(
        base_url=base_url,
        conversation_url=f"{base_url}c/test?mode={mode}",
        headless=True,
        timeouts=ChatGPTTimeouts(
            navigation_seconds=0.35,
            upload_seconds=0.35,
            response_seconds=0.8,
            stability_seconds=stability,
        ),
    )


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ({}, "https://chatgpt.com/"),
        ({"base_url": "https://review.example/"}, "https://review.example/"),
    ],
)
def test_settings_defaults_and_custom_base(mapping, expected):
    settings = ChatGPTWebSettings.from_mapping(mapping)
    assert settings.base_url == expected
    assert settings.headless is False


def test_settings_timeout_mapping_and_bounds():
    settings = ChatGPTWebSettings.from_mapping({"timeouts": {"navigation_seconds": 12, "response_seconds": 45}})
    assert settings.timeouts.navigation_seconds == 12
    assert settings.timeouts.response_seconds == 45
    with pytest.raises(Exception):
        ChatGPTTimeouts.from_mapping({"response_seconds": float("inf")})
    with pytest.raises(Exception):
        ChatGPTWebSettings.from_mapping({"browser_profile": "../outside"})


def test_conversation_origin_and_path_are_validated():
    with pytest.raises(Exception):
        ChatGPTWebSettings(base_url="https://chatgpt.com/", conversation_url="https://evil.example/c/1")
    with pytest.raises(Exception):
        ChatGPTWebSettings(base_url="https://chatgpt.com/", conversation_url="https://chatgpt.com/search")


def test_persistent_profile_is_created_inside_data_root(tmp_path):
    async def run():
        data = tmp_path / "portable"
        adapter = ChatGPTWebAdapter(data, ChatGPTWebSettings(headless=True))
        profile = await adapter.start()
        try:
            assert profile.is_dir()
            assert profile == (data / "browser-profile" / "default").resolve()
            profile.relative_to(data.resolve())
        finally:
            await adapter.close()

    _run(run())


def test_profile_name_cannot_escape_root(tmp_path):
    with pytest.raises(Exception):
        ChatGPTWebSettings(browser_profile="../../profile")
    with pytest.raises(ReviewerConfigurationError):
        ChatGPTWebAdapter(tmp_path / "data", project_id="only-project")


def test_navigation_login_and_conversation_readiness(fixture_server, tmp_path):
    async def run():
        login = ChatGPTWebAdapter(tmp_path / "login", _settings(fixture_server, "login"))
        try:
            with pytest.raises(LoginRequired) as caught:
                await login.open_task_conversation()
            assert caught.value.code == "LOGIN_REQUIRED"
            assert login.page is not None  # Owner can complete login in the still-open persistent window.
        finally:
            await login.close()

        for mode in ("missing-composer", "search-only", "disabled-composer"):
            adapter = ChatGPTWebAdapter(tmp_path / mode, _settings(fixture_server, mode))
            try:
                with pytest.raises(ConversationNotReady):
                    await adapter.open_task_conversation()
            finally:
                await adapter.close()

        failed = ChatGPTWebAdapter(tmp_path / "failed", _settings(fixture_server, "nav-error"))
        try:
            with pytest.raises(ConversationNavigationFailed):
                await failed.open_task_conversation()
        finally:
            await failed.close()

    _run(run())


def test_ask_chatgpt_composer_accessible_name_is_recognized(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "ask-chatgpt", _settings(fixture_server, "ask-chatgpt"))
        try:
            target = await adapter.open_task_conversation()
            assert target == f"{fixture_server}c/test?mode=ask-chatgpt"
            logical_name, _ = await adapter._require_composer()
            assert logical_name == "role=textbox[name=Ask ChatGPT]"
        finally:
            await adapter.close()

    _run(run())


def test_contenteditable_paragraphs_reconstruct_prompt_newlines(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "paragraph-composer", _settings(fixture_server, "ask-chatgpt"))
        try:
            await adapter.open_task_conversation()
            _, composer = await adapter._require_composer()
            lines = ["first line", "second line", "third line"]
            await composer.evaluate(
                """(el, paragraphs) => el.replaceChildren(...paragraphs.map(text => {
                    const paragraph = document.createElement("p");
                    paragraph.textContent = text;
                    return paragraph;
                }))""",
                lines,
            )
            assert await adapter._read_composer_text(composer) == "\n".join(lines)
        finally:
            await adapter.close()

    _run(run())


def test_cdp_backend_reuses_exact_existing_conversation_tab_without_navigation(fixture_server, tmp_path):
    async def run():
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        browser = await manager.chromium.launch(headless=True)
        context = await browser.new_context()
        unrelated = await context.new_page()
        target = f"{fixture_server}c/test?mode=ask-chatgpt"
        exact = await context.new_page()
        await exact.goto(target)
        navigations: list[str] = []
        exact.on("framenavigated", lambda frame: navigations.append(frame.url))

        class _ConnectedBrowser:
            contexts = [context]

            def is_connected(self):
                return True

            async def close(self):
                pass

        class _NoopPlaywright:
            async def stop(self):
                pass

        settings = ChatGPTWebSettings(
            base_url=fixture_server,
            conversation_url=target,
            headless=False,
            browser_backend="google-chrome-cdp",
        )
        adapter = ChatGPTWebAdapter(tmp_path / "cdp-existing-tab", settings)
        adapter._playwright = _NoopPlaywright()
        adapter._browser = _ConnectedBrowser()
        adapter._context = context
        adapter._page = unrelated
        try:
            assert await adapter.open_task_conversation() == target
            assert adapter.page is exact
            assert adapter.page.url == target
            assert navigations == []
            assert await adapter._find_composer() is not None
        finally:
            await context.close()
            await browser.close()
            await manager.stop()

    _run(run())


def test_cdp_backend_fails_closed_when_exact_conversation_tab_is_missing(fixture_server, tmp_path):
    async def run():
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        browser = await manager.chromium.launch(headless=True)
        context = await browser.new_context()
        blank = await context.new_page()
        navigations: list[str] = []
        blank.on("framenavigated", lambda frame: navigations.append(frame.url))

        class _ConnectedBrowser:
            contexts = [context]

            def is_connected(self):
                return True

            async def close(self):
                pass

        class _NoopPlaywright:
            async def stop(self):
                pass

        settings = ChatGPTWebSettings(
            base_url=fixture_server,
            conversation_url=f"{fixture_server}c/test?mode=ask-chatgpt",
            headless=False,
            browser_backend="google-chrome-cdp",
        )
        adapter = ChatGPTWebAdapter(tmp_path / "cdp-missing-tab", settings)
        adapter._playwright = _NoopPlaywright()
        adapter._browser = _ConnectedBrowser()
        adapter._context = context
        adapter._page = blank
        try:
            with pytest.raises(ConversationNavigationFailed):
                await adapter.open_task_conversation()
            assert blank.url == "about:blank"
            assert navigations == []
        finally:
            await context.close()
            await browser.close()
            await manager.stop()

    _run(run())


def test_cdp_backend_verifies_opaque_live_attachment_by_selection_and_chip_count(fixture_server, tmp_path):
    async def run():
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        browser = await manager.chromium.launch(headless=True)
        context = await browser.new_context()
        target = f"{fixture_server}c/test?mode=modern-live-opaque-upload"
        page = await context.new_page()
        await page.goto(target)

        class _ConnectedBrowser:
            contexts = [context]

            def is_connected(self):
                return True

            async def close(self):
                pass

        class _NoopPlaywright:
            async def stop(self):
                pass

        data = PortableDataRoot(tmp_path / "live-upload").create()
        attachment = data.safe_path("active/p/t/scratch/upload/relay-smoke.txt")
        attachment.parent.mkdir(parents=True)
        attachment.write_text("Harmless ReviewRelay transport smoke fixture.\n", encoding="utf-8")
        adapter = ChatGPTWebAdapter(
            data,
            ChatGPTWebSettings(
                base_url=fixture_server,
                conversation_url=target,
                headless=False,
                browser_backend=BrowserBackend.GOOGLE_CHROME_CDP,
                timeouts=ChatGPTTimeouts(
                    navigation_seconds=4.0,
                    upload_seconds=0.8,
                    response_seconds=0.8,
                    stability_seconds=0.03,
                ),
            ),
            project_id="p",
            task_id="t",
        )
        adapter._playwright = _NoopPlaywright()
        adapter._browser = _ConnectedBrowser()
        adapter._context = context
        adapter._page = page
        try:
            sent = await adapter.send_review_pack(
                prompt="first line",
                review_key="opaque-live-upload",
                attachment_paths=[attachment],
            )
            response = await adapter.wait_response(sent)
            assert response.review_key == sent.review_key
            assert await page.evaluate("window.__reviewrelayFileSelectionCheckV1.matches") is True
            assert await page.evaluate("window.sendClicks") == 1
        finally:
            await context.close()
            await browser.close()
            await manager.stop()

    _run(run())


def test_cdp_backend_waits_for_restored_ui_hydration_before_send(fixture_server, tmp_path):
    async def run():
        from playwright.async_api import async_playwright

        manager = await async_playwright().start()
        browser = await manager.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        target = f"{fixture_server}c/test?mode=loading-draft"
        await page.goto(target)

        class _ConnectedBrowser:
            contexts = [context]

            def is_connected(self):
                return True

            async def close(self):
                pass

        class _NoopPlaywright:
            async def stop(self):
                pass

        adapter = ChatGPTWebAdapter(
            tmp_path / "cdp-loading-draft",
            ChatGPTWebSettings(
                base_url=fixture_server,
                conversation_url=target,
                headless=False,
                browser_backend=BrowserBackend.GOOGLE_CHROME_CDP,
                timeouts=ChatGPTTimeouts(
                    navigation_seconds=20,
                    upload_seconds=0.35,
                    response_seconds=0.8,
                    stability_seconds=0.03,
                ),
            ),
        )
        adapter._playwright = _NoopPlaywright()
        adapter._browser = _ConnectedBrowser()
        adapter._context = context
        adapter._page = page
        try:
            with pytest.raises(ReviewerConversationChanged, match="attachment"):
                await adapter.send_review_pack(prompt="must not replace draft", review_key="loading-draft")
            assert await page.evaluate("window.sendClicks") == 0
            assert await adapter._visible_attachment_names() == ["owner-draft.txt"]
        finally:
            await context.close()
            await browser.close()
            await manager.stop()

    _run(run())


def test_restored_loading_status_disappearing_during_read_does_not_overrun_readiness(fixture_server, tmp_path):
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as manager:
            browser = await manager.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(f"{fixture_server}c/test")
            await page.evaluate("""() => {
                const status = document.createElement('div');
                status.id = 'vanishing-status'; status.role = 'status';
                status.textContent = 'Loading older messages…';
                document.body.append(status);
                const draft = document.createElement('div');
                draft.dataset.testid = 'attachment-chip'; draft.textContent = 'owner-draft.txt';
                document.querySelector('#attachments').append(draft);
            }""")

            class VanishingStatus:
                async def is_visible(self):
                    return await page.locator("#vanishing-status").is_visible()

                async def inner_text(self, **kwargs):
                    await page.evaluate("document.querySelector('#vanishing-status').remove()")
                    return await page.locator("#vanishing-status").inner_text(**kwargs)

            class StatusList:
                async def count(self):
                    return await page.locator("#vanishing-status").count()

                def nth(self, index):
                    return VanishingStatus()

            class PageWithStatusRace:
                def locator(self, selector):
                    return StatusList() if selector == "[role='status']" else page.locator(selector)

                def __getattr__(self, name):
                    return getattr(page, name)

            adapter = ChatGPTWebAdapter(tmp_path / "status-race", ChatGPTWebSettings(
                base_url=fixture_server, conversation_url=f"{fixture_server}c/test", headless=False,
                browser_backend=BrowserBackend.GOOGLE_CHROME_CDP,
                timeouts=ChatGPTTimeouts(navigation_seconds=5)))
            adapter._page = PageWithStatusRace()
            try:
                await asyncio.wait_for(adapter._wait_for_live_ui_ready(), timeout=4)
                assert await adapter._visible_attachment_names() == ["owner-draft.txt"]
                assert await page.evaluate("window.sendClicks") == 0
            finally:
                await browser.close()

    _run(run())


@pytest.mark.parametrize("label,offscreen,ready", [
    ("Loading older messages…", True, True),
    ("Loading older messages…", False, False),
    ("Loading conversation…", True, False),
    ("Loading messages…", True, False),
])
def test_restored_lazy_history_only_ignores_offscreen_older_loader(
    fixture_server, tmp_path, label, offscreen, ready
):
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as manager:
            browser = await manager.chromium.launch(headless=True)
            page = await browser.new_page()
            target = f"{fixture_server}c/test"
            await page.goto(target)
            await page.evaluate("""({label,offscreen}) => {
                const status = document.createElement('div');
                status.role = 'status'; status.textContent = label;
                status.style.cssText = 'position:fixed;left:20px;top:'
                    + (offscreen ? '-200px' : '20px') + ';height:30px';
                document.body.append(status);
                const draft = document.createElement('div');
                draft.dataset.testid = 'attachment-chip'; draft.textContent = 'owner-draft.txt';
                document.querySelector('#attachments').append(draft);
            }""", {"label": label, "offscreen": offscreen})
            adapter = ChatGPTWebAdapter(tmp_path / "lazy-history", ChatGPTWebSettings(
                base_url=fixture_server, conversation_url=target,
                browser_backend=BrowserBackend.GOOGLE_CHROME_CDP,
                timeouts=ChatGPTTimeouts(navigation_seconds=5 if ready else 0.5)))
            adapter._page = page
            try:
                if ready:
                    await asyncio.wait_for(adapter._wait_for_live_ui_ready(), timeout=4)
                else:
                    with pytest.raises(ConversationNotReady):
                        await adapter._wait_for_live_ui_ready()
                assert await adapter._visible_attachment_names() == ["owner-draft.txt"]
                assert await page.evaluate("window.sendClicks") == 0
            finally:
                await browser.close()

    _run(run())


def test_lazy_file_input_is_waited_for_after_add_files_menu(fixture_server, tmp_path):
    async def run():
        data = PortableDataRoot(tmp_path / "lazy-upload").create()
        attachment = data.safe_path("active/fixture/task/scratch/upload/relay-smoke.txt")
        attachment.parent.mkdir(parents=True, exist_ok=True)
        attachment.write_text("Harmless fixture attachment.\n", encoding="utf-8")
        adapter = ChatGPTWebAdapter(data, _settings(fixture_server, "lazy-upload"))
        try:
            result = await adapter.send_review_pack(
                prompt="Fixture upload transport check",
                review_key="lazy-upload-check",
                attachment_paths=[attachment],
            )
            assert result.disposition is SendDisposition.SEND_CONFIRMED
            assert await adapter._visible_attachment_names() == ["relay-smoke.txt"]
            response = await adapter.wait_response(result)
            assert response.disposition is SendDisposition.RESPONSE_RECEIVED
            assert await adapter.page.evaluate("window.sendClicks") == 1
        finally:
            await adapter.close()

    _run(run())


def test_http_error_with_login_ui_returns_login_required(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "login-error", _settings(fixture_server, "login-error"))
        try:
            with pytest.raises(LoginRequired) as caught:
                await adapter.open_task_conversation()
            assert caught.value.code == "LOGIN_REQUIRED"
        finally:
            await adapter.close()

    _run(run())


def test_normal_ui_send_is_single_and_response_is_raw(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server))
        try:
            result = await adapter.send_review_pack(prompt="Please review this candidate", review_key="rr-key-1")
            assert result.disposition is SendDisposition.SEND_CONFIRMED
            assert result.pre_send_baseline.assistant_turn_ids == ("old-assistant-1",)
            response = await adapter.wait_response(result)
            assert response.disposition is SendDisposition.RESPONSE_RECEIVED
            assert response.saw_streaming is True
            assert response.text == (
                'Markdown **kept**.\nProse before.\n'
                '<RELAY_CONTROL>{"protocol":"rr.v1","candidate_sha":"abc123","cycle":1,"action":"PASS"}'
                '</RELAY_CONTROL>\nProse after.'
            )
            assert "Earlier response" not in response.text
            assert await adapter.page.evaluate("window.sendClicks") == 1
            repeated = await adapter.send_review_pack(prompt="Please review this candidate", review_key="rr-key-1")
            assert repeated.user_turn_identity == result.user_turn_identity
            assert await adapter.page.evaluate("window.sendClicks") == 1
            assert await adapter.page.locator("[data-message-author-role='user']").count() == 2
            with pytest.raises(ReviewKeyConflict):
                await adapter.send_review_pack(prompt="different", review_key="rr-key-1")
        finally:
            await adapter.close()

    _run(run())


def test_valid_conversation_and_latest_response_are_owned(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server))
        try:
            target = await adapter.open_task_conversation()
            assert target.endswith("/c/test?mode=normal")
            result = await adapter.send_evidence(prompt="evidence", review_key="evidence-1")
            response = await adapter.get_latest_response(result)
            assert response.review_key == "evidence-1"
            assert "PASS is only old content" not in response.text
        finally:
            await adapter.close()

    _run(run())


def test_attachment_validation_limits_and_managed_storage(fixture_server, tmp_path):
    root = PortableDataRoot(tmp_path / "data").create()
    upload = root.safe_path("active/project/task/scratch/upload")
    upload.mkdir(parents=True)
    valid = upload / "review.txt"
    valid.write_text("safe review artifact", encoding="utf-8")
    directory = upload / "folder"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    large_single = upload / "large.bin"
    with large_single.open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)
    total_a, total_b = upload / "a.bin", upload / "b.bin"
    for path in (total_a, total_b):
        with path.open("wb") as stream:
            stream.truncate(26 * 1024 * 1024)

    adapter = ChatGPTWebAdapter(root, _settings(fixture_server))
    assert adapter._validate_attachments([valid]) == (valid.resolve(),)
    with pytest.raises(AttachmentNotFound):
        adapter._validate_attachments([upload / "missing.txt"])
    with pytest.raises(AttachmentNotFound):
        adapter._validate_attachments([directory])
    with pytest.raises(AttachmentNotAllowed):
        adapter._validate_attachments([outside])
    with pytest.raises(AttachmentLimitExceeded):
        adapter._validate_attachments([valid] * 11)
    with pytest.raises(AttachmentLimitExceeded):
        adapter._validate_attachments([large_single])
    with pytest.raises(AttachmentLimitExceeded):
        adapter._validate_attachments([total_a, total_b])
    scoped = ChatGPTWebAdapter(root, _settings(fixture_server), project_id="another", task_id="task")
    with pytest.raises(AttachmentNotAllowed):
        scoped._validate_attachments([valid])


def test_attachment_upload_order_and_upload_failure(fixture_server, tmp_path):
    async def run():
        data = PortableDataRoot(tmp_path / "data").create()
        upload = data.safe_path("active/p/t/scratch/upload")
        upload.mkdir(parents=True)
        first, second = upload / "first.txt", upload / "second.txt"
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        adapter = ChatGPTWebAdapter(data, _settings(fixture_server))
        try:
            result = await adapter.send_review_pack(prompt="Review attached files", review_key="attachments", attachment_paths=[first, second])
            response = await adapter.wait_response(result)
            assert response.text.startswith("Markdown")
            chips = await adapter.page.locator("[data-testid='attachment-chip']").all_inner_texts()
            assert chips == ["first.txt", "second.txt"]
        finally:
            await adapter.close()

        failing = ChatGPTWebAdapter(tmp_path / "failed", _settings(fixture_server, "upload-error"))
        try:
            failing_root = PortableDataRoot(tmp_path / "failed").create()
            failing_upload = failing_root.safe_path("active/p/t/scratch/upload")
            failing_upload.mkdir(parents=True)
            failing_file = failing_upload / "first.txt"
            failing_file.write_text("one", encoding="utf-8")
            with pytest.raises(AttachmentUploadFailed) as caught:
                await failing.send_review_pack(prompt="do not send", review_key="upload-fail", attachment_paths=[failing_file])
            assert caught.value.send_state == "NOT_SENT"
        finally:
            await failing.close()

    _run(run())


def test_missing_or_external_attachment_fails_before_browser_send(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server))
        outside = tmp_path / "repo.txt"
        outside.write_text("outside", encoding="utf-8")
        with pytest.raises(AttachmentNotAllowed) as caught:
            await adapter.send_review_pack(prompt="no", review_key="bad-path", attachment_paths=[tmp_path / "repo.txt"])
        assert caught.value.send_state == "NOT_SENT"

    _run(run())


def test_composer_and_send_failures_are_typed_and_do_not_send(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server))
        try:
            await adapter.open_task_conversation()
            await adapter.page.locator("#composer").evaluate("element => element.remove()")
            with pytest.raises(ComposerNotFound) as caught_composer:
                await adapter._require_composer()
            assert caught_composer.value.code == "COMPOSER_NOT_FOUND"
        finally:
            await adapter.close()

        no_send = ChatGPTWebAdapter(tmp_path / "no-send", _settings(fixture_server, "no-send-button"))
        try:
            with pytest.raises(MessageSendFailed) as caught_send:
                await no_send.send_review_pack(prompt="cannot send", review_key="no-send")
            assert caught_send.value.send_state == "NOT_SENT"
            assert await no_send.page.evaluate("window.sendClicks") == 0
        finally:
            await no_send.close()

    _run(run())


def test_ambiguous_send_is_remembered_and_not_retried(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server, "ambiguous-send"))
        try:
            with pytest.raises(MessageSendAmbiguous) as caught:
                await adapter.send_review_pack(prompt="possibly sent", review_key="ambiguous")
            assert caught.value.send_state == "SEND_AMBIGUOUS"
            assert await adapter.page.evaluate("window.sendClicks") == 1
            with pytest.raises(MessageSendAmbiguous):
                await adapter.send_review_pack(prompt="possibly sent", review_key="ambiguous")
            assert await adapter.page.evaluate("window.sendClicks") == 1
        finally:
            await adapter.close()

    _run(run())


def test_user_interruption_and_navigation_are_fail_closed(fixture_server, tmp_path):
    async def run():
        changed = ChatGPTWebAdapter(tmp_path / "changed", _settings(fixture_server, "extra-user"))
        try:
            with pytest.raises(ReviewerConversationChanged):
                await changed.send_review_pack(prompt="relay prompt", review_key="changed")
        finally:
            await changed.close()

        navigation = ChatGPTWebAdapter(tmp_path / "navigation", _settings(fixture_server))
        try:
            result = await navigation.send_review_pack(prompt="relay prompt", review_key="navigate")
            await navigation.page.evaluate("history.pushState({}, '', '/outside')")
            with pytest.raises(ReviewerConversationChanged):
                await navigation.wait_response(result)
        finally:
            await navigation.close()

        reload = ChatGPTWebAdapter(tmp_path / "reload", _settings(fixture_server, "timeout"))
        try:
            result = await reload.send_review_pack(prompt="relay prompt", review_key="reload")
            await reload.page.reload(wait_until="domcontentloaded")
            with pytest.raises(ReviewerConversationChanged):
                await reload.wait_response(result)
        finally:
            await reload.close()

    _run(run())


def test_pre_send_manual_changes_and_owner_drafts_are_preserved(fixture_server, tmp_path):
    async def run():
        draft = ChatGPTWebAdapter(tmp_path / "draft", _settings(fixture_server, "draft"))
        try:
            with pytest.raises(ReviewerConversationChanged) as caught:
                await draft.send_review_pack(prompt="relay must not overwrite this", review_key="draft")
            assert caught.value.send_state == "NOT_SENT"
            assert await draft.page.locator("#composer").inner_text() == "Owner draft that must not be overwritten"
            assert await draft.page.evaluate("window.sendClicks") == 0
        finally:
            await draft.close()

        data = PortableDataRoot(tmp_path / "concurrent").create()
        upload = data.safe_path("active/p/t/scratch/upload")
        upload.mkdir(parents=True)
        attachment = upload / "note.txt"
        attachment.write_text("safe", encoding="utf-8")
        concurrent = ChatGPTWebAdapter(data, _settings(fixture_server, "concurrent-upload"))
        try:
            with pytest.raises(ReviewerConversationChanged) as caught:
                await concurrent.send_review_pack(
                    prompt="relay message", review_key="concurrent", attachment_paths=[attachment]
                )
            assert caught.value.send_state == "NOT_SENT"
            assert await concurrent.page.evaluate("window.sendClicks") == 0
        finally:
            await concurrent.close()

        attachment_change = ChatGPTWebAdapter(data, _settings(fixture_server, "attachment-extra"))
        try:
            with pytest.raises(ReviewerConversationChanged):
                await attachment_change.send_review_pack(
                    prompt="relay message", review_key="attachment-change", attachment_paths=[attachment]
                )
            assert await attachment_change.page.evaluate("window.sendClicks") == 0
        finally:
            await attachment_change.close()

    _run(run())


def test_whitespace_only_composer_is_treated_as_empty(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "whitespace-empty", _settings(fixture_server, "whitespace-empty"))
        try:
            sent = await adapter.send_review_pack(prompt="relay prompt", review_key="whitespace-empty")
            response = await adapter.wait_response(sent)
            assert response.review_key == "whitespace-empty"
            assert response.assistant_turn_identity not in sent.pre_send_baseline.assistant_turn_ids
            assert await adapter.page.evaluate("window.sendClicks") == 1
        finally:
            await adapter.close()

    _run(run())


def test_multiple_new_assistant_turns_are_ambiguous(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server, "multiple-assistant"))
        try:
            result = await adapter.send_review_pack(prompt="relay prompt", review_key="multi")
            with pytest.raises(AmbiguousResponse) as caught:
                await adapter.wait_response(result)
            assert caught.value.code == "AMBIGUOUS_RESPONSE"
        finally:
            await adapter.close()

    _run(run())


def test_response_timeout_distinguishes_confirmed_send(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server, "timeout"))
        try:
            result = await adapter.send_review_pack(prompt="wait for nothing", review_key="timeout")
            with pytest.raises(ResponseTimeout) as caught:
                await adapter.wait_response(result, timeout_seconds=0.25)
            assert caught.value.send_state == "SEND_CONFIRMED"
            assert caught.value.send_result.review_key == "timeout"
        finally:
            await adapter.close()

    _run(run())


def test_phase2_parser_receives_raw_adapter_text(fixture_server, tmp_path):
    async def run():
        adapter = ChatGPTWebAdapter(tmp_path / "data", _settings(fixture_server, "integration"))
        try:
            sent = await adapter.send_review_pack(prompt="return a test control block", review_key="phase2-integration")
            response = await adapter.wait_response(sent)
            assert "PASS" in response.text  # Raw transport does not act on this value.
            validated = validate_review_response(
                response.text,
                expected_candidate_sha="abc123",
                expected_cycle=1,
            )
            assert validated.action is ReviewerAction.PASS
        finally:
            await adapter.close()

    _run(run())
