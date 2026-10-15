"""Kafka event envelope and payload schemas for document ingestion pipeline."""

import uuid
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentEventPayload(BaseModel):
    """Payload schema carried in document lifecycle events."""

    model_config = ConfigDict(extra="ignore")

    chunking_config_id: uuid.UUID


TPayload = TypeVar("TPayload", bound=BaseModel)


class EventEnvelope(BaseModel, Generic[TPayload]):
    """Standardized event envelope conforming to schema_version=1.

    Payloads carry IDs and metadata pointers only, never file contents or extracted text.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=1, description="Event envelope schema version")
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    correlation_id: str
    payload: TPayload

    @model_validator(mode="before")
    @classmethod
    def wrap_flattened_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # If payload is missing but chunking_config_id is present at root, wrap it
            if "payload" not in data and "chunking_config_id" in data:
                data_copy = dict(data)
                data_copy["payload"] = {"chunking_config_id": data_copy.get("chunking_config_id")}
                return data_copy
        return data


DocumentEventEnvelope = EventEnvelope[DocumentEventPayload]

