"""Build, reuse, or explicitly rebuild the persisted Voyage/Chroma vector
index for one already-validated derived chunk artifact, and expose the
narrow `search_history` application service over it.

Never calls GitHub, never rebuilds the normalized-source snapshot, and never
re-chunks anything: the normalized-source and chunk-artifact pairs at
`snapshot_dir` must already exist and be valid (see
`reporationale.adapters.snapshot_store`), which this module only reads.
Embeds every chunk exactly once, only when actually building a fresh index;
reusing a compatible completed index never calls the embedding provider's
document-embedding operation. A search still requires exactly one query
embedding.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reporationale.adapters.chroma_vector_store import (
    VectorIndexHandle,
    VectorIndexLookupResult,
    look_up_vector_index,
    open_vector_index,
    publish_vector_index,
)
from reporationale.adapters.snapshot_store import load_chunk_artifact, load_snapshot
from reporationale.adapters.voyage_embeddings import (
    VOYAGE_EMBEDDING_MODEL,
    EmbeddingProvider,
)
from reporationale.application.chunking import CHUNKER_ALGORITHM_VERSION
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION, SourceChunk
from reporationale.domain.retrieval import RankedEvidence
from reporationale.domain.snapshot import VectorIndexManifest

# Internally fixed; the public search service exposes no configurable
# result limit.
_SEARCH_HISTORY_RESULT_LIMIT = 5

_SCORE_KIND = "cosine_distance"


class SearchHistoryContractError(Exception):
    """A clear, typed failure when the embedding provider or the persisted
    vector index violates the narrow contract `search_history` depends on:
    embedding one query must return exactly one vector, and every chunk id
    a query result names must be a known, non-duplicate member of the
    canonical chunk corpus loaded from the chunk artifact."""


@dataclass(frozen=True)
class VectorSnapshotResult:
    """The typed outcome of one build/reuse/rebuild run. Fresh and reused
    results share this exact same shape."""

    manifest: VectorIndexManifest
    chunks: tuple[SourceChunk, ...]
    reused_existing_artifact: bool


def build_vector_snapshot(
    *,
    snapshot_dir: Path,
    max_chars: int,
    embedding_provider: EmbeddingProvider,
    force_rebuild: bool = False,
) -> VectorSnapshotResult:
    """Build, or reuse a compatible existing, persisted vector index for the
    derived chunk artifact at `snapshot_dir`.

    Steps: load and validate the existing normalized-source snapshot and
    chunk artifact at the requested `max_chars`; look up whether a
    compatible completed vector index already exists (skipped entirely when
    `force_rebuild=True`); if compatible, reuse it without calling
    `embedding_provider.embed_documents`; otherwise embed every chunk's
    text exactly once, atomically publish the vector index, and
    reload/validate the published result before returning it.

    `force_rebuild=True` rebuilds only the vector index from the
    already-persisted chunk artifact; it never calls GitHub, never rebuilds
    the normalized-source snapshot, and never re-chunks anything.
    """
    source_snapshot = load_snapshot(snapshot_dir)
    chunk_artifact = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=max_chars,
    )

    lookup: VectorIndexLookupResult = look_up_vector_index(
        snapshot_dir=snapshot_dir,
        expected_chunks=chunk_artifact.chunks,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
        chunks_digest=chunk_artifact.manifest.chunks_digest,
        embedding_model=VOYAGE_EMBEDDING_MODEL,
        force_rebuild=force_rebuild,
    )

    if lookup.kind == "compatible":
        assert lookup.manifest is not None  # guaranteed by "compatible"
        return VectorSnapshotResult(
            manifest=lookup.manifest,
            chunks=chunk_artifact.chunks,
            reused_existing_artifact=True,
        )

    embedding_batch = embedding_provider.embed_documents(
        [chunk.text for chunk in chunk_artifact.chunks]
    )
    manifest = publish_vector_index(
        snapshot_dir=snapshot_dir,
        chunks=chunk_artifact.chunks,
        vectors=embedding_batch.vectors,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
        chunks_digest=chunk_artifact.manifest.chunks_digest,
        embedding_model=VOYAGE_EMBEDDING_MODEL,
    )
    return VectorSnapshotResult(
        manifest=manifest,
        chunks=chunk_artifact.chunks,
        reused_existing_artifact=False,
    )


def _require_query(query: str) -> str:
    if not query.strip():
        raise ValueError("query must not be blank")
    return query


class SearchHistoryService:
    """The narrow `search_history` application service: retains one
    open vector-index handle and embedding provider, and answers any number
    of queries against the same persisted Chroma index without reopening it
    or re-embedding the repository per call.

    `search_history` accepts only the query text, exposes no source, date,
    folder, or backend filters, and returns at most
    `_SEARCH_HISTORY_RESULT_LIMIT` results — fewer only when the corpus
    itself contains fewer chunks.
    """

    def __init__(
        self,
        *,
        chunks: Sequence[SourceChunk],
        vector_index: VectorIndexHandle,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        self._vector_index = vector_index
        self._embedding_provider = embedding_provider

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]:
        _require_query(query)
        embedded_query = self._embedding_provider.embed_query(query)
        if len(embedded_query.vectors) != 1:
            raise SearchHistoryContractError(
                "embed_query must return exactly one query vector; got "
                f"{len(embedded_query.vectors)}."
            )
        query_vector = embedded_query.vectors[0]

        matches = self._vector_index.query(
            query_vector, limit=_SEARCH_HISTORY_RESULT_LIMIT
        )
        seen_chunk_ids: set[str] = set()
        for chunk_id, _distance in matches:
            if chunk_id in seen_chunk_ids:
                raise SearchHistoryContractError(
                    f"The vector index returned duplicate chunk id {chunk_id!r} "
                    "in one query result."
                )
            seen_chunk_ids.add(chunk_id)
            if chunk_id not in self._chunks_by_id:
                raise SearchHistoryContractError(
                    f"The vector index returned chunk id {chunk_id!r}, which is "
                    "not part of the canonical chunk corpus."
                )

        return tuple(
            RankedEvidence(
                evidence_id=chunk_id,
                rank=rank,
                score=distance,
                score_kind=_SCORE_KIND,
                chunk=self._chunks_by_id[chunk_id],
            )
            for rank, (chunk_id, distance) in enumerate(matches, start=1)
        )

    def close(self) -> None:
        self._vector_index.close()


def load_search_history_service(
    *,
    snapshot_dir: Path,
    max_chars: int,
    embedding_provider: EmbeddingProvider,
    force_rebuild: bool = False,
) -> SearchHistoryService:
    """Build or reuse the vector index at `snapshot_dir`, then open it for
    repeated querying and return a ready `SearchHistoryService`.

    Reopening a compatible completed index (`force_rebuild=False`, the
    default) never calls `embedding_provider.embed_documents` — this is
    what lets a fresh application instance answer questions from an
    already-built index across a restart without re-embedding the
    repository.
    """
    build_result = build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=max_chars,
        embedding_provider=embedding_provider,
        force_rebuild=force_rebuild,
    )
    manifest = build_result.manifest
    vector_index = open_vector_index(
        snapshot_dir=snapshot_dir,
        expected_chunks=build_result.chunks,
        expected_source_schema_version=manifest.source_schema_version,
        expected_sources_digest=manifest.sources_digest,
        expected_chunk_schema_version=manifest.chunk_schema_version,
        expected_chunker_algorithm_version=manifest.chunker_algorithm_version,
        expected_max_chars=manifest.max_chars,
        expected_chunks_digest=manifest.chunks_digest,
        expected_embedding_model=manifest.embedding_model,
        expected_embedding_dimension=manifest.embedding_dimension,
    )
    return SearchHistoryService(
        chunks=build_result.chunks,
        vector_index=vector_index,
        embedding_provider=embedding_provider,
    )
