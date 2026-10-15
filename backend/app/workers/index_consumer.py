"""Index consumer processing document.parsed events.

Claims document in DB, reads parsed.json, chunks hierarchically by token count,
generates embeddings, inserts chunks transactionally into pgvector, and sets status to READY.
"""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from aiokafka import (
    AIOKafkaConsumer,
    AIOKafkaProducer,
    TopicPartition,
)
from aiokafka.structs import OffsetAndMetadata
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import request_id_ctx, tenant_id_ctx
from app.db.session import async_session_maker, tenant_transaction
from app.models.chunk import Chunk
from app.models.chunking_config import ChunkingConfig
from app.models.document import Document
from app.models.enums import DocumentErrorCode, DocumentStatus
from app.schemas.events import DocumentEventEnvelope
from app.services.chunker import (
    HFTokenCounter,
    RecursiveChunker,
    RegexTokenCounter,
    TokenCounter,
)
from app.services.embedding import get_embedding_provider
from app.services.parser import ParsedDocument
from app.services.permissions import get_corpus_notifier
from app.services.storage import StorageBackend, get_document_storage_key, get_storage
from app.workers.dlq import publish_to_dlq
from app.workers.errors import PermanentError, classify_error
from app.workers.infra import get_topic_name
from app.workers.retry import calculate_backoff_delay

logger = logging.getLogger("vaultly.index_consumer")
settings = get_settings()


