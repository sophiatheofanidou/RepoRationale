"""Normalization of standalone GitHub issues and their comments into
citation-addressable `SourceDocument` records.

GitHub's issues-list endpoint also returns pull requests (every PR is also
an "issue" internally); an item carrying the documented `pull_request` key
is excluded here, since PR roots are collected via `pull_request.py`.
"""

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from reporationale.adapters.github._shared import (
    GitHubUserResponse,
    parse_and_validate_issue_comment,
    parse_github_timestamp,
    require_github_platform,
    require_strict_positive_int,
)
from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.models import validate_canonical_github_url
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

# --- Issue root -------------------------------------------------------

_DOCUMENTED_STATES = frozenset({"open", "closed"})
_DOCUMENTED_STATE_REASONS = frozenset(
    {"completed", "reopened", "not_planned", "duplicate"}
)


class _GitHubIssueOrPullRequestNumberResponse(BaseModel):
    """The minimal subset validated for an issues-list item identified as a
    pull request (via the documented `pull_request` marker), which is
    excluded from standalone issue normalization. Only `number` is needed
    for duplicate-identity detection."""

    model_config = ConfigDict(extra="ignore", strict=True)

    number: PositiveInt


class _GitHubIssueResponse(BaseModel):
    """The subset of the GitHub "List repository issues" REST response
    that is used for a genuine standalone issue.

    `body`, `state_reason`, `closed_at`, and `user` may legitimately hold
    an explicit `null` (no description, no specific reason, an open issue,
    or a since-deleted author).
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    number: PositiveInt
    title: str = Field(min_length=1)
    body: str | None
    state: str
    state_reason: str | None
    html_url: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    user: GitHubUserResponse | None
    author_association: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("state")
    @classmethod
    def state_must_be_documented(cls, value: str) -> str:
        if value not in _DOCUMENTED_STATES:
            raise ValueError(f"unexpected issue state: {value!r}")
        return value

    @field_validator("state_reason")
    @classmethod
    def state_reason_must_be_documented(cls, value: str | None) -> str | None:
        if value is not None and value not in _DOCUMENTED_STATE_REASONS:
            raise ValueError(f"unexpected issue state_reason: {value!r}")
        return value

    @field_validator("author_association")
    @classmethod
    def author_association_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author_association must not be blank")
        return value

    @field_validator("created_at", "updated_at", "closed_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)

    @model_validator(mode="after")
    def closed_issue_must_have_closed_at(self) -> "_GitHubIssueResponse":
        if self.state == "closed" and self.closed_at is None:
            raise ValueError("a closed issue must have a closed_at timestamp")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


def _render_issue_text(title: str, body: str | None) -> str:
    """Title alone, or title plus the original body verbatim when present
    and non-blank."""
    if body is None or not body.strip():
        return title
    return f"{title}\n\n{body}"


def parse_standalone_issue(
    raw_item: object, *, identity: RepositoryIdentity
) -> tuple[str, bool, SourceDocument | None]:
    """Validate one issues-list record and normalize it if it is a genuine
    standalone issue.

    Returns `(source_id, is_pull_request, document_or_none)`: `source_id`
    is always returned, even for an excluded PR-shaped item, so the caller
    can detect a duplicate raw item across pages either way. `document` is
    `None` whenever `is_pull_request` is `True`; a genuine issue always
    produces a document (an issue always has a non-blank title).
    """
    require_github_platform(identity)

    if not isinstance(raw_item, dict):
        raise GitHubMalformedResponse("Expected a JSON object for an issues-list item.")

    is_pull_request = "pull_request" in raw_item
    malformed_reason: str | None = None

    if is_pull_request:
        minimal: _GitHubIssueOrPullRequestNumberResponse | None = None
        try:
            minimal = _GitHubIssueOrPullRequestNumberResponse.model_validate(raw_item)
        except ValidationError:
            malformed_reason = (
                "The GitHub API issues-list pull-request-shaped item is "
                "missing a valid number."
            )
        if malformed_reason is not None:
            raise GitHubMalformedResponse(malformed_reason)
        if minimal is None:
            raise AssertionError("unreachable: minimal or malformed_reason must be set")
        source_id = (
            f"github:{identity.owner}/{identity.name}:"
            f"issue:{minimal.number}:description"
        )
        return source_id, True, None

    parsed: _GitHubIssueResponse | None = None
    try:
        parsed = _GitHubIssueResponse.model_validate(raw_item)
    except ValidationError:
        malformed_reason = "The GitHub API issue response failed validation."

    if malformed_reason is None and parsed is not None:
        expected_path = f"/{identity.owner}/{identity.name}/issues/{parsed.number}"
        try:
            validate_canonical_github_url(parsed.html_url, expected_path=expected_path)
        except ValueError:
            malformed_reason = (
                "The GitHub API issue html_url is not canonical for this repository."
            )

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = (
        f"github:{identity.owner}/{identity.name}:issue:{parsed.number}:description"
    )

    metadata: dict[str, JsonValue] = {
        "github_id": parsed.id,
        "state": parsed.state,
        "state_reason": parsed.state_reason,
        "closed_at": parsed.closed_at.isoformat() if parsed.closed_at else None,
        "author_login": parsed.user.login if parsed.user is not None else None,
        "author_type": parsed.user.type if parsed.user is not None else None,
        "author_association": parsed.author_association,
    }

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="issue",
        text=_render_issue_text(parsed.title, parsed.body),
        source_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        parent_source_id=None,
        item_number=parsed.number,
        title=parsed.title,
        metadata=metadata,
    )
    return source_id, False, document


# --- Issue comments (GitHub's "issue comment" object) --------------------


def parse_issue_comment(
    raw_item: object, *, identity: RepositoryIdentity, issue_number: int
) -> tuple[str, SourceDocument | None]:
    """Validate one standalone-issue comment record and normalize it if
    non-empty. Always returns the comment's deterministic `source_id`,
    even when skipped for an empty body."""
    require_github_platform(identity)
    require_strict_positive_int(issue_number, name="issue_number")

    parsed, malformed_reason = parse_and_validate_issue_comment(
        raw_item,
        identity=identity,
        html_path_segment="issues",
        item_number=issue_number,
    )
    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = (
        f"github:{identity.owner}/{identity.name}:issue:{issue_number}:"
        f"comment:{parsed.id}"
    )

    body = parsed.body
    if body is None or not body.strip():
        return source_id, None

    metadata: dict[str, JsonValue] = {
        "github_id": parsed.id,
        "author_login": parsed.user.login if parsed.user is not None else None,
        "author_type": parsed.user.type if parsed.user is not None else None,
        "author_association": parsed.author_association,
    }

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="issue_comment",
        text=body,
        source_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        parent_source_id=(
            f"github:{identity.owner}/{identity.name}:issue:{issue_number}:description"
        ),
        item_number=issue_number,
        title=None,
        metadata=metadata,
    )
    return source_id, document
