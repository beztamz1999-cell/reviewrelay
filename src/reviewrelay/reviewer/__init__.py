"""Reviewer transports. Phase 3 currently provides a ChatGPT web adapter."""

from .base import (
    AssistantResponse,
    BrowserBackend,
    ChatGPTTimeouts,
    ChatGPTWebSettings,
    ReviewerAdapter,
    SendDisposition,
    SendResult,
    TurnBaseline,
)
from .chatgpt_web import ChatGPTWebAdapter
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
    ReviewerAdapterError,
    ReviewerConfigurationError,
    ReviewerConversationChanged,
)

__all__ = [
    "AmbiguousResponse", "AssistantResponse", "AttachmentLimitExceeded", "AttachmentNotAllowed",
    "AttachmentNotFound", "AttachmentUploadFailed", "BrowserStartFailed", "ChatGPTTimeouts",
    "BrowserBackend", "ChatGPTWebAdapter", "ChatGPTWebSettings", "ComposerNotFound", "ConversationNavigationFailed",
    "ConversationNotReady", "LoginRequired", "MessageSendAmbiguous", "MessageSendFailed",
    "ResponseExtractionFailed", "ResponseTimeout", "ReviewKeyConflict", "ReviewerAdapter",
    "ReviewerAdapterError", "ReviewerConfigurationError", "ReviewerConversationChanged",
    "SendDisposition", "SendResult", "TurnBaseline",
]
