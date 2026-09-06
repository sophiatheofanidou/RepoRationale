"""Deterministic, source-aware splitting of one or many `SourceDocument`s
into `SourceChunk`s.

Two splitting behaviours only: Markdown sources (`source_type ==
"markdown"`) are split by heading section first, then by Markdown block
boundary (paragraph, contiguous list, table, or fenced code block); every
other current or future source type is split at its own natural
`SourceDocument` boundary, falling back to blank-line paragraph boundaries
only when the whole document does not fit. Either behaviour falls back to a
deterministic whitespace-preferring, hard-character-boundary split for any
single block or paragraph that alone exceeds `max_chars`.

A heading line is content, not just structure: it is preserved exactly
once, in the first emitted chunk of the section it introduces, counted
against `max_chars` like any other block. `heading_path` separately
records the full active heading hierarchy for every chunk; ancestor
headings are never synthetically repeated into a descendant chunk's text.

This is a small deterministic standard-library implementation appropriate
for the existing Markdown corpus, not a full CommonMark parser. It does not
select or expose a project-wide default chunk size, embed anything, or
build a corpus/registry/strategy abstraction beyond the two functions
below.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from reporationale.domain.chunk import SourceChunk
from reporationale.domain.source_document import SourceDocument

# Bumped whenever a change to this module's splitting behaviour could alter
# emitted chunk boundaries or ids for the same input and `max_chars`.
CHUNKER_ALGORITHM_VERSION = 1

_ATX_HEADING_PATTERN = re.compile(r"^(#{1,6})(?:[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*)?$")
_FENCE_OPEN_PATTERN = re.compile(r"^(`{3,}|~{3,})")
_LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_PARAGRAPH_SPLIT_PATTERN = re.compile(r"\n[ \t]*\n")


def _require_max_chars(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_chars must be a strict positive integer")
    return value


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Deterministically split one already-oversized `text` into ordered
    pieces, each at most `max_chars` characters, preferring to cut at a
    whitespace boundary and falling back to a hard character cut only when
    no whitespace exists within reach. Never emits an empty or
    whitespace-only piece; the pieces, read in order, preserve all
    non-whitespace source content in its original order."""
    pieces: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_chars:
            pieces.append(remaining)
            break
        window = remaining[:max_chars]
        last_space = max(window.rfind(" "), window.rfind("\t"), window.rfind("\n"))
        cut = last_space if last_space > 0 else max_chars
        piece = remaining[:cut].strip()
        if piece:
            pieces.append(piece)
        remaining = remaining[cut:].strip()
    return pieces


def _pack_blocks(block_texts: Sequence[str], max_chars: int) -> list[str]:
    """Greedily pack `block_texts` (already-ordered, block-boundary-aligned
    pieces of one document or heading section) into chunks joined by a
    blank line, keeping each whole block intact whenever it fits and never
    exceeding `max_chars`. A block that alone exceeds `max_chars` is
    deterministically split by `_split_oversized` instead of being kept
    whole."""
    chunks: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            chunks.append("\n\n".join(buffer))
            buffer.clear()

    for raw_block in block_texts:
        block = raw_block.strip()
        if not block:
            continue
        if len(block) > max_chars:
            flush()
            chunks.extend(_split_oversized(block, max_chars))
            continue
        candidate = [*buffer, block]
        if buffer and len("\n\n".join(candidate)) > max_chars:
            flush()
            candidate = [block]
        buffer[:] = candidate
    flush()
    return chunks


def _split_paragraphs(text: str) -> list[str]:
    return [
        paragraph
        for paragraph in _PARAGRAPH_SPLIT_PATTERN.split(text)
        if paragraph.strip()
    ]


def _chunk_generic_text(text: str, *, max_chars: int) -> list[str]:
    """Non-Markdown splitting: the whole document is the natural boundary;
    fall back to greedy blank-line paragraph splitting only when it does
    not fit."""
    stripped = text.strip()
    if not stripped:
        return []
    if len(stripped) <= max_chars:
        return [stripped]
    return _pack_blocks(_split_paragraphs(text), max_chars)


