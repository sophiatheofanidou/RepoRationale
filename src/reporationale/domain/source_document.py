"""Platform-independent normalized repository source."""

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


def require_timezone_aware(value: datetime) -> None:
    """Reject a timezone-naive datetime. Shared by every model that
    persists a timestamp (`SourceDocument` here, `SnapshotManifest` in
    `reporationale.domain.snapshot`), so a naive timestamp cannot become
    ambiguous once read back on a different machine."""
    if value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")


class SourceDocument(BaseModel):
    """One independently identifiable and citation-addressable source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*:\S+$")
    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    repository: str = Field(pattern=r"^\S+$")
    source_type: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    text: str
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

    @field_validator("text")
    @classmethod
    def text_must_contain_content(cls, value: str) -> str:
        """Reject empty textual sources while preserving their original text."""
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
    def validate_identity_and_relationships(self) -> "SourceDocument":
        """Keep identities namespaced and direct relationships consistent."""
        namespace = f"{self.platform}:"
        if not self.source_id.startswith(namespace):
            raise ValueError("source_id must be namespaced by platform")
        if self.parent_source_id is not None:
            if not self.parent_source_id.startswith(namespace):
                raise ValueError(
                    "parent_source_id must use the same platform namespace"
                )
            if self.parent_source_id == self.source_id:
                raise ValueError("a source document cannot be its own parent")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")
        return self
