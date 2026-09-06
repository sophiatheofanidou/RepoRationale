"""Local filesystem persistence for normalized sources and derived chunks.

Isolates all filesystem access for the `manifest.json` + `sources.jsonl`
snapshot pair behind this boundary, the same way `adapters.github` isolates
GitHub's REST API. Depends only on domain contracts
(`SourceDocument`, `SourceChunk`, repository identity, and snapshot manifest
models); knows nothing about application-level corpus or workflow types.
The normalized corpus remains canonical, while the chunk corpus is a separate
derived artifact beneath it. Embeddings and a searchable vector index remain
outside this boundary, but `reporationale.adapters.chroma_vector_store`
reuses this module's generic staged-directory publication helpers
(`write_file_durably`, `backup_path`, `publish_staged_directory`,
`remove_directory`, `recover_interrupted_replacement`) so every derived
artifact under a snapshot directory is published and recovered through the
exact same safe protocol.
"""

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple
from uuid import uuid4

from pydantic import ValidationError

from reporationale.domain.chunk import SourceChunk
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.snapshot import (
    CHUNK_ARTIFACT_MANIFEST_SCHEMA_VERSION,
    NORMALIZED_SOURCE_SCHEMA_VERSION,
    SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    ChunkArtifactManifest,
    SnapshotManifest,
)
from reporationale.domain.source_document import SourceDocument

_MANIFEST_FILENAME = "manifest.json"
_SOURCES_FILENAME = "sources.jsonl"
_CHUNKS_DIRNAME = "chunks"
_CHUNK_MANIFEST_FILENAME = "manifest.json"
_CHUNKS_FILENAME = "chunks.jsonl"
_SAFE_PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class SnapshotStoreError(Exception):
    """Base class for every typed local snapshot-store failure."""


class SnapshotNotFound(SnapshotStoreError):
    """No complete snapshot exists at the derived directory."""


class SnapshotCorrupted(SnapshotStoreError):
    """A snapshot directory exists but its content is malformed, internally
    inconsistent, or does not match the identity/revision it was requested
    for."""


class SnapshotIncompatible(SnapshotStoreError):
    """A snapshot exists and is internally well-formed, but its manifest
    or source schema version is not supported by this build."""


class SnapshotAlreadyExists(SnapshotStoreError):
    """A completed snapshot already exists at the target directory and
    `force=True` was not passed to explicitly replace it."""


def _validate_safe_path_segment(value: str, *, name: str) -> str:
    """Reject anything that could make a path segment unsafe: blank,
    `.`/`..`, or a character outside a strict allowlist. Applied to every
    identity/revision component before it becomes part of a filesystem
    path, independent of whatever validation the caller's own
    `RepositoryIdentity` or commit-sha value already passed — this module
    does not trust an upstream caller to have validated path-safety."""
    if value in ("", ".", ".."):
        raise ValueError(f"{name} must not be empty, '.', or '..'")
    if _SAFE_PATH_SEGMENT_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{name} contains characters unsafe for a filesystem path segment"
        )
    return value


def snapshot_directory(
    *,
    root: Path,
    identity: RepositoryIdentity,
    resolved_commit_sha: str,
    source_schema_version: int = NORMALIZED_SOURCE_SCHEMA_VERSION,
) -> Path:
    """Deterministically derive the exact directory for one normalized
    snapshot: `<root>/<platform>/<owner>/<name>/<resolved_commit_sha>/
    schema-v<source_schema_version>/`.

    Every segment is validated against a strict allowlist and the final
    path is confirmed to still resolve under `root`, so a repository
    owner/name or a commit sha can never make the derived path escape the
    configured snapshot root via `..` or another unsafe character.
    """
    if source_schema_version <= 0:
        raise ValueError("source_schema_version must be a positive integer")

    platform = _validate_safe_path_segment(identity.platform, name="platform")
    owner = _validate_safe_path_segment(identity.owner, name="owner")
    name = _validate_safe_path_segment(identity.name, name="repository name")
    revision = _validate_safe_path_segment(
        resolved_commit_sha, name="resolved_commit_sha"
    )

    candidate = (
        root / platform / owner / name / revision / f"schema-v{source_schema_version}"
    )

    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if (
        resolved_candidate != resolved_root
        and resolved_root not in resolved_candidate.parents
    ):
        raise ValueError("derived snapshot path escapes the configured snapshot root")
    return candidate