@dataclass(frozen=True)
class _MarkdownBlock:
    kind: str
    level: int | None
    text: str
    # Only meaningful for `kind == "heading"`: the heading's own label,
    # used to build `heading_path`. `text` (for every kind, headings
    # included) is the exact raw source line(s) that become chunk content —
    # a heading's line is deliberately not excluded from `text`, since the
    # line itself is content to preserve, not just structure.
    label: str = ""


def _continues_fenced_or_heading_free_run(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _ATX_HEADING_PATTERN.match(line):
        return False
    if _FENCE_OPEN_PATTERN.match(stripped):
        return False
    return True


def _continues_paragraph(line: str) -> bool:
    stripped = line.strip()
    if not _continues_fenced_or_heading_free_run(line):
        return False
    if _LIST_MARKER_PATTERN.match(line):
        return False
    if "|" in stripped:
        return False
    return True


def _split_markdown_blocks(text: str) -> list[_MarkdownBlock]:
    """Split `text` into an ordered sequence of heading, paragraph, list,
    table, and fenced-code blocks using a small deterministic
    line-oriented scan. Not a full CommonMark implementation."""
    lines = text.split("\n")
    blocks: list[_MarkdownBlock] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        stripped_line = line.strip()

        if not stripped_line:
            index += 1
            continue

        heading_match = _ATX_HEADING_PATTERN.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            label = (heading_match.group(2) or "").strip()
            blocks.append(
                _MarkdownBlock(kind="heading", level=level, text=line, label=label)
            )
            index += 1
            continue

        fence_match = _FENCE_OPEN_PATTERN.match(stripped_line)
        if fence_match:
            fence_char = fence_match.group(1)[0]
            fence_length = len(fence_match.group(1))
            block_lines = [line]
            index += 1
            while index < total:
                block_lines.append(lines[index])
                closing_candidate = lines[index].strip()
                is_closing = len(closing_candidate) >= fence_length and set(
                    closing_candidate
                ) == {fence_char}
                index += 1
                if is_closing:
                    break
            blocks.append(
                _MarkdownBlock(kind="code", level=None, text="\n".join(block_lines))
            )
            continue

        if _LIST_MARKER_PATTERN.match(line):
            block_lines = [line]
            index += 1
            while index < total and _continues_fenced_or_heading_free_run(lines[index]):
                block_lines.append(lines[index])
                index += 1
            blocks.append(
                _MarkdownBlock(kind="list", level=None, text="\n".join(block_lines))
            )
            continue

        if "|" in stripped_line:
            block_lines = [line]
            index += 1
            while (
                index < total
                and lines[index].strip()
                and "|" in lines[index]
                and not _ATX_HEADING_PATTERN.match(lines[index])
            ):
                block_lines.append(lines[index])
                index += 1
            blocks.append(
                _MarkdownBlock(kind="table", level=None, text="\n".join(block_lines))
            )
            continue

        block_lines = [line]
        index += 1
        while index < total and _continues_paragraph(lines[index]):
            block_lines.append(lines[index])
            index += 1
        blocks.append(
            _MarkdownBlock(kind="paragraph", level=None, text="\n".join(block_lines))
        )

    return blocks


def _group_by_heading_path(
    blocks: Sequence[_MarkdownBlock],
) -> list[tuple[tuple[str, ...], list[_MarkdownBlock]]]:
    """Group `blocks` into maximal consecutive runs, each tagged with the
    full heading hierarchy active at that point in the document.

    A heading block starts a new group and is itself the first block of
    that group's own content — the heading line it introduces is preserved
    exactly once, in its own section, and is never re-inserted into any
    descendant section's group. Since every non-blank line produces at
    least one block (see `_split_markdown_blocks`) and every block belongs
    to exactly one group, this never returns an empty list for non-blank
    input.
    """
    groups: list[tuple[tuple[str, ...], list[_MarkdownBlock]]] = []
    heading_stack: list[tuple[int, str]] = []
    current: list[_MarkdownBlock] = []

    def flush() -> None:
        if current:
            heading_path = tuple(label for _level, label in heading_stack)
            groups.append((heading_path, list(current)))
            current.clear()

    for block in blocks:
        if block.kind == "heading":
            flush()
            if block.level is None:
                raise AssertionError("unreachable: a heading block always has a level")
            while heading_stack and heading_stack[-1][0] >= block.level:
                heading_stack.pop()
            heading_stack.append((block.level, block.label))
        current.append(block)

    flush()
    return groups


def _chunk_markdown_text(
    text: str, *, max_chars: int
) -> list[tuple[str, tuple[str, ...]]]:
    """Markdown splitting: prefer a whole heading section (including the
    heading line that introduces it) as one chunk; within an oversized
    section, prefer Markdown block boundaries, with the heading line
    itself counted and packed like any other block. Returns
    `(chunk_text, heading_path)` pairs in document order."""
    blocks = _split_markdown_blocks(text)
    groups = _group_by_heading_path(blocks)

    if not groups:
        raise AssertionError(
            "unreachable: a non-blank Markdown document always yields at "
            "least one block, and every block belongs to some group"
        )

    results: list[tuple[str, tuple[str, ...]]] = []
    for heading_path, group_blocks in groups:
        block_texts = [block.text for block in group_blocks]
        whole_text = "\n\n".join(
            block_text.strip() for block_text in block_texts if block_text.strip()
        )
        if whole_text and len(whole_text) <= max_chars:
            results.append((whole_text, heading_path))
            continue
        for chunk_text in _pack_blocks(block_texts, max_chars):
            results.append((chunk_text, heading_path))
    return results


def chunk_source_document(
    document: SourceDocument, *, max_chars: int
) -> tuple[SourceChunk, ...]:
    """Split one normalized `document` into an ordered, deterministic
    sequence of `SourceChunk`s.

    `max_chars` must be an explicit strict positive integer (a boolean, a
    numeric string, zero, or a negative value is rejected). Positions
    restart at zero for every document. Calling this repeatedly with the
    same `document` and `max_chars` always returns equal chunks with equal
    ids, since it is a pure function of the document's own text and
    provenance.
    """
    validated_max_chars = _require_max_chars(max_chars)

    if document.source_type == "markdown":
        texts_with_headings = _chunk_markdown_text(
            document.text, max_chars=validated_max_chars
        )
    else:
        texts_with_headings = [
            (chunk_text, ())
            for chunk_text in _chunk_generic_text(
                document.text, max_chars=validated_max_chars
            )
        ]

    chunks: list[SourceChunk] = []
    for chunk_index, (chunk_text, heading_path) in enumerate(texts_with_headings):
        chunks.append(
            SourceChunk(
                chunk_id=f"{document.source_id}:chunk:{chunk_index}",
                source_id=document.source_id,
                chunk_index=chunk_index,
                text=chunk_text,
                platform=document.platform,
                repository=document.repository,
                source_type=document.source_type,
                source_url=document.source_url,
                created_at=document.created_at,
                updated_at=document.updated_at,
                parent_source_id=document.parent_source_id,
                item_number=document.item_number,
                title=document.title,
                metadata=document.metadata,
                heading_path=heading_path,
            )
        )
    return tuple(chunks)


def chunk_source_documents(
    documents: Sequence[SourceDocument], *, max_chars: int
) -> tuple[SourceChunk, ...]:
    """Chunk every document in `documents`, in order, and flatten the
    results. A thin deterministic flattening of `chunk_source_document`; it
    introduces no additional corpus model, source registry, or per-type
    strategy of its own."""
    validated_max_chars = _require_max_chars(max_chars)
    chunks: list[SourceChunk] = []
    for document in documents:
        chunks.extend(chunk_source_document(document, max_chars=validated_max_chars))
    return tuple(chunks)
