"""Platform-independent repository identity."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RepositoryIdentity(BaseModel):
    """A validated, platform-independent reference to one repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    owner: str
    name: str

    @field_validator("owner", "name")
    @classmethod
    def segment_must_be_a_bare_identifier(cls, value: str) -> str:
        """Keep identity segments non-blank and free of path separators.

        This is a platform-independent invariant only. GitHub-specific
        length and character rules live behind the GitHub adapter boundary,
        in `reporationale.adapters.github.reference`.
        """
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        if any(character.isspace() for character in value):
            raise ValueError("must not contain whitespace")
        if "/" in value:
            raise ValueError("must not contain '/'")
        return value

    @property
    def repository(self) -> str:
        """The `owner/name` form used as `SourceDocument.repository`."""
        return f"{self.owner}/{self.name}"
