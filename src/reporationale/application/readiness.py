"""Determine, without any paid or GitHub-collection work, whether a
complete queryable pipeline (normalized-source snapshot, derived chunk
artifact, and persisted vector index) already exists for a repository at
its currently resolved revision.

`reporationale.application.preflight.preflight_repository_reference`
deliberately never returns `ready` (see its module docstring): a
`sources_complete` normalized snapshot is real progress, but the chunk and
vector-index artifacts a truly queryable snapshot requires may not exist
yet. This module answers exactly that remaining question, so a caller (the
Streamlit composition root) can distinguish "already ready to query" from
"indexing must run" the same way `project.md`'s three-outcome preflight
promises, without duplicating the parsing, lookup, or admission logic
`preflight_repository_reference` already owns.

It composes the same read-only lookup/local-load functions
`application.chunk_snapshot`/`application.vector_retrieval` already use
internally to decide whether to reuse rather than rebuild; it just stops
at "would reuse" instead of ever building or embedding anything. The only
GitHub call involved (resolving the current commit SHA) is one the caller
has already made for its own preflight decision; this module makes none of
its own.
"""

from dataclasses import dataclass
from pathlib import Path

from reporationale.adapters.chroma_vector_store import look_up_vector_index
from reporationale.adapters.snapshot_store import (
    load_chunk_artifact,
    load_snapshot,
    look_up_chunk_artifact,
    look_up_normalized_source_snapshot,
    snapshot_directory,
)
from reporationale.adapters.voyage_embeddings import VOYAGE_EMBEDDING_MODEL
from reporationale.application.chunking import CHUNKER_ALGORITHM_VERSION
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.snapshot import SnapshotManifest, VectorIndexManifest


@dataclass(frozen=True)
class RepositoryReadiness:
    """Whether a complete, queryable pipeline already exists for one
    repository at one resolved revision, plus whichever manifests were
    already loaded while answering that question (so a caller does not
    need to reload them just to display repository/snapshot information).

    `source_manifest` is populated whenever a compatible normalized-source
    snapshot was found, even if `ready` is `False` because the chunk or
    vector-index layer built on top of it is still missing or incompatible.
    `vector_manifest` is populated only when `ready` is `True`.
    """

    ready: bool
    snapshot_dir: Path
    source_manifest: SnapshotManifest | None
    vector_manifest: VectorIndexManifest | None


def check_repository_readiness(
    *,
    identity: RepositoryIdentity,
    resolved_commit_sha: str,
    snapshot_root: Path,
    max_chars: int,
) -> RepositoryReadiness:
    """Check whether the repository at `identity`/`resolved_commit_sha`
    already has a compatible normalized-source snapshot, a compatible
    derived chunk artifact at `max_chars`, and a compatible persisted
    vector index built from it -- in that order, stopping at the first
    missing or incompatible layer.

    Never calls GitHub, an embedding provider, or Chroma's write path:
    every step here only loads or validates already-published local
    artifacts.
    """
    snapshot_dir = snapshot_directory(
        root=snapshot_root, identity=identity, resolved_commit_sha=resolved_commit_sha
    )

    source_lookup = look_up_normalized_source_snapshot(
        root=snapshot_root, identity=identity, resolved_commit_sha=resolved_commit_sha
    )
    if source_lookup.kind != "compatible":
        return RepositoryReadiness(
            ready=False,
            snapshot_dir=snapshot_dir,
            source_manifest=None,
            vector_manifest=None,
        )
    source_snapshot = load_snapshot(snapshot_dir)
    source_manifest = source_snapshot.manifest

    chunk_lookup = look_up_chunk_artifact(
        snapshot_dir=snapshot_dir,
        source_schema_version=source_manifest.source_schema_version,
        sources_digest=source_manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
    )
    if chunk_lookup.kind != "compatible":
        return RepositoryReadiness(
            ready=False,
            snapshot_dir=snapshot_dir,
            source_manifest=source_manifest,
            vector_manifest=None,
        )
    chunk_artifact = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_manifest.source_schema_version,
        expected_sources_digest=source_manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=max_chars,
    )

    vector_lookup = look_up_vector_index(
        snapshot_dir=snapshot_dir,
        expected_chunks=chunk_artifact.chunks,
        source_schema_version=source_manifest.source_schema_version,
        sources_digest=source_manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
        chunks_digest=chunk_artifact.manifest.chunks_digest,
        embedding_model=VOYAGE_EMBEDDING_MODEL,
    )
    if vector_lookup.kind != "compatible":
        return RepositoryReadiness(
            ready=False,
            snapshot_dir=snapshot_dir,
            source_manifest=source_manifest,
            vector_manifest=None,
        )
    return RepositoryReadiness(
        ready=True,
        snapshot_dir=snapshot_dir,
        source_manifest=source_manifest,
        vector_manifest=vector_lookup.manifest,
    )
