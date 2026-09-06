"""Normalized GitHub repository metadata returned by the adapter boundary."""

from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StrictBool,
    field_validator,
    model_validator,
)

from reporationale.domain.repository_identity import RepositoryIdentity

_TRUSTED_SCHEME = "https"
_TRUSTED_HTTPS_PORT = 443
_TRUSTED_HTML_HOSTNAME = "github.com"


def validate_canonical_github_url(html_url: str, *, expected_path: str) -> None:
    """Require an exact `https://github.com<expected_path>` URL.

    Shared by every GitHub response/metadata model that carries an
    `html_url`-style field (repository metadata, pull-request root records,
    and future source types), so each enforces the identical canonical-URL
    contract independently: no userinfo, no non-default port, no query
    string or fragment, and an exact path match. Raises `ValueError`, which
    callers embedding this in a Pydantic validator can let propagate, and
    which adapter call sites elsewhere catch and translate into
    `GitHubMalformedResponse`.
    """
    parsed = urlsplit(html_url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("html_url must not contain userinfo")
    if parsed.scheme != _TRUSTED_SCHEME:
        raise ValueError("html_url must use https")
    if (parsed.hostname or "").lower() != _TRUSTED_HTML_HOSTNAME:
        raise ValueError(f"html_url must be a {_TRUSTED_HTML_HOSTNAME} URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("html_url has an invalid port") from exc
    if port is not None and port != _TRUSTED_HTTPS_PORT:
        raise ValueError("html_url must use the default HTTPS port")
    if parsed.query or parsed.fragment:
        raise ValueError("html_url must not contain a query string or fragment")
    if parsed.path != expected_path:
        raise ValueError("html_url does not match the expected canonical path")


class GitHubRepositoryMetadata(BaseModel):
    """Repository metadata normalized from a GitHub REST API response.

    Contains only the fields required by repository ingestion. Raw GitHub JSON
    and HTTP response objects never cross this boundary. This model
    independently enforces its own contract (non-blank branch, canonical
    URL matching `identity`) rather than relying only on the adapter's
    private pre-validation of the raw API response, so direct construction
    elsewhere cannot bypass it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: RepositoryIdentity
    github_id: PositiveInt
    html_url: AnyHttpUrl
    private: StrictBool
    default_branch: str = Field(min_length=1)
    # GitHub's repository `size` field (kibibytes on GitHub's storage), when
    # the lookup response provided it. This is a coarse platform storage
    # metric, not a measure of ingestable text volume, so it is optional
    # and only ever used as a supplementary admission-estimate signal.
    size_kb: int | None = None

    @field_validator("default_branch")
    @classmethod
    def default_branch_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("default_branch must not be blank")
        return value

    @field_validator("size_kb")
    @classmethod
    def size_kb_must_be_nonnegative_when_present(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError("size_kb must be a nonnegative integer when present")
        return value

    @model_validator(mode="after")
    def html_url_must_match_identity(self) -> "GitHubRepositoryMetadata":
        validate_canonical_github_url(
            str(self.html_url),
            expected_path=f"/{self.identity.owner}/{self.identity.name}",
        )
        return self
