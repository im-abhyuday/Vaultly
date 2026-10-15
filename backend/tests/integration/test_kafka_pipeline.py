"""Integration tests for Phase 2B: Kafka Ingestion Pipeline.

Tests:
1. End-to-end processing (TXT, PDF, DOCX) -> UPLOADED -> PARSING -> PARSED -> INDEXING -> READY.
2. Chunk verification: 384-dim vector, tsvector, page/char metadata, contiguous ordinals.
3. Consumer idempotency on duplicate events.
4. Corrupt PDF -> FAILED (CORRUPT_FILE) + DLQ message with headers.
5. Scanned image-only PDF -> FAILED (NO_EXTRACTABLE_TEXT).
6. In-process transient retry and exhaustion -> FAILED (RETRIES_EXHAUSTED) + DLQ message.
7. Poison pill handling (garbage bytes / invalid schema) routed to DLQ without blocking consumer.
8. Delete during ingestion handled gracefully without DLQ noise.
9. Cross-tenant event mismatch ignored with no state mutation.
10. Outbox relay concurrency: SELECT FOR UPDATE SKIP LOCKED prevents double-publishing.
11. Slow real model embedding verification (@pytest.mark.slow).
"""

import asyncio
import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import docx
import pypdf
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.db.session import tenant_transaction
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentErrorCode, DocumentStatus
from app.models.outbox import OutboxEvent
from app.schemas.events import DocumentEventEnvelope, DocumentEventPayload
from app.services.embedding import LocalEmbeddingProvider
from app.services.storage import StorageBackend, get_storage
from app.workers.errors import TransientError
from app.workers.index_consumer import IndexConsumer
from app.workers.parse_consumer import ParseConsumer
from app.workers.relay import OutboxRelay

settings = get_settings()


# --- Test Helpers & Mocks ---


@dataclass
class FakeKafkaMessage:
    """Mock Kafka ConsumerRecord for deterministic unit & integration tests."""

    topic: str
    partition: int
    offset: int
    key: bytes
    value: bytes
    headers: list[tuple[str, bytes]] = field(default_factory=list)


class MockKafkaConsumer:
    """Mock Kafka Consumer capturing committed offsets."""

    def __init__(self) -> None:
        self.committed: dict[Any, Any] = {}

    async def commit(self, offsets: dict[Any, Any]) -> None:
        self.committed.update(offsets)


class MockKafkaProducer:
    """Mock Kafka Producer capturing published messages and headers."""

    def __init__(self) -> None:
        self.published_messages: list[dict[str, Any]] = []

    async def send_and_wait(
        self,
        topic: str,
        value: bytes,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> Any:
        self.published_messages.append({
            "topic": topic,
            "value": value,
            "key": key,
            "headers": headers or [],
        })
        return None


def create_test_docx(text: str = "Vaultly enterprise docx document.") -> bytes:
    """Create a minimal valid docx in memory."""
    doc = docx.Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def create_test_pdf_with_text() -> bytes:
    """Create a minimal PDF that produces text with pypdf."""
    # A standard minimal 1-page PDF stream
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length 53 >> stream\n"
        b"BT\n/F1 12 Tf\n100 700 Td\n(Vaultly PDF parsing integration test.) Tj\nET\nendstream\nendobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000266 00000 n \n0000000371 00000 n \n"
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n452\n%%EOF\n"
    )


