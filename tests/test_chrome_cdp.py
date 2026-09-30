from __future__ import annotations

import asyncio
from argparse import Namespace
import os
from pathlib import Path
import signal
import pytest

from reviewrelay.dev.chatgpt_smoke import _settings_from_args
from reviewrelay.dev.chatgpt_smoke import _arguments
from reviewrelay.reviewer import BrowserBackend, ChatGPTWebAdapter, ChatGPTWebSettings
from reviewrelay.reviewer.chrome_cdp import (
    BrowserProfileInUseError,
    ChromeMode,
    ReviewRelayProfileLock,
    build_chrome_launch_command,
    read_chrome_cdp_endpoint,
    request_chrome_shutdown,
)
from reviewrelay.storage import PortableDataRoot


class _FakePage:
    def on(self, *_: object) -> None:
        pass

    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        pass


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.contexts = [context]

    def is_connected(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def test_offline_default_and_live_smoke_choose_separate_backends():
    assert ChatGPTWebSettings().browser_backend is BrowserBackend.PLAYWRIGHT_CHROMIUM
    live = _settings_from_args(
        Namespace(
            base_url="https://chatgpt.com/",
            browser_profile="reviewer-chrome",
            conversation_url="https://chatgpt.com/c/existing",
        )
    )
    assert live.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP
    assert live.browser_profile == "reviewer-chrome"
    assert live.headless is False


def test_live_smoke_can_reuse_previously_authenticated_profile_without_auth_mode():
    args = _arguments(
        [
            "--data-root", "C:/ReviewRelay",
            "--conversation-url", "https://chatgpt.com/c/existing",
            "--automation-only", "--send",
        ]
    )
    assert args.automation_only is True


def test_adapter_dispatches_to_selected_browser_backend(monkeypatch, tmp_path):
    import playwright.async_api

    context = _FakeContext()
    calls: list[str] = []

    class _FakeChromium:
        async def launch_persistent_context(self, **_: object) -> _FakeContext:
            calls.append("playwright-chromium")
            return context

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self) -> None:
            pass

    class _FakeManager:
        async def start(self) -> _FakePlaywright:
            return _FakePlaywright()

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: _FakeManager())

    async def run() -> None:
        offline = ChatGPTWebAdapter(tmp_path / "offline", ChatGPTWebSettings())
        await offline.start()
        await offline.close()

        live = ChatGPTWebAdapter(
            tmp_path / "live",
            ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP),
        )

        async def attach_fake_chrome() -> None:
            calls.append("google-chrome-cdp")
            live._browser = _FakeBrowser(context)

        monkeypatch.setattr(live, "_start_or_reconnect_chrome", attach_fake_chrome)
        await live.start()
        await live.close()

    asyncio.run(run())
    assert calls == ["playwright-chromium", "google-chrome-cdp"]


def test_chrome_launch_command_uses_dedicated_profile_and_loopback_cdp(tmp_path):
    executable = (tmp_path / "Google Chrome" / "chrome.exe").resolve()
    profile = (tmp_path / "portable" / "browser-profile" / "reviewer-chrome").resolve()
    auth_command = build_chrome_launch_command(
        executable,
        profile,
        mode=ChromeMode.AUTH,
        conversation_url="https://chatgpt.com/c/existing",
    )
    automation_command = build_chrome_launch_command(
        executable,
        profile,
        mode=ChromeMode.AUTOMATION,
    )

    assert auth_command[0] == automation_command[0] == str(executable)
    assert f"--user-data-dir={profile}" in auth_command
    assert f"--user-data-dir={profile}" in automation_command
    assert not any("remote-debugging" in argument for argument in auth_command)
    assert "--remote-debugging-address=127.0.0.1" in automation_command
    assert "--remote-debugging-port=0" in automation_command
    assert "--remote-debugging-address=0.0.0.0" not in automation_command
    assert "--profile-directory=Default" not in auth_command + automation_command
    assert auth_command[-1] == "https://chatgpt.com/c/existing"
    assert automation_command[-1] == "about:blank"


def test_profile_lock_prevents_auth_and_automation_modes_from_overlapping(tmp_path):
    root = PortableDataRoot(tmp_path / "portable").create()
    auth_mode = ReviewRelayProfileLock(root, "reviewer-chrome").acquire()
    automation_mode = ReviewRelayProfileLock(root, "reviewer-chrome")
    try:
        assert auth_mode.profile_path == root.safe_path("browser-profile/reviewer-chrome")
        assert auth_mode.lock_path.parent != auth_mode.profile_path
        try:
            automation_mode.acquire()
        except BrowserProfileInUseError:
            pass
        else:
            raise AssertionError("A second mode acquired a profile already locked by Auth Mode")
    finally:
        auth_mode.release()
    automation_mode.acquire()
    automation_mode.release()


def test_cdp_endpoint_uses_only_valid_local_port(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    active_port = profile / "DevToolsActivePort"

    active_port.write_text("43127\n/devtools/browser/private-id\n", encoding="ascii")
    assert read_chrome_cdp_endpoint(profile) == "http://127.0.0.1:43127"

    active_port.write_text("70000\n/devtools/browser/private-id\n", encoding="ascii")
    assert read_chrome_cdp_endpoint(profile) is None


def test_chrome_shutdown_uses_windows_ctrl_break_signal():
    if os.name != "nt":
        pytest.skip("Windows process-group shutdown behavior")

    class _FakeProcess:
        return_code = None
        sent_signal = None

        def poll(self):
            return self.return_code

        def send_signal(self, value):
            self.sent_signal = value
            self.return_code = 0

        def wait(self, timeout=None):
            return self.return_code

    process = _FakeProcess()
    assert asyncio.run(request_chrome_shutdown(process)) is True
    assert process.sent_signal == signal.CTRL_BREAK_EVENT
