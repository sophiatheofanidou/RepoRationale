"""Local persisted Chroma vector-index boundary.

Isolates all Chroma access behind this module, the same way `adapters.github`
isolates GitHub's REST API and `adapters.snapshot_store` isolates the
normalized-source and chunk-artifact filesystem layout: nothing outside this
module ever imports `chromadb` or sees a Chroma client, collection, or
query-result dictionary. Callers depend only on `VectorIndexManifest` (see
`reporationale.domain.snapshot`), `VectorIndexHandle`, and the typed
`SnapshotStoreError` family already used by `adapters.snapshot_store`.

A vector index lives at `<snapshot_dir>/vector_index/` — a `manifest.json`
next to a `chroma/` directory holding Chroma's own persisted data — always
beneath an already-validated normalized-source snapshot directory, anchored
to the exact derived chunk artifact it was built from. It is published and
recovered from a crash through the exact same generic staged-directory
protocol `adapters.snapshot_store` uses for the normalized-source and chunk
artifacts (`write_file_durably`, `publish_staged_directory`,
`recover_interrupted_replacement`, `remove_directory`), reused here rather
than duplicated.

The complete canonical `SourceChunk` corpus is never reconstructed from
Chroma; it is always loaded from the parent chunk artifact and mapped back
by `chunk_id`. Every function that opens a Chroma client for a build or
validation step closes it again before returning — no Chroma client or
collection object outlives the call that created it, except the one kept
open inside a returned `VectorIndexHandle` for repeated querying. On
Windows this matters concretely: `PersistentClient.close()` combined with
letting every local reference to that client and its collection go out of
scope is what actually releases the directory's file locks so a later
`os.rename` (used by `publish_staged_directory`) can succeed; a leaked
reference held anywhere else would keep the directory locked.
"""

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import chromadb
from chromadb.api.client import Client as ChromaClient
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from pydantic import ValidationError

from reporationale.adapters.snapshot_store import (
    SnapshotCorrupted,
    SnapshotIncompatible,
    SnapshotNotFound,
    publish_staged_directory,
    recover_interrupted_replacement,
    remove_directory,
    write_file_durably,
)
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.snapshot import (
    VECTOR_INDEX_MANIFEST_SCHEMA_VERSION,
    VectorIndexManifest,
)

_VECTOR_STORE_NAME: Literal["chroma"] = "chroma"
_DISTANCE_METRIC: Literal["cosine"] = "cosine"
_COLLECTION_NAME = "chunks"

# Bumped whenever how this module builds or queries the Chroma collection
# changes in a way that could alter previously published index contents or
# query results for the same input (for example, the metadata mapping or
# the distance-space configuration) — deliberately separate from
# `VECTOR_INDEX_MANIFEST_SCHEMA_VERSION`, which tracks the manifest's own
# shape.
VECTOR_INDEX_ALGORITHM_VERSION = 1

_VECTOR_INDEX_DIRNAME = "vector_index"
_VECTOR_INDEX_MANIFEST_FILENAME = "manifest.json"
_CHROMA_DIRNAME = "chroma"


def chroma_version() -> str:
    """The installed Chroma package version, recorded in every published
    `VectorIndexManifest.vector_store_version`."""
    return chromadb.__version__


def vector_index_directory(snapshot_dir: Path) -> Path:
    """The fixed `vector_index/` directory beneath one normalized-source
    snapshot directory. There is exactly one active vector index per
    snapshot; this is not a generalized artifact registry."""
    return snapshot_dir / _VECTOR_INDEX_DIRNAME


def _chroma_directory(vector_index_dir: Path) -> Path:
    return vector_index_dir / _CHROMA_DIRNAME


def _manifest_path(vector_index_dir: Path) -> Path:
    return vector_index_dir / _VECTOR_INDEX_MANIFEST_FILENAME


def _chunk_metadata(chunk: SourceChunk) -> dict[str, str | int]:
    """Only the scalar provenance/position fields useful as Chroma
    metadata. The canonical `SourceChunk` is never reconstructed from this;
    it always comes from the loaded chunk artifact."""
    metadata: dict[str, str | int] = {
        "source_id": chunk.source_id,
        "source_type": chunk.source_type,
        "platform": chunk.platform,
        "repository": chunk.repository,
        "chunk_index": chunk.chunk_index,
        "source_url": str(chunk.source_url),
    }
    if chunk.heading_path:
        metadata["heading_path"] = " > ".join(chunk.heading_path)
    return metadata


