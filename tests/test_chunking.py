"""Tests for source-aware chunking: Markdown heading/block-boundary
splitting, natural-boundary splitting for every other source type, the
shared oversized-block fallback, and the two contract-specific validation
failures.
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from reporationale.application.chunking import chunk_source_document
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.source_document import SourceDocument


def _markdown_document(text: str, **overrides: object) -> SourceDocument:
    values: dict[str, object] = {
        "source_id": "github:octo/example:markdown:docs/guide.md",
        "platform": "github",
        "repository": "octo/example",
        "source_type": "markdown",
        "text": text,
        "source_url": ("https://github.com/octo/example/blob/main/docs/guide.md"),
        "metadata": {"path": "docs/guide.md"},
    }
    values.update(overrides)
    return SourceDocument.model_validate(values)


def _issue_document(text: str, **overrides: object) -> SourceDocument:
    values: dict[str, object] = {
        "source_id": "github:octo/example:issue:7:description",
        "platform": "github",
        "repository": "octo/example",
        "source_type": "issue",
        "text": text,
        "source_url": "https://github.com/octo/example/issues/7",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
        "parent_source_id": None,
        "item_number": 7,
        "title": "Crash on large files",
        "metadata": {"state": "closed"},
    }
    values.update(overrides)
    return SourceDocument.model_validate(values)


_MARKDOWN_TEXT = """# Guide

This tool helps you understand repository history quickly and clearly.

## Installation

Follow these steps carefully to get the tool installed on your machine correctly.

```bash
pip install reporationale
```

## Usage

