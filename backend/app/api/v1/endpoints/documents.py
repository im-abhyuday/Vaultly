"""Document management, upload, chunk retrieval, and access control endpoints."""

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_current_session,
    get_current_user,
    require_role,
)
from app.core.config import get_settings
from app.core.logging import request_id_ctx
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_acl import DocumentACL
from app.models.enums import DocumentStatus, DocumentVisibility, RoleEnum
from app.models.group import Group
from app.models.membership import Membership
from app.models.outbox import OutboxEvent
from app.schemas.document import (
    ChunkResponse,
    DocumentAccessUpdateRequest,
    DocumentListResponse,
    DocumentResponse,
)
from app.schemas.events import DocumentEventEnvelope, DocumentEventPayload
from app.services.chunking_config import get_or_create_active_config
from app.services.document_validation import detect_and_validate_file, sanitize_filename
from app.services.permissions import (
    can_manage,
    content_accessible,
    get_corpus_notifier,
    metadata_visible,
)
from app.services.state_machine import validate_transition
from app.services.storage import get_document_storage_key, get_storage

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


def _parse_id_list(raw_ids: list[str] | str | None) -> list[uuid.UUID]:
    """Parse list of UUID strings from form fields (handles multiple fields, JSON, or comma-separated)."""
    if not raw_ids:
        return []
    result: list[uuid.UUID] = []
    items: list[str] = []
    if isinstance(raw_ids, str):
        if raw_ids.startswith("[") and raw_ids.endswith("]"):
            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
            except Exception:
                items = raw_ids.split(",")
        else:
            items = raw_ids.split(",")
    elif isinstance(raw_ids, list):
        for item in raw_ids:
            if item.startswith("[") and item.endswith("]"):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, list):
                        items.extend(str(x) for x in parsed)
                        continue
                except Exception:
                    pass
            items.extend(item.split(","))

    for item in items:
        cleaned = item.strip().strip("'\"")
        if cleaned:
            try:
                result.append(uuid.UUID(cleaned))
            except ValueError as err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid UUID in ACL list: '{cleaned}'",
                ) from err
    return result


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    visibility: DocumentVisibility = Form(DocumentVisibility.PRIVATE),
    group_ids: list[str] | str | None = Form(None),
    user_ids: list[str] | str | None = Form(None),
    current_user: CurrentUser = Depends(require_role([RoleEnum.ADMIN, RoleEnum.MEMBER])),
    session: AsyncSession = Depends(get_current_session),
) -> Any:
    """Stream and store document, validate contents, check owner dedupe, and enqueue outbox event."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is missing from upload.",
        )

    clean_filename = sanitize_filename(file.filename)
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    storage = get_storage()

    # Stream file to a temporary file while computing sha256 and enforcing byte limits
    temp_dir = Path(settings.STORAGE_LOCAL_ROOT) / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"upload_{uuid.uuid4().hex}.tmp"

    hasher = hashlib.sha256()
    total_bytes = 0

    try:
        with temp_path.open("wb") as f_out:
            while chunk := await file.read(65536):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds maximum allowed size of {settings.MAX_UPLOAD_MB} MB.",
                    )
                hasher.update(chunk)
                f_out.write(chunk)
            f_out.flush()
            os.fsync(f_out.fileno())

        checksum_sha256 = hasher.hexdigest()

        # Validate magic bytes, file format, and zip-bomb safety
        detected_type = detect_and_validate_file(temp_path, clean_filename)

        # 1. Dedupe check: same owner and checksum within tenant
        dedupe_stmt = select(Document).where(
            Document.tenant_id == current_user.tenant_id,
            Document.owner_id == current_user.user_id,
            Document.checksum_sha256 == checksum_sha256,
        )
        existing_doc = (await session.execute(dedupe_stmt)).scalar_one_or_none()
        if existing_doc:
            # Clean up temp file, return 200 OK with duplicate=True
            if temp_path.exists():
                temp_path.unlink()
            response.status_code = status.HTTP_200_OK
            resp = DocumentResponse.model_validate(existing_doc)
            resp.duplicate = True
            return resp

        # 2. Pin active chunking config for this tenant
        chunking_config = await get_or_create_active_config(session, current_user.tenant_id)

        # 3. Target permanent storage
        doc_id = uuid.uuid4()
        storage_key = get_document_storage_key(current_user.tenant_id, doc_id, "original")
        final_path = Path(settings.STORAGE_LOCAL_ROOT) / storage_key
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, final_path)

        # 4. Insert Document row
        doc = Document(
            id=doc_id,
            tenant_id=current_user.tenant_id,
            owner_id=current_user.user_id,
            filename=clean_filename,
            detected_type=detected_type,
            size_bytes=total_bytes,
            checksum_sha256=checksum_sha256,
            storage_key=storage_key,
            status=DocumentStatus.UPLOADED.value,
            visibility=visibility.value,
            chunking_config_id=chunking_config.id,
            attempts=0,
            chunk_count=0,
        )
        session.add(doc)

        # 5. Insert ACL entries if provided
        parsed_group_ids = _parse_id_list(group_ids)
        parsed_user_ids = _parse_id_list(user_ids)

        for gid in parsed_group_ids:
            # Verify group exists in tenant
            grp_exists = (
                await session.execute(
                    select(Group.id).where(
                        Group.tenant_id == current_user.tenant_id,
                        Group.id == gid,
                    )
                )
            ).scalar_one_or_none()
            if not grp_exists:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Group '{gid}' does not exist in this tenant.",
                )
            acl_entry = DocumentACL(
                id=uuid.uuid4(),
                tenant_id=current_user.tenant_id,
                document_id=doc_id,
                group_id=gid,
                user_id=None,
            )
            session.add(acl_entry)

        for uid in parsed_user_ids:
            # Verify user membership exists in tenant
            mem_exists = (
                await session.execute(
                    select(Membership.id).where(
                        Membership.tenant_id == current_user.tenant_id,
                        Membership.user_id == uid,
                    )
                )
            ).scalar_one_or_none()
            if not mem_exists:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User '{uid}' is not a member of this tenant.",
                )
            acl_entry = DocumentACL(
                id=uuid.uuid4(),
                tenant_id=current_user.tenant_id,
                document_id=doc_id,
                group_id=None,
                user_id=uid,
            )
            session.add(acl_entry)

        # 6. Insert OutboxEvent in the SAME database transaction
        correlation_id = request_id_ctx.get() or str(uuid.uuid4())
        topic_name = f"{settings.KAFKA_TOPIC_PREFIX}.document.uploaded" if settings.KAFKA_TOPIC_PREFIX else "document.uploaded"
        envelope = DocumentEventEnvelope(
            event_type="document.uploaded",
            tenant_id=current_user.tenant_id,
            document_id=doc_id,
            correlation_id=correlation_id,
            payload=DocumentEventPayload(chunking_config_id=chunking_config.id),
        )
        outbox = OutboxEvent(
            id=uuid.uuid4(),
            topic=topic_name,
            key=str(doc_id),
            payload=json.loads(envelope.model_dump_json()),
            headers={"correlation_id": correlation_id, "schema_version": "1"},
        )
        session.add(outbox)


        await session.flush()
        return DocumentResponse.model_validate(doc)

    except Exception:
        # Atomic file cleanup on failure
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        if 'final_path' in locals() and final_path.exists():
            try:
                final_path.unlink()
            except OSError:
                pass
        raise


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: DocumentStatus | None = None,
    visibility: DocumentVisibility | None = None,
    mine: bool = Query(False),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> DocumentListResponse:
    """List documents visible to the current user with pagination and filters."""
    query = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        metadata_visible(current_user),
    )

    if mine:
        query = query.where(Document.owner_id == current_user.user_id)
    if status:
        query = query.where(Document.status == status.value)
    if visibility:
        query = query.where(Document.visibility == visibility.value)

    # Count total
    count_stmt = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_stmt)).scalar() or 0

    # Paginate
    query = (
        query.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    docs = (await session.execute(query)).scalars().all()

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in docs],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> DocumentResponse:
    """Retrieve document metadata by ID. Returns 404 for invisible or cross-tenant IDs."""
    stmt = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        Document.id == document_id,
        metadata_visible(current_user),
    )
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )
    return DocumentResponse.model_validate(doc)


@router.get("/{document_id}/chunks", response_model=list[ChunkResponse])
async def get_document_chunks(
    document_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> list[ChunkResponse]:
    """Retrieve paginated text chunks for a document. Accessible ONLY if content_accessible is satisfied."""
    # Verify document exists and content is accessible (Admins/Auditors do NOT bypass content access)
    doc_stmt = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        Document.id == document_id,
        content_accessible(current_user),
    )
    doc = (await session.execute(doc_stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # Fetch chunks
    chunk_stmt = (
        select(Chunk)
        .where(
            Chunk.tenant_id == current_user.tenant_id,
            Chunk.document_id == document_id,
        )
        .order_by(Chunk.ordinal.asc())
        .offset(offset)
        .limit(limit)
    )
    chunks = (await session.execute(chunk_stmt)).scalars().all()
    return [ChunkResponse.model_validate(c) for c in chunks]


@router.patch("/{document_id}/access", response_model=DocumentResponse)
async def update_document_access(
    document_id: uuid.UUID,
    payload: DocumentAccessUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> DocumentResponse:
    """Update visibility and ACL entries for a document (can_manage required)."""
    # Locate document visible to user
    stmt = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        Document.id == document_id,
        metadata_visible(current_user),
    )
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if not can_manage(current_user, doc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage this document.",
        )

    # Update visibility
    doc.visibility = payload.visibility.value

    # Replace ACL entries
    await session.execute(
        delete(DocumentACL).where(
            DocumentACL.tenant_id == current_user.tenant_id,
            DocumentACL.document_id == document_id,
        )
    )

    for gid in payload.group_ids:
        # Verify group exists in tenant
        grp_exists = (
            await session.execute(
                select(Group.id).where(
                    Group.tenant_id == current_user.tenant_id,
                    Group.id == gid,
                )
            )
        ).scalar_one_or_none()
        if not grp_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Group '{gid}' does not exist in this tenant.",
            )
        session.add(
            DocumentACL(
                id=uuid.uuid4(),
                tenant_id=current_user.tenant_id,
                document_id=document_id,
                group_id=gid,
                user_id=None,
            )
        )

    for uid in payload.user_ids:
        # Verify user exists in tenant
        mem_exists = (
            await session.execute(
                select(Membership.id).where(
                    Membership.tenant_id == current_user.tenant_id,
                    Membership.user_id == uid,
                )
            )
        ).scalar_one_or_none()
        if not mem_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User '{uid}' is not a member of this tenant.",
            )
        session.add(
            DocumentACL(
                id=uuid.uuid4(),
                tenant_id=current_user.tenant_id,
                document_id=document_id,
                group_id=None,
                user_id=uid,
            )
        )

    await session.flush()
    await get_corpus_notifier().notify(current_user.tenant_id, "access_changed")
    return DocumentResponse.model_validate(doc)


@router.delete("/{document_id}", status_code=status.HTTP_200_OK)
async def delete_document(
    document_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Delete a document, cascading database records and cleaning up stored files."""
    stmt = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        Document.id == document_id,
        metadata_visible(current_user),
    )
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if not can_manage(current_user, doc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this document.",
        )

    # Delete from DB (FK ON DELETE CASCADE removes chunks and document_acl)
    await session.delete(doc)
    await session.flush()

    # Best-effort file storage cleanup
    storage = get_storage()
    prefix = f"tenants/{current_user.tenant_id}/documents/{document_id}"
    storage.delete_prefix(prefix)

    # Notify corpus change
    await get_corpus_notifier().notify(current_user.tenant_id, "document_deleted")

    return {"message": "Document deleted successfully."}


