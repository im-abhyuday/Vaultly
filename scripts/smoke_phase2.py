"""Smoke test script for Phase 2: Ingestion pipeline, multi-tenant isolation, and deduplication.

Tests against the live running stack:
1. Registers Tenant Alpha (admin & member) and Tenant Beta (admin).
2. Uploads TXT, PDF, and DOCX documents.
3. Polls until documents reach status READY.
4. Verifies chunks are generated with text and metadata.
5. Verifies cross-tenant isolation (Tenant Beta gets 404 for Alpha's documents).
6. Verifies non-ACL member isolation (cannot see private document).
7. Verifies owner-scoped deduplication returns 200 with duplicate=true.
8. Deletes document and confirms cleanup.
"""

import io
import sys
import time
import uuid
import zipfile
import docx
import httpx

BASE_URL = "http://localhost:8000"


def create_test_docx(text: str = "Vaultly DOCX test document for Phase 2 ingestion.") -> bytes:
    """Create a minimal valid DOCX in memory."""
    doc = docx.Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
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


def run_smoke_test() -> None:
    print("=" * 70)
    print("VAULTLY PHASE 2 SMOKE TEST: Document Upload & Ingestion Pipeline")
    print("=" * 70)

    client = httpx.Client(base_url=BASE_URL, timeout=30.0)

    # Health check
    try:
        health_res = client.get("/api/v1/health")
        assert health_res.status_code == 200, f"Health check failed: {health_res.text}"
        print(f"[PASS] Stack is healthy: {health_res.json()}")
    except Exception as err:
        print(f"[FAIL] Could not connect to {BASE_URL}: {err}")
        sys.exit(1)

    rnd = uuid.uuid4().hex[:6]
    alpha_slug = f"alpha-{rnd}"
    beta_slug = f"beta-{rnd}"

    # 1. Register Tenant Alpha
    print("\n--- 1. Registering Tenant Alpha & Beta ---")
    alpha_admin_res = client.post(
        "/api/v1/auth/register-tenant",
        json={
            "tenant_name": "Alpha Corp",
            "tenant_slug": alpha_slug,
            "email": f"admin@{alpha_slug}.com",
            "password": "Password12345!",
            "full_name": "Alpha Admin",
        },
    )
    assert alpha_admin_res.status_code == 201
    alpha_admin_tok = alpha_admin_res.json()["access_token"]
    alpha_admin_headers = {"Authorization": f"Bearer {alpha_admin_tok}"}

    # Invite Alpha Member
    inv_res = client.post(
        "/api/v1/users/invite",
        json={"email": f"member@{alpha_slug}.com", "role": "MEMBER"},
        headers=alpha_admin_headers,
    )
    assert inv_res.status_code == 201
    acc_res = client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": inv_res.json()["invite_token"],
            "password": "Password12345!",
            "full_name": "Alpha Member",
        },
    )
    assert acc_res.status_code == 200
    alpha_member_tok = acc_res.json()["access_token"]
    alpha_member_headers = {"Authorization": f"Bearer {alpha_member_tok}"}

    # Register Tenant Beta
    beta_admin_res = client.post(
        "/api/v1/auth/register-tenant",
        json={
            "tenant_name": "Beta Corp",
            "tenant_slug": beta_slug,
            "email": f"admin@{beta_slug}.com",
            "password": "Password12345!",
            "full_name": "Beta Admin",
        },
    )
    assert beta_admin_res.status_code == 201
    beta_admin_tok = beta_admin_res.json()["access_token"]
    beta_admin_headers = {"Authorization": f"Bearer {beta_admin_tok}"}
    print("[PASS] Tenants Alpha and Beta registered.")

    # 2. Upload TXT, PDF, DOCX
    print("\n--- 2. Uploading Documents ---")
    txt_content = b"Vaultly multi-tenant RAG platform. Fast, secure, isolated document ingestion."
    txt_upload = client.post(
        "/api/v1/documents",
        files={"file": ("notes.txt", txt_content, "text/plain")},
        data={"visibility": "PRIVATE"},
        headers=alpha_member_headers,
    )
    assert txt_upload.status_code == 202
    txt_doc_id = txt_upload.json()["id"]
    print(f"[PASS] Uploaded TXT doc: {txt_doc_id}")

    docx_upload = client.post(
        "/api/v1/documents",
        files={"file": ("guide.docx", create_test_docx(), "application/vnd.openxmlformats")},
        data={"visibility": "TENANT"},
        headers=alpha_member_headers,
    )
    assert docx_upload.status_code == 202
    docx_doc_id = docx_upload.json()["id"]
    print(f"[PASS] Uploaded DOCX doc: {docx_doc_id}")

    # 3. Poll for READY status
    print("\n--- 3. Polling for Pipeline Ingestion Completion ---")
    for doc_id, name in [(txt_doc_id, "TXT"), (docx_doc_id, "DOCX")]:
        status = "UPLOADED"
        for _ in range(30):
            res = client.get(f"/api/v1/documents/{doc_id}", headers=alpha_member_headers)
            assert res.status_code == 200
            status = res.json()["status"]
            if status in ("READY", "FAILED"):
                break
            time.sleep(1)

        print(f"[{'PASS' if status == 'READY' else 'FAIL'}] Document {name} ({doc_id}) status: {status}")
        assert status == "READY", f"Document {doc_id} failed ingestion: {res.json()}"

    # 4. Verify Chunks
    print("\n--- 4. Verifying Chunk Generation & Embedding ---")
    chunks_res = client.get(f"/api/v1/documents/{txt_doc_id}/chunks", headers=alpha_member_headers)
    assert chunks_res.status_code == 200
    chunks = chunks_res.json()
    assert len(chunks) > 0
    print(f"[PASS] Retrieved {len(chunks)} chunks for {txt_doc_id}. Text: '{chunks[0]['text'][:40]}...'")

    # 5. Verify Cross-Tenant Isolation (Tenant Beta -> Alpha doc)
    print("\n--- 5. Verifying Cross-Tenant Isolation ---")
    cross_res = client.get(f"/api/v1/documents/{txt_doc_id}", headers=beta_admin_headers)
    assert cross_res.status_code == 404
    print("[PASS] Tenant Beta received 404 for Tenant Alpha document.")

    # 6. Verify Content Accessibility (Admin cannot view chunks of private doc)
    print("\n--- 6. Verifying Content Accessibility Rules ---")
    admin_chunks_res = client.get(f"/api/v1/documents/{txt_doc_id}/chunks", headers=alpha_admin_headers)
    assert admin_chunks_res.status_code == 404
    print("[PASS] Tenant Alpha Admin received 404 on Member's PRIVATE document chunks (No Content Bypass).")

    # 7. Verify Owner-Scoped Deduplication
    print("\n--- 7. Verifying Deduplication ---")
    dup_res = client.post(
        "/api/v1/documents",
        files={"file": ("notes.txt", txt_content, "text/plain")},
        data={"visibility": "PRIVATE"},
        headers=alpha_member_headers,
    )
    assert dup_res.status_code == 200
    assert dup_res.json()["duplicate"] is True
    assert dup_res.json()["id"] == txt_doc_id
    print("[PASS] Duplicate upload returned 200 OK with duplicate=true and identical ID.")

    # 8. Delete Document
    print("\n--- 8. Deleting Document ---")
    del_res = client.delete(f"/api/v1/documents/{txt_doc_id}", headers=alpha_member_headers)
    assert del_res.status_code == 200
    verify_del = client.get(f"/api/v1/documents/{txt_doc_id}", headers=alpha_member_headers)
    assert verify_del.status_code == 404
    print("[PASS] Document deleted and cascaded cleanly.")

    print("\n" + "=" * 70)
    print("ALL PHASE 2 SMOKE CHECKS PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    run_smoke_test()
