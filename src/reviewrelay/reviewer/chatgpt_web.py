"""Playwright transport for a configured ChatGPT web conversation.

Only the visible browser UI is used. This module deliberately does not import or
call the Phase 2 protocol parser: callers receive the response text verbatim.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import logging
import math
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence
from urllib.parse import urlsplit

from ..config import validate_identifier
from ..errors import PathSafetyError
from ..storage import PortableDataRoot
from .base import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_SINGLE_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_BYTES,
    AssistantResponse,
    BrowserBackend,
    ChatGPTWebSettings,
    SendDisposition,
    SendResult,
    TurnBaseline,
)
from .chrome_cdp import (
    BrowserProfileInUseError,
    ChromeMode,
    ReviewRelayProfileLock,
    find_google_chrome,
    launch_chrome,
    read_chrome_cdp_endpoint,
    request_chrome_shutdown,
)
from .errors import (
    AmbiguousResponse,
    AttachmentLimitExceeded,
    AttachmentNotAllowed,
    AttachmentNotFound,
    AttachmentUploadFailed,
    BrowserStartFailed,
    ComposerNotFound,
    ConversationNavigationFailed,
    ConversationNotReady,
    LoginRequired,
    MessageSendAmbiguous,
    MessageSendFailed,
    ResponseExtractionFailed,
    ResponseTimeout,
    ReviewKeyConflict,
    ReviewerConversationChanged,
    ReviewerConfigurationError,
)
from .selectors import ChatGPTSelectors, diagnostic_counts, first_visible, first_visible_enabled


_LOG = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 0.1
_LOGIN_PATH = re.compile(r"/(?:auth/)?(?:login|log-in|signin|sign-in)(?:/|$)", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _url_for_log(value: str) -> str:
    parts = urlsplit(value)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


class ChatGPTWebAdapter:
    """Single-page, single-conversation asynchronous ChatGPT UI adapter.

    Idempotency is process-local: review keys prevent duplicates for the life
    of this adapter instance. A later state controller must persist send
    metadata before attempting recovery across process restarts.
    """

    def __init__(
        self,
        data_root: PortableDataRoot | str | Path,
        settings: ChatGPTWebSettings | None = None,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        selectors: ChatGPTSelectors | None = None,
        profile_lock: ReviewRelayProfileLock | None = None,
    ) -> None:
        self.data_root = data_root if isinstance(data_root, PortableDataRoot) else PortableDataRoot(data_root)
        self.settings = settings or ChatGPTWebSettings()
        if (project_id is None) != (task_id is None):
            raise ReviewerConfigurationError("project_id and task_id must be supplied together")
        try:
            if project_id is not None:
                project_id = validate_identifier(project_id, "project_id")
                task_id = validate_identifier(task_id, "task_id")
        except PathSafetyError as exc:
            raise ReviewerConfigurationError(str(exc)) from exc
        self.project_id = project_id
        self.task_id = task_id
        self.selectors = selectors or ChatGPTSelectors()
        self._provided_profile_lock = profile_lock
        self._profile_lock: ReviewRelayProfileLock | None = None
        self._owns_profile_lock = False
        self.profile_path: Path | None = None
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._chrome_process: subprocess.Popen[bytes] | None = None
        self._cdp_endpoint: str | None = None
        self._start_lock = asyncio.Lock()
        self._navigation_generation = 0
        self._tracked_pages: set[int] = set()
        self._active_conversation_url: str | None = None
        self._send_registry: OrderedDict[str, tuple[str, SendResult]] = OrderedDict()
        self._responses: dict[str, AssistantResponse] = {}
        self._send_lock = asyncio.Lock()

    @property
    def page(self) -> Any:
        if self._page is None:
            raise BrowserStartFailed("Browser adapter has not been started")
        return self._page

    @property
    def browser_version(self) -> str | None:
        if self._browser is None:
            return None
        try:
            return str(self._browser.version)
        except Exception:
            return None

    async def start(self) -> Path:
        """Start or reconnect to the selected browser with a managed profile."""
        async with self._start_lock:
            if self._context is not None:
                if self.settings.browser_backend is BrowserBackend.PLAYWRIGHT_CHROMIUM or self._browser_is_connected():
                    return self.profile_path  # type: ignore[return-value]
                self._context = None
                self._page = None
                self._browser = None
            try:
                self.data_root.create()
                profile = self.data_root.safe_path(Path("browser-profile") / self.settings.browser_profile)
                self.data_root.assert_managed_path(profile)
                profile.mkdir(parents=True, exist_ok=True)
                self.profile_path = self.data_root.assert_managed_path(profile.resolve(strict=True))
                if self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP:
                    self._ensure_chrome_profile_lock()
                if self._playwright is None:
                    from playwright.async_api import async_playwright

                    self._playwright = await async_playwright().start()

                if self.settings.browser_backend is BrowserBackend.PLAYWRIGHT_CHROMIUM:
                    self._context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_path),
                        headless=self.settings.headless,
                        timeout=self.settings.timeouts.navigation_seconds * 1000,
                    )
                else:
                    await self._start_or_reconnect_chrome()
                    contexts = self._browser.contexts if self._browser is not None else []
                    if not contexts:
                        raise BrowserStartFailed("ReviewRelay Chrome has no attachable browser context")
                    self._context = contexts[0]

                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                self._track_page(self._page)
                return self.profile_path
            except BaseException as exc:
                await self._stop_partial_browser()
                if isinstance(exc, BrowserStartFailed):
                    raise
                if not isinstance(exc, Exception):
                    raise
                backend = self.settings.browser_backend.value
                raise BrowserStartFailed(f"Could not start {backend} browser: {type(exc).__name__}") from exc

    async def close(self) -> None:
        cleanup = asyncio.create_task(self._close_browser_resources())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            try:
                await cleanup
            finally:
                raise

    async def __aenter__(self) -> "ChatGPTWebAdapter":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _stop_partial_browser(self) -> None:
        await self._close_browser_resources()

    async def _close_browser_resources(self) -> None:
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        process = self._chrome_process
        self._cdp_endpoint = None
        self._page = None
        self._tracked_pages.clear()
        if self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP:
            if browser is not None:
                # Closing tabs individually prevents Chrome from restoring the authenticated reviewer tab.
                # Ask Chrome itself to exit cleanly while its restored tabs are still open.
                try:
                    session = await browser.new_browser_cdp_session()
                    await session.send("Browser.close")
                except Exception as exc:
                    _LOG.debug("ReviewRelay Chrome graceful browser close failed: %s", type(exc).__name__)
                try:
                    # This disconnects Playwright; Browser.close above owns the graceful Chrome shutdown.
                    await browser.close()
                except Exception as exc:
                    _LOG.debug("ReviewRelay Chrome CDP disconnect failed: %s", type(exc).__name__)
        elif context is not None:
            try:
                await context.close()
            except Exception as exc:
                _LOG.debug("Browser context close failed: %s", type(exc).__name__)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                _LOG.debug("Playwright shutdown failed: %s", type(exc).__name__)
        if process is not None:
            await self._stop_chrome_process(process)
            if process.poll() is not None:
                self._chrome_process = None
        profile_lock = self._profile_lock
        if profile_lock is not None and self._owns_profile_lock:
            if process is None or process.poll() is not None:
                profile_lock.release()
                self._profile_lock = None
                self._owns_profile_lock = False
            else:
                # Keep the lock in this adapter if Chrome did not exit; fail closed for reuse.
                self._profile_lock = profile_lock
                _LOG.warning("ReviewRelay profile lock retained because its Chrome process is still running")

    def _ensure_chrome_profile_lock(self) -> None:
        if self._profile_lock is not None and self._profile_lock.acquired:
            return
        if self.profile_path is None:
            raise BrowserStartFailed("ReviewRelay Chrome profile is not ready")
        if self._provided_profile_lock is not None:
            if (
                not self._provided_profile_lock.acquired
                or not self._provided_profile_lock.matches_profile(self.profile_path)
            ):
                raise BrowserStartFailed("The supplied ReviewRelay profile lock is missing or belongs to another profile")
            self._profile_lock = self._provided_profile_lock
            self._owns_profile_lock = False
            return
        lock = ReviewRelayProfileLock(self.data_root, self.settings.browser_profile)
        try:
            lock.acquire()
        except BrowserProfileInUseError as exc:
            raise BrowserStartFailed(str(exc)) from exc
        self._profile_lock = lock
        self._owns_profile_lock = True

    def _browser_is_connected(self) -> bool:
        if self._browser is None:
            return False
        try:
            return bool(self._browser.is_connected())
        except Exception:
            return False

    async def _start_or_reconnect_chrome(self) -> None:
        if self.profile_path is None or self._playwright is None:
            raise BrowserStartFailed("ReviewRelay Chrome profile is not ready")

        if self._chrome_process is None or self._chrome_process.poll() is not None:
            self._chrome_process = None
            self._cdp_endpoint = None
            active_port = self.profile_path / "DevToolsActivePort"
            self.data_root.assert_managed_path(active_port)
            active_port.unlink(missing_ok=True)
            try:
                self._chrome_process = launch_chrome(
                    find_google_chrome(),
                    self.profile_path,
                    mode=ChromeMode.AUTOMATION,
                )
            except Exception as exc:
                raise BrowserStartFailed(f"Could not launch the installed Google Chrome browser: {type(exc).__name__}") from exc

        deadline = asyncio.get_running_loop().time() + self.settings.timeouts.navigation_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._chrome_process is None or self._chrome_process.poll() is not None:
                raise BrowserStartFailed("ReviewRelay Google Chrome exited before its localhost CDP endpoint was ready")
            endpoint = read_chrome_cdp_endpoint(self.profile_path)
            if endpoint is not None:
                remaining_ms = max(1, int((deadline - asyncio.get_running_loop().time()) * 1000))
                try:
                    browser = await self._playwright.chromium.connect_over_cdp(
                        endpoint,
                        timeout=min(1000, remaining_ms),
                    )
                    if not browser.contexts:
                        await browser.close()
                        raise BrowserStartFailed("ReviewRelay Google Chrome exposed no default browser context")
                    self._browser = browser
                    self._cdp_endpoint = endpoint
                    return
                except BrowserStartFailed:
                    raise
                except Exception as exc:
                    last_error = exc
            await asyncio.sleep(0.1)
        raise BrowserStartFailed("ReviewRelay Google Chrome did not expose a usable localhost CDP endpoint") from last_error

    @staticmethod
    async def _stop_chrome_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if await request_chrome_shutdown(process, timeout_seconds=3):
            return
        if process.poll() is None:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=3)
                return
            except subprocess.TimeoutExpired:
                pass
            except OSError as exc:
                _LOG.debug("ReviewRelay Chrome graceful stop failed: %s", type(exc).__name__)
        if process.poll() is None:
            try:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _LOG.warning("ReviewRelay Chrome process did not stop cleanly: %s", type(exc).__name__)

    def _on_frame_navigated(self, frame: Any) -> None:
        try:
            if frame == self._page.main_frame:
                self._navigation_generation += 1
        except Exception:
            self._navigation_generation += 1

    def _track_page(self, page: Any) -> None:
        page_id = id(page)
        if page_id in self._tracked_pages:
            return
        page.on("framenavigated", self._on_frame_navigated)
        self._tracked_pages.add(page_id)

    async def open_task_conversation(self, conversation_url: str | None = None) -> str:
        target = self.settings.resolve_conversation_url(conversation_url)
        await self.start()
        if self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP:
            return await self._reuse_existing_chrome_conversation(target)
        last_error: Exception | None = None
        # A single bounded retry is safe here because no message has been sent.
        for attempt in range(2):
            try:
                response = await self.page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=self.settings.timeouts.navigation_seconds * 1000,
                )
                # Authentication UI takes precedence over the navigation status:
                # expired sessions can render a login page with a 4xx response.
                await self._raise_if_login_required()
                if response is not None and response.status >= 400:
                    raise ConversationNavigationFailed(f"Conversation navigation returned HTTP {response.status}")
                if not self._same_conversation(self.page.url, target):
                    await self._raise_if_login_required()
                    raise ConversationNavigationFailed("Browser did not remain on the configured conversation")
                composer = await self._find_composer()
                if composer is None:
                    raise ConversationNotReady(await self._diagnostic("configured conversation has no usable composer"))
                self._active_conversation_url = target
                return target
            except LoginRequired:
                # Keep the persistent context and visible browser alive for manual login.
                raise
            except (ConversationNotReady, ConversationNavigationFailed) as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.25)
        if isinstance(last_error, ConversationNotReady):
            raise last_error
        message = f"Could not open configured conversation: {type(last_error).__name__ if last_error else 'unknown'}"
        raise ConversationNavigationFailed(message) from last_error

    async def _reuse_existing_chrome_conversation(self, target: str) -> str:
        """Use only the exact tab restored by installed Chrome; never navigate it via CDP."""
        if self._context is None:
            raise BrowserStartFailed("ReviewRelay Chrome has no attached browser context")
        pages = [page for page in self._context.pages if not page.is_closed()]
        matching = [page for page in pages if self._same_conversation(page.url, target)]
        if len(matching) > 1:
            raise ConversationNavigationFailed("Multiple existing Chrome tabs match the configured conversation")
        if not matching:
            previous_page = self._page
            for page in pages:
                self._page = page
                try:
                    await self._raise_if_login_required()
                except LoginRequired:
                    raise
                except Exception:
                    continue
            self._page = previous_page
            raise ConversationNavigationFailed(
                "The configured conversation is not present in an existing ReviewRelay Chrome tab; "
                "open it manually in Auth Mode and close Chrome normally before Automation Mode"
            )

        self._page = matching[0]
        self._track_page(self._page)
        await self._raise_if_login_required()
        composer = await self._find_composer()
        if composer is None:
            raise ConversationNotReady(await self._diagnostic("restored conversation has no usable composer"))
        await self._wait_for_live_ui_ready()
        self._active_conversation_url = target
        return target

    async def _wait_for_live_ui_ready(self) -> None:
        """Wait for restored ChatGPT state to hydrate before inspecting drafts or sending."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self.settings.timeouts.navigation_seconds
        quiet_since: float | None = None
        last_snapshot: tuple[int, int, tuple[str, ...]] | None = None
        while asyncio.get_running_loop().time() < deadline:
            await self._raise_if_login_required()
            loading = False
            statuses = self.page.locator("[role='status']")
            try:
                for index in range(await statuses.count()):
                    status = statuses.nth(index)
                    if not await status.is_visible():
                        continue
                    label = (await status.inner_text()).strip().lower()
                    if label.startswith(("loading conversation", "loading message", "loading older message")):
                        loading = True
                        break
            except Exception:
                loading = True
            if loading:
                quiet_since = None
                last_snapshot = None
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            snapshot = (
                await self.page.locator(self.selectors.user_turns).count(),
                await self.page.locator(self.selectors.assistant_turns).count(),
                (
                    await self._visible_composer_attachment_count()
                    if self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP
                    else tuple(await self._visible_attachment_names())
                ),
            )
            now = asyncio.get_running_loop().time()
            if snapshot != last_snapshot:
                quiet_since = now
                last_snapshot = snapshot
            elif (
                quiet_since is not None
                and now - quiet_since >= 0.5
                and now - started >= 2.0
            ):
                return
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        raise ConversationNotReady(await self._diagnostic("restored conversation UI did not finish loading"))

    async def send_review_pack(
        self,
        *,
        prompt: str,
        review_key: str,
        attachment_paths: Sequence[str | Path] = (),
        conversation_url: str | None = None,
    ) -> SendResult:
        return await self._send(
            message_kind="review_pack",
            prompt=prompt,
            review_key=review_key,
            attachment_paths=attachment_paths,
            conversation_url=conversation_url,
        )

    async def send_evidence(
        self,
        *,
        prompt: str,
        review_key: str,
        attachment_paths: Sequence[str | Path] = (),
        conversation_url: str | None = None,
    ) -> SendResult:
        return await self._send(
            message_kind="evidence",
            prompt=prompt,
            review_key=review_key,
            attachment_paths=attachment_paths,
            conversation_url=conversation_url,
        )

    async def _send(
        self,
        *,
        message_kind: str,
        prompt: str,
        review_key: str,
        attachment_paths: Sequence[str | Path],
        conversation_url: str | None,
    ) -> SendResult:
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
            raise MessageSendFailed("Message prompt must be non-empty text")
        if not isinstance(review_key, str) or not review_key.strip():
            raise MessageSendFailed("A non-empty review_key is required for send idempotency")
        target = self.settings.resolve_conversation_url(conversation_url)
        files = self._validate_attachments(attachment_paths)
        signature = self._request_signature(target, message_kind, prompt, files)

        async with self._send_lock:
            previous = self._send_registry.get(review_key)
            if previous is not None:
                previous_signature, prior_result = previous
                if previous_signature != signature:
                    raise ReviewKeyConflict(f"review_key {review_key!r} was already used for a different request")
                self._send_registry.move_to_end(review_key)
                if prior_result.disposition is SendDisposition.SEND_AMBIGUOUS:
                    raise MessageSendAmbiguous("A prior send with this review_key remains ambiguous; it was not retried", send_result=prior_result)
                return prior_result

            await self.open_task_conversation(target)
            baseline = await self._capture_baseline()
            if await self._has_visible_stop_button():
                raise ConversationNotReady("The configured conversation is already generating a response")
            composer_entry = await self._require_composer()
            initial_composer_text = await self._read_composer_text(composer_entry[1])
            if initial_composer_text.strip():
                raise ReviewerConversationChanged("The configured composer contains an Owner draft; it was preserved and not overwritten")
            has_existing_attachments = (
                await self._visible_composer_attachment_count()
                if self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP
                else bool(await self._visible_attachment_names())
            )
            if has_existing_attachments:
                raise ReviewerConversationChanged("The composer already contains an attachment; it was preserved and not sent")
            await self._upload_attachments(files)
            composer_entry = await self._require_composer()
            composer_name, composer = composer_entry
            if await self._read_composer_text(composer) != initial_composer_text:
                raise ReviewerConversationChanged("Composer content changed during attachment preparation")
            try:
                await composer.fill(prompt)
                current_value = await self._read_composer_text(composer)
            except Exception as exc:
                raise MessageSendFailed(f"Could not fill the intended composer ({composer_name})") from exc
            if current_value != prompt:
                raise MessageSendFailed("Composer content did not match the requested prompt before send")

            current_baseline = await self._capture_baseline()
            if current_baseline != baseline or baseline.navigation_generation != self._navigation_generation:
                raise ReviewerConversationChanged("Conversation navigated while preparing the message")
            send_button_entry = await first_visible_enabled(self.selectors.send_button_candidates(self.page))
            if send_button_entry is None:
                raise MessageSendFailed(await self._diagnostic("no enabled send button is available"))
            if await self._read_composer_text(composer) != prompt:
                raise ReviewerConversationChanged("Composer content changed immediately before send")

            sent_at = _utc_now()
            pending = SendResult(
                review_key=review_key,
                conversation_url=target,
                sent_at=sent_at,
                message_kind=message_kind,
                attachment_paths=tuple(str(item) for item in files),
                prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                pre_send_baseline=baseline,
                user_turn_identity="pending",
                disposition=SendDisposition.SEND_AMBIGUOUS,
            )
            try:
                await send_button_entry[1].click()
            except Exception as exc:
                ambiguous = replace(pending, disposition=SendDisposition.SEND_AMBIGUOUS)
                self._remember(review_key, signature, ambiguous)
                raise MessageSendAmbiguous(
                    f"Send click outcome is unknown ({type(exc).__name__}); automatic retry is blocked",
                    send_result=ambiguous,
                ) from exc

            try:
                user_identity = await self._confirm_sent_user_turn(prompt, baseline)
            except ReviewerConversationChanged as exc:
                ambiguous = replace(pending, disposition=SendDisposition.SEND_AMBIGUOUS)
                self._remember(review_key, signature, ambiguous)
                raise ReviewerConversationChanged(
                    str(exc), send_result=ambiguous, send_state=SendDisposition.SEND_AMBIGUOUS.value
                ) from exc
            except MessageSendAmbiguous as exc:
                ambiguous = replace(pending, disposition=SendDisposition.SEND_AMBIGUOUS)
                self._remember(review_key, signature, ambiguous)
                raise MessageSendAmbiguous(str(exc), send_result=ambiguous) from exc
            except Exception as exc:
                ambiguous = replace(pending, disposition=SendDisposition.SEND_AMBIGUOUS)
                self._remember(review_key, signature, ambiguous)
                raise MessageSendAmbiguous(
                    f"Could not determine whether the message was sent ({type(exc).__name__}); automatic retry is blocked",
                    send_result=ambiguous,
                ) from exc
            result = replace(pending, user_turn_identity=user_identity, disposition=SendDisposition.SEND_CONFIRMED)
            self._remember(review_key, signature, result)
            return result

    async def _confirm_sent_user_turn(self, prompt: str, baseline: TurnBaseline) -> str:
        deadline = asyncio.get_running_loop().time() + self.settings.timeouts.navigation_seconds
        while asyncio.get_running_loop().time() < deadline:
            self._assert_conversation_unchanged(baseline.navigation_generation, self._active_conversation_url)
            turns = await self._read_turns("user")
            new_turns = [(identity, text) for identity, text in turns if identity not in baseline.user_turn_ids]
            if len(new_turns) > 1:
                raise ReviewerConversationChanged("More than one user turn appeared during relay send")
            if len(new_turns) == 1:
                if _normalize_turn_text(new_turns[0][1]) != _normalize_turn_text(prompt):
                    raise ReviewerConversationChanged("A different user turn appeared after relay send")
                return new_turns[0][0]
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        ambiguous = SendResult(
            review_key="",
            conversation_url=self._active_conversation_url or "",
            sent_at=_utc_now(),
            message_kind="unknown",
            attachment_paths=(),
            prompt_sha256="",
            pre_send_baseline=baseline,
            user_turn_identity="pending",
            disposition=SendDisposition.SEND_AMBIGUOUS,
        )
        raise MessageSendAmbiguous("Click completed but no matching user turn appeared before the bounded confirmation timeout", send_result=ambiguous)

    async def wait_response(
        self,
        send_result: SendResult,
        *,
        timeout_seconds: float | None = None,
    ) -> AssistantResponse:
        if not isinstance(send_result, SendResult) or send_result.disposition not in {
            SendDisposition.SEND_CONFIRMED,
            SendDisposition.RESPONSE_RECEIVED,
        }:
            raise MessageSendFailed("wait_response requires a confirmed SendResult")
        cached = self._responses.get(send_result.review_key)
        if cached is not None:
            return cached
        if not any(
            key == send_result.review_key and stored == send_result
            for key, (_, stored) in self._send_registry.items()
        ):
            raise MessageSendFailed("SendResult is not owned by this adapter instance")
        duration = self.settings.timeouts.response_seconds if timeout_seconds is None else timeout_seconds
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(float(duration)) or duration <= 0 or duration > 3600:
            raise ValueError("timeout_seconds must be finite and in (0, 3600]")

        deadline = asyncio.get_running_loop().time() + float(duration)
        saw_streaming = False
        previous_text: str | None = None
        stable_since: float | None = None
        while asyncio.get_running_loop().time() < deadline:
            self._assert_conversation_unchanged(
                send_result.pre_send_baseline.navigation_generation,
                send_result.conversation_url,
                send_result=send_result,
            )
            user_turns = await self._read_turns("user")
            new_users = [(identity, text) for identity, text in user_turns if identity not in send_result.pre_send_baseline.user_turn_ids]
            if len(new_users) != 1 or new_users[0][0] != send_result.user_turn_identity:
                raise ReviewerConversationChanged("Relay message ownership changed while waiting for response", send_result=send_result)
            assistant_turns = await self._read_turns("assistant")
            new_assistants = [(identity, text) for identity, text in assistant_turns if identity not in send_result.pre_send_baseline.assistant_turn_ids]
            if len(new_assistants) > 1:
                raise AmbiguousResponse("Multiple new assistant turns appeared for one relay message", send_result=send_result)
            if new_assistants:
                identity, text = new_assistants[0]
                if text != previous_text:
                    if previous_text is not None:
                        saw_streaming = True
                    previous_text = text
                    stable_since = asyncio.get_running_loop().time()
                if await self._has_visible_stop_button():
                    saw_streaming = True
                    stable_since = None
                else:
                    composer = await self._find_composer()
                    send_button = await first_visible(self.selectors.send_button_candidates(self.page))
                    composer_ready = composer is not None and send_button is not None
                    now = asyncio.get_running_loop().time()
                    if composer_ready and stable_since is not None and now - stable_since >= self.settings.timeouts.stability_seconds:
                        if not text.strip():
                            raise ResponseExtractionFailed("The new assistant turn is empty", send_result=send_result)
                        response = AssistantResponse(
                            review_key=send_result.review_key,
                            conversation_url=send_result.conversation_url,
                            text=text,
                            sent_at=send_result.sent_at,
                            received_at=_utc_now(),
                            assistant_turn_identity=identity,
                            saw_streaming=saw_streaming,
                        )
                        self._responses[send_result.review_key] = response
                        stored = self._send_registry.get(send_result.review_key)
                        if stored is not None:
                            self._send_registry[send_result.review_key] = (
                                stored[0], replace(stored[1], disposition=SendDisposition.RESPONSE_RECEIVED)
                            )
                        return response
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        raise ResponseTimeout(
            f"No complete owned assistant response arrived within {float(duration):g} seconds",
            send_result=send_result,
        )

    async def get_latest_response(self, send_result: SendResult) -> AssistantResponse:
        """Return only the response owned by send_result; never a prior chat turn."""
        return await self.wait_response(send_result)

    def _validate_attachments(self, attachment_paths: Sequence[str | Path]) -> tuple[Path, ...]:
        if isinstance(attachment_paths, (str, Path)):
            raise AttachmentNotAllowed("attachment_paths must be an explicit sequence of file paths")
        try:
            supplied = tuple(attachment_paths)
        except TypeError as exc:
            raise AttachmentNotAllowed("attachment_paths must be an explicit sequence of file paths") from exc
        if len(supplied) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise AttachmentLimitExceeded(f"At most {MAX_ATTACHMENTS_PER_MESSAGE} attachments are allowed")
        active_root = self.data_root.path.resolve(strict=False) / "active"
        managed_root = self.data_root.path.resolve(strict=False)
        files: list[Path] = []
        total = 0
        for raw_path in supplied:
            if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
                raise AttachmentNotAllowed("Every attachment must be an explicit non-empty local path")
            candidate = Path(raw_path).expanduser()
            try:
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise AttachmentNotFound(f"Attachment does not exist: {candidate}") from exc
            try:
                resolved.relative_to(managed_root)
                relative = resolved.relative_to(active_root.resolve(strict=False))
            except ValueError as exc:
                raise AttachmentNotAllowed("Attachments must be inside a managed active task scratch directory") from exc
            if len(relative.parts) < 4 or relative.parts[2] != "scratch":
                raise AttachmentNotAllowed("Attachments must be under active/<project>/<task>/scratch/")
            if self.project_id is not None and relative.parts[:2] != (self.project_id, self.task_id):
                raise AttachmentNotAllowed("Attachment does not belong to this adapter's configured active task")
            if not resolved.is_file():
                raise AttachmentNotFound(f"Attachment is not a regular file: {candidate}")
            size = resolved.stat().st_size
            if size > MAX_SINGLE_ATTACHMENT_BYTES:
                raise AttachmentLimitExceeded(f"Attachment exceeds {MAX_SINGLE_ATTACHMENT_BYTES} bytes: {resolved.name}")
            total += size
            if total > MAX_TOTAL_ATTACHMENT_BYTES:
                raise AttachmentLimitExceeded(f"Attachments exceed {MAX_TOTAL_ATTACHMENT_BYTES} total bytes")
            files.append(resolved)
        return tuple(files)

    def _request_signature(self, target: str, kind: str, prompt: str, files: tuple[Path, ...]) -> str:
        digest = hashlib.sha256()
        for part in (target.encode(), kind.encode(), prompt.encode("utf-8")):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        for path in files:
            digest.update(str(path).encode("utf-8"))
            digest.update(path.stat().st_size.to_bytes(8, "big"))
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            digest.update(file_hash.digest())
        return digest.hexdigest()

    def _remember(self, key: str, signature: str, result: SendResult) -> None:
        self._send_registry[key] = (signature, result)
        self._send_registry.move_to_end(key)

    async def _upload_attachments(self, files: tuple[Path, ...]) -> None:
        if not files:
            return
        live_count_based = self.settings.browser_backend is BrowserBackend.GOOGLE_CHROME_CDP
        initial_attachment_count = await self._visible_composer_attachment_count() if live_count_based else 0
        if live_count_based and initial_attachment_count:
            raise ReviewerConversationChanged("The composer already contains an attachment; it was preserved and not sent")
        locator = self.page.locator(self.selectors.file_inputs)
        try:
            count = await locator.count()
            if count == 0:
                attach_button = await self._wait_for_attachment_button()
                if attach_button is not None:
                    await attach_button[1].click()
                    try:
                        await locator.first.wait_for(
                            state="attached",
                            timeout=max(1, int(min(3, self.settings.timeouts.upload_seconds) * 1000)),
                        )
                    except Exception:
                        pass
                    locator = self.page.locator(self.selectors.file_inputs)
                    count = await locator.count()
            if count == 0:
                raise AttachmentUploadFailed(await self._diagnostic("normal file input was not found"))
            file_input = locator.nth(0)
            multiple = await file_input.get_attribute("multiple")
            if len(files) > 1 and multiple is None:
                raise AttachmentUploadFailed("The active UI file input does not accept multiple explicit attachments")
            if live_count_based:
                await file_input.evaluate(
                    """(el, expectedNames) => {
                        window.__reviewrelayFileSelectionCheckV1 = { seen: false, count: 0, matches: false };
                        el.addEventListener("change", event => {
                            const selectedNames = Array.from(event.currentTarget.files || []).map(file => file.name);
                            window.__reviewrelayFileSelectionCheckV1 = {
                                seen: true,
                                count: selectedNames.length,
                                matches: selectedNames.length === expectedNames.length &&
                                    selectedNames.every((name, index) => name === expectedNames[index]),
                            };
                        }, { capture: true, once: true });
                    }""",
                    [path.name for path in files],
                )
            await file_input.set_input_files([str(path) for path in files])
            if live_count_based:
                selection_check = await self.page.evaluate(
                    "() => window.__reviewrelayFileSelectionCheckV1 || null"
                )
                if (
                    not selection_check
                    or not selection_check.get("seen")
                    or selection_check.get("count") != len(files)
                    or not selection_check.get("matches")
                ):
                    raise AttachmentUploadFailed("The selected local files did not match the explicitly requested attachments")
        except AttachmentUploadFailed:
            raise
        except Exception as exc:
            raise AttachmentUploadFailed(f"Normal UI file upload could not be initiated ({type(exc).__name__})") from exc

        deadline = asyncio.get_running_loop().time() + self.settings.timeouts.upload_seconds
        pending_names = [path.name for path in files]
        while asyncio.get_running_loop().time() < deadline:
            if await self._visible_upload_error():
                raise AttachmentUploadFailed("ChatGPT UI reported an attachment upload error")
            if live_count_based:
                ready_count = await self._visible_composer_attachment_count()
                uploading_count = await self._visible_composer_upload_progress_count()
                if ready_count == len(files) and uploading_count == 0:
                    return
                if ready_count > len(files) or uploading_count > len(files):
                    raise ReviewerConversationChanged("Unexpected attachment count appeared during upload")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue
            ready_names = await self._visible_attachment_names()
            uploading_names = await self._visible_upload_progress_names()
            if ready_names == pending_names and not set(pending_names).intersection(uploading_names):
                return
            if ready_names and ready_names != pending_names[:len(ready_names)]:
                raise ReviewerConversationChanged("Unexpected or reordered attachment appeared during upload")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        raise AttachmentUploadFailed("Attachment did not become visibly ready before the upload timeout")

    async def _wait_for_attachment_button(self) -> tuple[str, Any] | None:
        candidates = self.selectors.attachment_button_candidates(self.page)
        deadline = asyncio.get_running_loop().time() + min(5, self.settings.timeouts.upload_seconds)
        while True:
            result = await first_visible_enabled(candidates)
            if result is not None:
                return result
            present = False
            for _, locator in candidates:
                try:
                    present = present or await locator.count() > 0
                except Exception:
                    continue
            if not present or asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _visible_attachment_names(self) -> list[str]:
        names: list[str] = []
        for selector in self.selectors.attachment_chips:
            locator = self.page.locator(selector)
            try:
                for index in range(await locator.count()):
                    item = locator.nth(index)
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text()).strip()
                    title = await item.get_attribute("title")
                    aria_label = await item.get_attribute("aria-label")
                    value = text or title
                    if not value and aria_label and aria_label.startswith("Remove "):
                        value = aria_label.removeprefix("Remove ").strip()
                    if value and value not in names:
                        names.append(value)
            except Exception:
                continue
        return names

    async def _visible_composer_attachment_count(self) -> int:
        """Count visible composer chips without reading their labels or historical message attachments."""
        for selector in ("[data-testid='attachment-chip']", "button[aria-label^='Remove ']" ):
            locator = self.page.locator(selector)
            visible_count = 0
            try:
                for index in range(await locator.count()):
                    item = locator.nth(index)
                    if not await item.is_visible():
                        continue
                    if await item.evaluate("el => !!el.closest('[data-message-author-role]')"):
                        continue
                    visible_count += 1
            except Exception:
                continue
            if visible_count:
                return visible_count
        return 0

    async def _visible_composer_upload_progress_count(self) -> int:
        locator = self.page.locator("[role='progressbar'][aria-label^='Uploading ']")
        visible_count = 0
        try:
            for index in range(await locator.count()):
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                if await item.evaluate("el => !!el.closest('[data-message-author-role]')"):
                    continue
                visible_count += 1
        except Exception:
            return visible_count
        return visible_count

    async def _visible_upload_progress_names(self) -> list[str]:
        locator = self.page.locator("[role='progressbar'][aria-label^='Uploading ']")
        names: list[str] = []
        try:
            for index in range(await locator.count()):
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                label = await item.get_attribute("aria-label")
                if label and label.startswith("Uploading "):
                    filename = label.removeprefix("Uploading ").strip()
                    if filename and filename not in names:
                        names.append(filename)
        except Exception:
            return names
        return names

    async def _visible_upload_error(self) -> bool:
        for selector in self.selectors.upload_errors:
            locator = self.page.locator(selector)
            try:
                for index in range(await locator.count()):
                    item = locator.nth(index)
                    if await item.is_visible():
                        value = (await item.inner_text()).lower()
                        if any(token in value for token in ("upload", "failed", "error", "too large", "could not")):
                            return True
            except Exception:
                continue
        return False

    async def _find_composer(self) -> tuple[str, Any] | None:
        return await first_visible_enabled(self.selectors.composer_candidates(self.page), editable=True)

    async def _require_composer(self) -> tuple[str, Any]:
        result = await self._find_composer()
        if result is None:
            raise ComposerNotFound(await self._diagnostic("no visible enabled editable message composer was found"))
        return result

    async def _read_composer_text(self, composer: Any) -> str:
        if await composer.evaluate("el => 'value' in el"):
            return await composer.input_value()
        return await composer.evaluate(
            """el => {
                const blocks = Array.from(el.children);
                const blockTags = new Set(["P", "DIV", "LI", "PRE", "BLOCKQUOTE"]);
                if (blocks.length && blocks.every(block => blockTags.has(block.tagName))) {
                    return blocks
                        .map(block => (block.innerText ?? block.textContent ?? "")
                            .replace(/\\r\\n?/g, "\\n")
                            .replace(/\\n+$/g, ""))
                        .join("\\n");
                }
                return el.innerText ?? "";
            }"""
        )

    async def _raise_if_login_required(self) -> None:
        current_url = self.page.url
        if _LOGIN_PATH.search(urlsplit(current_url).path):
            raise LoginRequired("ChatGPT requires manual login in the persistent ReviewRelay browser profile")
        for name in ("Log in", "Log In", "Sign up", "Sign in"):
            try:
                for role in ("button", "link"):
                    locator = self.page.get_by_role(role, name=name, exact=True)
                    if await locator.count() and await locator.first.is_visible():
                        raise LoginRequired("ChatGPT requires manual login in the persistent ReviewRelay browser profile")
            except LoginRequired:
                raise
            except Exception:
                continue

    async def _capture_baseline(self) -> TurnBaseline:
        return TurnBaseline(
            user_turn_ids=tuple(identity for identity, _ in await self._read_turns("user")),
            assistant_turn_ids=tuple(identity for identity, _ in await self._read_turns("assistant")),
            navigation_generation=self._navigation_generation,
        )

    async def _read_turns(self, role: str) -> list[tuple[str, str]]:
        selector = self.selectors.user_turns if role == "user" else self.selectors.assistant_turns
        locator = self.page.locator(selector)
        result: list[tuple[str, str]] = []
        try:
            count = await locator.count()
            for index in range(count):
                element = locator.nth(index)
                if not await element.is_visible():
                    continue
                text = await element.inner_text()
                identity = (
                    await element.get_attribute("data-message-id")
                    or await element.get_attribute("id")
                    or f"{role}:{index}"
                )
                result.append((identity, text))
        except Exception as exc:
            raise ReviewerConversationChanged(f"Could not inspect visible {role} turns ({type(exc).__name__})") from exc
        return result

    async def _has_visible_stop_button(self) -> bool:
        for _, locator in self.selectors.stop_button_candidates(self.page):
            try:
                if await locator.count() and await locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _assert_conversation_unchanged(
        self,
        expected_generation: int,
        expected_url: str | None,
        *,
        send_result: SendResult | None = None,
    ) -> None:
        if (
            expected_url is None
            or not self._same_conversation(self.page.url, expected_url)
            or self._navigation_generation != expected_generation
        ):
            raise ReviewerConversationChanged(
                "Browser navigated or reloaded after the relay message was sent",
                send_result=send_result,
            )

    async def _diagnostic(self, reason: str) -> str:
        counts = await diagnostic_counts(self.selectors.composer_candidates(self.page))
        send_candidates = await diagnostic_counts(self.selectors.send_button_candidates(self.page))
        try:
            file_input_count = await self.page.locator(self.selectors.file_inputs).count()
        except Exception:
            file_input_count = "unavailable"
        current_url = _url_for_log(self.page.url)
        _LOG.warning(
            "ChatGPT UI diagnosis: reason=%s url=%s composer=%s send=%s file_inputs=%s",
            reason,
            current_url,
            counts,
            send_candidates,
            file_input_count,
        )
        return f"{reason}; url={current_url}; composer={counts}; send={send_candidates}; file_inputs={file_input_count}"

    @staticmethod
    def _same_conversation(actual: str, expected: str) -> bool:
        a, e = urlsplit(actual), urlsplit(expected)
        return a.scheme == e.scheme and a.netloc.lower() == e.netloc.lower() and a.path.rstrip("/") == e.path.rstrip("/") and a.query == e.query


def _normalize_turn_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()
