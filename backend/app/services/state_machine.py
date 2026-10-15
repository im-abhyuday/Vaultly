"""Document ingestion state machine and transition validation."""

from app.models.enums import DocumentStatus


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal state transition is attempted."""

    def __init__(self, current_status: str, target_status: str) -> None:
        super().__init__(
            f"Illegal state transition from '{current_status}' to '{target_status}'. "
            f"Allowed next states for '{current_status}': "
            f"{[s.value for s in ALLOWED_TRANSITIONS.get(DocumentStatus(current_status), set())]}"
        )
        self.current_status = current_status
        self.target_status = target_status


# Transition table defining permitted lifecycle paths:
# UPLOADED -> PARSING -> PARSED -> INDEXING -> READY
# Any state can transition to FAILED.
# FAILED can transition to UPLOADED (for reprocessing).
ALLOWED_TRANSITIONS: dict[DocumentStatus, set[DocumentStatus]] = {
    DocumentStatus.UPLOADED: {DocumentStatus.PARSING, DocumentStatus.FAILED},
    DocumentStatus.PARSING: {DocumentStatus.PARSED, DocumentStatus.FAILED},
    DocumentStatus.PARSED: {DocumentStatus.INDEXING, DocumentStatus.FAILED},
    DocumentStatus.INDEXING: {DocumentStatus.READY, DocumentStatus.FAILED},
    DocumentStatus.READY: {DocumentStatus.FAILED},
    DocumentStatus.FAILED: {DocumentStatus.UPLOADED},
}


def can_transition(
    current: str | DocumentStatus,
    target: str | DocumentStatus,
) -> bool:
    """Return True if transition from current to target status is valid."""
    curr_enum = DocumentStatus(current)
    target_enum = DocumentStatus(target)
    return target_enum in ALLOWED_TRANSITIONS.get(curr_enum, set())


def validate_transition(
    current: str | DocumentStatus,
    target: str | DocumentStatus,
) -> None:
    """Validate that transition from current to target status is valid, or raise InvalidStateTransitionError."""
    curr_enum = DocumentStatus(current)
    target_enum = DocumentStatus(target)

    if target_enum not in ALLOWED_TRANSITIONS.get(curr_enum, set()):
        raise InvalidStateTransitionError(curr_enum.value, target_enum.value)
