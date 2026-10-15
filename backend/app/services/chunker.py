"""Deterministic recursive document chunker measured in tokens.

Splits parsed documents by hierarchy (paragraphs -> sentences -> words -> characters),
maintaining token size boundaries, token overlap, and page/character offset citations.
"""

import re
from dataclasses import dataclass
from typing import Protocol

from app.core.config import get_settings
from app.models.enums import DocumentErrorCode
from app.services.parser import ParsedDocument
from app.workers.errors import PermanentError

settings = get_settings()

MODEL_MAX_TOKENS = 512


class TokenCounter(Protocol):
    """Protocol for counting tokens in text."""

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens in text."""
        ...


class RegexTokenCounter:
    """Deterministic regex-based token counter for fake provider, tests, and CI.

    Approximates BPE tokenization by splitting words, contractions, and punctuation symbols.
    Caps word token spans at 8 characters to closely track BPE subword tokenization.
    """

    # Matches word fragments up to 8 chars, numbers, individual punctuation symbols
    _PATTERN = re.compile(r"\w{1,8}|[^\w\s]", re.UNICODE)

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self._PATTERN.findall(text))


class HFTokenCounter:
    """HuggingFace tokenizer wrapper for production embedding models."""

    def __init__(self, model_name: str) -> None:
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model_name)

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self.tokenizer.encode(text, add_special_tokens=False))


@dataclass(frozen=True)
class GeneratedChunk:
    """Generated chunk artifact ready for embedding and database insertion."""

    ordinal: int
    text: str
    token_count: int
    page_start: int | None
    page_end: int | None
    char_start: int
    char_end: int


class RecursiveChunker:
    """Deterministic hierarchical recursive text splitter with token-based boundaries."""

    def __init__(
        self,
        token_counter: TokenCounter,
        chunk_size: int = 256,
        chunk_overlap: int = 32,
        max_chunks_per_doc: int = 2000,
    ) -> None:
        # Enforce model ceiling
        self.chunk_size = min(chunk_size, MODEL_MAX_TOKENS)
        self.chunk_overlap = min(chunk_overlap, self.chunk_size - 1)
        self.token_counter = token_counter
        self.max_chunks_per_doc = max_chunks_per_doc

    def _split_text(self, text: str, separators: list[str]) -> list[str]:
        """Split text recursively using ordered separators."""
        if not text.strip():
            return []

        if self.token_counter.count_tokens(text) <= self.chunk_size:
            return [text]

        if not separators:
            # Hard character split fallback if even a single token/word is giant
            mid = len(text) // 2
            left = self._split_text(text[:mid], [])
            right = self._split_text(text[mid:], [])
            return left + right

        sep = separators[0]
        remaining_seps = separators[1:]

        if sep == "":
            # Character-level fallback
            chunks: list[str] = []
            cur = ""
            for char in text:
                candidate = cur + char
                if self.token_counter.count_tokens(candidate) > self.chunk_size and cur:
                    chunks.append(cur)
                    cur = char
                else:
                    cur = candidate
            if cur:
                chunks.append(cur)
            return chunks

        parts = text.split(sep)
        splits: list[str] = []
        for i, part in enumerate(parts):
            if not part:
                continue
            # Reattach separator except on last part
            reconstituted = part if (i == len(parts) - 1 or not sep) else part + sep
            if self.token_counter.count_tokens(reconstituted) <= self.chunk_size:
                splits.append(reconstituted)
            else:
                splits.extend(self._split_text(reconstituted, remaining_seps))

        return splits

    def chunk_document(self, parsed: ParsedDocument) -> list[GeneratedChunk]:
        """Chunk all segments of a parsed document and produce ordered GeneratedChunks."""
        separators = ["\n\n", "\n", ". ", "? ", "! ", " ", ""]

        # Track global character offset
        global_char_offset = 0
        all_pieces: list[tuple[str, int | None, int, int]] = []

        for seg in parsed.segments:
            seg_text = seg.text
            seg_splits = self._split_text(seg_text, separators)

            local_offset = 0
            for piece in seg_splits:
                p_text = piece.strip()
                if not p_text:
                    continue

                # Find piece location within segment text for accurate char offsets
                idx = seg_text.find(piece, local_offset)
                if idx == -1:
                    idx = local_offset
                local_offset = idx + len(piece)

                piece_char_start = global_char_offset + idx
                piece_char_end = piece_char_start + len(p_text)
                all_pieces.append((p_text, seg.page, piece_char_start, piece_char_end))

            global_char_offset += len(seg_text) + 2

        if not all_pieces:
            raise PermanentError(
                "Document produced zero chunks after text splitting.",
                error_code=DocumentErrorCode.NO_EXTRACTABLE_TEXT.value,
            )

        # Merge pieces into chunks up to chunk_size tokens, with chunk_overlap
        chunks: list[GeneratedChunk] = []
        cur_pieces: list[tuple[str, int | None, int, int]] = []
        cur_tokens = 0
        ordinal = 0

        i = 0
        while i < len(all_pieces):
            piece_text, p_page, p_cstart, p_cend = all_pieces[i]
            p_tokens = self.token_counter.count_tokens(piece_text)

            if cur_tokens + p_tokens <= self.chunk_size or not cur_pieces:
                cur_pieces.append((piece_text, p_page, p_cstart, p_cend))
                cur_tokens += p_tokens
                i += 1
            else:
                # Emit current chunk
                combined_text = " ".join(p[0] for p in cur_pieces).strip()
                pages = [p[1] for p in cur_pieces if p[1] is not None]
                page_start = min(pages) if pages else None
                page_end = max(pages) if pages else None
                c_start = cur_pieces[0][2]
                c_end = cur_pieces[-1][3]

                chunks.append(
                    GeneratedChunk(
                        ordinal=ordinal,
                        text=combined_text,
                        token_count=self.token_counter.count_tokens(combined_text),
                        page_start=page_start,
                        page_end=page_end,
                        char_start=c_start,
                        char_end=c_end,
                    )
                )
                ordinal += 1

                if len(chunks) > self.max_chunks_per_doc:
                    raise PermanentError(
                        f"Document chunk count exceeded limit of {self.max_chunks_per_doc} chunks.",
                        error_code=DocumentErrorCode.LIMIT_EXCEEDED.value,
                    )

                # Rewind back to satisfy overlap
                overlap_tokens = 0
                rewind_count = 0
                for r_piece in reversed(cur_pieces):
                    r_toks = self.token_counter.count_tokens(r_piece[0])
                    if overlap_tokens + r_toks <= self.chunk_overlap:
                        overlap_tokens += r_toks
                        rewind_count += 1
                    else:
                        break

                if rewind_count > 0 and rewind_count < len(cur_pieces):
                    cur_pieces = cur_pieces[-rewind_count:]
                    cur_tokens = sum(self.token_counter.count_tokens(p[0]) for p in cur_pieces)
                else:
                    cur_pieces = []
                    cur_tokens = 0

        # Emit trailing chunk
        if cur_pieces:
            combined_text = " ".join(p[0] for p in cur_pieces).strip()
            pages = [p[1] for p in cur_pieces if p[1] is not None]
            page_start = min(pages) if pages else None
            page_end = max(pages) if pages else None
            c_start = cur_pieces[0][2]
            c_end = cur_pieces[-1][3]

            chunks.append(
                GeneratedChunk(
                    ordinal=ordinal,
                    text=combined_text,
                    token_count=self.token_counter.count_tokens(combined_text),
                    page_start=page_start,
                    page_end=page_end,
                    char_start=c_start,
                    char_end=c_end,
                )
            )

        if len(chunks) > self.max_chunks_per_doc:
            raise PermanentError(
                f"Document chunk count exceeded limit of {self.max_chunks_per_doc} chunks.",
                error_code=DocumentErrorCode.LIMIT_EXCEEDED.value,
            )

        return chunks