def _open_client(directory: Path) -> ChromaClient:
    # `PersistentClient` is declared to return the abstract `ClientAPI`, but
    # for a local (non-server) client it always constructs the concrete
    # `chromadb.api.client.Client`, which is what exposes `close()`.
    return cast(
        ChromaClient,
        chromadb.PersistentClient(
            path=str(directory),
            settings=ChromaSettings(anonymized_telemetry=False),
        ),
    )


def _add_records_in_batches(
    collection: Collection,
    *,
    ids: Sequence[str],
    embeddings: Sequence[Sequence[float] | Sequence[int]],
    documents: Sequence[str],
    metadatas: Sequence[dict[str, str | int]],
    batch_size: int,
) -> None:
    """Insert records into `collection` in batches of at most `batch_size`,
    preserving exact positional alignment between ids/embeddings/documents/
    metadatas both within and across batches.

    Required because the supported normalized-source limit does not bound
    the derived chunk count below Chroma's own local insertion limit — a
    large repository's chunk count can exceed one `collection.add` call's
    maximum batch size.
    """
    total = len(ids)
    for start in range(0, total, batch_size):
        end = start + batch_size
        collection.add(
            ids=list(ids[start:end]),
            embeddings=list(embeddings[start:end]),
            documents=list(documents[start:end]),
            metadatas=list(metadatas[start:end]),
        )


def _build_chroma_collection(
    *,
    directory: Path,
    chunks: Sequence[SourceChunk],
    vectors: Sequence[Sequence[float]],
) -> None:
    """Create the fixed `chunks` collection at `directory` from scratch,
    with cosine distance configured explicitly through the collection's
    public configuration (not the legacy `metadata` convention) and no
    Chroma-managed embedding function, and populate it with one record per
    chunk using the caller-supplied embeddings, split into batches of at
    most the client's own reported maximum batch size. Opens and closes its
    own client entirely within this call, so its directory's OS-level file
    handles are released by the time this returns."""
    directory.mkdir(parents=True, exist_ok=True)
    client = _open_client(directory)
    try:
        collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            configuration={"hnsw": {"space": _DISTANCE_METRIC}},
            embedding_function=None,
        )
        if chunks:
            embeddings: list[Sequence[float] | Sequence[int]] = [
                list(vector) for vector in vectors
            ]
            _add_records_in_batches(
                collection,
                ids=[chunk.chunk_id for chunk in chunks],
                embeddings=embeddings,
                documents=[chunk.text for chunk in chunks],
                metadatas=[_chunk_metadata(chunk) for chunk in chunks],
                batch_size=client.get_max_batch_size(),
            )
    finally:
        client.close()


def _safe_open_client(directory: Path) -> ChromaClient:
    try:
        return _open_client(directory)
    except Exception:
        raise SnapshotCorrupted(
            "The persisted Chroma vector index is missing or unreadable."
        ) from None


def _ensure_supported_collection_configuration(collection: Collection) -> None:
    """A compatible index requires the reopened collection to actually be
    configured for cosine distance with no Chroma-managed embedding
    function — not merely that the manifest claims so. A mismatch here
    means this installed Chroma build interprets or stores the collection
    differently than the one that created it, which is an installation
    incompatibility rather than damaged data."""
    configuration = collection.configuration
    hnsw = configuration.get("hnsw")
    if hnsw is None or hnsw.get("space") != _DISTANCE_METRIC:
        raise SnapshotIncompatible(
            "The persisted Chroma collection is not configured for cosine distance."
        )
    if configuration.get("embedding_function") is not None:
        raise SnapshotIncompatible(
            "The persisted Chroma collection unexpectedly has a "
            "Chroma-managed embedding function configured."
        )


