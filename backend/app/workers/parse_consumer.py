"""Parse consumer processing document.uploaded events.

Claims document in DB, offloads parsing to worker thread, stores parsed.json,
updates status to PARSED, and writes document.parsed to transactional outbox.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from aiokafka import (
    AIOKafkaConsumer,
    AIOKafkaProducer,
    TopicPartition,
)
from aiokafka.structs import OffsetAndMetadata
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import request_id_ctx, tenant_id_ctx
from app.db.session import async_session_maker, tenant_transaction
from app.models.document import Document
from app.models.enums import DocumentErrorCode, DocumentStatus
from app.models.outbox import OutboxEvent
from app.schemas.events import DocumentEventEnvelope, DocumentEventPayload
from app.services.parser import parse_document
from app.services.storage import StorageBackend, get_document_storage_key, get_storage
from app.workers.dlq import publish_to_dlq
from app.workers.errors import classify_error
from app.workers.infra import get_topic_name
from app.workers.retry import calculate_backoff_delay

logger = logging.getLogger("vaultly.parse_consumer")
settings = get_settings()


async def _byte_iterator(data: bytes, chunk_size: int = 65536) -> AsyncIterator[bytes]:
    """Yield bytes in chunks for StorageBackend.put_stream."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


class ParseConsumer:
    """Consumer for document.uploaded topic."""

    def __init__(
        self,
        consumer: AIOKafkaConsumer,
        producer: AIOKafkaProducer,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        storage: StorageBackend | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.consumer = consumer
        self.producer = producer
        self.session_maker = session_maker or async_session_maker
        self.storage = storage or get_storage()
        self.max_attempts = max_attempts or settings.WORKER_MAX_ATTEMPTS
        self.dlq_topic = get_topic_name("document.uploaded.dlq")
        self.parsed_topic = get_topic_name("document.parsed")

    async def _claim_document(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[str, str, int] | None:
        """Claim document for parsing in an immediate commit transaction.

        Returns (storage_key, detected_type, attempts) or None if skipped.
        """
        async with tenant_transaction(tenant_id, self.session_maker) as session:
            stmt = (
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                    Document.status.in_([DocumentStatus.UPLOADED.value, DocumentStatus.PARSING.value]),
                )
                .values(
                    status=DocumentStatus.PARSING.value,
                    attempts=Document.attempts + 1,
                )
                .returning(Document.storage_key, Document.detected_type, Document.attempts)
            )
            result = (await session.execute(stmt)).first()
            if not result:
                return None
            return result[0], result[1], result[2]

    async def _mark_failed(
        self,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        error_code: str,
        error_message: str,
    ) -> None:
        """Record document failure in database."""
        try:
            async with tenant_transaction(tenant_id, self.session_maker) as session:
                stmt = (
                    update(Document)
                    .where(Document.id == document_id, Document.tenant_id == tenant_id)
                    .values(
                        status=DocumentStatus.FAILED.value,
                        error_code=error_code,
                        error_message=error_message,
                        failed_stage="parsing",
                    )
                )
                await session.execute(stmt)
        except Exception as err:
            logger.error(
                f"Failed to record FAILED status for document {document_id}: {err}",
                exc_info=True,
            )

    async def process_message(self, msg: Any) -> None:
        """Process a single Kafka message with poison pill, retry, and DLQ semantics."""
        tp = TopicPartition(msg.topic, msg.partition)
        next_offset = OffsetAndMetadata(msg.offset + 1, "")

        # 1. Parse and validate event envelope (poison pill protection)
        try:
            data = json.loads(msg.value.decode("utf-8"))
            envelope = DocumentEventEnvelope.model_validate(data)
        except Exception as err:
            logger.error(f"Poison pill detected on {msg.topic}:{msg.partition}:{msg.offset}: {err}")
            await publish_to_dlq(
                producer=self.producer,
                dlq_topic=self.dlq_topic,
                raw_message_bytes=msg.value,
                key_bytes=msg.key,
                stage="parsing",
                error_code=DocumentErrorCode.INTERNAL_ERROR.value,
                error_message=f"Poison pill schema validation failure: {err}",
                attempts=1,
                source_topic=msg.topic,
                source_partition=msg.partition,
                source_offset=msg.offset,
            )
            await self.consumer.commit({tp: next_offset})
            return

        # Bind logging context
        req_token = request_id_ctx.set(envelope.correlation_id)
        ten_token = tenant_id_ctx.set(str(envelope.tenant_id))

        try:
            # 2. In-process retry loop
            for attempt in range(1, self.max_attempts + 1):
                try:
                    # Claim document
                    claim_info = await self._claim_document(envelope.tenant_id, envelope.document_id)
                    if not claim_info:
                        logger.info(
                            f"Document {envelope.document_id} already past PARSING or deleted. Skipping."
                        )
                        await self.consumer.commit({tp: next_offset})
                        return

                    storage_key, detected_type, doc_attempts = claim_info

                    # Parse document off event loop thread
                    parsed = await parse_document(
                        storage_key=storage_key,
                        detected_type=detected_type,
                        storage=self.storage,
                    )

                    # Store parsed.json in storage
                    parsed_key = get_document_storage_key(
                        envelope.tenant_id, envelope.document_id, "parsed.json"
                    )
                    parsed_bytes = parsed.model_dump_json().encode("utf-8")
                    await self.storage.put_stream(parsed_key, _byte_iterator(parsed_bytes))

                    # Atomic DB update: status=PARSED, reset attempts, write outbox event
                    async with tenant_transaction(envelope.tenant_id, self.session_maker) as session:
                        await session.execute(
                            update(Document)
                            .where(
                                Document.id == envelope.document_id,
                                Document.tenant_id == envelope.tenant_id,
                            )
                            .values(
                                status=DocumentStatus.PARSED.value,
                                parsed_at=datetime.now(UTC),
                                page_count=parsed.page_count,
                                attempts=0,
                                error_code=None,
                                error_message=None,
                                failed_stage=None,
                            )
                        )

                        # Write document.parsed outbox event
                        parsed_envelope = DocumentEventEnvelope(
                            event_type="document.parsed",
                            tenant_id=envelope.tenant_id,
                            document_id=envelope.document_id,
                            correlation_id=envelope.correlation_id,
                            payload=DocumentEventPayload(chunking_config_id=envelope.payload.chunking_config_id),
                        )
                        outbox = OutboxEvent(
                            id=uuid.uuid4(),
                            topic=self.parsed_topic,
                            key=str(envelope.document_id),
                            payload=json.loads(parsed_envelope.model_dump_json()),
                            headers={
                                "correlation_id": envelope.correlation_id,
                                "schema_version": "1",
                            },
                        )
                        session.add(outbox)


                    # Success -> commit Kafka consumer offset
                    await self.consumer.commit({tp: next_offset})
                    logger.info(
                        f"Document {envelope.document_id} successfully parsed ({parsed.page_count} pages)."
                    )
                    return

                except Exception as exc:
                    is_transient, error_code, error_msg = classify_error(exc)
                    logger.warning(
                        f"Error parsing document {envelope.document_id} (attempt {attempt}/{self.max_attempts}, transient={is_transient}): {exc}"
                    )

                    if not is_transient:
                        # Permanent error -> DLQ and commit offset
                        await self._mark_failed(
                            envelope.tenant_id, envelope.document_id, error_code, error_msg
                        )
                        await publish_to_dlq(
                            producer=self.producer,
                            dlq_topic=self.dlq_topic,
                            raw_message_bytes=msg.value,
                            key_bytes=msg.key,
                            stage="parsing",
                            error_code=error_code,
                            error_message=error_msg,
                            attempts=attempt,
                            source_topic=msg.topic,
                            source_partition=msg.partition,
                            source_offset=msg.offset,
                        )
                        await self.consumer.commit({tp: next_offset})
                        return

                    # Transient error: backoff if retries remain
                    if attempt < self.max_attempts:
                        delay = calculate_backoff_delay(attempt)
                        await asyncio.sleep(delay)
                    else:
                        # Retries exhausted -> mark FAILED, DLQ, and commit offset
                        logger.error(
                            f"Retries exhausted ({self.max_attempts}) for document {envelope.document_id}."
                        )
                        retries_code = DocumentErrorCode.RETRIES_EXHAUSTED.value
                        retries_msg = f"Retries exhausted ({self.max_attempts} attempts): {error_msg}"
                        await self._mark_failed(
                            envelope.tenant_id, envelope.document_id, retries_code, retries_msg
                        )
                        await publish_to_dlq(
                            producer=self.producer,
                            dlq_topic=self.dlq_topic,
                            raw_message_bytes=msg.value,
                            key_bytes=msg.key,
                            stage="parsing",
                            error_code=retries_code,
                            error_message=retries_msg,
                            attempts=attempt,
                            source_topic=msg.topic,
                            source_partition=msg.partition,
                            source_offset=msg.offset,
                        )
                        await self.consumer.commit({tp: next_offset})
                        return

        finally:
            request_id_ctx.reset(req_token)
            tenant_id_ctx.reset(ten_token)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main parse consumer loop."""
        logger.info("Parse consumer loop started.")
        while not stop_event.is_set():
            try:
                records = await self.consumer.getmany(timeout_ms=1000, max_records=1)
                for _tp, batch in records.items():
                    for msg in batch:
                        await self.process_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"Unexpected error in parse consumer loop: {err}", exc_info=True)
                await asyncio.sleep(1.0)

        logger.info("Parse consumer loop stopped.")
