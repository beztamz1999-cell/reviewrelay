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
    chrome_cdp_endpoint,
    chrome_listener_ready,
    select_loopback_port,
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


def test_cdp_close_preserves_restored_tabs_until_chrome_saves_session(tmp_path):
    class _Page:
        closed = False

        async def close(self):
            self.closed = True

    class _Context:
        def __init__(self, page):
            self.pages = [page]

    class _Session:
        def __init__(self):
            self.commands = []

        async def send(self, command):
            self.commands.append(command)

    class _Browser:
        def __init__(self, context, session):
            self.contexts = [context]
            self.session = session

        async def new_browser_cdp_session(self):
            return self.session

        async def close(self):
            pass

    class _Playwright:
        async def stop(self):
            pass

    async def run():
        page = _Page()
        context = _Context(page)
        session = _Session()
        adapter = ChatGPTWebAdapter(
            tmp_path / "portable",
            ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP),
        )
        adapter._context = context
        adapter._browser = _Browser(context, session)
        adapter._playwright = _Playwright()
        await adapter.close()
        assert page.closed is False
        assert session.commands == ["Browser.close"]

    asyncio.run(run())


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
        debugging_port=43127,
    )

    assert auth_command[0] == automation_command[0] == str(executable)
    assert f"--user-data-dir={profile}" in auth_command
    assert f"--user-data-dir={profile}" in automation_command
    assert not any("remote-debugging" in argument for argument in auth_command)
    assert "--remote-debugging-address=127.0.0.1" in automation_command
    assert "--remote-debugging-port=43127" in automation_command
    assert "--remote-debugging-port=0" not in automation_command
    assert not any(argument in {"--enable-automation", "--headless"} for argument in automation_command)
    assert "--restore-last-session" in automation_command
    assert "--remote-debugging-address=0.0.0.0" not in automation_command
    assert "--profile-directory=Default" not in auth_command + automation_command
    assert auth_command[-1] == "https://chatgpt.com/c/existing"
    assert "about:blank" not in automation_command


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
    assert chrome_cdp_endpoint(43127) == "http://127.0.0.1:43127"
    assert chrome_cdp_endpoint(1) == "http://127.0.0.1:1"
    assert chrome_cdp_endpoint(65535) == "http://127.0.0.1:65535"


@pytest.mark.parametrize("port", [None, 0, -1, 65536, True, 12.5, "43127", "http://example.com"])
def test_invalid_cdp_ports_are_rejected_in_endpoint_and_command(tmp_path, port):
    with pytest.raises(ValueError):
        chrome_cdp_endpoint(port)
    with pytest.raises(ValueError):
        build_chrome_launch_command(tmp_path/'chrome.exe', tmp_path/'profile', debugging_port=port)


def test_auth_command_unchanged_and_rejects_debugging_port(tmp_path):
    expected = [str(tmp_path/'chrome.exe'), f"--user-data-dir={tmp_path/'profile'}",
        "--no-first-run", "--no-default-browser-check", "https://chatgpt.com/c/existing"]
    assert build_chrome_launch_command(tmp_path/'chrome.exe', tmp_path/'profile',
        mode=ChromeMode.AUTH, conversation_url=expected[-1]) == expected
    with pytest.raises(ValueError):
        build_chrome_launch_command(tmp_path/'chrome.exe', tmp_path/'profile',
            mode=ChromeMode.AUTH, conversation_url=expected[-1], debugging_port=43127)


def test_port_selection_binds_only_loopback(monkeypatch):
    import reviewrelay.reviewer.chrome_cdp as module
    calls = []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): calls.append("closed")
        def setsockopt(self, *args): pass
        def bind(self, address): calls.append(address)
        def getsockname(self): return ("127.0.0.1", 43127)
    monkeypatch.setattr(module.socket, "socket", lambda *a: Socket())
    assert select_loopback_port() == 43127
    assert calls == [("127.0.0.1", 0), "closed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows native listener table")
def test_native_listener_owner_is_read_without_chrome_or_authentication():
    import socket
    from types import SimpleNamespace
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        process = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        assert chrome_listener_ready(process, server.getsockname()[1]) is True


@pytest.mark.parametrize("listeners", [[("127.0.0.1", 999)], [("0.0.0.0", 123)],
    [("127.0.0.1", 123), ("::", 123)], [("127.0.0.1", 123), ("::1", 999)]])
def test_foreign_or_remotely_bound_listener_is_rejected(monkeypatch, listeners):
    from types import SimpleNamespace
    import reviewrelay.reviewer.chrome_cdp as module
    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setattr(module, "_windows_listeners", lambda port: listeners)
    with pytest.raises(OSError):
        chrome_listener_ready(SimpleNamespace(pid=123, poll=lambda: None), 43127)


