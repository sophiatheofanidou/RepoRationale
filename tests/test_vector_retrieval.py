"""Tests for the persisted Voyage/Chroma vector-retrieval product path:
end-to-end seed retrieval against a real temporary Chroma index, the
`search_history` contract (blank-query rejection, the internal maximum of
five, and the query-embedding/result contract), restart reuse without
re-embedding, propagation of a corrupted artifact versus rebuilding a merely
incompatible one, insertion batching, and survival of a previously valid
index across a failed forced rebuild.

Every test writes under `tmp_path`; no generated snapshot is ever written
into the repository, and no live Voyage or Chroma network call is ever
made — embeddings come from small hand-authored fake providers.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from chromadb.api.models.Collection import Collection

from reporationale.adapters.chroma_vector_store import (
    _add_records_in_batches,
    look_up_vector_index,
)
from reporationale.adapters.snapshot_store import (
    SnapshotCorrupted,
    load_chunk_artifact,
    load_snapshot,
    publish_snapshot,
    snapshot_directory,
)
from reporationale.application.chunk_snapshot import build_chunk_snapshot
from reporationale.application.chunking import CHUNKER_ALGORITHM_VERSION
from reporationale.application.progress import ProgressEvent
from reporationale.application.vector_retrieval import (
    SearchHistoryContractError,
    _embed_documents_with_progress,
    build_vector_snapshot,
    load_search_history_service,
)
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION
from reporationale.domain.embedding import EmbeddingBatch
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40
_MAX_CHARS = 1000

_POLLING_TEXT = (
    "Polling was chosen because webhook delivery was unreliable across "
    "customer network configurations."
)
_UNRELATED_TEXT = "Configuration keys are documented in the reference guide."


class _FixedVectorEmbeddingProvider:
    """A hand-authored fake `EmbeddingProvider`: every document text and
    every query text maps to one fixed vector, chosen so cosine distance
    ranks exactly as expected. Tracks how many times each operation was
    called, and can be told to fail `embed_documents` outright to prove a
    reused index never re-embeds the repository."""

    def __init__(
        self,
        *,
        document_vectors: dict[str, tuple[float, ...]],
        query_vectors: dict[str, tuple[float, ...]],
        forbid_document_embedding: bool = False,
    ) -> None:
        self._document_vectors = document_vectors
        self._query_vectors = query_vectors
        self._forbid_document_embedding = forbid_document_embedding
        self.document_call_count = 0
        self.query_call_count = 0

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        if self._forbid_document_embedding:
            raise AssertionError(
                "embed_documents must not be called when reusing a "
                "compatible persisted vector index."
            )
        self.document_call_count += 1
        vectors = tuple(self._document_vectors[text] for text in texts)
        return EmbeddingBatch(vectors=vectors, total_tokens=len(texts))

    def embed_query(self, text: str) -> EmbeddingBatch:
        self.query_call_count += 1
        return EmbeddingBatch(vectors=(self._query_vectors[text],), total_tokens=1)


def _document(source_id: str, text: str) -> SourceDocument:
    return SourceDocument(
        source_id=source_id,
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        text=text,
        source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
    )


def _publish_seed_snapshot(tmp_path: Path) -> Path:
    documents = (
        _document("github:octo-org/example-repo:markdown:a-config.md", _UNRELATED_TEXT),
        _document("github:octo-org/example-repo:markdown:b-polling.md", _POLLING_TEXT),
    )
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": len(documents)},
        producer_version="test/0",
    )
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    return snapshot_dir


def _seed_provider(
    *, forbid_document_embedding: bool = False
) -> _FixedVectorEmbeddingProvider:
    return _FixedVectorEmbeddingProvider(
        document_vectors={
            _POLLING_TEXT: (1.0, 0.0),
            _UNRELATED_TEXT: (0.0, 1.0),
        },
        query_vectors={"Why polling?": (0.99, 0.05)},
        forbid_document_embedding=forbid_document_embedding,
    )


class _ProgressRecordingEmbeddingProvider:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.calls: list[list[str]] = []
        self._fail_on_call = fail_on_call

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        call_number = len(self.calls) + 1
        self.calls.append(list(texts))
        if call_number == self._fail_on_call:
            raise RuntimeError("progress group failed")
        return EmbeddingBatch(
            vectors=tuple(
                (
                    float(text.removeprefix("chunk-"))
                    if text.startswith("chunk-")
                    else float(index),
                    1.0,
                )
                for index, text in enumerate(texts)
            ),
            total_tokens=call_number * 100,
        )

    def embed_query(self, text: str) -> EmbeddingBatch:
        raise AssertionError("query embedding is not expected")


def test_progress_embedding_groups_preserve_order_tokens_and_exact_counts() -> None:
    texts = [f"chunk-{index}" for index in range(1100)]
    provider = _ProgressRecordingEmbeddingProvider()
    events: list[ProgressEvent] = []

    result = _embed_documents_with_progress(
        texts=texts, embedding_provider=provider, on_progress=events.append
    )

    assert [len(call) for call in provider.calls] == [512, 512, 76]
    assert [text for call in provider.calls for text in call] == texts
    assert [vector[0] for vector in result.vectors] == [float(i) for i in range(1100)]
    assert result.total_tokens == 100 + 200 + 300
    assert [(event.status, event.completed, event.total) for event in events] == [
        ("started", None, 1100),
        ("progress", 512, 1100),
        ("progress", 1024, 1100),
        ("progress", 1100, 1100),
        ("completed", 1100, 1100),
    ]


def test_progress_embedding_does_not_report_a_failed_group_as_completed() -> None:
    texts = [f"chunk-{index}" for index in range(1100)]
    provider = _ProgressRecordingEmbeddingProvider(fail_on_call=2)
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="progress group failed"):
        _embed_documents_with_progress(
            texts=texts, embedding_provider=provider, on_progress=events.append
        )

    assert [len(call) for call in provider.calls] == [512, 512]
    assert [(event.status, event.completed) for event in events] == [
        ("started", None),
        ("progress", 512),
    ]


def test_build_vector_snapshot_without_progress_keeps_one_full_provider_call(
    tmp_path: Path,
) -> None:
    documents = tuple(
        sorted(
            (
                _document(
                    f"github:octo-org/example-repo:markdown:doc{index}.md",
                    f"Distinct rationale paragraph number {index} about the change.",
                )
                for index in range(513)
            ),
            key=lambda document: document.source_id,
        )
    )
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": len(documents)},
        producer_version="test/0",
    )
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    provider = _ProgressRecordingEmbeddingProvider()

    result = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=provider,
    )

    assert len(result.chunks) == 513
    assert [len(call) for call in provider.calls] == [513]


def test_search_history_ranks_expected_evidence_first_with_full_provenance(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    provider = _seed_provider()

    service = load_search_history_service(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, embedding_provider=provider
    )
    results = service.search_history("Why polling?")

    assert len(results) == 2
    assert [result.rank for result in results] == [1, 2]
    assert results[0].score_kind == "cosine_distance"
    assert results[0].chunk.text == _POLLING_TEXT
    assert (
        results[0].chunk.source_id
        == "github:octo-org/example-repo:markdown:b-polling.md"
    )
    assert results[0].chunk.source_url is not None
    assert results[0].evidence_id == results[0].chunk.chunk_id
    assert results[0].score < results[1].score
    assert provider.document_call_count == 1
    assert provider.query_call_count == 1


def test_search_history_rejects_blank_query_and_caps_results_at_five(
    tmp_path: Path,
) -> None:
    texts = [
        f"Distinct rationale paragraph number {i} about the change." for i in range(6)
    ]
    documents = tuple(
        _document(f"github:octo-org/example-repo:markdown:doc{i}.md", text)
        for i, text in enumerate(texts)
    )
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": len(documents)},
        producer_version="test/0",
    )
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)

    provider = _FixedVectorEmbeddingProvider(
        document_vectors={text: (float(i), 1.0) for i, text in enumerate(texts)},
        query_vectors={"rationale": (0.0, 1.0)},
    )
    service = load_search_history_service(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, embedding_provider=provider
    )

    results = service.search_history("rationale")
    assert len(results) == 5

    with pytest.raises(ValueError, match="blank"):
        service.search_history("   ")
    with pytest.raises(ValueError, match="blank"):
        service.search_history("")


class _MultiVectorQueryProvider:
    """A fake `EmbeddingProvider` that embeds documents normally but always
    returns two vectors for one query, to exercise the query-embedding
    cardinality guard without a malformed-query permutation matrix."""

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        document_vectors = {_POLLING_TEXT: (1.0, 0.0), _UNRELATED_TEXT: (0.0, 1.0)}
        vectors = tuple(document_vectors[text] for text in texts)
        return EmbeddingBatch(vectors=vectors, total_tokens=len(texts))

    def embed_query(self, text: str) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=((1.0, 0.0), (0.0, 1.0)), total_tokens=2)


def test_search_history_rejects_a_query_that_embeds_to_more_than_one_vector(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    service = load_search_history_service(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=_MultiVectorQueryProvider(),
    )

    with pytest.raises(SearchHistoryContractError, match="exactly one query vector"):
        service.search_history("Why polling?")


def test_fresh_service_reuses_persisted_index_without_reembedding_and_leaves_artifacts_unchanged(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    building_provider = _seed_provider()
    load_search_history_service(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=building_provider,
    )
    assert building_provider.document_call_count == 1

    sources_path = snapshot_dir / "sources.jsonl"
    manifest_path = snapshot_dir / "manifest.json"
    chunks_path = snapshot_dir / "chunks" / "chunks.jsonl"
    sources_bytes_before = sources_path.read_bytes()
    manifest_bytes_before = manifest_path.read_bytes()
    chunks_bytes_before = chunks_path.read_bytes()

    # A fresh service instance (simulating a restart) with a provider that
    # fails the test outright if document embedding is ever requested.
    reuse_provider = _seed_provider(forbid_document_embedding=True)
    reused_service = load_search_history_service(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=reuse_provider,
    )
    results = reused_service.search_history("Why polling?")

    assert results[0].chunk.text == _POLLING_TEXT
    assert reuse_provider.document_call_count == 0
    assert reuse_provider.query_call_count == 1
    assert sources_path.read_bytes() == sources_bytes_before
    assert manifest_path.read_bytes() == manifest_bytes_before
    assert chunks_path.read_bytes() == chunks_bytes_before


def test_corrupted_vector_index_propagates_and_never_triggers_reembedding(
    tmp_path: Path,
) -> None:
    """A `chunks_digest` mismatch means the persisted index is anchored to
    a different chunk artifact than the one on disk — genuine data
    corruption, not a configuration mismatch. It must propagate as a typed
    `SnapshotCorrupted` failure rather than being silently classified as
    rebuildable, and must never reach the embedding provider."""
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    provider = _seed_provider()
    build_vector_snapshot(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, embedding_provider=provider
    )
    assert provider.document_call_count == 1

    vector_manifest_path = snapshot_dir / "vector_index" / "manifest.json"
    raw = json.loads(vector_manifest_path.read_text(encoding="utf-8"))
    raw["chunks_digest"] = "0" * 64
    vector_manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    source_snapshot = load_snapshot(snapshot_dir)
    chunk_artifact = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=_MAX_CHARS,
    )

    with pytest.raises(SnapshotCorrupted):
        look_up_vector_index(
            snapshot_dir=snapshot_dir,
            expected_chunks=chunk_artifact.chunks,
            source_schema_version=source_snapshot.manifest.source_schema_version,
            sources_digest=source_snapshot.manifest.sources_digest,
            chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
            chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
            max_chars=_MAX_CHARS,
            chunks_digest=chunk_artifact.manifest.chunks_digest,
            embedding_model="voyage-4",
        )

    # `build_vector_snapshot` must let the corruption propagate rather than
    # silently rebuilding, so the provider that would fail the test if
    # asked to embed documents must never be called.
    non_embedding_provider = _seed_provider(forbid_document_embedding=True)
    with pytest.raises(SnapshotCorrupted):
        build_vector_snapshot(
            snapshot_dir=snapshot_dir,
            max_chars=_MAX_CHARS,
            embedding_provider=non_embedding_provider,
        )
    assert non_embedding_provider.document_call_count == 0


def test_incompatible_vector_index_is_rebuilt_rather_than_reused(
    tmp_path: Path,
) -> None:
    """A different stored `vector_store_version` is a genuine compatibility
    mismatch (this build's installed Chroma differs from the one that
    produced the index), not corrupted data — it must classify as
    `"incompatible"` and `build_vector_snapshot` must rebuild it."""
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    provider = _seed_provider()
    build_vector_snapshot(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, embedding_provider=provider
    )
    assert provider.document_call_count == 1

    vector_manifest_path = snapshot_dir / "vector_index" / "manifest.json"
    raw = json.loads(vector_manifest_path.read_text(encoding="utf-8"))
    raw["vector_store_version"] = "0.0.0-different"
    vector_manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    source_snapshot = load_snapshot(snapshot_dir)
    chunk_artifact = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=_MAX_CHARS,
    )
    lookup = look_up_vector_index(
        snapshot_dir=snapshot_dir,
        expected_chunks=chunk_artifact.chunks,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=_MAX_CHARS,
        chunks_digest=chunk_artifact.manifest.chunks_digest,
        embedding_model="voyage-4",
    )
    assert lookup.kind == "incompatible"

    rebuild_provider = _seed_provider()
    rebuilt = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=rebuild_provider,
    )
    assert rebuilt.reused_existing_artifact is False
    assert rebuild_provider.document_call_count == 1


def test_failed_forced_rebuild_leaves_previous_valid_index_reusable(
    tmp_path: Path,
) -> None:
    snapshot_dir = _publish_seed_snapshot(tmp_path)
    provider = _seed_provider()
    first = build_vector_snapshot(
        snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS, embedding_provider=provider
    )

    manifest_path = snapshot_dir / "vector_index" / "manifest.json"
    manifest_bytes_before = manifest_path.read_bytes()

    class _BrokenProvider:
        def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
            # Returns one fewer vector than requested chunks, which
            # `publish_vector_index` must reject before publishing anything.
            return EmbeddingBatch(vectors=((1.0, 0.0),), total_tokens=1)

        def embed_query(self, text: str) -> EmbeddingBatch:
            raise AssertionError("not expected to be called in this test")

    with pytest.raises(ValueError):
        build_vector_snapshot(
            snapshot_dir=snapshot_dir,
            max_chars=_MAX_CHARS,
            embedding_provider=_BrokenProvider(),
            force_rebuild=True,
        )

    assert manifest_path.read_bytes() == manifest_bytes_before
    assert not any(
        entry.name.startswith(".vector-index-staging-")
        or entry.name.endswith(".backup")
        for entry in snapshot_dir.iterdir()
    )

    reused_provider = _seed_provider(forbid_document_embedding=True)
    reused = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=reused_provider,
    )
    assert reused.reused_existing_artifact is True
    assert reused.manifest == first.manifest


class _RecordingCollection:
    """A controlled fake standing in for a Chroma `Collection`: records
    every `add` call's arguments without touching any real Chroma state."""

    def __init__(self) -> None:
        self.calls: list[dict[str, list[object]]] = []

    def add(
        self,
        *,
        ids: list[str],
        embeddings: list[object],
        documents: list[str],
        metadatas: list[object],
    ) -> None:
        self.calls.append(
            {
                "ids": list(ids),
                "embeddings": list(embeddings),
                "documents": list(documents),
                "metadatas": list(metadatas),
            }
        )


def test_add_records_in_batches_preserves_order_and_alignment_across_batches() -> None:
    collection = _RecordingCollection()
    ids = [f"chunk-{i}" for i in range(5)]
    embeddings: list[Sequence[float]] = [[float(i), 0.0] for i in range(5)]
    documents = [f"doc-{i}" for i in range(5)]
    metadatas: list[dict[str, str | int]] = [{"chunk_index": i} for i in range(5)]

    _add_records_in_batches(
        cast(Collection, collection),
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
        batch_size=2,
    )

    assert [len(call["ids"]) for call in collection.calls] == [2, 2, 1]
    flattened_ids = [id_ for call in collection.calls for id_ in call["ids"]]
    flattened_documents = [
        document for call in collection.calls for document in call["documents"]
    ]
    flattened_metadatas = [
        metadata for call in collection.calls for metadata in call["metadatas"]
    ]
    assert flattened_ids == ids
    assert flattened_documents == documents
    assert flattened_metadatas == metadatas
