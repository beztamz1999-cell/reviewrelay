"""Safe bootstrap helpers for an Owner-authenticated Google Chrome profile."""

from __future__ import annotations

import asyncio
from enum import Enum
import os
from pathlib import Path
import signal
import shutil
import socket
import struct
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
    debugging_port: int | None = None,
) -> list[str]:
    """Build either normal Auth Mode or loopback-CDP Automation Mode."""
    chrome = Path(executable).expanduser()
    profile = Path(user_data_dir).expanduser()
    if not chrome.is_absolute() or not profile.is_absolute():
        raise ValueError("Chrome executable and ReviewRelay user-data directory must be absolute paths")
    mode = ChromeMode(mode)
    if mode is ChromeMode.AUTH and not conversation_url:
        raise ValueError("Auth Mode requires the configured existing conversation URL")
    if mode is ChromeMode.AUTH and debugging_port is not None:
        raise ValueError("Auth Mode does not accept a debugging port")
    if mode is ChromeMode.AUTOMATION and conversation_url is not None:
        raise ValueError("Automation Mode restores existing tabs and does not accept a launch URL")
    if mode is ChromeMode.AUTOMATION:
        chrome_cdp_endpoint(debugging_port)
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
                f"--remote-debugging-port={debugging_port}",
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
    debugging_port: int | None = None,
) -> subprocess.Popen[bytes]:
    command = build_chrome_launch_command(
        executable,
        user_data_dir,
        mode=mode,
        conversation_url=conversation_url,
        debugging_port=debugging_port,
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


def chrome_cdp_endpoint(port: int) -> str:
    """Only an explicitly selected, nonzero loopback endpoint; no profile file."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Chrome CDP requires a nonzero TCP port in range 1..65535")
    return f"http://127.0.0.1:{port}"


def select_loopback_port() -> int:
    """Ask the OS for an available local port; Chrome receives the actual number.

    Releasing this socket before Chrome binds has an unavoidable race. The
    adapter checks listener ownership before attaching and retries boundedly.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    chrome_cdp_endpoint(port)
    return port


def _windows_listeners(port: int) -> list[tuple[str, int]]:
    """Read only the selected port's listeners using Windows' owner-PID table."""
    import ctypes

    api = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    api.restype = ctypes.c_uint32
    result = []
    for family, row_size in ((socket.AF_INET, 24), (socket.AF_INET6, 56)):
        size = ctypes.c_uint32(0)
        if api(None, ctypes.byref(size), False, family, 3, 0) != 122:
            raise OSError("Could not size Chrome listener ownership table")
        for _ in range(3):
            if not 4 <= size.value <= 4 * 1024 * 1024:
                raise OSError("Invalid Chrome listener ownership table size")
            buffer = ctypes.create_string_buffer(size.value)
            code = api(buffer, ctypes.byref(size), False, family, 3, 0)
            if code == 122:
                continue
            if code:
                raise OSError("Could not read Chrome listener ownership table")
            raw = buffer.raw
            count = struct.unpack_from("<I", raw)[0]
            if 4 + count * row_size > len(raw):
                raise OSError("Incomplete Chrome listener ownership table")
            for offset in range(4, 4 + count * row_size, row_size):
                port_offset = offset + (8 if family == socket.AF_INET else 20)
                local_port = socket.ntohs(struct.unpack_from("<I", raw, port_offset)[0] & 65535)
                if local_port != port:
                    continue
                address_offset = offset + (4 if family == socket.AF_INET else 0)
                address_size = 4 if family == socket.AF_INET else 16
                address = socket.inet_ntop(family, raw[address_offset:address_offset + address_size])
                pid = struct.unpack_from("<I", raw, offset + row_size - 4)[0]
                result.append((address, pid))
            break
        else:
            raise OSError("Chrome listener ownership table kept changing")
    return result


def chrome_listener_ready(process: subprocess.Popen[bytes], port: int) -> bool:
    """Never attach to a raced foreign listener or a remotely exposed endpoint.

    Native ownership verification is supported on the Windows production host.
    Other hosts fail closed; offline Chromium needs no listener inspection.
    """
    chrome_cdp_endpoint(port)
    if process.poll() is not None:
        return False
    if os.name != "nt":
        raise OSError("Chrome CDP listener ownership verification requires Windows")
    listeners = _windows_listeners(port)
    if not listeners:
        return False
    if (not any(address == "127.0.0.1" for address, _ in listeners)
            or any(address not in {"127.0.0.1", "::1"} or pid != process.pid for address, pid in listeners)):
        raise OSError("Selected Chrome CDP port belongs to another process or is not loopback-only")
    return process.poll() is None