def test_bootstrap_waits_for_owned_listener_without_active_port_file(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import reviewrelay.reviewer.chatgpt_web as module
    adapter = ChatGPTWebAdapter(tmp_path, ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP))
    adapter.profile_path = tmp_path/'browser-profile'
    launches, endpoints, checks = [], [], []
    process = SimpleNamespace(pid=123, poll=lambda: None)
    browser = _FakeBrowser(_FakeContext())
    async def connect(endpoint, **kwargs):
        endpoints.append(endpoint)
        return browser
    adapter._playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    monkeypatch.setattr(module, "select_loopback_port", lambda: 43127)
    monkeypatch.setattr(module, "find_google_chrome", lambda: tmp_path/'chrome.exe')
    monkeypatch.setattr(module, "launch_chrome", lambda *a, **kw: launches.append(kw) or process)
    def ready(p, port):
        checks.append((p, port))
        return len(checks) >= 2
    monkeypatch.setattr(module, "chrome_listener_ready", ready)
    asyncio.run(adapter._start_or_reconnect_chrome())
    assert endpoints == ["http://127.0.0.1:43127"] and len(checks) == 2
    assert launches[0]["debugging_port"] == 43127
    assert adapter._browser is browser and adapter._cdp_port == 43127
    assert not (adapter.profile_path/'DevToolsActivePort').exists()
    asyncio.run(adapter._start_or_reconnect_chrome())
    assert len(launches) == 1 and len(endpoints) == 2  # Reconnect to the same process/port.


def test_raced_port_retries_after_prior_chrome_exit_without_contacting_foreign_listener(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import reviewrelay.reviewer.chatgpt_web as module
    adapter = ChatGPTWebAdapter(tmp_path, ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP))
    adapter.profile_path = tmp_path/'profile'
    launches, endpoints, stopped = [], [], []
    ports = iter([43127, 43128])
    browser = _FakeBrowser(_FakeContext())
    async def connect(endpoint, **kwargs):
        endpoints.append(endpoint)
        return browser
    adapter._playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    monkeypatch.setattr(module, "select_loopback_port", lambda: next(ports))
    monkeypatch.setattr(module, "find_google_chrome", lambda: tmp_path/'chrome.exe')
    def launch(*args, **kwargs):
        assert not launches or launches[-1].code == 0
        process = SimpleNamespace(pid=123+len(launches), code=None)
        process.poll = lambda: process.code
        launches.append(process)
        return process
    monkeypatch.setattr(module, "launch_chrome", launch)
    def ready(process, port):
        if port == 43127: raise OSError("foreign listener")
        return True
    monkeypatch.setattr(module, "chrome_listener_ready", ready)
    async def stop(process):
        stopped.append(process)
        process.code = 0
    monkeypatch.setattr(adapter, "_stop_chrome_process", stop)
    asyncio.run(adapter._start_or_reconnect_chrome())
    assert endpoints == ["http://127.0.0.1:43128"]
    assert len(launches) == 2 and stopped == launches[:1]


def test_failed_chrome_shutdown_never_launches_second_profile_process(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from reviewrelay.reviewer.errors import BrowserStartFailed
    import reviewrelay.reviewer.chatgpt_web as module
    adapter = ChatGPTWebAdapter(tmp_path, ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP))
    adapter.profile_path = tmp_path/'profile'
    adapter._playwright = object()
    launches = []
    process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(module, "select_loopback_port", lambda: 43127)
    monkeypatch.setattr(module, "find_google_chrome", lambda: tmp_path/'chrome.exe')
    monkeypatch.setattr(module, "launch_chrome", lambda *a, **kw: launches.append(kw) or process)
    monkeypatch.setattr(module, "chrome_listener_ready", lambda *a: (_ for _ in ()).throw(OSError("port conflict")))
    async def stop(process): pass
    monkeypatch.setattr(adapter, "_stop_chrome_process", stop)
    with pytest.raises(BrowserStartFailed, match="refusing concurrent"):
        asyncio.run(adapter._start_or_reconnect_chrome())
    assert len(launches) == 1


def test_failed_port_acquisition_has_bounded_retries(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from reviewrelay.reviewer.errors import BrowserStartFailed
    import reviewrelay.reviewer.chatgpt_web as module
    adapter = ChatGPTWebAdapter(tmp_path, ChatGPTWebSettings(browser_backend=BrowserBackend.GOOGLE_CHROME_CDP))
    adapter.profile_path = tmp_path/'profile'
    adapter._playwright = object()  # There must be no connect call to any listener.
    launches = []
    ports = iter([43127, 43128, 43129])
    monkeypatch.setattr(module, "select_loopback_port", lambda: next(ports))
    monkeypatch.setattr(module, "find_google_chrome", lambda: tmp_path/'chrome.exe')
    monkeypatch.setattr(module, "launch_chrome", lambda *a, **kw: launches.append(kw['debugging_port'])
        or SimpleNamespace(pid=123, poll=lambda: 1))
    with pytest.raises(BrowserStartFailed):
        asyncio.run(adapter._start_or_reconnect_chrome())
    assert launches == [43127, 43128, 43129]


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
