"""Safe bootstrap helpers for an Owner-authenticated Google Chrome profile."""

from __future__ import annotations

import asyncio
from enum import Enum
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from typing import BinaryIO

from ..storage import PortableDataRoot


class ChromeMode(str, Enum):
    AUTH = "auth"
    AUTOMATION = "automation"


class BrowserProfileInUseError(RuntimeError):
    pass


class ReviewRelayProfileLock:
    """Cooperative cross-process lock held across Auth and Automation modes."""

    def __init__(self, data_root: PortableDataRoot, profile_name: str) -> None:
        self.data_root = data_root
        self.profile_name = profile_name
        self.profile_path = data_root.safe_path(Path("browser-profile") / profile_name)
        self.lock_path = data_root.safe_path(Path("browser-profile-locks") / f"{profile_name}.lock")
        self._stream: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._stream is not None

    def acquire(self) -> "ReviewRelayProfileLock":
        if self._stream is not None:
            return self
        self.data_root.create()
        self.profile_path.mkdir(parents=True, exist_ok=True)
        self.data_root.assert_managed_path(self.profile_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_root.assert_managed_path(self.lock_path)
        stream = self.lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            stream.close()
            raise BrowserProfileInUseError("The ReviewRelay Chrome profile is already in use") from exc
        self._stream = stream
        return self

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def matches_profile(self, profile_path: str | Path) -> bool:
        expected = self.profile_path.resolve(strict=False)
        actual = Path(profile_path).resolve(strict=False)
        return expected == actual

    def __enter__(self) -> "ReviewRelayProfileLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def find_google_chrome() -> Path:
    """Find an installed Google Chrome executable without selecting its default profile."""
    candidates: list[Path] = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramW6432", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":
        candidates.extend(
            (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )
        )
    else:
        for name in ("google-chrome", "google-chrome-stable"):
            executable = shutil.which(name)
            if executable:
                candidates.append(Path(executable))

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise FileNotFoundError("Google Chrome was not found in a supported installation location")


def build_chrome_launch_command(
    executable: str | Path,
    user_data_dir: str | Path,
    *,
    mode: ChromeMode = ChromeMode.AUTOMATION,
    conversation_url: str | None = None,
) -> list[str]:
    """Build either normal Auth Mode or loopback-CDP Automation Mode."""
    chrome = Path(executable).expanduser()
    profile = Path(user_data_dir).expanduser()
    if not chrome.is_absolute() or not profile.is_absolute():
        raise ValueError("Chrome executable and ReviewRelay user-data directory must be absolute paths")
    mode = ChromeMode(mode)
    if mode is ChromeMode.AUTH and not conversation_url:
        raise ValueError("Auth Mode requires the configured existing conversation URL")
    if mode is ChromeMode.AUTOMATION and conversation_url is not None:
        raise ValueError("Automation Mode navigates through Playwright and does not accept a launch URL")
    command = [
        str(chrome),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if mode is ChromeMode.AUTOMATION:
        command.extend(
            (
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--restore-last-session",
            )
        )
    else:
        command.append(conversation_url or "")
    return command


def launch_chrome(
    executable: str | Path,
    user_data_dir: str | Path,
    *,
    mode: ChromeMode,
    conversation_url: str | None = None,
) -> subprocess.Popen[bytes]:
    command = build_chrome_launch_command(
        executable,
        user_data_dir,
        mode=mode,
        conversation_url=conversation_url,
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=flags,
    )


async def wait_for_chrome_exit(process: subprocess.Popen[bytes], timeout_seconds: float | None = None) -> bool:
    if process.poll() is not None:
        return True
    try:
        if timeout_seconds is None:
            await asyncio.to_thread(process.wait)
        else:
            await asyncio.to_thread(process.wait, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


async def open_manual_auth_mode(data_root: PortableDataRoot, settings) -> None:
    """UI setup: normal dedicated Chrome, no CDP/Playwright or credentials."""
    conversation_url = settings.resolve_conversation_url()
    with ReviewRelayProfileLock(data_root, settings.browser_profile) as lock:
        process = launch_chrome(find_google_chrome(), lock.profile_path,
                                mode=ChromeMode.AUTH, conversation_url=conversation_url)
        # Owner closes Chrome normally, saving the exact tab for the accepted adapter.
        while not await wait_for_chrome_exit(process, timeout_seconds=0.25):
            await asyncio.sleep(0.1)


async def request_chrome_shutdown(process: subprocess.Popen[bytes], timeout_seconds: float = 5) -> bool:
    """Ask our isolated Chrome process group to close, without force-killing it."""
    if process.poll() is not None:
        return True
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
    return await wait_for_chrome_exit(process, timeout_seconds)


def read_chrome_cdp_endpoint(user_data_dir: str | Path) -> str | None:
    """Read only Chrome's ephemeral local debugging port, never its WebSocket path."""
    port_file = Path(user_data_dir) / "DevToolsActivePort"
    try:
        port_text = port_file.read_text(encoding="ascii").splitlines()[0].strip()
        port = int(port_text)
    except (OSError, UnicodeError, IndexError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return f"http://127.0.0.1:{port}"