def create_scanned_pdf() -> bytes:
    """Create a minimal valid PDF with zero text."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


async def register_tenant_and_get_token(client: AsyncClient, name_prefix: str = "tenant") -> tuple[uuid.UUID, str]:
    """Helper to register a tenant and return (tenant_id, access_token)."""
    slug = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    reg = {
        "tenant_name": f"Org {slug}",
        "tenant_slug": slug,
        "email": f"admin@{slug}.com",
        "password": "Password12345!",
        "full_name": "Admin User",
    }
    res = await client.post("/api/v1/auth/register-tenant", json=reg)
    assert res.status_code == 201
    data = res.json()
    token = data["access_token"]
    payload = decode_access_token(token)
    return uuid.UUID(payload["tid"]), token




# --- Integration Test Cases ---


async def test_pipeline_e2e_full_lifecycle(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end pipeline: upload TXT, PDF, DOCX -> relay -> parse -> relay -> index -> READY."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "pipeline-e2e")
    headers = {"Authorization": f"Bearer {token}"}
    storage = get_storage()

    # 1. Upload TXT document
    txt_content = (
        b"Vaultly is an enterprise RAG system.\n\n"
        b"It provides Row-Level Security, multi-tenancy, and audit logs.\n\n"
        b"All queries run with tenant isolation enforced at the database level."
    )
    res_txt = await async_client.post(
        "/api/v1/documents",
        files={"file": ("sample.txt", txt_content, "text/plain")},
        headers=headers,
    )
    assert res_txt.status_code == 202
    txt_doc_id = uuid.UUID(res_txt.json()["id"])

    # 2. Upload DOCX document
    docx_bytes = create_test_docx("Enterprise security architecture paragraph.")
    res_docx = await async_client.post(
        "/api/v1/documents",
        files={"file": ("sample.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=headers,
    )
    assert res_docx.status_code == 202
    docx_doc_id = uuid.UUID(res_docx.json()["id"])

    # 3. Upload PDF document
    pdf_bytes = create_test_pdf_with_text()
    res_pdf = await async_client.post(
        "/api/v1/documents",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert res_pdf.status_code == 202
    pdf_doc_id = uuid.UUID(res_pdf.json()["id"])


    doc_ids = [txt_doc_id, docx_doc_id, pdf_doc_id]

    # Step A: Outbox Relay publishes document.uploaded
    producer = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker, batch_size=10)
    published_count = await relay.publish_batch(producer)
    assert published_count == 3

    # Step B: ParseConsumer processes document.uploaded
    parse_consumer_mock = MockKafkaConsumer()
    parse_consumer = ParseConsumer(
        consumer=parse_consumer_mock,
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )

    for i, msg_data in enumerate(producer.published_messages[:3]):
        fake_msg = FakeKafkaMessage(
            topic=msg_data["topic"],
            partition=0,
            offset=i,
            key=msg_data["key"],
            value=msg_data["value"],
            headers=msg_data["headers"],
        )
        await parse_consumer.process_message(fake_msg)

    # Verify all 3 are now PARSED
    async with tenant_transaction(tenant_id, app_session_maker) as session:
        for did in doc_ids:
            doc = (await session.execute(select(Document).where(Document.id == did))).scalar_one()
            assert doc.status == DocumentStatus.PARSED.value
            assert doc.page_count is not None
            assert doc.page_count >= 1

    # Step C: Outbox Relay publishes document.parsed
    producer.published_messages.clear()
    published_parsed = await relay.publish_batch(producer)
    assert published_parsed == 3

    # Step D: IndexConsumer processes document.parsed
    index_consumer_mock = MockKafkaConsumer()
    index_consumer = IndexConsumer(
        consumer=index_consumer_mock,
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )

    for i, msg_data in enumerate(producer.published_messages):
        fake_msg = FakeKafkaMessage(
            topic=msg_data["topic"],
            partition=0,
            offset=i,
            key=msg_data["key"],
            value=msg_data["value"],
            headers=msg_data["headers"],
        )
        await index_consumer.process_message(fake_msg)

    # Step E: Verify all 3 documents are READY and chunks are fully indexed
    async with tenant_transaction(tenant_id, app_session_maker) as session:
        for did in doc_ids:
            doc = (await session.execute(select(Document).where(Document.id == did))).scalar_one()
            assert doc.status == DocumentStatus.READY.value

            # Check chunks in pgvector
            chunks_stmt = select(Chunk).where(Chunk.document_id == did).order_by(Chunk.ordinal.asc())
            chunks = (await session.execute(chunks_stmt)).scalars().all()
            assert len(chunks) > 0

            for idx, c in enumerate(chunks):
                assert c.ordinal == idx  # contiguous starting at 0
                assert c.tenant_id == tenant_id
                assert c.embedding is not None
                assert len(c.embedding) == 384  # 384-dim vector
                assert c.tsv is not None  # tsvector populated
                assert c.chunk_metadata is not None
                assert "char_start" in c.chunk_metadata
                assert "char_end" in c.chunk_metadata

    # Check chunks API endpoint
    res_chunks = await async_client.get(f"/api/v1/documents/{txt_doc_id}/chunks", headers=headers)
    assert res_chunks.status_code == 200
    chunks_data = res_chunks.json()
    assert len(chunks_data) > 0
    assert "text" in chunks_data[0]
    assert "embedding" not in chunks_data[0]  # chunks API does not leak raw embeddings


async def test_pipeline_idempotency_duplicate_events(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate Kafka events (at-least-once delivery) are idempotently handled without duplicating chunks."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "idempotent")
    headers = {"Authorization": f"Bearer {token}"}
    storage = get_storage()

    res = await async_client.post(
        "/api/v1/documents",
        files={"file": ("test.txt", b"Idempotent processing test content.\n\nSecond block.", "text/plain")},
        headers=headers,
    )
    doc_id = uuid.UUID(res.json()["id"])

    producer = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker)
    await relay.publish_batch(producer)

    uploaded_msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=producer.published_messages[0]["key"],
        value=producer.published_messages[0]["value"],
    )

    parse_consumer = ParseConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )
    # Parse once
    await parse_consumer.process_message(uploaded_msg)

    # Relay to parsed topic
    producer.published_messages.clear()
    await relay.publish_batch(producer)

    parsed_msg = FakeKafkaMessage(
        topic="vaultly.document.parsed",
        partition=0,
        offset=0,
        key=producer.published_messages[0]["key"],
        value=producer.published_messages[0]["value"],
    )

    index_consumer = IndexConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )
    # Index once -> doc becomes READY
    await index_consumer.process_message(parsed_msg)

    async with tenant_transaction(tenant_id, app_session_maker) as session:
        initial_chunks = (
            await session.execute(select(Chunk).where(Chunk.document_id == doc_id))
        ).scalars().all()
        assert len(initial_chunks) > 0
        initial_chunk_count = len(initial_chunks)

    # Deliver duplicate document.uploaded event
    await parse_consumer.process_message(uploaded_msg)

    # Deliver duplicate document.parsed event
    await index_consumer.process_message(parsed_msg)

    # Verify status is still READY and chunk count is unchanged
    async with tenant_transaction(tenant_id, app_session_maker) as session:
        doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc.status == DocumentStatus.READY.value

        current_chunks = (
            await session.execute(select(Chunk).where(Chunk.document_id == doc_id))
        ).scalars().all()
        assert len(current_chunks) == initial_chunk_count


async def test_pipeline_corrupt_file_and_dlq(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Corrupt file transitions document to FAILED (CORRUPT_FILE) and emits message to DLQ with headers."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "corrupt")
    headers = {"Authorization": f"Bearer {token}"}
    storage = get_storage()

    corrupt_pdf = b"%PDF-1.4 corrupt content not a valid pdf %%EOF"
    res = await async_client.post(
        "/api/v1/documents",
        files={"file": ("corrupt.pdf", corrupt_pdf, "application/pdf")},
        headers=headers,
    )
    assert res.status_code == 202
    doc_id = uuid.UUID(res.json()["id"])


    producer = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker)
    await relay.publish_batch(producer)

    msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=producer.published_messages[0]["key"],
        value=producer.published_messages[0]["value"],
    )

    parse_consumer = ParseConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )
    await parse_consumer.process_message(msg)

    # Verify DB status is FAILED
    async with tenant_transaction(tenant_id, app_session_maker) as session:
        doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc.status == DocumentStatus.FAILED.value
        assert doc.error_code == DocumentErrorCode.CORRUPT_FILE.value
        assert doc.failed_stage == "parsing"

    # Verify DLQ message in producer
    dlq_messages = [m for m in producer.published_messages if "dlq" in m["topic"]]
    assert len(dlq_messages) == 1
    dlq_headers = dict(dlq_messages[0]["headers"])
    err_val = dlq_headers.get("error_code") or dlq_headers.get(b"error_code")
    assert err_val is not None
    assert (err_val.decode() if isinstance(err_val, bytes) else err_val) == DocumentErrorCode.CORRUPT_FILE.value
    stage_val = dlq_headers.get("stage") or dlq_headers.get(b"stage")
    assert stage_val is not None
    assert (stage_val.decode() if isinstance(stage_val, bytes) else stage_val) == "parsing"


async def test_pipeline_scanned_image_only_pdf(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Scanned or image-only PDF transitions to FAILED with NO_EXTRACTABLE_TEXT."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "scanned")
    headers = {"Authorization": f"Bearer {token}"}
    storage = get_storage()

    scanned_bytes = create_scanned_pdf()
    res = await async_client.post(
        "/api/v1/documents",
        files={"file": ("scanned.pdf", scanned_bytes, "application/pdf")},
        headers=headers,
    )
    assert res.status_code == 202
    doc_id = uuid.UUID(res.json()["id"])


    producer = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker)
    await relay.publish_batch(producer)

    msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=producer.published_messages[0]["key"],
        value=producer.published_messages[0]["value"],
    )

    parse_consumer = ParseConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer,
        session_maker=app_session_maker,
        storage=storage,
    )
    await parse_consumer.process_message(msg)

    async with tenant_transaction(tenant_id, app_session_maker) as session:
        doc = (await session.execute(select(Document).where(Document.id == doc_id))).scalar_one()
        assert doc.status == DocumentStatus.FAILED.value
        assert doc.error_code == DocumentErrorCode.NO_EXTRACTABLE_TEXT.value


class FlakyStorageWrapper:
    """Storage wrapper simulating transient errors before succeeding or always failing."""

    def __init__(self, inner: StorageBackend, fail_count: int) -> None:
        self.inner = inner
        self.fail_count = fail_count
        self.attempts = 0

    def open(self, path: str) -> BinaryIO:
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise TransientError("Simulated transient I/O delay or lock conflict")
        return self.inner.open(path)

    def exists(self, path: str) -> bool:
        return self.inner.exists(path)

    async def put_stream(self, path: str, stream: Any) -> int:
        return await self.inner.put_stream(path, stream)

    def delete_prefix(self, prefix: str) -> None:
        self.inner.delete_prefix(prefix)


async def test_pipeline_transient_retry_and_exhaustion(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Transient failures retry in-process up to max_attempts; exhaustion routes to DLQ."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "retry")
    headers = {"Authorization": f"Bearer {token}"}
    real_storage = get_storage()

    # Case A: Fails twice, succeeds on attempt 3 (within max_attempts=3)
    res_a = await async_client.post(
        "/api/v1/documents",
        files={"file": ("flaky.txt", b"Content that succeeds on retry.", "text/plain")},
        headers=headers,
    )
    doc_a_id = uuid.UUID(res_a.json()["id"])

    producer_a = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker)
    await relay.publish_batch(producer_a)

    flaky_storage = FlakyStorageWrapper(real_storage, fail_count=2)
    consumer_a = ParseConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer_a,
        session_maker=app_session_maker,
        storage=flaky_storage,
        max_attempts=3,
    )

    msg_a = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=producer_a.published_messages[0]["key"],
        value=producer_a.published_messages[0]["value"],
    )
    await consumer_a.process_message(msg_a)

    async with tenant_transaction(tenant_id, app_session_maker) as session:
        doc_a = (await session.execute(select(Document).where(Document.id == doc_a_id))).scalar_one()
        assert doc_a.status == DocumentStatus.PARSED.value

    # Case B: Always fails (exceeds max_attempts=2) -> marks FAILED with RETRIES_EXHAUSTED
    res_b = await async_client.post(
        "/api/v1/documents",
        files={"file": ("exhausted.txt", b"Content that exhausts retries.", "text/plain")},
        headers=headers,
    )
    doc_b_id = uuid.UUID(res_b.json()["id"])

    producer_b = MockKafkaProducer()
    await relay.publish_batch(producer_b)

    permanently_flaky = FlakyStorageWrapper(real_storage, fail_count=999)
    consumer_b = ParseConsumer(
        consumer=MockKafkaConsumer(),
        producer=producer_b,
        session_maker=app_session_maker,
        storage=permanently_flaky,
        max_attempts=2,
    )

    msg_b_data = next(
        m for m in producer_b.published_messages if m["key"] == str(doc_b_id).encode("utf-8")
    )
    msg_b = FakeKafkaMessage(
        topic=msg_b_data["topic"],
        partition=0,
        offset=1,
        key=msg_b_data["key"],
        value=msg_b_data["value"],
    )
    await consumer_b.process_message(msg_b)


    async with tenant_transaction(tenant_id, app_session_maker) as session:
        doc_b = (await session.execute(select(Document).where(Document.id == doc_b_id))).scalar_one()
        assert doc_b.status == DocumentStatus.FAILED.value
        assert doc_b.error_code == DocumentErrorCode.RETRIES_EXHAUSTED.value

    dlq_messages = [m for m in producer_b.published_messages if "dlq" in m["topic"]]
    assert len(dlq_messages) == 1
    dlq_headers_b = dict(dlq_messages[0]["headers"])
    err_b = dlq_headers_b.get("error_code") or dlq_headers_b.get(b"error_code")
    assert err_b is not None
    assert (err_b.decode() if isinstance(err_b, bytes) else err_b) == DocumentErrorCode.RETRIES_EXHAUSTED.value


async def test_pipeline_poison_pill_handling(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Malformed or schema-invalid Kafka messages route directly to DLQ and commit offset without crashing."""
    producer = MockKafkaProducer()
    consumer = MockKafkaConsumer()
    parse_consumer = ParseConsumer(
        consumer=consumer,
        producer=producer,
        session_maker=app_session_maker,
    )

    # 1. Unparseable bytes
    garbage_msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=1,
        offset=42,
        key=b"corrupt-key",
        value=b"not-json{{{",
    )
    await parse_consumer.process_message(garbage_msg)

    # Offset must be committed
    assert len(consumer.committed) == 1
    # DLQ message published
    assert len(producer.published_messages) == 1
    assert "dlq" in producer.published_messages[0]["topic"]

    # 2. Valid JSON but missing required fields
    invalid_schema_msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=1,
        offset=43,
        key=b"missing-fields",
        value=json.dumps({"wrong": "envelope"}).encode("utf-8"),
    )
    await parse_consumer.process_message(invalid_schema_msg)
    assert len(producer.published_messages) == 2


