"""Unit tests for worker error classifications, backoff calculation, and event envelope serialization."""

import json
import uuid

from app.models.enums import DocumentErrorCode
from app.schemas.events import DocumentEventEnvelope, DocumentEventPayload
from app.workers.errors import (
    PermanentError,
    PoisonPillError,
    TransientError,
    classify_error,
)
from app.workers.retry import calculate_backoff_delay


def test_calculate_backoff_delay_bounds() -> None:
    """Backoff delay calculation respects base, exponential progression, and max_delay ceiling."""
    # Without jitter: exact exponential values capped at max_delay
    assert calculate_backoff_delay(0, base=1.0, max_delay=30.0, jitter=False) == 1.0
    assert calculate_backoff_delay(1, base=1.0, max_delay=30.0, jitter=False) == 2.0
    assert calculate_backoff_delay(2, base=1.0, max_delay=30.0, jitter=False) == 4.0
    assert calculate_backoff_delay(3, base=1.0, max_delay=30.0, jitter=False) == 8.0
    assert calculate_backoff_delay(4, base=1.0, max_delay=30.0, jitter=False) == 16.0
    assert calculate_backoff_delay(5, base=1.0, max_delay=30.0, jitter=False) == 30.0  # capped at 30
    assert calculate_backoff_delay(10, base=1.0, max_delay=30.0, jitter=False) == 30.0

    # With jitter: delay in range [0, ceiling]
    for attempt in range(6):
        delay = calculate_backoff_delay(attempt, base=1.0, max_delay=30.0, jitter=True)
        ceiling = min(30.0, 1.0 * (2**attempt))
        assert 0.0 <= delay <= ceiling


def test_classify_error_permanent() -> None:
    """PermanentError and its subclasses are marked non-transient with preserved error codes."""
    err = PermanentError("Corrupt PDF bytes", error_code=DocumentErrorCode.CORRUPT_FILE.value)
    is_transient, code, msg = classify_error(err)
    assert not is_transient
    assert code == DocumentErrorCode.CORRUPT_FILE.value
    assert msg == "Corrupt PDF bytes"


def test_classify_error_poison_pill() -> None:
    """PoisonPillError is marked non-transient."""
    err = PoisonPillError("Malformed JSON payload in message")
    is_transient, code, msg = classify_error(err)
    assert not is_transient
    assert code == DocumentErrorCode.INTERNAL_ERROR.value
    assert "Malformed JSON" in msg


def test_classify_error_transient() -> None:
    """TransientError is marked transient."""
    err = TransientError("Storage temporarily unavailable", error_code=DocumentErrorCode.INTERNAL_ERROR.value)
    is_transient, code, msg = classify_error(err)
    assert is_transient
    assert code == DocumentErrorCode.INTERNAL_ERROR.value
    assert msg == "Storage temporarily unavailable"


def test_classify_error_db_and_network_exceptions() -> None:
    """Common network/timeout/connection exceptions are inferred as transient."""
    exc1 = ConnectionResetError("Connection reset by peer")
    is_transient1, _, _ = classify_error(exc1)
    assert is_transient1

    exc2 = TimeoutError("Request timed out after 30s")
    is_transient2, _, _ = classify_error(exc2)
    assert is_transient2

    exc3 = RuntimeError("could not obtain lock on row in table documents")
    is_transient3, _, _ = classify_error(exc3)
    assert is_transient3


def test_event_envelope_roundtrip_serialization() -> None:
    """EventEnvelope serializes to JSON and deserializes correctly."""
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    config_id = uuid.uuid4()

    envelope = DocumentEventEnvelope(
        event_type="document.uploaded",
        tenant_id=tenant_id,
        document_id=document_id,
        correlation_id="corr-test-1234",
        payload=DocumentEventPayload(chunking_config_id=config_id),
    )

    json_data = envelope.model_dump_json()
    parsed_dict = json.loads(json_data)

    assert parsed_dict["schema_version"] == 1
    assert parsed_dict["event_type"] == "document.uploaded"
    assert parsed_dict["tenant_id"] == str(tenant_id)
    assert parsed_dict["document_id"] == str(document_id)
    assert parsed_dict["correlation_id"] == "corr-test-1234"
    assert parsed_dict["payload"]["chunking_config_id"] == str(config_id)

    # Deserialize back into envelope
    recovered = DocumentEventEnvelope.model_validate_json(json_data)
    assert recovered.schema_version == 1
    assert recovered.event_id == envelope.event_id
    assert recovered.tenant_id == tenant_id
    assert recovered.document_id == document_id
    assert recovered.payload.chunking_config_id == config_id


def test_event_envelope_schema_evolution_ignores_extra_fields() -> None:
    """Event envelope extra fields are safely ignored for backward/forward compatibility."""
    payload_with_extra = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "event_type": "document.uploaded",
        "occurred_at": "2026-10-15T12:00:00Z",
        "tenant_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "correlation_id": "corr-extra",
        "future_field_unrecognized": "some_value",
        "payload": {
            "chunking_config_id": str(uuid.uuid4()),
            "another_future_field": 42,
        },
    }
    envelope = DocumentEventEnvelope.model_validate(payload_with_extra)
    assert envelope.correlation_id == "corr-extra"