def serialize_sources_jsonl(documents: Sequence[SourceDocument]) -> bytes:
    """Serialize `documents` deterministically: one compact JSON object per
    line, UTF-8, with exactly one trailing newline (none for an empty
    sequence). A pure function of the documents' own field values —
    `SourceDocument.model_dump_json()` serializes fields in their fixed
    declaration order — so repeated calls with the same documents always
    produce byte-identical output. Callers are expected to have already
    sorted `documents` deterministically (`RepositoryCorpus` guarantees
    this); this function does not re-sort them.
    """
    if not documents:
        return b""
    lines = [document.model_dump_json() for document in documents]
    return ("\n".join(lines) + "\n").encode("utf-8")


class LoadedSnapshot(NamedTuple):
    """A fully validated normalized-source snapshot: typed models, never a
    raw or partially-checked dictionary."""

    manifest: SnapshotManifest
    documents: tuple[SourceDocument, ...]


def load_snapshot(
    directory: Path,
    *,
    expected_identity: RepositoryIdentity | None = None,
    expected_commit_sha: str | None = None,
) -> LoadedSnapshot:
    """Load and completely validate one normalized-source snapshot.

    Rejects: a missing manifest or sources file (`SnapshotNotFound`); an
    unsupported manifest/source schema version (`SnapshotIncompatible`);
    and, as `SnapshotCorrupted`, everything else — a manifest that fails
    validation (including a non-`sources_complete` status, which the
    manifest's own `Literal` type already forces to fail validation);
    malformed JSON, a blank line, or an invalid `SourceDocument` in
    `sources.jsonl`; a duplicate or unsorted source id; a mismatch against
    `expected_identity`/`expected_commit_sha` when supplied; a
    `source_count`/`counts_by_source_type` mismatch; and a `sources_digest`
    mismatch. Nothing is returned unless every check passed.
    """
    manifest_path = directory / _MANIFEST_FILENAME
    sources_path = directory / _SOURCES_FILENAME
    if not manifest_path.is_file() or not sources_path.is_file():
        raise SnapshotNotFound(
            f"No complete normalized-source snapshot exists at {directory}."
        )

    try:
        manifest = SnapshotManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise SnapshotCorrupted(
            "The snapshot manifest is missing, unreadable, or invalid."
        ) from None

    if (
        manifest.manifest_schema_version != SNAPSHOT_MANIFEST_SCHEMA_VERSION
        or manifest.source_schema_version != NORMALIZED_SOURCE_SCHEMA_VERSION
    ):
        raise SnapshotIncompatible(
            "The snapshot's manifest or source schema version is not "
            "supported by this build."
        )
    if expected_identity is not None and manifest.repository != expected_identity:
        raise SnapshotCorrupted(
            "The snapshot manifest's repository does not match the one requested."
        )
    if (
        expected_commit_sha is not None
        and manifest.resolved_commit_sha != expected_commit_sha
    ):
        raise SnapshotCorrupted(
            "The snapshot manifest's resolved commit does not match the one requested."
        )

    try:
        raw_bytes = sources_path.read_bytes()
    except OSError:
        raise SnapshotCorrupted("The snapshot sources file is unreadable.") from None

    if hashlib.sha256(raw_bytes).hexdigest() != manifest.sources_digest:
        raise SnapshotCorrupted(
            "The snapshot sources file digest does not match the manifest."
        )

    documents = _parse_sources_jsonl(raw_bytes)

    if len(documents) != manifest.source_count:
        raise SnapshotCorrupted(
            "The snapshot's source_count does not match its sources file."
        )
    recomputed_counts: dict[str, int] = {}
    for document in documents:
        recomputed_counts[document.source_type] = (
            recomputed_counts.get(document.source_type, 0) + 1
        )
    if recomputed_counts != manifest.counts_by_source_type:
        raise SnapshotCorrupted(
            "The snapshot's counts_by_source_type does not match its sources file."
        )

    return LoadedSnapshot(manifest=manifest, documents=documents)


