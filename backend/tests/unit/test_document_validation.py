"""Unit tests for document validation, filename sanitization, and magic byte checking."""

import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.document_validation import (
    detect_and_validate_file,
    sanitize_filename,
)


def test_sanitize_filename() -> None:
    """Verify filename sanitization strips paths, control chars, and preserves extensions."""
    assert sanitize_filename("../../etc/passwd.txt") == "passwd.txt"
    assert sanitize_filename(r"..\..\Windows\System32\cmd.exe") == "cmd.exe"
    assert sanitize_filename("my\x00bad\x1fname.pdf") == "mybadname.pdf"
    assert sanitize_filename("   spaced_name.docx   ") == "spaced_name.docx"
    assert sanitize_filename("résumé_2026.pdf") == "résumé_2026.pdf"

    # Giant filename capping at 255 chars
    long_name = "a" * 300 + ".pdf"
    sanitized = sanitize_filename(long_name)
    assert len(sanitized) == 255
    assert sanitized.endswith(".pdf")


def test_validation_empty_file(tmp_path: Path) -> None:
    """Verify empty file is rejected with 400."""
    empty_file = tmp_path / "empty.txt"
    empty_file.touch()

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(empty_file, "empty.txt")
    assert exc.value.status_code == 400
    assert "empty" in exc.value.detail.lower()


def test_validation_unsupported_extension(tmp_path: Path) -> None:
    """Verify unsupported file extensions are rejected with 400."""
    exe_file = tmp_path / "malware.exe"
    exe_file.write_bytes(b"MZ\x90\x00binary")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(exe_file, "malware.exe")
    assert exc.value.status_code == 400
    assert "unsupported" in exc.value.detail.lower()


def test_validation_pdf_magic_bytes(tmp_path: Path) -> None:
    """Verify PDF magic bytes check (%PDF-)."""
    # 1. Fake PDF with wrong header (e.g. .exe renamed to .pdf)
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"NOT_A_PDF_FILE_HEADER")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(fake_pdf, "fake.pdf")
    assert exc.value.status_code == 400
    assert "%pdf-" in exc.value.detail.lower()

    # 2. Valid PDF header
    valid_pdf = tmp_path / "valid.pdf"
    valid_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF")
    assert detect_and_validate_file(valid_pdf, "valid.pdf") == "pdf"


def test_validation_docx_structure(tmp_path: Path) -> None:
    """Verify DOCX zip structure and word/document.xml requirement."""
    # 1. Not a zip archive
    not_zip = tmp_path / "corrupt.docx"
    not_zip.write_bytes(b"Definitely not a zip file")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(not_zip, "corrupt.docx")
    assert exc.value.status_code == 400

    # 2. Valid zip but missing word/document.xml
    missing_doc = tmp_path / "missing_xml.docx"
    with zipfile.ZipFile(missing_doc, "w") as zf:
        zf.writestr("some_other_file.txt", "hello")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(missing_doc, "missing_xml.docx")
    assert exc.value.status_code == 400
    assert "word/document.xml" in exc.value.detail

    # 3. Zip containing only word/document.xml (missing [Content_Types].xml and _rels/.rels) -> 400
    only_doc_xml = tmp_path / "only_doc_xml.docx"
    with zipfile.ZipFile(only_doc_xml, "w") as zf:
        zf.writestr("word/document.xml", "<w:document><w:body><w:p/></w:body></w:document>")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(only_doc_xml, "only_doc_xml.docx")
    assert exc.value.status_code == 400
    assert "[Content_Types].xml" in exc.value.detail or "_rels/.rels" in exc.value.detail

    # 4. Real valid DOCX generated with python-docx
    import docx

    real_doc = docx.Document()
    real_doc.add_paragraph("Valid python-docx test content.")
    real_docx_path = tmp_path / "real.docx"
    real_doc.save(str(real_docx_path))
    assert detect_and_validate_file(real_docx_path, "real.docx") == "docx"


def test_validation_docx_zip_bomb(tmp_path: Path) -> None:
    """Verify zip bomb defense triggers on excessive uncompressed size or ratio."""
    bomb_file = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", "A" * (1024 * 1024 * 5))  # Highly compressible 5MB string

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(bomb_file, "bomb.docx")
    assert exc.value.status_code == 400
    assert "zip bomb" in exc.value.detail.lower()


def test_validation_txt_and_md_nul_bytes(tmp_path: Path) -> None:
    """Verify text files containing NUL bytes are rejected."""
    nul_txt = tmp_path / "nul.txt"
    nul_txt.write_bytes(b"Normal text here\x00and then a nul byte")

    with pytest.raises(HTTPException) as exc:
        detect_and_validate_file(nul_txt, "nul.txt")
    assert exc.value.status_code == 400
    assert "nul" in exc.value.detail.lower()

    # Valid UTF-8
    valid_txt = tmp_path / "valid.txt"
    valid_txt.write_text("Hello, Vaultly multi-tenant text file!", encoding="utf-8")
    assert detect_and_validate_file(valid_txt, "valid.txt") == "txt"

    # Valid Markdown
    valid_md = tmp_path / "valid.md"
    valid_md.write_text("# Markdown Title\n\nContent here.", encoding="utf-8")
    assert detect_and_validate_file(valid_md, "valid.md") == "md"
