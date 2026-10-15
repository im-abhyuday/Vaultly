"""Integration tests for Phase 2A: Document Upload, Storage, ACL, and Outbox.

Tests upload streaming, file type validation, magic bytes, zip-bomb defense,
owner-scoped deduplication, atomicity file cleanup, permissions matrix
(metadata vs content access), cascading deletes, and reprocessing.
All operations execute against the real PostgreSQL database and runtime role.
"""

import io
import uuid
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import tenant_transaction
from app.main import app
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.enums import DocumentStatus
from app.models.outbox import OutboxEvent
from app.services.storage import get_document_storage_key, get_storage

settings = get_settings()


def create_test_docx(text: str = "Hello Vaultly docx text") -> bytes:
    """Create a minimal valid docx in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
        )
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
        )
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def create_test_pdf() -> bytes:
    """Create a minimal valid PDF in memory."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000117 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n168\n%%EOF\n"
    )


async def register_tenant_and_users(client: AsyncClient) -> dict[str, str]:
    """Helper to register a tenant with admin, member1, member2, and auditor."""
    slug = f"org-{uuid.uuid4().hex[:8]}"
    # 1. Admin / Tenant creator
    reg = {
        "tenant_name": f"Org {slug}",
        "tenant_slug": slug,
        "email": f"admin@{slug}.com",
        "password": "Password12345!",
        "full_name": "Admin User",
    }
    res = await client.post("/api/v1/auth/register-tenant", json=reg)
    assert res.status_code == 201
    admin_token = res.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 2. Member 1
    inv1 = await client.post(
        "/api/v1/users/invite",
        json={"email": f"member1@{slug}.com", "role": "MEMBER"},
        headers=admin_headers,
    )
    acc1 = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": inv1.json()["invite_token"],
            "password": "Password12345!",
            "full_name": "Member One",
        },
    )
    member1_token = acc1.json()["access_token"]

    # 3. Member 2
    inv2 = await client.post(
        "/api/v1/users/invite",
        json={"email": f"member2@{slug}.com", "role": "MEMBER"},
        headers=admin_headers,
    )
    acc2 = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": inv2.json()["invite_token"],
            "password": "Password12345!",
            "full_name": "Member Two",
        },
    )
    member2_token = acc2.json()["access_token"]

    # 4. Auditor
    inv_aud = await client.post(
        "/api/v1/users/invite",
        json={"email": f"auditor@{slug}.com", "role": "AUDITOR"},
        headers=admin_headers,
    )
    acc_aud = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": inv_aud.json()["invite_token"],
            "password": "Password12345!",
            "full_name": "Auditor User",
        },
    )
    auditor_token = acc_aud.json()["access_token"]

    return {
        "slug": slug,
        "admin_token": admin_token,
        "member1_token": member1_token,
        "member2_token": member2_token,
        "auditor_token": auditor_token,
    }


@pytest.mark.asyncio
async def test_document_upload_success_and_deduplication(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify document upload returns 202, dedupe returns 200, and owner scoping prevents cross-owner leaks."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        m1_headers = {"Authorization": f"Bearer {tokens['member1_token']}"}
        m2_headers = {"Authorization": f"Bearer {tokens['member2_token']}"}

        pdf_bytes = create_test_pdf()

        # 1. Member 1 uploads valid PDF -> 202 Accepted, duplicate=False
        files = {"file": ("sample.pdf", pdf_bytes, "application/pdf")}
        data = {"visibility": "PRIVATE"}
        res1 = await client.post("/api/v1/documents", files=files, data=data, headers=m1_headers)
        assert res1.status_code == 202
        doc1 = res1.json()
        assert doc1["duplicate"] is False
        assert doc1["status"] == "UPLOADED"
        assert doc1["filename"] == "sample.pdf"
        doc1_id = doc1["id"]

        # 2. Member 1 uploads identical file again -> 200 OK, duplicate=True, same document ID
        files_dup = {"file": ("sample.pdf", pdf_bytes, "application/pdf")}
        res_dup = await client.post(
            "/api/v1/documents", files=files_dup, data=data, headers=m1_headers
        )
        assert res_dup.status_code == 200
        doc_dup = res_dup.json()
        assert doc_dup["duplicate"] is True
        assert doc_dup["id"] == doc1_id

        # Check DB: only 1 document row and 1 outbox event exists for doc1_id
        async with app_session_maker() as session:
            outbox_events = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.key == str(doc1_id))
                )
            ).scalars().all()
            assert len(outbox_events) == 1
            assert outbox_events[0].topic.endswith("document.uploaded")

        # 3. Member 2 uploads identical file -> 202 Accepted, duplicate=False, distinct document ID
        files_m2 = {"file": ("sample.pdf", pdf_bytes, "application/pdf")}
        res_m2 = await client.post(
            "/api/v1/documents", files=files_m2, data=data, headers=m2_headers
        )
        assert res_m2.status_code == 202
        doc_m2 = res_m2.json()
        assert doc_m2["duplicate"] is False
        assert doc_m2["id"] != doc1_id


