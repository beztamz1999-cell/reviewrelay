"""Typed failures for the ChatGPT web transport boundary."""

from __future__ import annotations

from typing import Any

from ..errors import ReviewRelayError


class ReviewerAdapterError(ReviewRelayError):
    code = "REVIEWER_ADAPTER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        send_state: str = "NOT_SENT",
        send_result: Any = None,
    ) -> None:
        self.send_state = send_state
        self.send_result = send_result
        super().__init__(message, code=code)


class ReviewerConfigurationError(ReviewerAdapterError):
    code = "INVALID_REVIEWER_CONFIGURATION"


class BrowserStartFailed(ReviewerAdapterError):
    code = "BROWSER_START_FAILED"


class LoginRequired(ReviewerAdapterError):
    code = "LOGIN_REQUIRED"


class ConversationNavigationFailed(ReviewerAdapterError):
    code = "CONVERSATION_NAVIGATION_FAILED"


class ConversationNotReady(ReviewerAdapterError):
    code = "CONVERSATION_NOT_READY"


class ComposerNotFound(ReviewerAdapterError):
    code = "COMPOSER_NOT_FOUND"


class AttachmentNotFound(ReviewerAdapterError):
    code = "ATTACHMENT_NOT_FOUND"


class AttachmentNotAllowed(ReviewerAdapterError):
    code = "ATTACHMENT_NOT_ALLOWED"


class AttachmentLimitExceeded(ReviewerAdapterError):
    code = "ATTACHMENT_LIMIT_EXCEEDED"


class AttachmentUploadFailed(ReviewerAdapterError):
    code = "ATTACHMENT_UPLOAD_FAILED"


class MessageSendFailed(ReviewerAdapterError):
    code = "MESSAGE_SEND_FAILED"


class MessageSendAmbiguous(ReviewerAdapterError):
    code = "MESSAGE_SEND_AMBIGUOUS"

    def __init__(self, message: str, *, send_result: Any) -> None:
        super().__init__(message, send_state="SEND_AMBIGUOUS", send_result=send_result)


class ResponseTimeout(ReviewerAdapterError):
    code = "RESPONSE_TIMEOUT"

    def __init__(self, message: str, *, send_result: Any) -> None:
        super().__init__(message, send_state="SEND_CONFIRMED", send_result=send_result)


class AmbiguousResponse(ReviewerAdapterError):
    code = "AMBIGUOUS_RESPONSE"

    def __init__(self, message: str, *, send_result: Any = None) -> None:
        super().__init__(message, send_state="SEND_CONFIRMED", send_result=send_result)


class ReviewerConversationChanged(ReviewerAdapterError):
    code = "REVIEWER_CONVERSATION_CHANGED"

    def __init__(self, message: str, *, send_result: Any = None, send_state: str | None = None) -> None:
        inferred = "SEND_CONFIRMED" if send_result else "NOT_SENT"
        super().__init__(message, send_state=send_state or inferred, send_result=send_result)


class ResponseExtractionFailed(ReviewerAdapterError):
    code = "RESPONSE_EXTRACTION_FAILED"

    def __init__(self, message: str, *, send_result: Any = None) -> None:
        super().__init__(message, send_state="SEND_CONFIRMED" if send_result else "NOT_SENT", send_result=send_result)


class ReviewKeyConflict(ReviewerAdapterError):
    code = "REVIEW_KEY_CONFLICT"
