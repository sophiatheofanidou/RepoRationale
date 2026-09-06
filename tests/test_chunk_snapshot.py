"""Tests for the derived chunk artifact: deterministic publish/load,
build/reuse/rebuild via the small application workflow, and anchoring to
the parent normalized-source snapshot.

Every test writes under `tmp_path`; no generated snapshot is ever written
into the repository. The normalized-source `manifest.json`/`sources.jsonl`
pair is created once per test through the existing snapshot-store helpers
and is never touched by anything under test here.
"""

import hashlib
from pathlib import Path

import pytest

from reporationale.adapters.snapshot_store import (
    SnapshotCorrupted,
    SnapshotNotFound,
    load_chunk_artifact,
    load_snapshot,
    publish_chunk_artifact,
    publish_snapshot,
    serialize_chunks_jsonl,
    snapshot_directory,
)
from reporationale.application.chunk_snapshot import build_chunk_snapshot
from reporationale.application.chunking import (
    CHUNKER_ALGORITHM_VERSION,
    chunk_source_documents,
)
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40


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


def test_publish_and_load_chunk_artifact_round_trip(tmp_path: Path) -> None:
    documents = (
        _document("github:octo-org/example-repo:markdown:a.md", "Alpha document body."),
        _document("github:octo-org/example-repo:markdown:b.md", "Beta document body."),
    )
    snapshot_dir = _publish_normalized_snapshot(tmp_path, documents)
    source_snapshot = load_snapshot(snapshot_dir)

    chunks = chunk_source_documents(source_snapshot.documents, max_chars=1000)

    manifest = publish_chunk_artifact(
        snapshot_dir=snapshot_dir,
        chunks=chunks,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=1000,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
    )

    assert manifest.status == "chunks_complete"
    assert manifest.chunk_count == len(chunks)
    assert manifest.sources_digest == source_snapshot.manifest.sources_digest

    loaded = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=1000,
    )
    assert loaded.manifest == manifest
    assert loaded.chunks == chunks
    assert [chunk.source_id for chunk in loaded.chunks] == [
        "github:octo-org/example-repo:markdown:a.md",
        "github:octo-org/example-repo:markdown:b.md",
    ]

    first_bytes = serialize_chunks_jsonl(chunks)
    second_bytes = serialize_chunks_jsonl(chunks)
    assert first_bytes == second_bytes
    assert first_bytes.endswith(b"\n")
    assert hashlib.sha256(first_bytes).hexdigest() == manifest.chunks_digest


def test_build_chunk_snapshot_reuses_and_rebuilds_without_touching_parent_snapshot(
    tmp_path: Path,
) -> None:
    documents = (
        _document("github:octo-org/example-repo:markdown:a.md", "Alpha document body."),
        _document("github:octo-org/example-repo:markdown:b.md", "Beta document body."),
    )
    snapshot_dir = _publish_normalized_snapshot(tmp_path, documents)
    manifest_path = snapshot_dir / "manifest.json"
    sources_path = snapshot_dir / "sources.jsonl"
    manifest_bytes_before = manifest_path.read_bytes()
    sources_bytes_before = sources_path.read_bytes()

    first = build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=1000)
    assert first.reused_existing_artifact is False
    assert len(first.chunks) > 0

    second = build_chunk_snapshot(snapshot_dir=snapshot_dir, max_chars=1000)
    assert second.reused_existing_artifact is True
    assert second.chunks == first.chunks
    assert second.manifest == first.manifest

    third = build_chunk_snapshot(
        snapshot_dir=snapshot_dir, max_chars=1000, force_rebuild=True
    )
    assert third.reused_existing_artifact is False
    assert third.chunks == first.chunks
    assert third.manifest == first.manifest

    assert manifest_path.read_bytes() == manifest_bytes_before
    assert sources_path.read_bytes() == sources_bytes_before


def test_load_chunk_artifact_rejects_artifact_anchored_to_wrong_sources_digest(
    tmp_path: Path,
) -> None:
    """A chunk artifact must never be treated as valid for a
    normalized-source snapshot it was not actually built from — the
    highest-value corruption check, since silently accepting it would let
    stale or foreign chunks answer questions about the wrong corpus."""
    documents = (
        _document("github:octo-org/example-repo:markdown:a.md", "Alpha document body."),
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

    with pytest.raises(SnapshotCorrupted):
        load_chunk_artifact(
            snapshot_dir,
            expected_source_schema_version=source_snapshot.manifest.source_schema_version,
            expected_sources_digest="0" * 64,
            expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
            expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
            expected_max_chars=1000,
        )


def test_publish_chunk_artifact_rejects_missing_or_mismatched_normalized_parent(
    tmp_path: Path,
) -> None:
    """Publication must validate the normalized-source parent before
    creating anything: a missing parent must not be implicitly created as
    a side effect, and a parent whose `sources_digest` disagrees with the
    value being published must not be silently accepted either."""
    missing_snapshot_dir = tmp_path / "does" / "not" / "exist"

    with pytest.raises(SnapshotNotFound):
        publish_chunk_artifact(
            snapshot_dir=missing_snapshot_dir,
            chunks=(),
            chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
            chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
            max_chars=1000,
            source_schema_version=1,
            sources_digest="0" * 64,
        )
    assert not missing_snapshot_dir.exists()

    documents = (
        _document("github:octo-org/example-repo:markdown:a.md", "Alpha document body."),
    )
    snapshot_dir = _publish_normalized_snapshot(tmp_path, documents)
    source_snapshot = load_snapshot(snapshot_dir)

    with pytest.raises(SnapshotCorrupted):
        publish_chunk_artifact(
            snapshot_dir=snapshot_dir,
            chunks=(),
            chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
            chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
            max_chars=1000,
            source_schema_version=source_snapshot.manifest.source_schema_version,
            sources_digest="0" * 64,
        )
    assert not (snapshot_dir / "chunks").exists()