def _validate_persisted_collection(
    *, directory: Path, expected_chunks: Sequence[SourceChunk]
) -> None:
    """Fully validate the persisted Chroma collection at `directory`
    against `expected_chunks`: a supported collection configuration,
    readable collection data, a record count matching the chunk artifact,
    no missing/duplicate/unknown chunk ids, and stored document text
    identical to each canonical chunk's own text. Opens and closes its own
    client, like `_build_chroma_collection`."""
    client = _safe_open_client(directory)
    try:
        try:
            collection = client.get_collection(
                name=_COLLECTION_NAME, embedding_function=None
            )
        except Exception:
            raise SnapshotCorrupted(
                "The persisted Chroma vector index collection is missing or corrupted."
            ) from None

        _ensure_supported_collection_configuration(collection)

        try:
            raw = collection.get(include=["documents"])
        except Exception:
            raise SnapshotCorrupted(
                "The persisted Chroma vector index collection is unreadable."
            ) from None

        ids = raw["ids"]
        documents = raw["documents"]
        if documents is None or len(ids) != len(documents):
            raise SnapshotCorrupted(
                "The persisted Chroma vector index collection has malformed contents."
            )
        if len(set(ids)) != len(ids):
            raise SnapshotCorrupted(
                "The persisted Chroma vector index collection contains a duplicate id."
            )
        if len(ids) != len(expected_chunks):
            raise SnapshotCorrupted(
                "The persisted Chroma vector index's record count does not "
                "match the chunk artifact."
            )

        expected_text_by_id = {chunk.chunk_id: chunk.text for chunk in expected_chunks}
        stored_by_id = dict(zip(ids, documents, strict=True))
        if set(stored_by_id) != set(expected_text_by_id):
            raise SnapshotCorrupted(
                "The persisted Chroma vector index contains an unknown or "
                "missing chunk id."
            )
        for chunk_id, text in expected_text_by_id.items():
            if stored_by_id[chunk_id] != text:
                raise SnapshotCorrupted(
                    "The persisted Chroma vector index's stored document text "
                    "does not match the canonical chunk text."
                )
    finally:
        client.close()