@pytest.mark.asyncio
async def test_document_upload_validation_errors() -> None:
    """Verify validation: empty file, bad extension, fake pdf, zip-bomb docx, NUL bytes, filename sanitization."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        headers = {"Authorization": f"Bearer {tokens['member1_token']}"}

        # 1. Empty file (0 bytes) -> 400
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("empty.txt", b"", "text/plain")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "empty" in res.json()["detail"].lower()

        # 2. Unsupported extension (.exe) -> 400
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("malware.exe", b"MZbinary", "application/octet-stream")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "unsupported" in res.json()["detail"].lower()

        # 3. Fake PDF (.exe renamed to .pdf) -> 400 (magic bytes mismatch)
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("fake.pdf", b"MZ\x90\x00notapdf", "application/pdf")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "%pdf-" in res.json()["detail"].lower()

        # 4. Text file with NUL bytes -> 400
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("bad.txt", b"hello\x00world", "text/plain")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "nul" in res.json()["detail"].lower()

        # 5. Invalid DOCX (not a zip file) -> 400
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("corrupt.docx", b"not-a-zip-content", "application/vnd.openxmlformats")},
            headers=headers,
        )
        assert res.status_code == 400

        # 6. DOCX zip-bomb defense (ratio exceeded) -> 400
        bomb_buf = io.BytesIO()
        with zipfile.ZipFile(bomb_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "A" * 50000)
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("bomb.docx", bomb_buf.getvalue(), "application/vnd.openxmlformats")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "ratio" in res.json()["detail"].lower() or "bomb" in res.json()["detail"].lower()

        # 7. Filename sanitization (path traversal stripped) -> 202, filename is "passwd.txt"
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("../../etc/passwd.txt", b"clean text content", "text/plain")},
            headers=headers,
        )
        assert res.status_code == 202
        assert res.json()["filename"] == "passwd.txt"

        # 8. Unicode filename preserved -> 202
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("résumé_2026.txt", b"unicode distinct text content", "text/plain")},
            headers=headers,
        )
        assert res.status_code == 202
        assert res.json()["filename"] == "résumé_2026.txt"


@pytest.mark.asyncio
async def test_document_upload_oversize_rejected_and_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify upload exceeding MAX_UPLOAD_MB aborts mid-stream with 413 and leaves no temp file."""
    # Temporarily cap MAX_UPLOAD_MB to 1 MB for testing
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        headers = {"Authorization": f"Bearer {tokens['member1_token']}"}

        # 1.5 MB payload
        large_payload = b"A" * (1500 * 1024)
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("oversize.txt", large_payload, "text/plain")},
            headers=headers,
        )
        assert res.status_code == 413
        assert "maximum allowed size" in res.json()["detail"].lower()

        # Verify no temp files remain in storage temp dir
        temp_dir = Path(settings.STORAGE_LOCAL_ROOT) / "temp"
        if temp_dir.exists():
            assert len(list(temp_dir.glob("upload_*.tmp"))) == 0


