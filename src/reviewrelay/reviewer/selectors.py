"""Centralized, UI-only locator strategies for the ChatGPT web page."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatGPTSelectors:
    """Small selector catalog; accessible names precede stable DOM fallbacks."""

    user_turns: str = "[data-message-author-role='user'], main [class~='bg-user-message']"
    assistant_turns: str = (
        "[data-message-author-role='assistant'], "
        "main [class~='group'][class~='min-w-0'][class~='flex-col']:has([class*='MarkdownRoot-'])"
    )
    file_inputs: str = "input[type='file'][aria-label='Attach files']"
    attachment_chips: tuple[str, ...] = (
        "[data-testid='attachment-chip']",
        "button[aria-label^='Remove ']",
    )
    upload_errors: tuple[str, ...] = (
        "[data-testid='upload-error']",
        "[role='alert']",
    )
    stop_buttons: tuple[str, ...] = (
        "[data-testid='stop-button']",
        "button[aria-label='Stop generating']",
    )
    send_buttons: tuple[str, ...] = (
        "Send message",
        "Send",
        "Send prompt",
    )
    attachment_buttons: tuple[str, ...] = ("Add files and more", "Attach files", "Add files", "Upload file")

    def composer_candidates(self, page: Any) -> tuple[tuple[str, Any], ...]:
        return (
            ("role=textbox[name=Ask ChatGPT]", page.get_by_role("textbox", name="Ask ChatGPT", exact=True)),
            ("role=textbox[name=Message ChatGPT]", page.get_by_role("textbox", name="Message ChatGPT", exact=True)),
            ("role=textbox[name=Message]", page.get_by_role("textbox", name="Message", exact=True)),
            ("testid=prompt-textarea", page.get_by_test_id("prompt-textarea")),
            ("textarea[placeholder=Message ChatGPT]", page.locator("textarea[placeholder='Message ChatGPT']")),
            ("contenteditable[role=textbox][aria-label=Message ChatGPT]", page.locator("[contenteditable='true'][role='textbox'][aria-label='Message ChatGPT']")),
        )

    def send_button_candidates(self, page: Any) -> tuple[tuple[str, Any], ...]:
        candidates = tuple(
            (f"role=button[name={name}]", page.get_by_role("button", name=name, exact=True))
            for name in self.send_buttons
        )
        return candidates + (("testid=send-button", page.get_by_test_id("send-button")),)

    def stop_button_candidates(self, page: Any) -> tuple[tuple[str, Any], ...]:
        return tuple((selector, page.locator(selector)) for selector in self.stop_buttons) + (
            ("role=button[name=Stop generating]", page.get_by_role("button", name="Stop generating", exact=True)),
        )

    def attachment_button_candidates(self, page: Any) -> tuple[tuple[str, Any], ...]:
        return tuple(
            (f"role=button[name={name}]", page.get_by_role("button", name=name, exact=True))
            for name in self.attachment_buttons
        )


async def first_visible_enabled(candidates: tuple[tuple[str, Any], ...], *, editable: bool = False) -> tuple[str, Any] | None:
    for logical_name, locator in candidates:
        try:
            count = await locator.count()
            for index in range(count):
                item = locator.nth(index)
                if not await item.is_visible() or not await item.is_enabled():
                    continue
                if editable and not await item.is_editable():
                    continue
                if editable:
                    semantically_editable = await item.evaluate(
                        "el => el.getAttribute('contenteditable')?.toLowerCase() !== 'false' && !el.hasAttribute('readonly')"
                    )
                    if not semantically_editable:
                        continue
                return logical_name, item
        except Exception as exc:  # Playwright can detach a locator during UI navigation.
            _LOG.debug("Ignoring transient locator error for %s: %s", logical_name, type(exc).__name__)
    return None


async def first_visible(candidates: tuple[tuple[str, Any], ...]) -> tuple[str, Any] | None:
    """Return a visible control even when the UI disables it for an empty composer."""
    for logical_name, locator in candidates:
        try:
            for index in range(await locator.count()):
                item = locator.nth(index)
                if await item.is_visible():
                    return logical_name, item
        except Exception as exc:
            _LOG.debug("Ignoring transient locator error for %s: %s", logical_name, type(exc).__name__)
    return None


async def diagnostic_counts(candidates: tuple[tuple[str, Any], ...]) -> str:
    counts: list[str] = []
    for name, locator in candidates:
        try:
            count = await locator.count()
            visible = 0
            for index in range(count):
                try:
                    visible += int(await locator.nth(index).is_visible())
                except Exception:
                    continue
            counts.append(f"{name}=count:{count},visible:{visible}")
        except Exception:
            counts.append(f"{name}=unavailable")
    return ", ".join(counts)
