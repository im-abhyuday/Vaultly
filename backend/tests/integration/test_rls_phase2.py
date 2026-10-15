"""Integration tests for Phase 2 PostgreSQL Row-Level Security (RLS) and multi-tenant isolation.

Verifies tenant isolation, cross-tenant mutation prevention, composite FK enforcement,
and cascading deletes on Phase 2 tables:
- chunking_configs
- documents
- document_acl
- chunks
- outbox_events (system table allowlist)
All operations run against real PostgreSQL using the unprivileged runtime role (tenantrag_app).
"""

import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from app.db.session import set_tenant_context, tenant_transaction
from app.models.chunk import Chunk
from app.models.chunking_config import ChunkingConfig
from app.models.document import Document
from app.models.document_acl import DocumentACL
from app.models.enums import DocumentStatus, DocumentVisibility, RoleEnum
from app.models.group import Group
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.models.tenant import Tenant
from app.models.user import User


async def seed_phase2_data(
    owner_session_maker: async_sessionmaker[AsyncSession],
) -> dict[str, uuid.UUID]:
    """Seed two distinct tenants with users, configs, documents, ACLs, and chunks."""
    async with owner_session_maker() as session:
        tid_a = uuid.uuid4()
        tid_b = uuid.uuid4()
        uid_a = uuid.uuid4()
        uid_b = uuid.uuid4()
        gid_a = uuid.uuid4()
        gid_b = uuid.uuid4()
        cfg_a_id = uuid.uuid4()
        cfg_b_id = uuid.uuid4()
        doc_a_id = uuid.uuid4()
        doc_b_id = uuid.uuid4()
        acl_a_id = uuid.uuid4()
        acl_b_id = uuid.uuid4()
        chunk_a_id = uuid.uuid4()
        chunk_b_id = uuid.uuid4()

        # Tenants
        tenant_a = Tenant(id=tid_a, name="Tenant Alpha P2", slug=f"alpha-p2-{uuid.uuid4().hex[:6]}")
        tenant_b = Tenant(id=tid_b, name="Tenant Beta P2", slug=f"beta-p2-{uuid.uuid4().hex[:6]}")
        session.add_all([tenant_a, tenant_b])

        # Users
        user_a = User(
            id=uid_a,
            email=f"user_a_{uuid.uuid4().hex[:6]}@alpha.com",
            password_hash="argon2_hash_a",
            full_name="User Alpha P2",
        )
        user_b = User(
            id=uid_b,
            email=f"user_b_{uuid.uuid4().hex[:6]}@beta.com",
            password_hash="argon2_hash_b",
            full_name="User Beta P2",
        )
        session.add_all([user_a, user_b])
        await session.flush()

        # Memberships
        mem_a = Membership(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            user_id=uid_a,
            role=RoleEnum.ADMIN.value,
        )
        mem_b = Membership(
            id=uuid.uuid4(),
            tenant_id=tid_b,
            user_id=uid_b,
            role=RoleEnum.MEMBER.value,
        )
        session.add_all([mem_a, mem_b])

        # Groups
        grp_a = Group(
            id=gid_a,
            tenant_id=tid_a,
            name="Alpha Team",
            description="Alpha group",
        )
        grp_b = Group(
            id=gid_b,
            tenant_id=tid_b,
            name="Beta Team",
            description="Beta group",
        )
        session.add_all([grp_a, grp_b])
        await session.flush()

        # Chunking Configs
        cfg_a = ChunkingConfig(
            id=cfg_a_id,
            tenant_id=tid_a,
            name="Default Config Alpha",
            strategy="recursive",
            chunk_size_tokens=256,
            chunk_overlap_tokens=32,
            embedding_model="BAAI/bge-small-en-v1.5",
            embedding_dim=384,
            is_active=True,
        )
        cfg_b = ChunkingConfig(
            id=cfg_b_id,
            tenant_id=tid_b,
            name="Default Config Beta",
            strategy="recursive",
            chunk_size_tokens=256,
            chunk_overlap_tokens=32,
            embedding_model="BAAI/bge-small-en-v1.5",
            embedding_dim=384,
            is_active=True,
        )
        session.add_all([cfg_a, cfg_b])
        await session.flush()

        # Documents
        doc_a = Document(
            id=doc_a_id,
            tenant_id=tid_a,
            owner_id=uid_a,
            filename="alpha_doc.txt",
            detected_type="txt",
            size_bytes=1024,
            checksum_sha256="checksum_alpha_p2_12345",
            storage_key=f"tenants/{tid_a}/documents/{doc_a_id}/original",
            status=DocumentStatus.READY.value,
            visibility=DocumentVisibility.GROUP.value,
            chunking_config_id=cfg_a_id,
            attempts=0,
            chunk_count=1,
        )
        doc_b = Document(
            id=doc_b_id,
            tenant_id=tid_b,
            owner_id=uid_b,
            filename="beta_doc.txt",
            detected_type="txt",
            size_bytes=2048,
            checksum_sha256="checksum_beta_p2_67890",
            storage_key=f"tenants/{tid_b}/documents/{doc_b_id}/original",
            status=DocumentStatus.READY.value,
            visibility=DocumentVisibility.PRIVATE.value,
            chunking_config_id=cfg_b_id,
            attempts=0,
            chunk_count=1,
        )
        session.add_all([doc_a, doc_b])
        await session.flush()

        # Document ACLs
        acl_a = DocumentACL(
            id=acl_a_id,
            tenant_id=tid_a,
            document_id=doc_a_id,
            group_id=gid_a,
            user_id=None,
        )
        acl_b = DocumentACL(
            id=acl_b_id,
            tenant_id=tid_b,
            document_id=doc_b_id,
            group_id=None,
            user_id=uid_b,
        )
        session.add_all([acl_a, acl_b])

        # Chunks
        chunk_a = Chunk(
            id=chunk_a_id,
            tenant_id=tid_a,
            document_id=doc_a_id,
            chunking_config_id=cfg_a_id,
            ordinal=0,
            text="Alpha chunk content text",
            token_count=5,
            embedding=[0.1] * 384,
            chunk_metadata={"page_start": 1, "page_end": 1},
        )
        chunk_b = Chunk(
            id=chunk_b_id,
            tenant_id=tid_b,
            document_id=doc_b_id,
            chunking_config_id=cfg_b_id,
            ordinal=0,
            text="Beta chunk content text",
            token_count=5,
            embedding=[0.2] * 384,
            chunk_metadata={"page_start": 1, "page_end": 1},
        )
        session.add_all([chunk_a, chunk_b])

        # System Outbox Events
        outbox_a = OutboxEvent(
            id=uuid.uuid4(),
            topic="document.uploaded",
            key=str(doc_a_id),
            payload={"document_id": str(doc_a_id), "tenant_id": str(tid_a)},
        )
        outbox_b = OutboxEvent(
            id=uuid.uuid4(),
            topic="document.uploaded",
            key=str(doc_b_id),
            payload={"document_id": str(doc_b_id), "tenant_id": str(tid_b)},
        )
        session.add_all([outbox_a, outbox_b])

        await session.commit()

        return {
            "tid_a": tid_a,
            "tid_b": tid_b,
            "uid_a": uid_a,
            "uid_b": uid_b,
            "gid_a": gid_a,
            "gid_b": gid_b,
            "cfg_a_id": cfg_a_id,
            "cfg_b_id": cfg_b_id,
            "doc_a_id": doc_a_id,
            "doc_b_id": doc_b_id,
            "acl_a_id": acl_a_id,
            "acl_b_id": acl_b_id,
            "chunk_a_id": chunk_a_id,
            "chunk_b_id": chunk_b_id,
        }