@router.post("/{document_id}/reprocess", response_model=DocumentResponse)
async def reprocess_document(
    document_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> DocumentResponse:
    """Reset a FAILED document to UPLOADED and enqueue a new outbox event."""
    stmt = select(Document).where(
        Document.tenant_id == current_user.tenant_id,
        Document.id == document_id,
        metadata_visible(current_user),
    )
    doc = (await session.execute(stmt)).scalar_one_or_none()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    if not can_manage(current_user, doc):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to reprocess this document.",
        )

    if doc.status != DocumentStatus.FAILED.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only FAILED documents can be reprocessed (current status: '{doc.status}').",
        )

    # State machine transition check
    try:
        validate_transition(doc.status, DocumentStatus.UPLOADED)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # Reset failure metadata
    doc.status = DocumentStatus.UPLOADED.value
    doc.error_code = None
    doc.error_message = None
    doc.failed_stage = None
    doc.attempts = 0

    # Insert outbox event for document.uploaded
    correlation_id = request_id_ctx.get() or str(uuid.uuid4())
    topic_name = f"{settings.KAFKA_TOPIC_PREFIX}.document.uploaded" if settings.KAFKA_TOPIC_PREFIX else "document.uploaded"
    outbox = OutboxEvent(
        id=uuid.uuid4(),
        topic=topic_name,
        key=str(doc.id),
        payload={
            "event_id": str(uuid.uuid4()),
            "event_type": "document.uploaded",
            "tenant_id": str(current_user.tenant_id),
            "document_id": str(doc.id),
            "chunking_config_id": str(doc.chunking_config_id),
            "correlation_id": correlation_id,
        },
        headers={"correlation_id": correlation_id, "schema_version": "1"},
    )
    session.add(outbox)
    await session.flush()

    return DocumentResponse.model_validate(doc)
