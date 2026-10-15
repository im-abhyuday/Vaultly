"""Unit tests for document state machine transition rules."""

import pytest

from app.models.enums import DocumentStatus
from app.services.state_machine import (
    InvalidStateTransitionError,
    can_transition,
    validate_transition,
)


def test_happy_path_lifecycle_transitions() -> None:
    """Verify normal sequential lifecycle: UPLOADED -> PARSING -> PARSED -> INDEXING -> READY."""
    assert can_transition(DocumentStatus.UPLOADED, DocumentStatus.PARSING)
    assert can_transition(DocumentStatus.PARSING, DocumentStatus.PARSED)
    assert can_transition(DocumentStatus.PARSED, DocumentStatus.INDEXING)
    assert can_transition(DocumentStatus.INDEXING, DocumentStatus.READY)


def test_transitions_to_failed_from_any_state() -> None:
    """Verify any state can transition to FAILED."""
    for state in DocumentStatus:
        if state != DocumentStatus.FAILED:
            assert can_transition(state, DocumentStatus.FAILED)
            validate_transition(state, DocumentStatus.FAILED)


def test_reprocessing_transition_from_failed() -> None:
    """Verify FAILED status can transition back to UPLOADED."""
    assert can_transition(DocumentStatus.FAILED, DocumentStatus.UPLOADED)
    validate_transition(DocumentStatus.FAILED, DocumentStatus.UPLOADED)


def test_illegal_state_transitions_raise() -> None:
    """Verify illegal jumps across stages raise InvalidStateTransitionError."""
    illegal_pairs = [
        (DocumentStatus.UPLOADED, DocumentStatus.READY),
        (DocumentStatus.UPLOADED, DocumentStatus.INDEXING),
        (DocumentStatus.PARSED, DocumentStatus.UPLOADED),
        (DocumentStatus.READY, DocumentStatus.UPLOADED),
        (DocumentStatus.READY, DocumentStatus.PARSING),
        (DocumentStatus.READY, DocumentStatus.INDEXING),
        (DocumentStatus.FAILED, DocumentStatus.PARSING),
        (DocumentStatus.FAILED, DocumentStatus.READY),
    ]

    for curr, target in illegal_pairs:
        assert not can_transition(curr, target)
        with pytest.raises(InvalidStateTransitionError):
            validate_transition(curr, target)
