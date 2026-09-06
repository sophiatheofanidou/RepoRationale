"""Platform-independent normalized-source snapshot manifest contract.

This persistence boundary records in a `sources_complete` snapshot
that the complete supported corpus for one repository, at one resolved
commit, was normalized and serialized deterministically. It deliberately
says nothing about chunks, embeddings, or a searchable vector index — those
are separate derived artifacts, and a `sources_complete` manifest must not be confused
with an application-level `ready` (fully queryable) snapshot.

Filesystem access, atomic publication, and loading/validation of the
`sources.jsonl` file this manifest describes live in
`reporationale.adapters.snapshot_store`, which depends on this module; this
module has no filesystem or GitHub-adapter dependency of its own.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import require_timezone_aware

_StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]
_StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

# Bumped whenever `SnapshotManifest`'s own shape changes incompatibly.
SNAPSHOT_MANIFEST_SCHEMA_VERSION = 1

# Bumped whenever the normalized `SourceDocument` shape recorded in
# `sources.jsonl` changes incompatibly. Deliberately separate from the
# manifest's own schema version: the manifest format and the normalized
# source-document format can evolve independently.
NORMALIZED_SOURCE_SCHEMA_VERSION = 1

# Only one normalized-source status exists. It is intentionally not named "ready" or
# "complete" alone: "sources_complete" states precisely what is guaranteed
# (the normalized source corpus is complete and validated) and nothing
# about chunks, embeddings, or a searchable index, which do not exist yet.
SnapshotStatus = Literal["sources_complete"]

_GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")
_SHA256_HEX_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class SnapshotManifest(BaseModel):
    """Everything needed to verify and reuse one normalized-source snapshot
    without re-reading `sources.jsonl` first.

    Frozen and independently validated (direct construction and
    deserialization alike): a non-blank branch, a valid resolved-commit
    object id, a valid sha256 hex digest, and counts that cannot be coerced
    from a boolean or a numeric string. Carries no credential or
    secret-derived value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema_version: _StrictPositiveInt
    source_schema_version: _StrictPositiveInt
    repository: RepositoryIdentity
    resolved_commit_sha: str
    default_branch: str
    status: SnapshotStatus
    created_at: datetime
    completed_at: datetime
    source_count: _StrictNonNegativeInt
    counts_by_source_type: dict[str, int]
    sources_digest: str
    producer_version: str = Field(min_length=1)

    @field_validator("created_at", "completed_at")
    @classmethod
    def timestamps_must_include_timezone(cls, value: datetime) -> datetime:
        require_timezone_aware(value)
        return value

    @field_validator("default_branch")
    @classmethod
    def default_branch_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("default_branch must not be blank")
        return value

    @field_validator("resolved_commit_sha")
    @classmethod
    def resolved_commit_sha_must_be_valid_object_id(cls, value: str) -> str:
        if _GIT_OBJECT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "resolved_commit_sha must be a valid lowercase-hex Git object id"
            )
        return value

    @field_validator("sources_digest")
    @classmethod
    def sources_digest_must_be_a_sha256_hex_digest(cls, value: str) -> str:
        if _SHA256_HEX_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "sources_digest must be a 64-character lowercase-hex sha256 digest"
            )
        return value

    @field_validator("producer_version")
    @classmethod
    def producer_version_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("producer_version must not be blank")
        return value

    @field_validator("counts_by_source_type", mode="before")
    @classmethod
    def counts_must_be_raw_nonnegative_ints(cls, value: object) -> object:
        """Reject a boolean or numeric-string count before pydantic's own
        lenient `int` coercion could otherwise accept either and erase the
        distinction this validator needs to reject (mirrors
        `RepositoryCorpus.counts_must_be_raw_nonnegative_ints`)."""
        if isinstance(value, dict):
            for source_type, count in value.items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(
                        f"counts_by_source_type[{source_type!r}] must be a "
                        "nonnegative integer"
                    )
        return value

    @model_validator(mode="after")
    def completed_at_must_not_precede_created_at(self) -> "SnapshotManifest":
        if self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        return self

    @model_validator(mode="after")
    def counts_must_sum_to_source_count(self) -> "SnapshotManifest":
        if sum(self.counts_by_source_type.values()) != self.source_count:
            raise ValueError("counts_by_source_type must sum to source_count")
        return self


