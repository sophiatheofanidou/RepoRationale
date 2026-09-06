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