@pytest.mark.asyncio
async def test_phase2_tenant_isolation_reads(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Under Tenant A context, queries on Phase 2 tables return strictly Tenant A rows."""
    seeded = await seed_phase2_data(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # 1. Documents
        docs = (await session.execute(select(Document))).scalars().all()
        assert len(docs) == 1
        assert docs[0].id == seeded["doc_a_id"]
        assert docs[0].tenant_id == tid_a

        # 2. Chunks
        chunks = (await session.execute(select(Chunk))).scalars().all()
        assert len(chunks) == 1
        assert chunks[0].id == seeded["chunk_a_id"]
        assert chunks[0].tenant_id == tid_a

        # 3. ChunkingConfigs
        configs = (await session.execute(select(ChunkingConfig))).scalars().all()
        assert len(configs) == 1
        assert configs[0].id == seeded["cfg_a_id"]
        assert configs[0].tenant_id == tid_a

        # 4. DocumentACL
        acls = (await session.execute(select(DocumentACL))).scalars().all()
        assert len(acls) == 1
        assert acls[0].id == seeded["acl_a_id"]
        assert acls[0].tenant_id == tid_a

    # Now verify Tenant B context
    async with tenant_transaction(tid_b, app_session_maker) as session:
        docs_b = (await session.execute(select(Document))).scalars().all()
        assert len(docs_b) == 1
        assert docs_b[0].id == seeded["doc_b_id"]
        assert docs_b[0].tenant_id == tid_b

        chunks_b = (await session.execute(select(Chunk))).scalars().all()
        assert len(chunks_b) == 1
        assert chunks_b[0].id == seeded["chunk_b_id"]


@pytest.mark.asyncio
async def test_phase2_no_tenant_context_returns_zero_rows(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Without tenant context, queries on Phase 2 tenant tables return 0 rows (fail closed)."""
    await seed_phase2_data(owner_session_maker)

    async with app_session_maker() as session:
        for model in [Document, Chunk, ChunkingConfig, DocumentACL]:
            res = (await session.execute(select(model))).scalars().all()
            assert len(res) == 0, f"Table {model.__tablename__} leaked rows without tenant context"


@pytest.mark.asyncio
async def test_phase2_cross_tenant_insert_and_mutation_rejected(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Cross-tenant INSERT fails WITH CHECK, and UPDATE/DELETE affect 0 rows."""
    seeded = await seed_phase2_data(owner_session_maker)
    tid_a, tid_b = seeded["tid_a"], seeded["tid_b"]
    doc_b_id = seeded["doc_b_id"]

    # 1. Attempt cross-tenant INSERT under Tenant A context
    async with app_session_maker() as session:
        await set_tenant_context(session, tid_a)
        rogue_doc = Document(
            id=uuid.uuid4(),
            tenant_id=tid_b,  # Tenant B's ID under Tenant A context
            owner_id=seeded["uid_a"],
            filename="rogue.txt",
            detected_type="txt",
            size_bytes=100,
            checksum_sha256="rogue_checksum",
            storage_key="rogue_key",
            status=DocumentStatus.UPLOADED.value,
            chunking_config_id=seeded["cfg_a_id"],
        )
        session.add(rogue_doc)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "tenant_isolation_policy" in str(exc_info.value).lower() or "check" in str(exc_info.value).lower()
        await session.rollback()

    # 2. Attempt cross-tenant UPDATE and DELETE on Tenant B's document under Tenant A context
    async with tenant_transaction(tid_a, app_session_maker) as session:
        update_res = await session.execute(
            update(Document).where(Document.id == doc_b_id).values(filename="hacked.txt")
        )
        assert isinstance(update_res, CursorResult)
        assert update_res.rowcount == 0

        delete_res = await session.execute(
            delete(Document).where(Document.id == doc_b_id)
        )
        assert isinstance(delete_res, CursorResult)
        assert delete_res.rowcount == 0

    # Verify Tenant B's doc is intact
    async with tenant_transaction(tid_b, app_session_maker) as session:
        b_doc = (await session.execute(select(Document).where(Document.id == doc_b_id))).scalar_one_or_none()
        assert b_doc is not None
        assert b_doc.filename == "beta_doc.txt"


@pytest.mark.asyncio
async def test_phase2_document_acl_composite_fk_rejection(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Composite FKs on document_acl reject cross-tenant links and enforce XOR constraint."""
    seeded = await seed_phase2_data(owner_session_maker)
    tid_a = seeded["tid_a"]
    doc_a_id = seeded["doc_a_id"]
    gid_b = seeded["gid_b"]
    uid_b = seeded["uid_b"]

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # 1. Cross-tenant group_id: Tenant A doc linking Tenant B group
        bad_group_acl = DocumentACL(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            document_id=doc_a_id,
            group_id=gid_b,
            user_id=None,
        )
        session.add(bad_group_acl)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "foreign key" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()
        await session.rollback()

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # 2. Cross-tenant user_id: Tenant A doc linking Tenant B user
        bad_user_acl = DocumentACL(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            document_id=doc_a_id,
            group_id=None,
            user_id=uid_b,
        )
        session.add(bad_user_acl)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "foreign key" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()
        await session.rollback()

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # 3. Check constraint violation: both group_id and user_id set
        both_acl = DocumentACL(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            document_id=doc_a_id,
            group_id=seeded["gid_a"],
            user_id=seeded["uid_a"],
        )
        session.add(both_acl)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "check" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()
        await session.rollback()

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # 4. Check constraint violation: neither group_id nor user_id set
        neither_acl = DocumentACL(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            document_id=doc_a_id,
            group_id=None,
            user_id=None,
        )
        session.add(neither_acl)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "check" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()
        await session.rollback()


@pytest.mark.asyncio
async def test_phase2_chunks_composite_fk_rejection(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Composite FK on chunks (tenant_id, document_id) rejects linking another tenant's document."""
    seeded = await seed_phase2_data(owner_session_maker)
    tid_a = seeded["tid_a"]
    doc_b_id = seeded["doc_b_id"]

    async with tenant_transaction(tid_a, app_session_maker) as session:
        bad_chunk = Chunk(
            id=uuid.uuid4(),
            tenant_id=tid_a,
            document_id=doc_b_id,  # Beta's doc with Alpha's tenant_id
            chunking_config_id=seeded["cfg_a_id"],
            ordinal=99,
            text="Illegal chunk",
            token_count=2,
            embedding=[0.0] * 384,
            chunk_metadata={},
        )
        session.add(bad_chunk)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            await session.flush()
        assert "foreign key" in str(exc_info.value).lower() or "violates" in str(exc_info.value).lower()
        await session.rollback()


@pytest.mark.asyncio
async def test_phase2_delete_cascade(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Deleting a document cascades deletion to its chunks and ACL entries."""
    seeded = await seed_phase2_data(owner_session_maker)
    tid_a = seeded["tid_a"]
    doc_a_id = seeded["doc_a_id"]
    chunk_a_id = seeded["chunk_a_id"]
    acl_a_id = seeded["acl_a_id"]

    async with tenant_transaction(tid_a, app_session_maker) as session:
        # Delete document A
        del_res = await session.execute(delete(Document).where(Document.id == doc_a_id))
        assert isinstance(del_res, CursorResult)
        assert del_res.rowcount == 1

        # Chunks and ACL entries must be cascaded
        remaining_chunks = (
            await session.execute(select(Chunk).where(Chunk.id == chunk_a_id))
        ).scalars().all()
        assert len(remaining_chunks) == 0

        remaining_acls = (
            await session.execute(select(DocumentACL).where(DocumentACL.id == acl_a_id))
        ).scalars().all()
        assert len(remaining_acls) == 0


@pytest.mark.asyncio
async def test_phase2_outbox_is_system_table_accessible_cross_tenant(
    owner_session_maker: async_sessionmaker[AsyncSession],
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Outbox table has no RLS and allows relay to select unpublished events across tenants."""
    seeded = await seed_phase2_data(owner_session_maker)

    async with app_session_maker() as session:
        # Even with no tenant context, outbox_events can be selected
        all_events = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
            )
        ).scalars().all()
        assert len(all_events) >= 2

        # Both Alpha and Beta event keys are readable
        doc_keys = {ev.key for ev in all_events}
        assert str(seeded["doc_a_id"]) in doc_keys
        assert str(seeded["doc_b_id"]) in doc_keys
