"""Build, reuse, or explicitly rebuild the derived chunk artifact for one
already-validated normalized-source snapshot.

Never calls GitHub and never rebuilds the normalized-source snapshot: the
normalized-source `manifest.json`/`sources.jsonl` pair at `snapshot_dir`
must already exist and be valid (see
`reporationale.adapters.snapshot_store.load_snapshot`), which this workflow
only reads. It is not wired to preflight or the normalized-source ingestion
workflow in this slice.
"""

from dataclasses import dataclass
from pathlib import Path

from reporationale.adapters.snapshot_store import (
    ChunkArtifactLookupResult,
    load_chunk_artifact,
    load_snapshot,
    look_up_chunk_artifact,
    publish_chunk_artifact,
)
from reporationale.application.chunking import (
    CHUNKER_ALGORITHM_VERSION,
    chunk_source_documents,
)
from reporationale.application.progress import ProgressEvent, ProgressObserver
from reporationale.domain.chunk import SOURCE_CHUNK_SCHEMA_VERSION, SourceChunk
from reporationale.domain.snapshot import ChunkArtifactManifest


@dataclass(frozen=True)
class ChunkSnapshotResult:
    """The typed outcome of one build/reuse/rebuild run. Fresh and reused
    results share this exact same shape."""

    manifest: ChunkArtifactManifest
    chunks: tuple[SourceChunk, ...]
    reused_existing_artifact: bool


def build_chunk_snapshot(
    *,
    snapshot_dir: Path,
    max_chars: int,
    force_rebuild: bool = False,
    on_progress: ProgressObserver | None = None,
) -> ChunkSnapshotResult:
    """Build, or reuse a compatible existing, derived chunk artifact for
    the normalized-source snapshot at `snapshot_dir`.

    Steps: load and validate the existing normalized-source snapshot; look
    up whether a compatible completed chunk artifact already exists at the
    requested `max_chars` (skipped entirely when `force_rebuild=True`); if
    compatible, reuse it; otherwise chunk the loaded `SourceDocument`
    sequence, atomically publish the derived chunk artifact, and
    reload/validate the published result before returning it.

    `force_rebuild=True` rebuilds only the derived `chunks/` artifact from
    the already-persisted canonical `sources.jsonl`; it never calls GitHub
    and never rebuilds the normalized-source snapshot itself.

    `on_progress`, when supplied, is called with a `creating_chunks`
    `ProgressEvent` (`"started"` then `"completed"` for a fresh build;
    `"completed"` alone, reporting the reused chunk count, when a
    compatible artifact is reused) -- see
    `reporationale.application.progress`. Omitted (the default), this
    function's behaviour is unchanged.

    Raises whatever `load_snapshot`, `chunk_source_documents`, or the
    chunk-artifact store raises for any validation or filesystem failure
    (propagated unchanged); never returns a partially published artifact.
    """
    source_snapshot = load_snapshot(snapshot_dir)

    lookup: ChunkArtifactLookupResult = look_up_chunk_artifact(
        snapshot_dir=snapshot_dir,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
        force_rebuild=force_rebuild,
    )

    if lookup.kind == "compatible":
        loaded = load_chunk_artifact(
            snapshot_dir,
            expected_source_schema_version=source_snapshot.manifest.source_schema_version,
            expected_sources_digest=source_snapshot.manifest.sources_digest,
            expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
            expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
            expected_max_chars=max_chars,
        )
        if on_progress is not None:
            on_progress(
                ProgressEvent(
                    phase="creating_chunks",
                    status="completed",
                    completed=len(loaded.chunks),
                    total=len(loaded.chunks),
                    detail=f"reused {len(loaded.chunks):,} chunks",
                )
            )
        return ChunkSnapshotResult(
            manifest=loaded.manifest,
            chunks=loaded.chunks,
            reused_existing_artifact=True,
        )

    if on_progress is not None:
        on_progress(ProgressEvent(phase="creating_chunks", status="started"))

    chunks = chunk_source_documents(source_snapshot.documents, max_chars=max_chars)

    publish_chunk_artifact(
        snapshot_dir=snapshot_dir,
        chunks=chunks,
        chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        max_chars=max_chars,
        source_schema_version=source_snapshot.manifest.source_schema_version,
        sources_digest=source_snapshot.manifest.sources_digest,
    )

    loaded = load_chunk_artifact(
        snapshot_dir,
        expected_source_schema_version=source_snapshot.manifest.source_schema_version,
        expected_sources_digest=source_snapshot.manifest.sources_digest,
        expected_chunk_schema_version=SOURCE_CHUNK_SCHEMA_VERSION,
        expected_chunker_algorithm_version=CHUNKER_ALGORITHM_VERSION,
        expected_max_chars=max_chars,
    )
    if on_progress is not None:
        on_progress(
            ProgressEvent(
                phase="creating_chunks",
                status="completed",
                completed=len(loaded.chunks),
                total=len(loaded.chunks),
                detail=f"{len(loaded.chunks):,} chunks created",
            )
        )
    return ChunkSnapshotResult(
        manifest=loaded.manifest,
        chunks=loaded.chunks,
        reused_existing_artifact=False,
    )
