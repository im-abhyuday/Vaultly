"""Unit tests for deterministic recursive chunker."""

import pytest

from app.models.enums import DocumentErrorCode
from app.services.chunker import (
    MODEL_MAX_TOKENS,
    RecursiveChunker,
    RegexTokenCounter,
)
from app.services.parser import ParsedDocument, ParsedSegment
from app.workers.errors import PermanentError


@pytest.fixture
def token_counter() -> RegexTokenCounter:
    return RegexTokenCounter()


def test_chunker_deterministic_output(token_counter: RegexTokenCounter) -> None:
    """Chunking identical input multiple times yields identical results."""
    text = (
        "Vaultly is an enterprise RAG platform. It features multi-tenancy, Row-Level Security, "
        "and granular RBAC. Every document is parsed, chunked, and embedded into pgvector.\n\n"
        "Security is enforced at the database level via PostgreSQL RLS policies. "
        "Application queries execute under tenant context."
    )
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=text)],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=20, chunk_overlap=5)

    result1 = chunker.chunk_document(doc)
    result2 = chunker.chunk_document(doc)

    assert len(result1) == len(result2)
    for c1, c2 in zip(result1, result2, strict=True):
        assert c1.ordinal == c2.ordinal
        assert c1.text == c2.text
        assert c1.token_count == c2.token_count
        assert c1.page_start == c2.page_start
        assert c1.page_end == c2.page_end
        assert c1.char_start == c2.char_start
        assert c1.char_end == c2.char_end


def test_chunker_never_exceeds_token_limit(token_counter: RegexTokenCounter) -> None:
    """Chunks never exceed the specified chunk_size."""
    long_text = " ".join([f"Word{i} with some explanation text." for i in range(100)])
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=long_text)],
        page_count=1,
    )
    for size in (15, 30, 50, 100):
        chunker = RecursiveChunker(token_counter=token_counter, chunk_size=size, chunk_overlap=5)
        chunks = chunker.chunk_document(doc)
        assert len(chunks) > 0
        for chunk in chunks:
            # Token count must be <= chunk_size (or slightly up to chunk_size depending on splitting boundary)
            assert chunk.token_count <= size, f"Chunk exceeded size {size}: tokens={chunk.token_count}"


def test_chunker_model_max_ceiling(token_counter: RegexTokenCounter) -> None:
    """Chunker clamps chunk_size to MODEL_MAX_TOKENS (512)."""
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=1024, chunk_overlap=100)
    assert chunker.chunk_size == MODEL_MAX_TOKENS
    assert chunker.chunk_overlap == 100

    chunker_large_overlap = RecursiveChunker(token_counter=token_counter, chunk_size=100, chunk_overlap=200)
    assert chunker_large_overlap.chunk_overlap == 99


def test_chunker_overlap_correctness(token_counter: RegexTokenCounter) -> None:
    """Consecutive chunks contain overlapping tokens when overlap is requested."""
    paragraphs = [
        f"Paragraph {i}: This is a detailed section containing multiple sentences to test chunk overlap behavior."
        for i in range(10)
    ]
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text="\n\n".join(paragraphs))],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=30, chunk_overlap=10)
    chunks = chunker.chunk_document(doc)
    assert len(chunks) > 1

    for i in range(len(chunks) - 1):
        cur_words = set(chunks[i].text.split())
        next_words = set(chunks[i + 1].text.split())
        # Overlapping tokens should mean common words exist between consecutive chunks
        assert len(cur_words.intersection(next_words)) > 0


def test_chunker_no_empty_chunks(token_counter: RegexTokenCounter) -> None:
    """Chunker never produces empty or whitespace-only chunks."""
    text = "Hello world!  \n\n\n  \t   \n  Second paragraph with content.   \n\n"
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=text)],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=20, chunk_overlap=5)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) > 0
    for chunk in chunks:
        assert chunk.text
        assert chunk.text.strip() == chunk.text
        assert chunk.token_count > 0


def test_chunker_citations_and_page_mapping(token_counter: RegexTokenCounter) -> None:
    """Page numbers and character offsets accurately reflect multi-page documents."""
    doc = ParsedDocument(
        segments=[
            ParsedSegment(page=1, text="Page one intro. " * 10),
            ParsedSegment(page=2, text="Page two body. " * 10),
            ParsedSegment(page=3, text="Page three conclusions. " * 10),
        ],
        page_count=3,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=25, chunk_overlap=5)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) >= 3
    # First chunk starts on page 1
    assert chunks[0].page_start == 1
    # Ordinals are monotonically contiguous starting at 0
    ordinals = [c.ordinal for c in chunks]
    assert ordinals == list(range(len(chunks)))

    # Verify character bounds are ordered
    for chunk in chunks:
        assert chunk.char_start < chunk.char_end
        assert chunk.page_start is not None
        assert chunk.page_end is not None
        assert chunk.page_start <= chunk.page_end


def test_chunker_giant_single_token_hard_split(token_counter: RegexTokenCounter) -> None:
    """A giant single token (no spaces, e.g. base64 or DNA sequence) splits via character fallback."""
    giant_token = "A" * 500
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=giant_token)],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=50, chunk_overlap=10)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) > 1
    # The giant token should be covered without recursion depth error
    assert len(chunks) >= 2



def test_chunker_unicode_and_emojis(token_counter: RegexTokenCounter) -> None:
    """Chunker handles UTF-8 multi-byte characters and emojis gracefully."""
    text = "Vaultly 支持多租户 🚀 🔐. 包含 Row-Level Security 与 RBAC 权限控制. 東京と京都の風景 🌸. Καλημέρα κόσμε!"
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=text)],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=15, chunk_overlap=3)
    chunks = chunker.chunk_document(doc)

    assert len(chunks) > 0
    full_combined = " ".join(c.text for c in chunks)
    assert "🚀" in full_combined
    assert "多租户" in full_combined


def test_chunker_empty_document_raises_permanent_error(token_counter: RegexTokenCounter) -> None:
    """Document with only whitespace raises PermanentError NO_EXTRACTABLE_TEXT."""
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text="   \n\t  ")],
        page_count=1,
    )
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=50)
    with pytest.raises(PermanentError) as exc_info:
        chunker.chunk_document(doc)
    assert exc_info.value.error_code == DocumentErrorCode.NO_EXTRACTABLE_TEXT.value


def test_chunker_exceeding_max_chunks_limit(token_counter: RegexTokenCounter) -> None:
    """Document exceeding max_chunks_per_doc raises PermanentError LIMIT_EXCEEDED."""
    long_text = " ".join([f"Sentence number {i} is here." for i in range(50)])
    doc = ParsedDocument(
        segments=[ParsedSegment(page=1, text=long_text)],
        page_count=1,
    )
    # Set max_chunks_per_doc = 3
    chunker = RecursiveChunker(token_counter=token_counter, chunk_size=10, chunk_overlap=2, max_chunks_per_doc=3)
    with pytest.raises(PermanentError) as exc_info:
        chunker.chunk_document(doc)
    assert exc_info.value.error_code == DocumentErrorCode.LIMIT_EXCEEDED.value