@pytest.mark.asyncio
async def test_document_permissions_matrix(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify role x visibility x ACL matrix:

    - Admin/Auditor see metadata everywhere
    - Admin/Auditor CANNOT access private document chunks (NO content bypass)
    - Group and User ACL rules govern content access
    - 404 for hidden or cross-tenant IDs
    - Auditor upload rejected (403)
    - Anonymous rejected (401)
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        admin_h = {"Authorization": f"Bearer {tokens['admin_token']}"}
        m1_h = {"Authorization": f"Bearer {tokens['member1_token']}"}
        m2_h = {"Authorization": f"Bearer {tokens['member2_token']}"}
        aud_h = {"Authorization": f"Bearer {tokens['auditor_token']}"}

        # 1. Auditor cannot upload -> 403 Forbidden
        aud_upload = await client.post(
            "/api/v1/documents",
            files={"file": ("doc.txt", b"auditor doc", "text/plain")},
            headers=aud_h,
        )
        assert aud_upload.status_code == 403

        # 2. Anonymous cannot upload -> 401 Unauthorized
        anon_upload = await client.post(
            "/api/v1/documents",
            files={"file": ("doc.txt", b"anon doc", "text/plain")},
        )
        assert anon_upload.status_code == 401

        # 3. Create a Group and add Member 2 to it
        group_res = await client.post(
            "/api/v1/groups",
            json={"name": "Engineering", "description": "Eng team"},
            headers=admin_h,
        )
        assert group_res.status_code == 201
        group_id = group_res.json()["id"]

        # Add Member 2 to group
        # Get member 2 user_id from /auth/me
        m2_me = await client.get("/api/v1/auth/me", headers=m2_h)
        m2_user_id = m2_me.json()["user_id"]

        add_mem_res = await client.post(
            f"/api/v1/groups/{group_id}/members",
            json={"user_id": m2_user_id},
            headers=admin_h,
        )
        assert add_mem_res.status_code == 201

        # 4. Member 1 uploads:
        # A) doc_private: PRIVATE
        doc_priv_res = await client.post(
            "/api/v1/documents",
            files={"file": ("private.txt", b"Private text content 1", "text/plain")},
            data={"visibility": "PRIVATE"},
            headers=m1_h,
        )
        assert doc_priv_res.status_code == 202
        doc_priv_id = doc_priv_res.json()["id"]

        # B) doc_group: GROUP (linked to Engineering group)
        doc_grp_res = await client.post(
            "/api/v1/documents",
            files={"file": ("group.txt", b"Group text content 2", "text/plain")},
            data={"visibility": "GROUP", "group_ids": group_id},
            headers=m1_h,
        )
        assert doc_grp_res.status_code == 202
        doc_grp_id = doc_grp_res.json()["id"]

        # C) doc_tenant: TENANT
        doc_ten_res = await client.post(
            "/api/v1/documents",
            files={"file": ("tenant.txt", b"Tenant text content 3", "text/plain")},
            data={"visibility": "TENANT"},
            headers=m1_h,
        )
        assert doc_ten_res.status_code == 202
        doc_ten_id = doc_ten_res.json()["id"]

        # 5. Insert mock chunks for each document directly in DB to test GET /chunks
        tenant_id = uuid.UUID(doc_priv_res.json()["tenant_id"])
        async with tenant_transaction(tenant_id, app_session_maker) as session:
            for d_id, txt in [
                (doc_priv_id, "Private chunk text"),
                (doc_grp_id, "Group chunk text"),
                (doc_ten_id, "Tenant chunk text"),
            ]:
                # Fetch chunking_config_id and tenant_id from document
                doc_obj = (
                    await session.execute(
                        select(Document).where(Document.id == uuid.UUID(d_id))
                    )
                ).scalar_one()
                chunk_obj = Chunk(
                    id=uuid.uuid4(),
                    tenant_id=doc_obj.tenant_id,
                    document_id=doc_obj.id,
                    chunking_config_id=doc_obj.chunking_config_id,
                    ordinal=0,
                    text=txt,
                    token_count=4,
                    embedding=[0.05] * 384,
                    chunk_metadata={"page_start": 1, "page_end": 1},
                )
                session.add(chunk_obj)

        # 6. METADATA VISIBILITY TESTS (GET /documents/{id})
        # Admin can view metadata of all 3
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}", headers=admin_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_grp_id}", headers=admin_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_ten_id}", headers=admin_h)).status_code == 200

        # Auditor can view metadata of all 3
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}", headers=aud_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_grp_id}", headers=aud_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_ten_id}", headers=aud_h)).status_code == 200

        # Member 1 (owner) can view metadata of all 3
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}", headers=m1_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_grp_id}", headers=m1_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_ten_id}", headers=m1_h)).status_code == 200

        # Member 2 (in group) can view metadata of doc_grp and doc_ten; gets 404 for doc_priv
        assert (await client.get(f"/api/v1/documents/{doc_grp_id}", headers=m2_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_ten_id}", headers=m2_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}", headers=m2_h)).status_code == 404

        # 7. CONTENT ACCESSIBILITY TESTS (GET /documents/{id}/chunks)
        # CRITICAL TEST: Admin CANNOT view chunks of private document (NO content bypass!)
        admin_priv_chunks = await client.get(f"/api/v1/documents/{doc_priv_id}/chunks", headers=admin_h)
        assert admin_priv_chunks.status_code == 404

        # Admin CAN view chunks of TENANT document
        admin_ten_chunks = await client.get(f"/api/v1/documents/{doc_ten_id}/chunks", headers=admin_h)
        assert admin_ten_chunks.status_code == 200
        assert len(admin_ten_chunks.json()) == 1

        # Auditor CANNOT view chunks of private document
        aud_priv_chunks = await client.get(f"/api/v1/documents/{doc_priv_id}/chunks", headers=aud_h)
        assert aud_priv_chunks.status_code == 404

        # Member 1 (owner) can view chunks of all 3
        m1_priv_chunks = await client.get(f"/api/v1/documents/{doc_priv_id}/chunks", headers=m1_h)
        assert m1_priv_chunks.status_code == 200
        chunks_data = m1_priv_chunks.json()
        assert len(chunks_data) == 1
        assert chunks_data[0]["text"] == "Private chunk text"
        assert chunks_data[0]["metadata"] == {"page_start": 1, "page_end": 1}
        # Ensure embedding vectors are NOT exposed in API output
        assert "embedding" not in chunks_data[0]

        # Member 2 (in group) can view chunks of doc_grp and doc_ten, but gets 404 on doc_priv
        assert (await client.get(f"/api/v1/documents/{doc_grp_id}/chunks", headers=m2_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_ten_id}/chunks", headers=m2_h)).status_code == 200
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}/chunks", headers=m2_h)).status_code == 404

        # 8. MANAGEMENT PERMISSIONS (PATCH /access, DELETE)
        # Member 2 (non-owner) cannot PATCH access on Member 1's doc -> 404
        bad_patch = await client.patch(
            f"/api/v1/documents/{doc_priv_id}/access",
            json={"visibility": "TENANT"},
            headers=m2_h,
        )
        assert bad_patch.status_code == 404

        # Member 1 (owner) can PATCH access
        good_patch = await client.patch(
            f"/api/v1/documents/{doc_priv_id}/access",
            json={"visibility": "TENANT"},
            headers=m1_h,
        )
        assert good_patch.status_code == 200
        assert good_patch.json()["visibility"] == "TENANT"

        # Now Member 2 can see chunks of formerly private doc because visibility is TENANT
        assert (await client.get(f"/api/v1/documents/{doc_priv_id}/chunks", headers=m2_h)).status_code == 200