class IndexConsumer:
    """Consumer for document.parsed topic."""

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
        self.dlq_topic = get_topic_name("document.parsed.dlq")

    async def _claim_document(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> tuple[uuid.UUID, int] | None:
        """Claim document for indexing in an immediate commit transaction.

        Returns (chunking_config_id, attempts) or None if skipped.
        """
        async with tenant_transaction(tenant_id, self.session_maker) as session:
            stmt = (
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                    Document.status.in_([DocumentStatus.PARSED.value, DocumentStatus.INDEXING.value]),
                )
                .values(
                    status=DocumentStatus.INDEXING.value,
                    attempts=Document.attempts + 1,
                )
                .returning(Document.chunking_config_id, Document.attempts)
            )
            result = (await session.execute(stmt)).first()
            if not result:
                return None
            return result[0], result[1]

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
                        failed_stage="indexing",
                    )
                )
                await session.execute(stmt)
        except Exception as err:
            logger.error(
                f"Failed to record FAILED status for document {document_id}: {err}",
                exc_info=True,
            )

    async def process_message(self, msg: Any) -> None:
        """Process a single document.parsed message."""
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
                stage="indexing",
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
            for attempt in range(1, self.max_attempts + 1):
                try:
                    # Claim document
                    claim_info = await self._claim_document(envelope.tenant_id, envelope.document_id)
                    if not claim_info:
                        logger.info(
                            f"Document {envelope.document_id} already past INDEXING or deleted. Skipping."
                        )
                        await self.consumer.commit({tp: next_offset})
                        return

                    config_id, doc_attempts = claim_info

                    # Fetch pinned chunking configuration
                    async with tenant_transaction(envelope.tenant_id, self.session_maker) as session:
                        cfg_stmt = select(ChunkingConfig).where(
                            ChunkingConfig.id == config_id,
                            ChunkingConfig.tenant_id == envelope.tenant_id,
                        )
                        config = (await session.execute(cfg_stmt)).scalar_one_or_none()

                    if not config:
                        raise PermanentError(
                            f"ChunkingConfig '{config_id}' not found for tenant '{envelope.tenant_id}'.",
                            error_code=DocumentErrorCode.INTERNAL_ERROR.value,
                        )

                    # Read parsed.json from storage
                    parsed_key = get_document_storage_key(
                        envelope.tenant_id, envelope.document_id, "parsed.json"
                    )
                    if not self.storage.exists(parsed_key):
                        raise PermanentError(
                            f"Parsed document artifact missing at key: {parsed_key}",
                            error_code=DocumentErrorCode.CORRUPT_FILE.value,
                        )

                    with self.storage.open(parsed_key) as f:
                        parsed_bytes = f.read()

                    parsed_doc = ParsedDocument.model_validate_json(parsed_bytes)

                    # Initialize TokenCounter
                    token_counter: TokenCounter
                    if settings.EMBEDDING_PROVIDER.lower().strip() == "local":
                        try:
                            token_counter = HFTokenCounter(config.embedding_model)
                        except Exception:
                            token_counter = RegexTokenCounter()
                    else:
                        token_counter = RegexTokenCounter()

                    # Chunk document recursively
                    chunker = RecursiveChunker(
                        token_counter=token_counter,
                        chunk_size=config.chunk_size_tokens,
                        chunk_overlap=config.chunk_overlap_tokens,
                        max_chunks_per_doc=settings.MAX_CHUNKS_PER_DOC,
                    )
                    generated_chunks = chunker.chunk_document(parsed_doc)

                    # Embed chunks off the event loop thread
                    texts = [c.text for c in generated_chunks]
                    embedder = get_embedding_provider(
                        provider_name=settings.EMBEDDING_PROVIDER,
                        model_name=config.embedding_model,
                        dim=config.embedding_dim,
                    )
                    embeddings = await asyncio.to_thread(embedder.embed_documents, texts)

                    # Atomic persistence: lock document, replace chunks, set READY
                    async with tenant_transaction(envelope.tenant_id, self.session_maker) as session:
                        # Lock document row
                        doc = (
                            await session.execute(
                                select(Document)
                                .where(
                                    Document.id == envelope.document_id,
                                    Document.tenant_id == envelope.tenant_id,
                                )
                                .with_for_update()
                            )
                        ).scalar_one_or_none()

                        if not doc:
                            logger.info(f"Document {envelope.document_id} was deleted during indexing. Skipping.")
                            await self.consumer.commit({tp: next_offset})
                            return

                        # Delete any previous chunks for idempotency
                        await session.execute(
                            delete(Chunk).where(
                                Chunk.document_id == envelope.document_id,
                                Chunk.chunking_config_id == config.id,
                            )
                        )

                        # Insert all new chunks
                        chunk_records = [
                            Chunk(
                                id=uuid.uuid4(),
                                tenant_id=envelope.tenant_id,
                                document_id=envelope.document_id,
                                chunking_config_id=config.id,
                                ordinal=c.ordinal,
                                text=c.text,
                                token_count=c.token_count,
                                embedding=embeddings[idx],
                                chunk_metadata={
                                    "page_start": c.page_start,
                                    "page_end": c.page_end,
                                    "char_start": c.char_start,
                                    "char_end": c.char_end,
                                },
                            )
                            for idx, c in enumerate(generated_chunks)
                        ]
                        session.add_all(chunk_records)

                        # Update document state to READY
                        doc.status = DocumentStatus.READY.value
                        doc.chunk_count = len(generated_chunks)
                        doc.indexed_at = datetime.now(UTC)
                        doc.attempts = 0
                        doc.error_code = None
                        doc.error_message = None
                        doc.failed_stage = None

                    # Notify cache of corpus change (no-op in Phase 2)
                    await get_corpus_notifier().notify(envelope.tenant_id, "ready")

                    # Commit Kafka consumer offset
                    await self.consumer.commit({tp: next_offset})
                    logger.info(
                        f"Document {envelope.document_id} indexed successfully ({len(generated_chunks)} chunks, dim={config.embedding_dim})."
                    )
                    return

                except Exception as exc:
                    is_transient, error_code, error_msg = classify_error(exc)
                    logger.warning(
                        f"Error indexing document {envelope.document_id} (attempt {attempt}/{self.max_attempts}, transient={is_transient}): {exc}"
                    )

                    if not is_transient:
                        await self._mark_failed(
                            envelope.tenant_id, envelope.document_id, error_code, error_msg
                        )
                        await publish_to_dlq(
                            producer=self.producer,
                            dlq_topic=self.dlq_topic,
                            raw_message_bytes=msg.value,
                            key_bytes=msg.key,
                            stage="indexing",
                            error_code=error_code,
                            error_message=error_msg,
                            attempts=attempt,
                            source_topic=msg.topic,
                            source_partition=msg.partition,
                            source_offset=msg.offset,
                        )
                        await self.consumer.commit({tp: next_offset})
                        return

                    if attempt < self.max_attempts:
                        delay = calculate_backoff_delay(attempt)
                        await asyncio.sleep(delay)
                    else:
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
                            stage="indexing",
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
        """Main index consumer loop (max_poll_records=1)."""
        logger.info("Index consumer loop started.")
        while not stop_event.is_set():
            try:
                records = await self.consumer.getmany(timeout_ms=1000, max_records=1)
                for _tp, batch in records.items():
                    for msg in batch:
                        await self.process_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"Unexpected error in index consumer loop: {err}", exc_info=True)
                await asyncio.sleep(1.0)

        logger.info("Index consumer loop stopped.")
