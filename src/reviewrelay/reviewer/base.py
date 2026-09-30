"""Transport-neutral reviewer boundary and ChatGPT web transport value types."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from ..config import validate_identifier
from ..errors import PathSafetyError
from .errors import ReviewerConfigurationError


DEFAULT_CHATGPT_BASE_URL = "https://chatgpt.com/"
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_SINGLE_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ChatGPTTimeouts:
    navigation_seconds: float = 30
    upload_seconds: float = 60
    response_seconds: float = 600
    stability_seconds: float = 2

    def __post_init__(self) -> None:
        _validate_timeout("navigation_seconds", self.navigation_seconds, maximum=300)
        _validate_timeout("upload_seconds", self.upload_seconds, maximum=900)
        _validate_timeout("response_seconds", self.response_seconds, maximum=3600)
        _validate_timeout("stability_seconds", self.stability_seconds, maximum=10, allow_zero=True)

    @classmethod
    def from_mapping(cls, value: object) -> "ChatGPTTimeouts":
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise ReviewerConfigurationError("chatgpt.timeouts must be a mapping")
        allowed = {"navigation_seconds", "upload_seconds", "response_seconds", "stability_seconds"}
        extra = set(value) - allowed
        if extra:
            raise ReviewerConfigurationError("Unknown chatgpt timeout(s): " + ", ".join(sorted(extra)))
        defaults = cls()
        return cls(
            navigation_seconds=value.get("navigation_seconds", defaults.navigation_seconds),
            upload_seconds=value.get("upload_seconds", defaults.upload_seconds),
            response_seconds=value.get("response_seconds", defaults.response_seconds),
            stability_seconds=value.get("stability_seconds", defaults.stability_seconds),
        )


class BrowserBackend(str, Enum):
    """Browser engine used by the web adapter."""

    PLAYWRIGHT_CHROMIUM = "playwright-chromium"
    GOOGLE_CHROME_CDP = "google-chrome-cdp"


@dataclass(frozen=True)
class ChatGPTWebSettings:
    base_url: str = DEFAULT_CHATGPT_BASE_URL
    browser_profile: str = "default"
    conversation_url: str | None = None
    headless: bool = False
    browser_backend: BrowserBackend = BrowserBackend.PLAYWRIGHT_CHROMIUM
    timeouts: ChatGPTTimeouts = field(default_factory=ChatGPTTimeouts)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validate_base_url(self.base_url))
        try:
            validate_identifier(self.browser_profile, "browser_profile")
        except PathSafetyError as exc:
            raise ReviewerConfigurationError(str(exc)) from exc
        if not isinstance(self.headless, bool):
            raise ReviewerConfigurationError("chatgpt.headless must be a boolean")
        try:
            backend = BrowserBackend(self.browser_backend)
        except (TypeError, ValueError) as exc:
            raise ReviewerConfigurationError("chatgpt.browser_backend must be a supported browser backend") from exc
        object.__setattr__(self, "browser_backend", backend)
        if backend is BrowserBackend.GOOGLE_CHROME_CDP and self.headless:
            raise ReviewerConfigurationError("google-chrome-cdp requires a visible browser for Owner authentication")
        if not isinstance(self.timeouts, ChatGPTTimeouts):
            raise ReviewerConfigurationError("chatgpt.timeouts must be ChatGPTTimeouts")
        if self.conversation_url is not None:
            _validate_conversation_url(self.conversation_url, self.base_url)

    @classmethod
    def from_mapping(cls, value: object) -> "ChatGPTWebSettings":
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
            raise ReviewerConfigurationError("chatgpt configuration must be a mapping")
        allowed = {"base_url", "browser_profile", "conversation_url", "headless", "browser_backend", "timeouts"}
        extra = set(value) - allowed
        if extra:
            raise ReviewerConfigurationError("Unknown ChatGPT web setting(s): " + ", ".join(sorted(extra)))
        conversation_url = value.get("conversation_url")
        if conversation_url is not None and not isinstance(conversation_url, str):
            raise ReviewerConfigurationError("chatgpt.conversation_url must be text")
        settings = cls(
            base_url=value.get("base_url", DEFAULT_CHATGPT_BASE_URL),
            browser_profile=value.get("browser_profile", "default"),
            conversation_url=conversation_url,
            headless=value.get("headless", False),
            browser_backend=value.get("browser_backend", BrowserBackend.PLAYWRIGHT_CHROMIUM),
            timeouts=ChatGPTTimeouts.from_mapping(value.get("timeouts")),
        )
        return settings

    @classmethod
    def from_project_config(cls, config: object) -> "ChatGPTWebSettings":
        raw = getattr(config, "chatgpt", None)
        return cls.from_mapping(raw)

    def resolve_conversation_url(self, value: str | None = None) -> str:
        selected = value if value is not None else self.conversation_url
        if selected is None:
            raise ReviewerConfigurationError("An explicit existing ChatGPT conversation_url is required")
        return _validate_conversation_url(selected, self.base_url)


class SendDisposition(str, Enum):
    NOT_SENT = "NOT_SENT"
    SEND_CONFIRMED = "SEND_CONFIRMED"
    SEND_AMBIGUOUS = "SEND_AMBIGUOUS"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"


@dataclass(frozen=True)
class TurnBaseline:
    user_turn_ids: tuple[str, ...]
    assistant_turn_ids: tuple[str, ...]
    navigation_generation: int


@dataclass(frozen=True)
class SendResult:
    review_key: str
    conversation_url: str
    sent_at: str
    message_kind: str
    attachment_paths: tuple[str, ...]
    prompt_sha256: str
    pre_send_baseline: TurnBaseline
    user_turn_identity: str
    disposition: SendDisposition = SendDisposition.SEND_CONFIRMED


@dataclass(frozen=True)
class AssistantResponse:
    review_key: str
    conversation_url: str
    text: str
    sent_at: str
    received_at: str
    assistant_turn_identity: str
    saw_streaming: bool
    disposition: SendDisposition = SendDisposition.RESPONSE_RECEIVED


class ReviewerAdapter(Protocol):
    async def open_task_conversation(self, conversation_url: str | None = None) -> str: ...

    async def send_review_pack(
        self,
        *,
        prompt: str,
        review_key: str,
        attachment_paths: Sequence[str | Path] = (),
        conversation_url: str | None = None,
    ) -> SendResult: ...

    async def send_evidence(
        self,
        *,
        prompt: str,
        review_key: str,
        attachment_paths: Sequence[str | Path] = (),
        conversation_url: str | None = None,
    ) -> SendResult: ...

    async def wait_response(self, send_result: SendResult, *, timeout_seconds: float | None = None) -> AssistantResponse: ...

    async def get_latest_response(self, send_result: SendResult) -> AssistantResponse: ...


def _validate_timeout(name: str, value: object, *, maximum: float, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ReviewerConfigurationError(f"chatgpt.timeouts.{name} must be a finite number")
    minimum = 0 if allow_zero else 0.001
    if value < minimum or value > maximum:
        raise ReviewerConfigurationError(f"chatgpt.timeouts.{name} must be between {minimum} and {maximum} seconds")


def _validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewerConfigurationError("chatgpt.base_url must be a non-empty URL")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ReviewerConfigurationError("chatgpt.base_url is malformed") from exc
    hostname = (parts.hostname or "").lower()
    local_http = parts.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parts.scheme != "https" and not local_http
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ReviewerConfigurationError("chatgpt.base_url must be HTTPS (HTTP is allowed only for loopback fixtures)")
    path = parts.path.rstrip("/") + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _validate_conversation_url(value: object, base_url: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewerConfigurationError("conversation_url must be a non-empty absolute URL")
    try:
        parts = urlsplit(value)
        base = urlsplit(base_url)
    except ValueError as exc:
        raise ReviewerConfigurationError("conversation_url is malformed") from exc
    if (
        parts.scheme.lower() != base.scheme.lower()
        or parts.netloc.lower() != base.netloc.lower()
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ReviewerConfigurationError("conversation_url must stay on the configured ChatGPT origin")
    segments = [segment for segment in parts.path.split("/") if segment]
    is_conversation = len(segments) >= 2 and segments[0] == "c" and bool(segments[1])
    is_gpt_conversation = len(segments) >= 4 and segments[0] == "g" and segments[2] == "c" and bool(segments[3])
    if not (is_conversation or is_gpt_conversation):
        raise ReviewerConfigurationError("conversation_url must identify an existing /c/<id> conversation")
    return value