Run the command line tool to ask a question.
"""


def test_chunk_markdown_document_follows_heading_and_block_boundaries() -> None:
    document = _markdown_document(_MARKDOWN_TEXT)

    chunks = chunk_source_document(document, max_chars=100)

    # Heading hierarchy: the intro is tagged with the top heading alone,
    # "Installation" and "Usage" content is tagged with the nested path.
    assert [chunk.heading_path for chunk in chunks] == [
        ("Guide",),
        ("Guide", "Installation"),
        ("Guide", "Installation"),
        ("Guide", "Usage"),
    ]
    assert all(len(chunk.text) <= 100 for chunk in chunks)

    # Each heading line is preserved as searchable text, exactly once, in
    # the first emitted chunk of the section it introduces — never
    # synthetically repeated into a descendant chunk.
    assert chunks[0].text.startswith("# Guide")
    assert chunks[1].text.startswith("## Installation")
    assert chunks[3].text.startswith("## Usage")
    combined_text = "\n".join(chunk.text for chunk in chunks)
    assert combined_text.count("# Guide") == 1
    assert combined_text.count("## Installation") == 1
    assert combined_text.count("## Usage") == 1

    # The fenced code block remains intact as its own chunk because it
    # fits alone.
    assert "```bash\npip install reporationale\n```" in [chunk.text for chunk in chunks]

    # All non-whitespace source content survives, in its original order,
    # across however many chunks the section boundaries produced.
    assert " ".join(chunk.text for chunk in chunks).split() == _MARKDOWN_TEXT.split()

    # Ordered indexes and stable, source-anchored ids.
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert [chunk.chunk_id for chunk in chunks] == [
        f"github:octo/example:markdown:docs/guide.md:chunk:{i}"
        for i in range(len(chunks))
    ]

    # Provenance is copied from the originating document.
    for chunk in chunks:
        assert chunk.source_id == document.source_id
        assert chunk.platform == document.platform
        assert chunk.repository == document.repository
        assert chunk.source_type == document.source_type
        assert str(chunk.source_url) == str(document.source_url)
        assert chunk.metadata == document.metadata

    # Deterministic: repeating the call with the same inputs is stable.
    assert chunk_source_document(document, max_chars=100) == chunks


def test_markdown_heading_only_document_keeps_heading_path() -> None:
    """A document consisting solely of a heading line must not fall back
    to an empty heading path: it behaves the same as any other section,
    just with no body content beneath it."""
    document = _markdown_document("# Just A Title")

    chunks = chunk_source_document(document, max_chars=1000)

    assert len(chunks) == 1
    assert chunks[0].heading_path == ("Just A Title",)
    assert chunks[0].text == "# Just A Title"


def test_markdown_heading_recognition_does_not_treat_trailing_hash_as_closing_syntax() -> (
    None
):
    """A trailing `#` that is part of the heading's own text (as in `C#`)
    is not preceded by Markdown closing-sequence whitespace, so it must
    not be stripped as a closing marker."""
    document = _markdown_document("# Learning C#\n\nSome notes about the language.")

    chunks = chunk_source_document(document, max_chars=1000)

    assert chunks[0].heading_path == ("Learning C#",)
    assert "# Learning C#" in chunks[0].text


def test_chunk_non_markdown_document_uses_natural_boundary_and_paragraph_fallback() -> (
    None
):
    text = (
        "This issue reports a crash when parsing very large uploaded files.\n\n"
        "The crash happens because the buffer size was fixed too small for "
        "larger inputs."
    )
    document = _issue_document(text)

    # The whole document is the natural citation boundary when it fits.
    whole_chunks = chunk_source_document(document, max_chars=1000)
    assert len(whole_chunks) == 1
    assert whole_chunks[0].text == text
    assert whole_chunks[0].heading_path == ()
    assert whole_chunks[0].parent_source_id is None
    assert whole_chunks[0].item_number == 7
    assert whole_chunks[0].title == "Crash on large files"
    assert whole_chunks[0].metadata == {"state": "closed"}

    # Only when it does not fit does splitting fall back to blank-line
    # paragraph boundaries.
    split_chunks = chunk_source_document(document, max_chars=90)
    assert len(split_chunks) == 2
    assert [chunk.chunk_index for chunk in split_chunks] == [0, 1]
    assert [chunk.chunk_id for chunk in split_chunks] == [
        "github:octo/example:issue:7:description:chunk:0",
        "github:octo/example:issue:7:description:chunk:1",
    ]
    assert all(len(chunk.text) <= 90 for chunk in split_chunks)
    assert split_chunks[0].text.startswith("This issue reports")
    assert split_chunks[1].text.startswith("The crash happens")
    for chunk in split_chunks:
        assert chunk.parent_source_id is None
        assert chunk.item_number == 7
        assert chunk.title == "Crash on large files"
        assert chunk.metadata == {"state": "closed"}


def test_oversized_block_falls_back_to_deterministic_split_without_reordering() -> None:
    words = [f"word{i}" for i in range(60)]
    text = " ".join(words)
    document = _issue_document(text)

    chunks = chunk_source_document(document, max_chars=40)

    assert len(chunks) > 1
    assert all(chunk.text.strip() for chunk in chunks)
    assert all(len(chunk.text) <= 40 for chunk in chunks)
    reconstructed_words = " ".join(chunk.text for chunk in chunks).split()
    assert reconstructed_words == words
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


@pytest.mark.parametrize("bad_max_chars", [True, "10", 0, -5])
def test_chunk_source_document_rejects_invalid_max_chars(bad_max_chars: object) -> None:
    document = _issue_document("Some short body text for the issue.")

    with pytest.raises(ValueError):
        chunk_source_document(document, max_chars=bad_max_chars)  # type: ignore[arg-type]


def test_source_chunk_rejects_id_disagreeing_with_source_and_index() -> None:
    mismatched = {
        "chunk_id": "github:octo/example:issue:7:description:chunk:1",
        "source_id": "github:octo/example:issue:7:description",
        "chunk_index": 0,
        "text": "mismatched chunk id",
        "platform": "github",
        "repository": "octo/example",
        "source_type": "issue",
        "source_url": "https://github.com/octo/example/issues/7",
    }

    with pytest.raises(ValidationError, match="chunk_id must equal"):
        SourceChunk(**mismatched)  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="chunk_id must equal"):
        SourceChunk.model_validate_json(json.dumps(mismatched))


def test_source_chunk_rejects_contradictory_provenance() -> None:
    base: dict[str, object] = {
        "chunk_id": "github:octo/example:issue:7:description:chunk:0",
        "source_id": "github:octo/example:issue:7:description",
        "chunk_index": 0,
        "text": "Some chunk text.",
        "platform": "github",
        "repository": "octo/example",
        "source_type": "issue",
        "source_url": "https://github.com/octo/example/issues/7",
    }

    wrong_namespace: dict[str, object] = {
        **base,
        "chunk_id": "gitlab:octo/example:issue:7:description:chunk:0",
        "source_id": "gitlab:octo/example:issue:7:description",
    }
    with pytest.raises(ValidationError, match="namespaced by platform"):
        SourceChunk(**wrong_namespace)  # type: ignore[arg-type]

    parent_wrong_namespace: dict[str, object] = {
        **base,
        "parent_source_id": "gitlab:octo/example:issue:7",
    }
    with pytest.raises(ValidationError, match="same platform namespace"):
        SourceChunk(**parent_wrong_namespace)  # type: ignore[arg-type]

    parent_is_self: dict[str, object] = {
        **base,
        "parent_source_id": base["source_id"],
    }
    with pytest.raises(ValidationError, match="cannot identify its own source"):
        SourceChunk(**parent_is_self)  # type: ignore[arg-type]

    timestamps_reversed: dict[str, object] = {
        **base,
        "created_at": datetime(2026, 1, 2, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    with pytest.raises(ValidationError, match="cannot precede"):
        SourceChunk(**timestamps_reversed)  # type: ignore[arg-type]