def _is_valid_vector_index_directory(path: Path) -> bool:
    """A shallow, self-contained structural check used only by crash
    recovery: the manifest parses at a supported schema version and the
    persisted collection's record count matches it. Deliberately lighter
    than `_validate_persisted_collection` (which needs the actual canonical
    chunks and is used for real compatibility decisions), the same way
    `_is_valid_chunk_artifact_directory` in `adapters.snapshot_store` is
    lighter than `load_chunk_artifact`."""
    manifest_file = _manifest_path(path)
    chroma_dir = _chroma_directory(path)
    if not manifest_file.is_file() or not chroma_dir.is_dir():
        return False
    try:
        manifest = VectorIndexManifest.model_validate_json(
            manifest_file.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        return False
    if manifest.manifest_schema_version != VECTOR_INDEX_MANIFEST_SCHEMA_VERSION:
        return False

    try:
        client = _open_client(chroma_dir)
    except Exception:
        return False
    try:
        try:
            collection = client.get_collection(
                name=_COLLECTION_NAME, embedding_function=None
            )
            count: int = collection.count()
        except Exception:
            return False
        return count == manifest.indexed_record_count
    finally:
        client.close()


def _load_and_validate_vector_index(
    directory: Path,
    *,
    expected_chunks: Sequence[SourceChunk],
    expected_source_schema_version: int,
    expected_sources_digest: str,
    expected_chunk_schema_version: int,
    expected_chunker_algorithm_version: int,
    expected_max_chars: int,
    expected_chunks_digest: str,
    expected_embedding_model: str,
    expected_embedding_dimension: int | None = None,
) -> VectorIndexManifest:
    """Load and completely validate one vector index against the caller's
    full compatibility context, including the persisted Chroma contents.

    Rejects, as `SnapshotIncompatible` (a rebuildable mismatch, never
    corrupted data): a missing manifest or Chroma directory
    (`SnapshotNotFound` — also rebuildable); an unsupported manifest/
    algorithm version; a chunk-configuration mismatch; an embedding-model
    mismatch; an embedding-dimension mismatch (only when
    `expected_embedding_dimension` is supplied — the dimension is not known
    before a fresh build actually embeds something); an unsupported
    vector-store/distance configuration; a stored Chroma version different
    from this build's installed Chroma version; or a reopened collection
    that is not actually configured for cosine distance or has a
    Chroma-managed embedding function configured.

    Rejects, as `SnapshotCorrupted` (a genuine data-integrity failure that
    must propagate rather than trigger a silent rebuild): a manifest that
    fails validation, an artifact anchored to a different normalized-source
    snapshot or chunk artifact, a `chunk_count` mismatch, or any other
    persisted Chroma content failure detected by
    `_validate_persisted_collection` (missing/duplicate/unknown ids, a
    record-count mismatch, or mismatched stored document text).
    """
    manifest_file = _manifest_path(directory)
    chroma_dir = _chroma_directory(directory)
    if not manifest_file.is_file() or not chroma_dir.is_dir():
        raise SnapshotNotFound(f"No complete vector index exists at {directory}.")

    try:
        manifest = VectorIndexManifest.model_validate_json(
            manifest_file.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise SnapshotCorrupted(
            "The vector index manifest is missing, unreadable, or invalid."
        ) from None

    if manifest.manifest_schema_version != VECTOR_INDEX_MANIFEST_SCHEMA_VERSION:
        raise SnapshotIncompatible(
            "The vector index manifest schema version is not supported by this build."
        )
    if manifest.vector_index_algorithm_version != VECTOR_INDEX_ALGORITHM_VERSION:
        raise SnapshotIncompatible(
            "The vector index algorithm version is not supported by this build."
        )
    if manifest.source_schema_version != expected_source_schema_version:
        raise SnapshotIncompatible(
            "The vector index's source schema version does not match the "
            "normalized-source snapshot it is anchored to."
        )
    if manifest.sources_digest != expected_sources_digest:
        raise SnapshotCorrupted(
            "The vector index is anchored to a different normalized-source snapshot."
        )
    if (
        manifest.chunk_schema_version != expected_chunk_schema_version
        or manifest.chunker_algorithm_version != expected_chunker_algorithm_version
        or manifest.max_chars != expected_max_chars
    ):
        raise SnapshotIncompatible(
            "The vector index's chunk configuration does not match the "
            "requested chunk artifact."
        )
    if manifest.chunks_digest != expected_chunks_digest:
        raise SnapshotCorrupted(
            "The vector index is anchored to a different chunk artifact."
        )
    if manifest.embedding_model != expected_embedding_model:
        raise SnapshotIncompatible(
            "The vector index's embedding model does not match the "
            "requested configuration."
        )
    if (
        expected_embedding_dimension is not None
        and manifest.embedding_dimension != expected_embedding_dimension
    ):
        raise SnapshotIncompatible(
            "The vector index's embedding dimension does not match the "
            "requested configuration."
        )
    if (
        manifest.vector_store != _VECTOR_STORE_NAME
        or manifest.distance_metric != _DISTANCE_METRIC
    ):
        raise SnapshotIncompatible(
            "The vector index's vector-store configuration is not supported "
            "by this build."
        )
    if manifest.vector_store_version != chroma_version():
        raise SnapshotIncompatible(
            "The vector index's installed Chroma version does not match "
            "this build's installed Chroma version."
        )
    if manifest.chunk_count != len(expected_chunks):
        raise SnapshotCorrupted(
            "The vector index's chunk_count does not match the chunk artifact."
        )

    _validate_persisted_collection(
        directory=chroma_dir, expected_chunks=expected_chunks
    )

    return manifest


VectorIndexLookupKind = Literal[
    "missing", "compatible", "incompatible", "rebuild_requested"
]


class VectorIndexLookupResult:
    """A deterministic classification of one snapshot's vector-index state,
    without scanning or modifying anything outside the one `vector_index/`
    directory beneath `snapshot_dir`."""

    __slots__ = ("kind", "directory", "manifest", "reason")

    def __init__(
        self,
        *,
        kind: VectorIndexLookupKind,
        directory: Path,
        manifest: VectorIndexManifest | None = None,
        reason: str | None = None,
    ) -> None:
        self.kind = kind
        self.directory = directory
        self.manifest = manifest
        self.reason = reason


def look_up_vector_index(
    *,
    snapshot_dir: Path,
    expected_chunks: Sequence[SourceChunk],
    source_schema_version: int,
    sources_digest: str,
    chunk_schema_version: int,
    chunker_algorithm_version: int,
    max_chars: int,
    chunks_digest: str,
    embedding_model: str,
    force_rebuild: bool = False,
) -> VectorIndexLookupResult:
    """Classify the vector-index state beneath `snapshot_dir` against the
    requested chunk/embedding configuration, fully validating the persisted
    Chroma contents against `expected_chunks` when a manifest exists.

    `force_rebuild=True` short-circuits to `"rebuild_requested"` without
    reading the filesystem at all. Otherwise: `"missing"` (nothing at
    `vector_index/`), `"compatible"` (a validated index anchored to the
    requested chunk artifact and embedding model), or `"incompatible"`
    (something exists but does not match the request, or uses an
    unsupported schema/algorithm/Chroma version or collection
    configuration — always safely rebuildable).

    A `SnapshotCorrupted` failure — a genuine data-integrity problem such as
    an artifact anchored to the wrong chunk artifact, a record-count
    mismatch, or mismatched stored document text — is deliberately **not**
    caught here: it propagates to the caller instead of being silently
    treated as "needs a rebuild," so a corrupted index can never trigger
    paid re-embedding on its own.
    """
    directory = vector_index_directory(snapshot_dir)
    recover_interrupted_replacement(
        directory, is_valid=_is_valid_vector_index_directory
    )
    if force_rebuild:
        return VectorIndexLookupResult(kind="rebuild_requested", directory=directory)

    if not directory.exists():
        return VectorIndexLookupResult(kind="missing", directory=directory)

    try:
        manifest = _load_and_validate_vector_index(
            directory,
            expected_chunks=expected_chunks,
            expected_source_schema_version=source_schema_version,
            expected_sources_digest=sources_digest,
            expected_chunk_schema_version=chunk_schema_version,
            expected_chunker_algorithm_version=chunker_algorithm_version,
            expected_max_chars=max_chars,
            expected_chunks_digest=chunks_digest,
            expected_embedding_model=embedding_model,
        )
    except SnapshotNotFound:
        return VectorIndexLookupResult(kind="missing", directory=directory)
    except SnapshotIncompatible as error:
        return VectorIndexLookupResult(
            kind="incompatible", directory=directory, reason=str(error)
        )

    return VectorIndexLookupResult(
        kind="compatible", directory=directory, manifest=manifest
    )


def publish_vector_index(
    *,
    snapshot_dir: Path,
    chunks: Sequence[SourceChunk],
    vectors: Sequence[Sequence[float]],
    source_schema_version: int,
    sources_digest: str,
    chunk_schema_version: int,
    chunker_algorithm_version: int,
    max_chars: int,
    chunks_digest: str,
    embedding_model: str,
    manifest_schema_version: int = VECTOR_INDEX_MANIFEST_SCHEMA_VERSION,
    vector_index_algorithm_version: int = VECTOR_INDEX_ALGORITHM_VERSION,
) -> VectorIndexManifest:
    """Atomically publish, or replace, the vector index beneath
    `snapshot_dir`: build a fresh Chroma collection in a staging directory
    from `chunks`/`vectors`, write its manifest, validate the staged pair
    exactly the way a later `open_vector_index`/`look_up_vector_index` would
    (including reopening the persisted Chroma contents), and only then
    publish it via the same staged-directory protocol
    `adapters.snapshot_store` uses for its own artifacts.

    On any failure (build, write, validation, or publication), the staging
    directory this call created is removed and nothing about the target
    changes: a failed or interrupted build never appears as a completed
    vector index, and a failed replacement leaves the previous completed
    index intact. `snapshot_dir` itself, its normalized-source pair, and its
    `chunks/` artifact are never modified by this call.
    """
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors must have the same length")
    if not chunks:
        raise ValueError("chunks must not be empty")

    embedding_dimension = len(vectors[0])
    if any(len(vector) != embedding_dimension for vector in vectors):
        raise ValueError("every vector must share the same embedding dimension")

    target = vector_index_directory(snapshot_dir)
    recover_interrupted_replacement(target, is_valid=_is_valid_vector_index_directory)

    staging = snapshot_dir / f".vector-index-staging-{uuid4().hex}"
    staging.mkdir(exist_ok=False)
    try:
        _build_chroma_collection(
            directory=_chroma_directory(staging), chunks=chunks, vectors=vectors
        )

        manifest = VectorIndexManifest(
            manifest_schema_version=manifest_schema_version,
            vector_index_algorithm_version=vector_index_algorithm_version,
            source_schema_version=source_schema_version,
            sources_digest=sources_digest,
            chunk_schema_version=chunk_schema_version,
            chunker_algorithm_version=chunker_algorithm_version,
            max_chars=max_chars,
            chunks_digest=chunks_digest,
            chunk_count=len(chunks),
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            vector_store=_VECTOR_STORE_NAME,
            vector_store_version=chroma_version(),
            distance_metric=_DISTANCE_METRIC,
            indexed_record_count=len(chunks),
            status="vector_index_complete",
        )
        write_file_durably(
            _manifest_path(staging), manifest.model_dump_json().encode("utf-8")
        )

        # Validate the staged index exactly the way a later open/lookup
        # would, before anything is published.
        _load_and_validate_vector_index(
            staging,
            expected_chunks=chunks,
            expected_source_schema_version=source_schema_version,
            expected_sources_digest=sources_digest,
            expected_chunk_schema_version=chunk_schema_version,
            expected_chunker_algorithm_version=chunker_algorithm_version,
            expected_max_chars=max_chars,
            expected_chunks_digest=chunks_digest,
            expected_embedding_model=embedding_model,
            expected_embedding_dimension=embedding_dimension,
        )

        publish_staged_directory(staging, target)
    except BaseException:
        remove_directory(staging)
        raise
    return manifest


def _query_collection(
    collection: Collection, *, vector: Sequence[float], limit: int
) -> tuple[tuple[str, float], ...]:
    query_embeddings: list[Sequence[float] | Sequence[int]] = [list(vector)]
    try:
        result = collection.query(
            query_embeddings=query_embeddings, n_results=limit, include=["distances"]
        )
    except Exception:
        raise SnapshotCorrupted("The Chroma vector index query failed.") from None

    ids_batches = result["ids"]
    distance_batches = result["distances"]
    if distance_batches is None or len(ids_batches) != 1 or len(distance_batches) != 1:
        raise SnapshotCorrupted(
            "The Chroma vector index query result has an unexpected shape."
        )

    ids = ids_batches[0]
    distances = distance_batches[0]
    if len(ids) != len(distances):
        raise SnapshotCorrupted(
            "The Chroma vector index query result has an unexpected shape."
        )

    matches: list[tuple[str, float]] = []
    for chunk_id, distance in zip(ids, distances, strict=True):
        distance_value = float(distance)
        if not math.isfinite(distance_value):
            raise SnapshotCorrupted(
                "The Chroma vector index returned a non-finite distance."
            )
        matches.append((chunk_id, distance_value))
    return tuple(matches)


class VectorIndexHandle:
    """One open, query-capable handle onto a validated, persisted Chroma
    vector index. Holds a live Chroma client for its entire lifetime so
    repeated queries never reopen the index or re-embed the repository;
    `close()` releases it. Never exposes the underlying Chroma client,
    collection, or raw query-result shape to its caller."""

    def __init__(
        self,
        *,
        client: ChromaClient,
        collection: Collection,
        manifest: VectorIndexManifest,
    ) -> None:
        self._client = client
        self._collection = collection
        self.manifest = manifest

    def query(
        self, vector: Sequence[float], *, limit: int
    ) -> tuple[tuple[str, float], ...]:
        """Return up to `limit` `(chunk_id, cosine_distance)` matches for
        `vector`, ordered by ascending distance with ties broken
        deterministically by `chunk_id`, and consecutive one-based ranking
        left to the caller (this returns the ordered matches, not ranks)."""
        matches = list(_query_collection(self._collection, vector=vector, limit=limit))
        matches.sort(key=lambda item: (item[1], item[0]))
        return tuple(matches[:limit])

    def close(self) -> None:
        self._client.close()


def open_vector_index(
    *,
    snapshot_dir: Path,
    expected_chunks: Sequence[SourceChunk],
    expected_source_schema_version: int,
    expected_sources_digest: str,
    expected_chunk_schema_version: int,
    expected_chunker_algorithm_version: int,
    expected_max_chars: int,
    expected_chunks_digest: str,
    expected_embedding_model: str,
    expected_embedding_dimension: int,
) -> VectorIndexHandle:
    """Validate the compatible, completed vector index beneath
    `snapshot_dir` against the caller's full compatibility context (the same
    checks `look_up_vector_index` applies), then reopen a fresh Chroma
    client for repeated querying. Raises whatever
    `_load_and_validate_vector_index` raises for a missing, corrupted, or
    incompatible index. Never calls an embedding provider."""
    directory = vector_index_directory(snapshot_dir)
    manifest = _load_and_validate_vector_index(
        directory,
        expected_chunks=expected_chunks,
        expected_source_schema_version=expected_source_schema_version,
        expected_sources_digest=expected_sources_digest,
        expected_chunk_schema_version=expected_chunk_schema_version,
        expected_chunker_algorithm_version=expected_chunker_algorithm_version,
        expected_max_chars=expected_max_chars,
        expected_chunks_digest=expected_chunks_digest,
        expected_embedding_model=expected_embedding_model,
        expected_embedding_dimension=expected_embedding_dimension,
    )

    client = _open_client(_chroma_directory(directory))
    try:
        collection = client.get_collection(
            name=_COLLECTION_NAME, embedding_function=None
        )
    except Exception:
        client.close()
        raise SnapshotCorrupted(
            "The vector index collection could not be reopened after validation."
        ) from None
    return VectorIndexHandle(client=client, collection=collection, manifest=manifest)
