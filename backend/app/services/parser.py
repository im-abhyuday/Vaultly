"""Document parsing service supporting PDF, DOCX, TXT, and Markdown.

Enforces extraction limits (page count, max characters) and offloads CPU-bound operations
via asyncio.to_thread to maintain Kafka consumer heartbeats.
"""

import asyncio
import io
import re
from typing import BinaryIO

import docx
import pypdf
from pydantic import BaseModel

from app.core.config import get_settings
from app.models.enums import DocumentErrorCode
from app.services.storage import StorageBackend, get_storage
from app.workers.errors import PermanentError

settings = get_settings()

PARSER_NAME = "vaultly-parser"
PARSER_VERSION = "1.0.0"


class ParsedSegment(BaseModel):
    """Extracted text segment with optional page association."""

    page: int | None = None
    text: str


class ParsedDocument(BaseModel):
    """Complete parsed artifact stored as parsed.json."""

    segments: list[ParsedSegment]
    page_count: int
    parser: str = PARSER_NAME
    parser_version: str = PARSER_VERSION


def clean_text(raw: str) -> str:
    """Strip NUL bytes, normalize whitespace and control characters."""
    # Postgres rejects \x00 unconditionally
    without_nul = raw.replace("\x00", "")
    # Normalize carriage returns and form feeds
    normalized = re.sub(r"\r\n|\r", "\n", without_nul)
    # Collapse 3 or more newlines to 2 newlines (preserve paragraphs)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    # Collapse multiple inline spaces/tabs to a single space
    cleaned = re.sub(r"[ \t]+", " ", normalized)
    return cleaned.strip()


def parse_text_stream(raw_bytes: bytes) -> tuple[list[ParsedSegment], int]:
    """Decode raw bytes using UTF-8, UTF-8-sig, with fallback to cp1252."""
    decoded: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            decoded = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        raise PermanentError(
            "File encoding could not be decoded as UTF-8 or CP1252.",
            error_code=DocumentErrorCode.CORRUPT_FILE.value,
        )

    cleaned = clean_text(decoded)
    if not cleaned:
        raise PermanentError(
            "Document contains no extractable text.",
            error_code=DocumentErrorCode.NO_EXTRACTABLE_TEXT.value,
        )

    return [ParsedSegment(page=1, text=cleaned)], 1


def parse_pdf_stream(stream: BinaryIO, max_pages: int) -> tuple[list[ParsedSegment], int]:
    """Extract per-page text from PDF using pypdf."""
    try:
        reader = pypdf.PdfReader(stream)
        num_pages = len(reader.pages)
    except Exception as err:
        raise PermanentError(
            f"Corrupt or invalid PDF file: {err}",
            error_code=DocumentErrorCode.CORRUPT_FILE.value,
        ) from err

    if num_pages == 0:
        raise PermanentError(
            "PDF contains zero pages.",
            error_code=DocumentErrorCode.CORRUPT_FILE.value,
        )

    if num_pages > max_pages:
        raise PermanentError(
            f"PDF page count ({num_pages}) exceeds limit of {max_pages} pages.",
            error_code=DocumentErrorCode.LIMIT_EXCEEDED.value,
        )

    segments: list[ParsedSegment] = []
    total_text_length = 0

    for idx, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as err:
            raise PermanentError(
                f"Error extracting text from PDF page {idx}: {err}",
                error_code=DocumentErrorCode.CORRUPT_FILE.value,
            ) from err

        cleaned = clean_text(page_text)
        if cleaned:
            segments.append(ParsedSegment(page=idx, text=cleaned))
            total_text_length += len(cleaned)

    if not segments or total_text_length == 0:
        raise PermanentError(
            "PDF contains no extractable text (likely image-only or scanned; OCR not supported).",
            error_code=DocumentErrorCode.NO_EXTRACTABLE_TEXT.value,
        )

    return segments, num_pages


def parse_docx_stream(stream: BinaryIO) -> tuple[list[ParsedSegment], int]:
    """Extract paragraphs and tables from DOCX using python-docx."""
    try:
        doc = docx.Document(stream)
    except Exception as err:
        raise PermanentError(
            f"Corrupt or invalid DOCX document: {err}",
            error_code=DocumentErrorCode.CORRUPT_FILE.value,
        ) from err

    lines: list[str] = []
    # 1. Paragraphs
    for p in doc.paragraphs:
        txt = clean_text(p.text)
        if txt:
            lines.append(txt)

    # 2. Tables
    for table in doc.tables:
        for row in table.rows:
            row_cells = [clean_text(cell.text) for cell in row.cells if clean_text(cell.text)]
            if row_cells:
                lines.append(" | ".join(row_cells))

    if not lines:
        raise PermanentError(
            "DOCX contains no extractable text.",
            error_code=DocumentErrorCode.NO_EXTRACTABLE_TEXT.value,
        )

    full_text = "\n\n".join(lines)
    return [ParsedSegment(page=1, text=full_text)], 1


def sync_parse_document(
    storage: StorageBackend,
    storage_key: str,
    detected_type: str,
    max_pages: int = 500,
    max_extracted_chars: int = 5_000_000,
) -> ParsedDocument:
    """Synchronously parse document from storage and enforce size/page limits."""
    if not storage.exists(storage_key):
        raise PermanentError(
            f"Document file not found at storage key: {storage_key}",
            error_code=DocumentErrorCode.CORRUPT_FILE.value,
        )

    with storage.open(storage_key) as f:
        file_bytes = f.read()

    stream = io.BytesIO(file_bytes)

    dtype = detected_type.lower().strip()
    if dtype in ("txt", "md"):
        segments, page_count = parse_text_stream(file_bytes)
    elif dtype == "pdf":
        segments, page_count = parse_pdf_stream(stream, max_pages)
    elif dtype == "docx":
        segments, page_count = parse_docx_stream(stream)
    else:
        raise PermanentError(
            f"Unsupported document format for parsing: '{detected_type}'.",
            error_code=DocumentErrorCode.UNSUPPORTED_TYPE.value,
        )

    total_extracted_chars = sum(len(s.text) for s in segments)
    if total_extracted_chars > max_extracted_chars:
        raise PermanentError(
            f"Extracted characters ({total_extracted_chars}) exceeds limit of {max_extracted_chars}.",
            error_code=DocumentErrorCode.LIMIT_EXCEEDED.value,
        )

    return ParsedDocument(
        segments=segments,
        page_count=page_count,
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )


async def parse_document(
    storage_key: str,
    detected_type: str,
    storage: StorageBackend | None = None,
    max_pages: int | None = None,
    max_extracted_chars: int | None = None,
) -> ParsedDocument:
    """Offload CPU-bound document parsing to worker thread to prevent event loop starvation."""
    storage_instance = storage or get_storage()
    pages_limit = max_pages or settings.MAX_PAGES_PER_DOC
    chars_limit = max_extracted_chars or settings.MAX_EXTRACTED_CHARS

    return await asyncio.to_thread(
        sync_parse_document,
        storage_instance,
        storage_key,
        detected_type,
        pages_limit,
        chars_limit,
    )
