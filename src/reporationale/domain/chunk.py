"""Platform-independent derived retrieval chunk of one normalized source.

`SourceChunk` is the stable, provider-independent unit of retrieval: one
deterministic passage carved out of exactly one already-normalized
`SourceDocument` (see `reporationale.domain.source_document`), carrying
enough of that source's own provenance and metadata to be consumed later
without reopening or re-parsing the originating platform response. It says
nothing about embeddings, a vector index, or how it was produced; the
splitting behaviour that creates chunks from a `SourceDocument` lives in
`reporationale.application.chunking`, and its persistence as `chunks.jsonl`
lives in `reporationale.adapters.snapshot_store`.
"""

from datetime import datetime

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from reporationale.domain.source_document import require_timezone_aware

# Bumped whenever `SourceChunk`'s own shape changes incompatibly.
SOURCE_CHUNK_SCHEMA_VERSION = 1


class SourceChunk(BaseModel):
    """One deterministic, citation-addressable retrieval passage derived
    from a single `SourceDocument`.

    `chunk_id` is fixed to `f"{source_id}:chunk:{chunk_index}"` and is
    independently re-checked against `source_id`/`chunk_index` on both
    direct construction and deserialization, so a chunk's identity can
    never disagree with its own source and position.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*:\S+$")
    chunk_index: int = Field(strict=True, ge=0)
    text: str
    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    repository: str = Field(pattern=r"^\S+$")
    source_type: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    source_url: AnyHttpUrl
    created_at: datetime | None = None
    updated_at: datetime | None = None
    parent_source_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]*:\S+$",
    )
    item_number: int | None = Field(default=None, ge=1)
    title: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    heading_path: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("text")
    @classmethod
    def text_must_contain_content(cls, value: str) -> str:
        """Reject an empty or whitespace-only chunk (mirrors
        `SourceDocument.text_must_contain_content`)."""
        if not value.strip():
            raise ValueError("text must contain non-whitespace content")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_include_timezone(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None:
            require_timezone_aware(value)
        return value

    @model_validator(mode="after")
    def chunk_id_must_match_source_and_index(self) -> "SourceChunk":
        expected_chunk_id = f"{self.source_id}:chunk:{self.chunk_index}"
        if self.chunk_id != expected_chunk_id:
            raise ValueError("chunk_id must equal '<source_id>:chunk:<chunk_index>'")
        return self

    @model_validator(mode="after")
    def validate_identity_and_relationships(self) -> "SourceChunk":
        """The same persisted-provenance invariants `SourceDocument` itself
        enforces (see `SourceDocument.validate_identity_and_relationships`):
        a chunk's identity stays namespaced by its own platform, any parent
        relationship stays within that same namespace and cannot loop back
        onto the chunk's own source, and its timestamps stay chronological.
        A chunk is derived from an already-validated `SourceDocument`, so
        this does not repeat that model's full validation matrix — only the
        subset a persisted, independently deserialized chunk must still
        guarantee on its own."""
        namespace = f"{self.platform}:"
        if not self.source_id.startswith(namespace):
            raise ValueError("source_id must be namespaced by platform")
        if self.parent_source_id is not None:
            if not self.parent_source_id.startswith(namespace):
                raise ValueError(
                    "parent_source_id must use the same platform namespace"
                )
            if self.parent_source_id == self.source_id:
                raise ValueError("a chunk cannot identify its own source as its parent")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")
        return self