# Bumped whenever `ChunkArtifactManifest`'s own shape changes incompatibly.
CHUNK_ARTIFACT_MANIFEST_SCHEMA_VERSION = 1

# Only one derived chunk-artifact status exists, named for exactly what it
# guarantees (the derived `chunks.jsonl` artifact is complete and
# validated) and nothing about embeddings or a searchable index, which do
# not exist yet.
ChunkArtifactStatus = Literal["chunks_complete"]


class ChunkArtifactManifest(BaseModel):
    """Everything needed to verify and reuse one derived `chunks/chunks.jsonl`
    artifact, stored beneath its parent normalized-source snapshot
    directory, without re-reading it first.

    Anchored to its parent normalized-source snapshot by
    `source_schema_version` and the exact `sources_digest` it was built
    from, so a chunk artifact can never be silently reused against a
    normalized-source snapshot it was not actually built from. Frozen and
    independently validated (direct construction and deserialization
    alike). Carries no credential, embedding, or vector-index
    configuration; this manifest means only that the derived chunk corpus
    itself is complete, never that a searchable index exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema_version: _StrictPositiveInt
    chunk_schema_version: _StrictPositiveInt
    chunker_algorithm_version: _StrictPositiveInt
    max_chars: _StrictPositiveInt
    source_schema_version: _StrictPositiveInt
    sources_digest: str
    status: ChunkArtifactStatus
    chunk_count: _StrictNonNegativeInt
    chunks_digest: str

    @field_validator("sources_digest", "chunks_digest")
    @classmethod
    def digest_must_be_a_sha256_hex_digest(cls, value: str) -> str:
        if _SHA256_HEX_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a 64-character lowercase-hex sha256 digest")
        return value


# Bumped whenever `VectorIndexManifest`'s own shape changes incompatibly.
# Separate from `reporationale.adapters.chroma_vector_store`'s
# `VECTOR_INDEX_ALGORITHM_VERSION`, which is bumped instead when how the
# Chroma collection itself is built or queried changes.
VECTOR_INDEX_MANIFEST_SCHEMA_VERSION = 1

# Only one vector-index status exists, named for exactly what it guarantees
# (embeddings for the complete anchored chunk artifact were persisted in a
# validated Chroma collection) and nothing about the answering agent or
# `search_history`, which are separate concerns built on top of it.
VectorIndexStatus = Literal["vector_index_complete"]


class VectorIndexManifest(BaseModel):
    """Everything needed to verify and reuse one persisted Chroma vector
    index, stored beneath its parent normalized-source snapshot directory,
    without reopening the Chroma collection first.

    Anchored to its parent normalized-source snapshot (`source_schema_version`,
    `sources_digest`) and to the exact derived chunk artifact it was built
    from (`chunk_schema_version`, `chunker_algorithm_version`, `max_chars`,
    `chunks_digest`, `chunk_count`), so a vector index can never be silently
    reused against a chunk artifact it was not actually built from. Frozen
    and independently validated. Carries no credential and no embedding
    vector or Chroma-internal data of its own; those remain in the Chroma
    collection and are validated separately by
    `reporationale.adapters.chroma_vector_store`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema_version: _StrictPositiveInt
    vector_index_algorithm_version: _StrictPositiveInt
    source_schema_version: _StrictPositiveInt
    sources_digest: str
    chunk_schema_version: _StrictPositiveInt
    chunker_algorithm_version: _StrictPositiveInt
    max_chars: _StrictPositiveInt
    chunks_digest: str
    chunk_count: _StrictNonNegativeInt
    embedding_model: str = Field(min_length=1)
    embedding_dimension: _StrictPositiveInt
    vector_store: Literal["chroma"]
    vector_store_version: str = Field(min_length=1)
    distance_metric: Literal["cosine"]
    indexed_record_count: _StrictNonNegativeInt
    status: VectorIndexStatus

    @field_validator("sources_digest", "chunks_digest")
    @classmethod
    def digest_must_be_a_sha256_hex_digest(cls, value: str) -> str:
        if _SHA256_HEX_DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("must be a 64-character lowercase-hex sha256 digest")
        return value

    @model_validator(mode="after")
    def indexed_record_count_must_match_chunk_count(self) -> "VectorIndexManifest":
        if self.indexed_record_count != self.chunk_count:
            raise ValueError("indexed_record_count must equal chunk_count")
        return self
