"""Document file validation, magic byte checking, zip-bomb defense, and filename sanitization."""

import os
import re
import zipfile
from pathlib import Path

from fastapi import HTTPException, status

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
ZIP_BOMB_MAX_RATIO = 50.0
ZIP_BOMB_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024  # 100 MB


def sanitize_filename(filename: str) -> str:
    """Sanitize user-provided filename by stripping path traversal, control chars, and capping length."""
    # Strip directory components (handles both / and \)
    base = os.path.basename(filename).replace("\\", "/")
    base = base.split("/")[-1]

    # Remove non-printable / control characters
    cleaned = "".join(ch for ch in base if ch.isprintable() and ord(ch) >= 32).strip()

    # Prevent hidden files or path traversal relics like '..'
    cleaned = re.sub(r"^\.+", "", cleaned).strip()
    if not cleaned:
        cleaned = "document"

    # Ensure max 255 chars while preserving extension
    if len(cleaned) > 255:
        stem, ext = os.path.splitext(cleaned)
        max_stem_len = 255 - len(ext)
        cleaned = f"{stem[:max_stem_len]}{ext}"

    return cleaned


def detect_and_validate_file(file_path: Path, filename: str) -> str:
    """Validate extension and inspect file contents/magic bytes. Returns detected_type ('pdf','docx','txt','md')."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file extension '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    file_size = file_path.stat().st_size
    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    detected_type = ext.lstrip(".")

    # 1. PDF Validation: Must start with %PDF-
    if ext == ".pdf":
        with file_path.open("rb") as f:
            header = f.read(5)
            if header != b"%PDF-":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid PDF file: Missing '%PDF-' magic header.",
                )

    # 2. DOCX Validation: Valid zip containing word/document.xml with zip-bomb limits
    elif ext == ".docx":
        if not zipfile.is_zipfile(file_path):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid DOCX file: Not a valid ZIP archive.",
            )

        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                namelist = {name.replace("\\", "/") for name in zf.namelist()}
                required_entries = ["[Content_Types].xml", "_rels/.rels", "word/document.xml"]
                missing_entries = [req for req in required_entries if req not in namelist]
                if missing_entries:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid DOCX file: Missing required OOXML components ({', '.join(missing_entries)}).",
                    )

                # Zip bomb checks: total uncompressed size and compression ratio
                total_uncompressed = 0
                for info in zf.infolist():
                    total_uncompressed += info.file_size
                    if total_uncompressed > ZIP_BOMB_MAX_UNCOMPRESSED_BYTES:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="File rejected: Uncompressed content exceeds safety limits (zip bomb protection).",
                        )

                ratio = total_uncompressed / max(1, file_size)
                if ratio > ZIP_BOMB_MAX_RATIO:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"File rejected: Abnormal compression ratio ({ratio:.1f}x) detected (zip bomb protection).",
                    )
        except zipfile.BadZipFile as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Corrupt DOCX file: {e}",
            ) from e

    # 3. TXT / MD Validation: Must decode as valid text and contain no NUL bytes
    elif ext in (".txt", ".md"):
        with file_path.open("rb") as f:
            raw_bytes = f.read()

        if b"\x00" in raw_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Text file contains NUL bytes which are not allowed.",
            )

        # Attempt UTF-8, then UTF-8-sig, then CP1252 decoding
        decoded = False
        for encoding in ("utf-8", "utf-8-sig", "cp1252"):
            try:
                raw_bytes.decode(encoding)
                decoded = True
                break
            except UnicodeDecodeError:
                continue

        if not decoded:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unable to decode text file. Ensure it is encoded in UTF-8 or CP1252.",
            )

    return detected_type