async def test_pipeline_delete_during_ingestion(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Document deleted while event was in-flight is gracefully skipped with no crash or DLQ noise."""
    tenant_id, token = await register_tenant_and_get_token(async_client, "delete-flight")
    headers = {"Authorization": f"Bearer {token}"}

    res = await async_client.post(
        "/api/v1/documents",
        files={"file": ("to_delete.txt", b"Text that will be deleted in flight.", "text/plain")},
        headers=headers,
    )
    doc_id = uuid.UUID(res.json()["id"])

    producer = MockKafkaProducer()
    relay = OutboxRelay(session_maker=app_session_maker)
    await relay.publish_batch(producer)

    msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=producer.published_messages[0]["key"],
        value=producer.published_messages[0]["value"],
    )

    # Delete the document before consumer processes it
    del_res = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
    assert del_res.status_code == 200


    consumer = MockKafkaConsumer()
    parse_consumer = ParseConsumer(
        consumer=consumer,
        producer=producer,
        session_maker=app_session_maker,
    )
    await parse_consumer.process_message(msg)

    # Consumer offset committed
    assert len(consumer.committed) == 1
    # No DLQ messages sent
    dlq_messages = [m for m in producer.published_messages if "dlq" in m["topic"]]
    assert len(dlq_messages) == 0


async def test_pipeline_cross_tenant_isolation(
    async_client: AsyncClient,
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Envelope with tenant mismatch is rejected by tenant_transaction and untouched."""
    tenant_a_id, token_a = await register_tenant_and_get_token(async_client, "tenant-a")
    tenant_b_id, _ = await register_tenant_and_get_token(async_client, "tenant-b")

    res = await async_client.post(
        "/api/v1/documents",
        files={"file": ("doc_a.txt", b"Tenant A private document.", "text/plain")},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    doc_a_id = uuid.UUID(res.json()["id"])

    # Construct spoofed message claiming doc_a belongs to tenant_b
    envelope = DocumentEventEnvelope(
        event_type="document.uploaded",
        tenant_id=tenant_b_id,  # Wrong tenant!
        document_id=doc_a_id,
        correlation_id="spoofed-corr",
        payload=DocumentEventPayload(chunking_config_id=uuid.uuid4()),
    )
    spoofed_msg = FakeKafkaMessage(
        topic="vaultly.document.uploaded",
        partition=0,
        offset=0,
        key=str(doc_a_id).encode("utf-8"),
        value=envelope.model_dump_json().encode("utf-8"),
    )

    producer = MockKafkaProducer()
    consumer = MockKafkaConsumer()
    parse_consumer = ParseConsumer(
        consumer=consumer,
        producer=producer,
        session_maker=app_session_maker,
    )
    await parse_consumer.process_message(spoofed_msg)

    # Document in Tenant A was NOT claimed or modified
    async with tenant_transaction(tenant_a_id, app_session_maker) as session:
        doc = (await session.execute(select(Document).where(Document.id == doc_a_id))).scalar_one()
        assert doc.status == DocumentStatus.UPLOADED.value
        assert doc.attempts == 0


async def test_outbox_relay_concurrency_no_duplicates(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent OutboxRelay workers use SELECT FOR UPDATE SKIP LOCKED to prevent duplicate publishes."""
    # Seed 10 outbox events
    async with app_session_maker() as session:
        async with session.begin():
            for i in range(10):
                ev = OutboxEvent(
                    id=uuid.uuid4(),
                    topic="vaultly.document.uploaded",
                    key=f"key-{i}",
                    payload={"event_idx": i},
                    headers={"schema_version": "1"},
                )
                session.add(ev)

    producer = MockKafkaProducer()
    relay1 = OutboxRelay(session_maker=app_session_maker, batch_size=5)
    relay2 = OutboxRelay(session_maker=app_session_maker, batch_size=5)

    # Run two relays concurrently
    res1, res2 = await asyncio.gather(
        relay1.publish_batch(producer),
        relay2.publish_batch(producer),
    )

    # Total published must be exactly 10 across both workers
    assert res1 + res2 == 10
    assert len(producer.published_messages) == 10

    # Ensure all 10 are marked published in the DB
    async with app_session_maker() as session:
        stmt = select(OutboxEvent).where(OutboxEvent.published_at.is_not(None))
        published_events = (await session.execute(stmt)).scalars().all()
        assert len(published_events) == 10


@pytest.mark.slow
def test_real_embedding_model() -> None:
    """Verify production embedding provider generates 384-dim normalized vectors."""
    provider = LocalEmbeddingProvider("BAAI/bge-small-en-v1.5")
    texts = [
        "Vaultly enterprise document search.",
        "Row level security multi tenant architecture.",
    ]
    doc_vectors = provider.embed_documents(texts)
    assert len(doc_vectors) == 2
    for vec in doc_vectors:
        assert len(vec) == 384
        # Normalized vector has norm approximately 1.0
        norm = sum(x * x for x in vec) ** 0.5
        assert pytest.approx(norm, rel=1e-3) == 1.0

    query_vec = provider.embed_query("How does Vaultly enforce security?")
    assert len(query_vec) == 384
    query_norm = sum(x * x for x in query_vec) ** 0.5
    assert pytest.approx(query_norm, rel=1e-3) == 1.0