def _parse_sources_jsonl(raw_bytes: bytes) -> tuple[SourceDocument, ...]:
    if not raw_bytes:
        return ()

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise SnapshotCorrupted(
            "The snapshot sources file is not valid UTF-8."
        ) from None

    if not text.endswith("\n"):
        raise SnapshotCorrupted(
            "The snapshot sources file must end with exactly one newline."
        )

    documents: list[SourceDocument] = []
    previous_source_id: str | None = None
    for line in text[:-1].split("\n"):
        if not line.strip():
            raise SnapshotCorrupted("The snapshot sources file contains a blank line.")
        try:
            document = SourceDocument.model_validate_json(line)
        except ValidationError:
            raise SnapshotCorrupted(
                "The snapshot sources file contains an invalid source document."
            ) from None
        if previous_source_id is not None:
            if document.source_id == previous_source_id:
                raise SnapshotCorrupted(
                    "The snapshot sources file contains a duplicate source_id."
                )
            if document.source_id < previous_source_id:
                raise SnapshotCorrupted(
                    "The snapshot sources file is not ordered by source_id."
                )
        previous_source_id = document.source_id
        documents.append(document)
    return tuple(documents)


def _default_clock() -> datetime:
    return datetime.now(UTC)


def publish_snapshot(
    *,
    root: Path,
    repository: RepositoryIdentity,
    default_branch: str,
    resolved_commit_sha: str,
    documents: Sequence[SourceDocument],
    counts_by_source_type: dict[str, int],
    producer_version: str,
    manifest_schema_version: int = SNAPSHOT_MANIFEST_SCHEMA_VERSION,
    source_schema_version: int = NORMALIZED_SOURCE_SCHEMA_VERSION,
    clock: Callable[[], datetime] = _default_clock,
    force: bool = False,
) -> SnapshotManifest:
    """Atomically publish one complete normalized-source snapshot.

    Writes `sources.jsonl` and `manifest.json` to a staging directory
    created under the exact same `root` as the target, flushes and fsyncs
    both files, then validates the staged pair by loading it back through
    `load_snapshot` before publishing anything. Raises `SnapshotAlreadyExists`
    if a completed snapshot already exists at the target and `force` is not
    `True` — an existing completed snapshot is never overwritten implicitly.

    On any failure (write, validation, or publication), the staging
    directory this call created is removed and nothing about the target
    changes: a failed or interrupted write never appears as a completed
    snapshot, and a failed forced replacement leaves the previous completed
    snapshot intact. Only the exact staging (and, for a forced replacement,
    exact prior-target) directories this call itself created are ever
    removed; no broad or recursive deletion is performed against any other
    path.
    """
    target = snapshot_directory(
        root=root,
        identity=repository,
        resolved_commit_sha=resolved_commit_sha,
        source_schema_version=source_schema_version,
    )
    recover_interrupted_replacement(target, is_valid=_is_valid_snapshot_directory)
    if target.exists() and not force:
        raise SnapshotAlreadyExists(
            f"A completed snapshot already exists at {target}; pass force=True "
            "to replace it explicitly."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".staging-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        created_at = clock()
        sources_bytes = serialize_sources_jsonl(documents)
        digest = hashlib.sha256(sources_bytes).hexdigest()
        completed_at = clock()

        manifest = SnapshotManifest(
            manifest_schema_version=manifest_schema_version,
            source_schema_version=source_schema_version,
            repository=repository,
            resolved_commit_sha=resolved_commit_sha,
            default_branch=default_branch,
            status="sources_complete",
            created_at=created_at,
            completed_at=completed_at,
            source_count=len(documents),
            counts_by_source_type=counts_by_source_type,
            sources_digest=digest,
            producer_version=producer_version,
        )

        write_file_durably(staging / _SOURCES_FILENAME, sources_bytes)
        write_file_durably(
            staging / _MANIFEST_FILENAME, manifest.model_dump_json().encode("utf-8")
        )

        # Validate the staged pair exactly the way a later load would,
        # before anything is published.
        load_snapshot(
            staging,
            expected_identity=repository,
            expected_commit_sha=resolved_commit_sha,
        )

        publish_staged_directory(staging, target)
    except BaseException:
        remove_directory(staging)
        raise
    return manifest


def write_file_durably(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def backup_path(target: Path) -> Path:
    """The one deterministic, exact backup location for `target`'s forced
    replacement — a fixed name adjacent to `target`, not an unbounded,
    UUID-named directory nothing could ever find again after a crash."""
    return target.parent / f"{target.name}.backup"


def _is_valid_snapshot_directory(path: Path) -> bool:
    """Whether `path` is a completely valid, self-consistent
    `sources_complete` snapshot, using the exact same checks `load_snapshot`
    applies to a normal read (schema versions, digest, counts) — an
    invalid or partial directory is never treated as completed."""
    try:
        load_snapshot(path)
    except SnapshotStoreError:
        return False
    return True


def recover_interrupted_replacement(
    target: Path, *, is_valid: Callable[[Path], bool]
) -> None:
    """Idempotently recover from a crash during a forced replacement of
    `target` with a staged directory, for any artifact whose publication
    goes through `publish_staged_directory`.

    A forced replacement moves the previous completed artifact to
    `backup_path(target)`, then moves the newly staged and already-
    validated replacement into `target`, then removes the backup. If the
    process is interrupted between the first and second move, `target` is
    absent and the backup is still the valid previous artifact: restore
    it (validated with the caller's own `is_valid`). If it is interrupted
    between the second move and the final backup removal, both exist and
    `target` is already the valid new artifact: discard the stale backup.
    Called before every lookup and publication, so neither can observe a
    directory left mid-replacement by an earlier crash. Touches only
    `target` and `backup_path(target)` — never scans or modifies any other
    path.
    """
    backup = backup_path(target)
    if not backup.exists():
        return
    if not target.exists():
        if is_valid(backup):
            os.rename(backup, target)
        return
    if is_valid(target):
        remove_directory(backup)
    # else: both `target` and a backup exist, but `target` fails
    # validation. This cannot arise from this module's own replacement
    # protocol (a completed rename always leaves a valid `target`); leave
    # both alone rather than guess, so the caller's own load reports
    # `target` as corrupted/incompatible instead of this function silently
    # repairing or discarding either directory.


def publish_staged_directory(staging: Path, target: Path) -> None:
    """Move `staging` to `target`, replacing an existing completed
    artifact at `target` (the caller has already confirmed a replacement
    is authorized) using the fixed backup path a crash can always recover
    from: move the old directory to `backup_path(target)` first, then
    move the new one into place, restoring the backup if that second move
    fails for any reason, and finally removing the (now stale) backup.
    """
    if not target.exists():
        os.rename(staging, target)
        return

    backup = backup_path(target)
    os.rename(target, backup)
    try:
        os.rename(staging, target)
    except OSError:
        os.rename(backup, target)
        raise
    remove_directory(backup)


def remove_directory(path: Path) -> None:
    """Remove exactly `path` — an exact, known directory this module
    itself created moments earlier — and nothing else. Never called with a
    glob, a caller-supplied arbitrary path, or an unresolved path."""
    shutil.rmtree(path, ignore_errors=True)


SnapshotLookupKind = Literal[
    "missing", "compatible", "incompatible", "rebuild_requested"
]


@dataclass(frozen=True)
class SnapshotLookupResult:
    """A deterministic classification of one repository/revision's
    normalized-source snapshot state, without scanning or modifying
    anything outside the one directory `snapshot_directory` derives."""

    kind: SnapshotLookupKind
    directory: Path
    manifest: SnapshotManifest | None = None
    reason: str | None = None


def look_up_normalized_source_snapshot(
    *,
    root: Path,
    identity: RepositoryIdentity,
    resolved_commit_sha: str,
    source_schema_version: int = NORMALIZED_SOURCE_SCHEMA_VERSION,
    force_rebuild: bool = False,
) -> SnapshotLookupResult:
    """Classify the normalized-source snapshot state for `identity` at
    `resolved_commit_sha`.

    `force_rebuild=True` short-circuits to `"rebuild_requested"` without
    reading the filesystem at all, so an explicit rebuild request always
    bypasses reuse regardless of what happens to exist on disk. Otherwise:
    `"missing"` (nothing at the derived path), `"compatible"` (a validated,
    matching `sources_complete` snapshot), or `"incompatible"` (something
    exists at the derived path but failed validation or uses an
    unsupported schema version).
    """
    directory = snapshot_directory(
        root=root,
        identity=identity,
        resolved_commit_sha=resolved_commit_sha,
        source_schema_version=source_schema_version,
    )
    recover_interrupted_replacement(directory, is_valid=_is_valid_snapshot_directory)
    if force_rebuild:
        return SnapshotLookupResult(kind="rebuild_requested", directory=directory)

    if not directory.exists():
        return SnapshotLookupResult(kind="missing", directory=directory)

    try:
        manifest, _documents = load_snapshot(
            directory,
            expected_identity=identity,
            expected_commit_sha=resolved_commit_sha,
        )
    except SnapshotNotFound:
        return SnapshotLookupResult(kind="missing", directory=directory)
    except (SnapshotCorrupted, SnapshotIncompatible) as error:
        return SnapshotLookupResult(
            kind="incompatible", directory=directory, reason=str(error)
        )

    return SnapshotLookupResult(
        kind="compatible", directory=directory, manifest=manifest
    )


# --- Derived chunk artifact: `<snapshot directory>/chunks/{manifest.json, --
# --- chunks.jsonl}`. Always beneath an already-validated normalized-      -
# --- source snapshot directory; never touches that snapshot's own        -
# --- `manifest.json`/`sources.jsonl`.                                    -


def chunk_artifact_directory(snapshot_dir: Path) -> Path:
    """The fixed `chunks/` directory beneath one normalized-source snapshot
    directory. There is exactly one active chunk configuration per
    snapshot; this is not a generalized artifact registry."""
    return snapshot_dir / _CHUNKS_DIRNAME


def serialize_chunks_jsonl(chunks: Sequence[SourceChunk]) -> bytes:
    """Serialize `chunks` deterministically, following the same conventions
    as `serialize_sources_jsonl`: one compact JSON object per line, UTF-8,
    with exactly one trailing newline (none for an empty sequence). Callers
    are expected to have already ordered `chunks` by source-document order
    and then `chunk_index`; this function does not re-sort them."""
    if not chunks:
        return b""
    lines = [chunk.model_dump_json() for chunk in chunks]
    return ("\n".join(lines) + "\n").encode("utf-8")


class LoadedChunkArtifact(NamedTuple):
    """A fully validated derived chunk artifact: typed models, never a raw
    or partially-checked dictionary."""

    manifest: ChunkArtifactManifest
    chunks: tuple[SourceChunk, ...]


def _parse_chunks_jsonl(raw_bytes: bytes, *, max_chars: int) -> tuple[SourceChunk, ...]:
    if not raw_bytes:
        return ()

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise SnapshotCorrupted(
            "The chunk artifact chunks file is not valid UTF-8."
        ) from None

    if not text.endswith("\n"):
        raise SnapshotCorrupted(
            "The chunk artifact chunks file must end with exactly one newline."
        )

    chunks: list[SourceChunk] = []
    seen_chunk_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    previous_source_id: str | None = None
    previous_chunk_index: int | None = None

    for line in text[:-1].split("\n"):
        if not line.strip():
            raise SnapshotCorrupted(
                "The chunk artifact chunks file contains a blank line."
            )
        try:
            chunk = SourceChunk.model_validate_json(line)
        except ValidationError:
            raise SnapshotCorrupted(
                "The chunk artifact chunks file contains an invalid chunk."
            ) from None

        if len(chunk.text) > max_chars:
            raise SnapshotCorrupted(
                "The chunk artifact contains chunk text exceeding the "
                "manifest's max_chars."
            )
        if chunk.chunk_id in seen_chunk_ids:
            raise SnapshotCorrupted(
                "The chunk artifact chunks file contains a duplicate chunk_id."
            )
        seen_chunk_ids.add(chunk.chunk_id)

        if chunk.source_id != previous_source_id:
            if chunk.source_id in seen_source_ids:
                raise SnapshotCorrupted(
                    "The chunk artifact chunks file is not grouped by source."
                )
            if previous_source_id is not None and chunk.source_id < previous_source_id:
                raise SnapshotCorrupted(
                    "The chunk artifact chunks file is not ordered by "
                    "source-document order."
                )
            if chunk.chunk_index != 0:
                raise SnapshotCorrupted(
                    "The chunk artifact chunks file does not start a new "
                    "source at chunk_index 0."
                )
            seen_source_ids.add(chunk.source_id)
        else:
            if (
                previous_chunk_index is None
                or chunk.chunk_index != previous_chunk_index + 1
            ):
                raise SnapshotCorrupted(
                    "The chunk artifact chunks file has a missing or "
                    "repeated chunk position."
                )

        previous_source_id = chunk.source_id
        previous_chunk_index = chunk.chunk_index
        chunks.append(chunk)

    return tuple(chunks)


def _load_chunk_artifact_from_directory(
    directory: Path,
    *,
    expected_source_schema_version: int | None,
    expected_sources_digest: str | None,
    expected_chunk_schema_version: int | None,
    expected_chunker_algorithm_version: int | None,
    expected_max_chars: int | None,
) -> LoadedChunkArtifact:
    manifest_path = directory / _CHUNK_MANIFEST_FILENAME
    chunks_path = directory / _CHUNKS_FILENAME
    if not manifest_path.is_file() or not chunks_path.is_file():
        raise SnapshotNotFound(
            f"No complete derived chunk artifact exists at {directory}."
        )

    try:
        manifest = ChunkArtifactManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise SnapshotCorrupted(
            "The chunk artifact manifest is missing, unreadable, or invalid."
        ) from None

    if manifest.manifest_schema_version != CHUNK_ARTIFACT_MANIFEST_SCHEMA_VERSION:
        raise SnapshotIncompatible(
            "The chunk artifact manifest schema version is not supported by this build."
        )
    if (
        expected_chunk_schema_version is not None
        and manifest.chunk_schema_version != expected_chunk_schema_version
    ):
        raise SnapshotIncompatible(
            "The chunk artifact's chunk schema version is not supported by this build."
        )
    if (
        expected_chunker_algorithm_version is not None
        and manifest.chunker_algorithm_version != expected_chunker_algorithm_version
    ):
        raise SnapshotIncompatible(
            "The chunk artifact's chunker algorithm version is not supported "
            "by this build."
        )
    if expected_max_chars is not None and manifest.max_chars != expected_max_chars:
        raise SnapshotIncompatible(
            "The chunk artifact's max_chars does not match the requested configuration."
        )
    if (
        expected_source_schema_version is not None
        and manifest.source_schema_version != expected_source_schema_version
    ):
        raise SnapshotIncompatible(
            "The chunk artifact's source schema version does not match the "
            "normalized-source snapshot it is anchored to."
        )
    if (
        expected_sources_digest is not None
        and manifest.sources_digest != expected_sources_digest
    ):
        raise SnapshotCorrupted(
            "The chunk artifact is anchored to a different normalized-source snapshot."
        )

    try:
        raw_bytes = chunks_path.read_bytes()
    except OSError:
        raise SnapshotCorrupted(
            "The chunk artifact chunks file is unreadable."
        ) from None

    if hashlib.sha256(raw_bytes).hexdigest() != manifest.chunks_digest:
        raise SnapshotCorrupted(
            "The chunk artifact chunks file digest does not match the manifest."
        )

    chunks = _parse_chunks_jsonl(raw_bytes, max_chars=manifest.max_chars)

    if len(chunks) != manifest.chunk_count:
        raise SnapshotCorrupted(
            "The chunk artifact's chunk_count does not match its chunks file."
        )

    return LoadedChunkArtifact(manifest=manifest, chunks=chunks)


def load_chunk_artifact(
    snapshot_dir: Path,
    *,
    expected_source_schema_version: int,
    expected_sources_digest: str,
    expected_chunk_schema_version: int,
    expected_chunker_algorithm_version: int,
    expected_max_chars: int,
) -> LoadedChunkArtifact:
    """Load and completely validate the derived chunk artifact beneath
    `snapshot_dir` against the caller's full compatibility context.

    Every `expected_*` argument is required: this is the public loading
    API, and a caller that omits one could otherwise silently accept an
    artifact built with an unknown chunk schema, chunker algorithm, source
    schema, source digest, or `max_chars`. Callers that genuinely need
    structural-only validation without a compatibility context (crash
    recovery) use the internal `_load_chunk_artifact_from_directory`
    instead, which keeps those checks optional.

    Rejects: a missing manifest or chunks file (`SnapshotNotFound`); an
    unsupported manifest/chunk-schema/chunker-algorithm version or a
    `max_chars`/source-schema mismatch against the supplied expectations
    (`SnapshotIncompatible`); and, as `SnapshotCorrupted`, everything else —
    a manifest that fails validation; malformed JSON, a blank line, or an
    invalid `SourceChunk` in `chunks.jsonl`; a duplicate `chunk_id`; a
    missing or repeated chunk position within one source; chunk text
    exceeding the manifest's `max_chars`; an artifact anchored to a
    different `sources_digest` than expected; and a `chunk_count` or
    `chunks_digest` mismatch. Nothing is returned unless every check
    passed.
    """
    return _load_chunk_artifact_from_directory(
        chunk_artifact_directory(snapshot_dir),
        expected_source_schema_version=expected_source_schema_version,
        expected_sources_digest=expected_sources_digest,
        expected_chunk_schema_version=expected_chunk_schema_version,
        expected_chunker_algorithm_version=expected_chunker_algorithm_version,
        expected_max_chars=expected_max_chars,
    )


def _is_valid_chunk_artifact_directory(path: Path) -> bool:
    try:
        _load_chunk_artifact_from_directory(
            path,
            expected_source_schema_version=None,
            expected_sources_digest=None,
            expected_chunk_schema_version=None,
            expected_chunker_algorithm_version=None,
            expected_max_chars=None,
        )
    except SnapshotStoreError:
        return False
    return True


def _recover_interrupted_chunk_replacement(target: Path) -> None:
    """The same idempotent crash-recovery protocol as
    `recover_interrupted_replacement`, scoped to one chunk artifact
    directory. Touches only `target` and `backup_path(target)`."""
    recover_interrupted_replacement(target, is_valid=_is_valid_chunk_artifact_directory)


def publish_chunk_artifact(
    *,
    snapshot_dir: Path,
    chunks: Sequence[SourceChunk],
    chunk_schema_version: int,
    chunker_algorithm_version: int,
    max_chars: int,
    source_schema_version: int,
    sources_digest: str,
    manifest_schema_version: int = CHUNK_ARTIFACT_MANIFEST_SCHEMA_VERSION,
) -> ChunkArtifactManifest:
    """Atomically publish, or replace, the derived chunk artifact beneath
    `snapshot_dir`.

    Before anything else, loads and validates the normalized-source
    snapshot at `snapshot_dir` (see `load_snapshot`) and confirms it is the
    exact parent this chunk artifact is meant to anchor to: a missing,
    unreadable, or malformed parent propagates `load_snapshot`'s own
    `SnapshotNotFound`/`SnapshotCorrupted`/`SnapshotIncompatible`; a parent
    whose `source_schema_version` does not match `source_schema_version`
    raises `SnapshotIncompatible`; and a parent whose `sources_digest` does
    not match `sources_digest` raises `SnapshotCorrupted`. All of this
    happens before any staging directory is created, so a bad parent can
    never leave behind a half-published chunk artifact.

    Writes `chunks.jsonl` and `manifest.json` to a staging directory
    created directly under the already-confirmed-existing `snapshot_dir`
    (never with `parents=True`, so a missing normalized-source snapshot
    directory is never implicitly created by this call), then validates
    the staged pair by loading it back through the same checks
    `load_chunk_artifact` applies, before publishing anything.
    `snapshot_dir` itself — and its own `manifest.json`/`sources.jsonl` —
    is never created, replaced, or otherwise modified by this call; only
    the fixed `chunks/` subdirectory is affected. There is exactly one
    active chunk configuration per snapshot, so an existing chunk artifact
    is always replaced rather than requiring an explicit `force` flag.

    On any failure (write, validation, or publication), the staging
    directory this call created is removed and nothing about the target
    changes: a failed or interrupted write never appears as a completed
    chunk artifact, and a failed replacement leaves the previous completed
    chunk artifact intact.
    """
    parent_snapshot = load_snapshot(snapshot_dir)
    if parent_snapshot.manifest.source_schema_version != source_schema_version:
        raise SnapshotIncompatible(
            "The normalized-source snapshot's source schema version does "
            "not match the requested chunk configuration."
        )
    if parent_snapshot.manifest.sources_digest != sources_digest:
        raise SnapshotCorrupted(
            "The normalized-source snapshot's sources_digest does not "
            "match the value this chunk artifact is being published to "
            "anchor to."
        )

    target = chunk_artifact_directory(snapshot_dir)
    _recover_interrupted_chunk_replacement(target)

    staging = snapshot_dir / f".chunks-staging-{uuid4().hex}"
    staging.mkdir(exist_ok=False)

    try:
        chunks_bytes = serialize_chunks_jsonl(chunks)
        digest = hashlib.sha256(chunks_bytes).hexdigest()

        manifest = ChunkArtifactManifest(
            manifest_schema_version=manifest_schema_version,
            chunk_schema_version=chunk_schema_version,
            chunker_algorithm_version=chunker_algorithm_version,
            max_chars=max_chars,
            source_schema_version=source_schema_version,
            sources_digest=sources_digest,
            status="chunks_complete",
            chunk_count=len(chunks),
            chunks_digest=digest,
        )

        write_file_durably(staging / _CHUNKS_FILENAME, chunks_bytes)
        write_file_durably(
            staging / _CHUNK_MANIFEST_FILENAME,
            manifest.model_dump_json().encode("utf-8"),
        )

        # Validate the staged pair exactly the way a later load would,
        # before anything is published.
        _load_chunk_artifact_from_directory(
            staging,
            expected_source_schema_version=source_schema_version,
            expected_sources_digest=sources_digest,
            expected_chunk_schema_version=chunk_schema_version,
            expected_chunker_algorithm_version=chunker_algorithm_version,
            expected_max_chars=max_chars,
        )

        publish_staged_directory(staging, target)
    except BaseException:
        remove_directory(staging)
        raise
    return manifest


@dataclass(frozen=True)
class ChunkArtifactLookupResult:
    """A deterministic classification of one snapshot's derived
    chunk-artifact state, without scanning or modifying anything outside
    the one `chunks/` directory beneath `snapshot_dir`."""

    kind: SnapshotLookupKind
    directory: Path
    manifest: ChunkArtifactManifest | None = None
    reason: str | None = None


def look_up_chunk_artifact(
    *,
    snapshot_dir: Path,
    source_schema_version: int,
    sources_digest: str,
    chunk_schema_version: int,
    chunker_algorithm_version: int,
    max_chars: int,
    force_rebuild: bool = False,
) -> ChunkArtifactLookupResult:
    """Classify the derived chunk-artifact state beneath `snapshot_dir`
    against the requested chunk configuration.

    `force_rebuild=True` short-circuits to `"rebuild_requested"` without
    reading the filesystem at all. Otherwise: `"missing"` (nothing at
    `chunks/`), `"compatible"` (a validated chunk artifact anchored to
    `sources_digest` at the requested `chunk_schema_version`/
    `chunker_algorithm_version`/`max_chars`), or `"incompatible"`
    (something exists but failed validation or does not match the
    requested configuration).
    """
    directory = chunk_artifact_directory(snapshot_dir)
    _recover_interrupted_chunk_replacement(directory)
    if force_rebuild:
        return ChunkArtifactLookupResult(kind="rebuild_requested", directory=directory)

    if not directory.exists():
        return ChunkArtifactLookupResult(kind="missing", directory=directory)

    try:
        loaded = _load_chunk_artifact_from_directory(
            directory,
            expected_source_schema_version=source_schema_version,
            expected_sources_digest=sources_digest,
            expected_chunk_schema_version=chunk_schema_version,
            expected_chunker_algorithm_version=chunker_algorithm_version,
            expected_max_chars=max_chars,
        )
    except SnapshotNotFound:
        return ChunkArtifactLookupResult(kind="missing", directory=directory)
    except (SnapshotCorrupted, SnapshotIncompatible) as error:
        return ChunkArtifactLookupResult(
            kind="incompatible", directory=directory, reason=str(error)
        )

    return ChunkArtifactLookupResult(
        kind="compatible", directory=directory, manifest=loaded.manifest
    )