@pytest.mark.asyncio
async def test_document_delete_cascades_and_removes_file(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify DELETE /documents/{id} cascades chunks, ACLs, and unlinks file from disk."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        m1_h = {"Authorization": f"Bearer {tokens['member1_token']}"}

        # 1. Upload doc
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("to_delete.txt", b"Document to be deleted", "text/plain")},
            headers=m1_h,
        )
        assert res.status_code == 202
        doc_data = res.json()
        doc_id = doc_data["id"]
        tenant_id = doc_data["tenant_id"]
        storage_key = get_document_storage_key(uuid.UUID(tenant_id), uuid.UUID(doc_id), "original")

        # Verify storage file exists on disk
        storage = get_storage()
        assert storage.exists(storage_key)

        # 2. Insert mock chunk directly
        async with tenant_transaction(uuid.UUID(tenant_id), app_session_maker) as session:
            doc_obj = (
                await session.execute(
                    select(Document).where(Document.id == uuid.UUID(doc_id))
                )
            ).scalar_one()
            chunk_obj = Chunk(
                id=uuid.uuid4(),
                tenant_id=doc_obj.tenant_id,
                document_id=doc_obj.id,
                chunking_config_id=doc_obj.chunking_config_id,
                ordinal=0,
                text="Chunk to be deleted",
                token_count=4,
                embedding=[0.1] * 384,
                chunk_metadata={},
            )
            session.add(chunk_obj)

        # 3. DELETE the document
        del_res = await client.delete(f"/api/v1/documents/{doc_id}", headers=m1_h)
        assert del_res.status_code == 200

        # 4. Verify DB records are gone
        async with tenant_transaction(uuid.UUID(tenant_id), app_session_maker) as session:
            d_check = (
                await session.execute(
                    select(Document).where(Document.id == uuid.UUID(doc_id))
                )
            ).scalar_one_or_none()
            assert d_check is None

            c_check = (
                await session.execute(
                    select(Chunk).where(Chunk.document_id == uuid.UUID(doc_id))
                )
            ).scalars().all()
            assert len(c_check) == 0

        # 5. Verify file is removed from storage
        assert not storage.exists(storage_key)


