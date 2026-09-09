"""Tests for the full-pipeline readiness check used by the Streamlit
composition root to decide "ready to query" versus "indexing must run"
without ever calling GitHub, an embedding provider, or Chroma's write path.

Every test writes under `tmp_path`; no generated snapshot is ever written
into the repository, and no live network call is ever made.
"""

from collections.abc import Sequence
from pathlib import Path

from reporationale.adapters.snapshot_store import publish_snapshot, snapshot_directory
from reporationale.application.chunk_snapshot import build_chunk_snapshot
from reporationale.application.readiness import check_repository_readiness
from reporationale.application.vector_retrieval import build_vector_snapshot
from reporationale.domain.embedding import EmbeddingBatch
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "d" * 40
_MAX_CHARS = 1000

_POLLING_TEXT = (
    "Polling was chosen because webhook delivery was unreliable across "
    "customer network configurations."
)


class _FixedVectorEmbeddingProvider:
    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=tuple((1.0, 0.0) for _ in texts), total_tokens=len(texts)
        )

    def embed_query(self, text: str) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=((1.0, 0.0),), total_tokens=1)


def _publish_sources(tmp_path: Path) -> None:
    documents = (
        SourceDocument(
            source_id="github:octo-org/example-repo:markdown:a.md",
            platform="github",
            repository="octo-org/example-repo",
            source_type="markdown",
            text=_POLLING_TEXT,
            source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
        ),
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


def test_missing_snapshot_is_not_ready(tmp_path: Path) -> None:
    readiness = check_repository_readiness(
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        snapshot_root=tmp_path,
        max_chars=_MAX_CHARS,
    )

    assert readiness.ready is False
    assert readiness.source_manifest is None
    assert readiness.vector_manifest is None


def test_normalized_snapshot_without_chunks_is_not_ready_but_reports_source_manifest(
    tmp_path: Path,
) -> None:
    _publish_sources(tmp_path)

    readiness = check_repository_readiness(
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        snapshot_root=tmp_path,
        max_chars=_MAX_CHARS,
    )

    assert readiness.ready is False
    assert readiness.source_manifest is not None
    assert readiness.source_manifest.source_count == 1
    assert readiness.vector_manifest is None


def test_chunks_without_vector_index_is_not_ready(tmp_path: Path) -> None:
    _publish_sources(tmp_path)
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)

    readiness = check_repository_readiness(
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        snapshot_root=tmp_path,
        max_chars=_MAX_CHARS,
    )

    assert readiness.ready is False
    assert readiness.source_manifest is not None
    assert readiness.vector_manifest is None


def test_complete_pipeline_is_ready_and_reports_both_manifests(tmp_path: Path) -> None:
    _publish_sources(tmp_path)
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=_FixedVectorEmbeddingProvider(),
    )

    readiness = check_repository_readiness(
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        snapshot_root=tmp_path,
        max_chars=_MAX_CHARS,
    )

    assert readiness.ready is True
    assert readiness.snapshot_dir == snapshot_dir
    assert readiness.source_manifest is not None
    assert readiness.vector_manifest is not None
    assert readiness.vector_manifest.indexed_record_count == 1


def test_ready_pipeline_at_a_different_max_chars_is_not_ready(tmp_path: Path) -> None:
    """A vector index built for one chunk size must not be reported ready
    for a readiness check requesting a different one -- the chunk-artifact
    lookup itself is anchored to `max_chars` (see `look_up_chunk_artifact`)."""
    _publish_sources(tmp_path)
    snapshot_dir = snapshot_directory(
        root=tmp_path, identity=_IDENTITY, resolved_commit_sha=_COMMIT_SHA
    )
    build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=_MAX_CHARS)
    build_vector_snapshot(
        snapshot_dir=snapshot_dir,
        max_chars=_MAX_CHARS,
        embedding_provider=_FixedVectorEmbeddingProvider(),
    )

    readiness = check_repository_readiness(
        identity=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        snapshot_root=tmp_path,
        max_chars=_MAX_CHARS + 1,
    )

    assert readiness.ready is False
