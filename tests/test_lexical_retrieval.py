"""Tests for the offline BM25 lexical retrieval baseline and its shared
`RankedEvidence` contract: representative ranking, deterministic tie
handling and limiting, no-match/empty-corpus behaviour, contract-specific
invalid inputs, and loading a reusable retriever from a persisted chunk
artifact without rebuilding or rewriting anything.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from reporationale.adapters.snapshot_store import (
    load_snapshot,
    publish_chunk_artifact,
    publish_snapshot,
    snapshot_directory,
)
from reporationale.application.chunking import (
    CHUNKER_ALGORITHM_VERSION,
    chunk_source_documents,
)
from reporationale.application.lexical_retrieval import (
    BM25Retriever,
    load_lexical_retriever,
)
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION, SourceChunk
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.retrieval import RankedEvidence
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40


def _chunk(chunk_id: str, text: str, **overrides: object) -> SourceChunk:
    source_id, _separator, chunk_index_text = chunk_id.rpartition(":chunk:")
    values: dict[str, object] = {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "chunk_index": int(chunk_index_text),
        "text": text,
        "platform": "github",
        "repository": "octo-org/example-repo",
        "source_type": "markdown",
        "source_url": "https://github.com/octo-org/example-repo/blob/main/a.md",
    }
    values.update(overrides)
    return SourceChunk.model_validate(values)


def test_search_ranks_expected_chunk_first() -> None:
    chunks = [
        _chunk(
            "github:octo-org/example-repo:markdown:a.md:chunk:0",
            "The retry logic backs off exponentially after a network timeout.",
        ),
        _chunk(
            "github:octo-org/example-repo:markdown:b.md:chunk:0",
            "This document explains how to configure logging levels.",
        ),
        _chunk(
            "github:octo-org/example-repo:markdown:c.md:chunk:0",
            "Timeouts are configurable per request in the HTTP client.",
        ),
    ]
    retriever = BM25Retriever(chunks)

    results = retriever.search("network timeout retry", limit=5)

    assert len(results) >= 1
    assert results[0].evidence_id == chunks[0].chunk_id
    assert results[0].chunk == chunks[0]
    assert [result.rank for result in results] == list(range(1, len(results) + 1))
    scores = [result.score for result in results]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)
    assert all(result.score_kind == "bm25" for result in results)


def test_search_breaks_ties_by_chunk_id_and_applies_limit() -> None:
    same_text = "Configuration keys are documented in the reference guide."
    chunk_b = _chunk("github:octo-org/example-repo:markdown:b.md:chunk:0", same_text)
    chunk_a = _chunk("github:octo-org/example-repo:markdown:a.md:chunk:0", same_text)
    # Supplied in an order different from their chunk_id order.
    retriever = BM25Retriever([chunk_b, chunk_a])

    limited = retriever.search("configuration keys reference", limit=1)
    assert len(limited) == 1
    assert limited[0].evidence_id == chunk_a.chunk_id
    assert limited[0].rank == 1

    unlimited = retriever.search("configuration keys reference", limit=10)
    assert [result.evidence_id for result in unlimited] == [
        chunk_a.chunk_id,
        chunk_b.chunk_id,
    ]
    assert unlimited[0].score == unlimited[1].score
    assert [result.rank for result in unlimited] == [1, 2]


def test_search_returns_no_evidence_for_no_match_or_empty_corpus() -> None:
    chunks = [
        _chunk(
            "github:octo-org/example-repo:markdown:a.md:chunk:0",
            "The retry logic backs off exponentially after a network timeout.",
        ),
    ]
    retriever = BM25Retriever(chunks)

    assert retriever.search("nonexistent zzzqux term", limit=5) == ()

    empty_retriever = BM25Retriever([])
    assert empty_retriever.search("network timeout", limit=5) == ()


def test_contract_specific_invalid_inputs() -> None:
    chunk = _chunk(
        "github:octo-org/example-repo:markdown:a.md:chunk:0",
        "Some example chunk text about retries.",
    )
    retriever = BM25Retriever([chunk])

    with pytest.raises(ValueError, match="searchable token"):
        retriever.search("   ", limit=5)
    with pytest.raises(ValueError, match="searchable token"):
        retriever.search("...", limit=5)

    for bad_limit in (True, "5", 0, -1):
        with pytest.raises(ValueError, match="limit"):
            retriever.search("retries", limit=bad_limit)  # type: ignore[arg-type]

    duplicate = _chunk(
        "github:octo-org/example-repo:markdown:a.md:chunk:0",
        "Duplicate id chunk.",
    )
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        BM25Retriever([chunk, duplicate])

    with pytest.raises(ValidationError, match="evidence_id must equal"):
        RankedEvidence(
            evidence_id="github:octo-org/example-repo:markdown:other.md:chunk:0",
            rank=1,
            score=1.0,
            score_kind="bm25",
            chunk=chunk,
        )

    with pytest.raises(ValidationError, match="score_kind must not be blank"):
        RankedEvidence(
            evidence_id=chunk.chunk_id,
            rank=1,
            score=1.0,
            score_kind="   ",
            chunk=chunk,
        )


def _document(source_id: str, text: str) -> SourceDocument:
    return SourceDocument(
        source_id=source_id,
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        text=text,
        source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
    )


def _publish_normalized_snapshot(
    tmp_path: Path, documents: tuple[SourceDocument, ...]
) -> Path:
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": len(documents)},
        producer_version="test/0",
    )
    return snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )


def test_load_lexical_retriever_from_persisted_artifact_supports_multiple_queries(
    tmp_path: Path,
) -> None:
    documents = (
        _document(
            "github:octo-org/example-repo:markdown:a.md",
            "The retry logic backs off exponentially after a network timeout.",
        ),
        _document(
            "github:octo-org/example-repo:markdown:b.md",
            "Configuration keys are documented in the reference guide.",
        ),
    )
    snapshot_dir = _publish_normalized_snapshot(tmp_path, documents)
    source_snapshot = load_snapshot(snapshot_dir)
    chunks = chunk_source_documents(source_snapshot.documents, max_chars=1000)
    publish_chunk_artifact(
        snapshot_dir=snapshot_dir,
        chunks=chunks,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=1000,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
    )

    manifest_path = snapshot_dir / "manifest.json"
    sources_path = snapshot_dir / "sources.jsonl"
    chunks_manifest_path = snapshot_dir / "chunks" / "manifest.json"
    chunks_jsonl_path = snapshot_dir / "chunks" / "chunks.jsonl"
    manifest_bytes_before = manifest_path.read_bytes()
    sources_bytes_before = sources_path.read_bytes()
    chunks_manifest_bytes_before = chunks_manifest_path.read_bytes()
    chunks_jsonl_bytes_before = chunks_jsonl_path.read_bytes()

    retriever = load_lexical_retriever(snapshot_dir=snapshot_dir, max_chars=1000)

    first = retriever.search("network timeout retry", limit=1)
    assert len(first) == 1
    assert first[0].chunk.source_id == "github:octo-org/example-repo:markdown:a.md"

    second = retriever.search("configuration reference guide", limit=1)
    assert len(second) == 1
    assert second[0].chunk.source_id == "github:octo-org/example-repo:markdown:b.md"

    assert manifest_path.read_bytes() == manifest_bytes_before
    assert sources_path.read_bytes() == sources_bytes_before
    assert chunks_manifest_path.read_bytes() == chunks_manifest_bytes_before
    assert chunks_jsonl_path.read_bytes() == chunks_jsonl_bytes_before
