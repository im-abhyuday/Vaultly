"""Worker error classifications for pipeline retry and DLQ routing.

Differentiates TransientError (retriable in-process) from PermanentError (routed immediately to DLQ).
"""


from app.models.enums import DocumentErrorCode


class PipelineError(Exception):
    """Base class for document pipeline errors."""

    def __init__(self, message: str, error_code: str = DocumentErrorCode.INTERNAL_ERROR.value) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class TransientError(PipelineError):
    """Temporary failure (network glitch, database lock, storage I/O delay) suitable for in-process retry."""


class PermanentError(PipelineError):
    """Fatal, non-retriable failure (corrupt file, unsupported type, limit exceeded, no text)."""


class PoisonPillError(PermanentError):
    """Undecodable or schema-invalid Kafka message that must not block the consumer loop."""

    def __init__(self, message: str) -> None:
        super().__init__(message, error_code=DocumentErrorCode.INTERNAL_ERROR.value)


def classify_error(exc: Exception) -> tuple[bool, str, str]:
    """Classify an exception into (is_transient, error_code, error_message).

    Transient errors trigger in-process retry with backoff.
    Permanent errors route directly to Dead-Letter Queue (DLQ).
    """
    if isinstance(exc, PoisonPillError):
        return False, exc.error_code, exc.message

    if isinstance(exc, PermanentError):
        return False, exc.error_code, exc.message

    if isinstance(exc, TransientError):
        return True, exc.error_code, exc.message

    # Handle known third-party exceptions
    exc_type = type(exc).__name__
    exc_str = str(exc).lower()

    # DB connection / network / deadlock transient errors
    transient_indicators = [
        "connection",
        "timeout",
        "lock",
        "deadlock",
        "closed",
        "reset by peer",
        "temporarily unavailable",
    ]
    if any(ind in exc_str for ind in transient_indicators):
        return True, DocumentErrorCode.INTERNAL_ERROR.value, f"Transient network/database error: {exc}"

    # Default unknown exceptions are treated as transient up to max attempts
    return True, DocumentErrorCode.INTERNAL_ERROR.value, f"Unexpected error ({exc_type}): {exc}"
