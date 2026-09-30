"""Typed worker transport failures; messages deliberately omit server diagnostic content."""

from ..errors import ReviewRelayError


class WorkerError(ReviewRelayError):
    code = "WORKER_ERROR"


class WorkerStartFailed(WorkerError):
    code = "WORKER_START_FAILED"


class WorkerProtocolError(WorkerError):
    code = "WORKER_PROTOCOL_ERROR"


class WorkerProcessDied(WorkerError):
    code = "WORKER_PROCESS_DIED"


class WorkerTimeout(WorkerError):
    code = "WORKER_TIMEOUT"


class WorkerRequestFailed(WorkerError):
    code = "WORKER_REQUEST_FAILED"


class WorkerThreadResumeFailed(WorkerError):
    code = "WORKER_THREAD_RESUME_FAILED"


class WorkerBindingMismatch(WorkerError):
    code = "WORKER_BINDING_MISMATCH"


class WorkerTurnAlreadyActive(WorkerError):
    code = "WORKER_TURN_ALREADY_ACTIVE"


class WorkerTurnFailed(WorkerError):
    code = "WORKER_TURN_FAILED"


class WorkerTurnInterrupted(WorkerError):
    code = "WORKER_TURN_INTERRUPTED"


class WorkerAuthRequired(WorkerError):
    code = "WORKER_AUTH_REQUIRED"


class WorkerInteractionRequired(WorkerError):
    code = "WORKER_INTERACTION_REQUIRED"


class WorkerResponseMissing(WorkerError):
    code = "WORKER_RESPONSE_MISSING"