@pytest.mark.asyncio
async def test_document_reprocess_status_flow(
    app_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Verify reprocess is rejected on UPLOADED/READY, but succeeds on FAILED and creates outbox event."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tokens = await register_tenant_and_users(client)
        m1_h = {"Authorization": f"Bearer {tokens['member1_token']}"}

        # 1. Upload doc (status: UPLOADED)
        res = await client.post(
            "/api/v1/documents",
            files={"file": ("reprocess_test.txt", b"Reprocess test content", "text/plain")},
            headers=m1_h,
        )
        assert res.status_code == 202
        doc_data = res.json()
        doc_id = doc_data["id"]
        tenant_id = uuid.UUID(doc_data["tenant_id"])

        # 2. Attempt to reprocess while UPLOADED -> 400 Bad Request
        bad_rep = await client.post(f"/api/v1/documents/{doc_id}/reprocess", headers=m1_h)
        assert bad_rep.status_code == 400
        assert "only failed" in bad_rep.json()["detail"].lower()

        # 3. Manually simulate failure in DB using tenant_transaction for RLS
        async with tenant_transaction(tenant_id, app_session_maker) as session:
            await session.execute(
                update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(
                    status=DocumentStatus.FAILED.value,
                    error_code="CORRUPT_FILE",
                    error_message="Simulated corruption failure",
                    failed_stage="parsing",
                )
            )

        # 4. Reprocess from FAILED -> 200 OK, reset to UPLOADED, error cleared
        good_rep = await client.post(f"/api/v1/documents/{doc_id}/reprocess", headers=m1_h)
        assert good_rep.status_code == 200
        data = good_rep.json()
        assert data["status"] == "UPLOADED"
        assert data["error_code"] is None
        assert data["error_message"] is None
        assert data["failed_stage"] is None

        # 5. Verify a new OutboxEvent was inserted
        async with app_session_maker() as session:
            events = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.key == str(doc_id))
                )
            ).scalars().all()
            # 1 from initial upload, 1 from reprocess = 2 events
            assert len(events) == 2
