"""Small internal helpers shared across GitHub adapter response parsers.

Not part of the adapter's public surface. Centralizing these avoids
inconsistent duplicate logic across the growing set of GitHub source-type
parsers, without changing any previously reviewed parser's external
behavior: each parser module imports what it needs instead of redefining
an equivalent copy.
"""

import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from reporationale.domain.repository_identity import RepositoryIdentity

TRUSTED_SCHEME = "https"
TRUSTED_HTTPS_PORT = 443
TRUSTED_HTML_HOSTNAME = "github.com"
TRUSTED_API_HOSTNAME = "api.github.com"


def require_github_platform(identity: RepositoryIdentity) -> None:
    """Reject a non-GitHub identity before it can produce a GitHub-namespaced
    `SourceDocument`.

    Every exported parser calls this itself, regardless of whether its
    client-level caller already checked, so direct construction cannot
    bypass the identity contract.
    """
    if identity.platform != "github":
        raise ValueError(
            f"identity.platform must be 'github', got {identity.platform!r}"
        )


def require_strict_positive_int(value: int, *, name: str) -> None:
    """Reject a boolean, non-int, zero, or negative value for an identifier
    that must be a strict positive integer (a PR or issue number, say).

    Every exported parser that takes such a number calls this itself,
    regardless of whether its client-level caller already checked.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a strict positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a strict positive integer")


def parse_github_timestamp(value: object) -> datetime | None:
    """Parse a GitHub ISO 8601 timestamp string into a timezone-aware
    `datetime`, or pass `None` through for an explicitly null timestamp.

    Used as a `mode="before"` field validator: pydantic's own strict mode
    rejects *any* string input for a `datetime` field, including a
    well-formed ISO 8601 string, so the string must be parsed here before
    the field's declared `datetime` type is checked.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be a valid ISO 8601 string") from exc
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


class GitHubUserResponse(BaseModel):
    """The subset of a GitHub `user` ("Simple User") object used across
    response types (PRs, comments, reviews, issues, commits)."""

    model_config = ConfigDict(extra="ignore", strict=True)

    login: str = Field(min_length=1)
    type: str = Field(min_length=1)

    @field_validator("login", "type")
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


def validate_github_url(
    url: str,
    *,
    hostname: str,
    expected_path: str,
    expected_fragment: str | re.Pattern[str] | None,
) -> None:
    """Validate a GitHub-related URL's scheme, host, port, userinfo, query,
    path, and (optionally) fragment.

    A generalized version of the comment-URL validation first written for
    PR general comments, reused by every new source type that needs a
    specific host (`github.com` or `api.github.com`) and, sometimes, a
    specific fragment (a comment/review anchor). `expected_fragment` may be
    an exact string (most call sites), a compiled pattern matched with
    `fullmatch` (for a source type that documents more than one accepted
    fragment family, such as a review comment's `discussion_r{id}` or
    `discussion-diff-{id}` anchor), or `None` to require no fragment at all.
    Does not modify or weaken
    `reporationale.adapters.github.models.validate_canonical_github_url`,
    which the already-reviewed repository and PR-root parsers use unchanged.
    """
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain userinfo")
    if parsed.scheme != TRUSTED_SCHEME:
        raise ValueError("url must use https")
    if (parsed.hostname or "").lower() != hostname:
        raise ValueError(f"url must be a {hostname} URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url has an invalid port") from exc
    if port is not None and port != TRUSTED_HTTPS_PORT:
        raise ValueError("url must use the default HTTPS port")
    if parsed.query:
        raise ValueError("url must not contain a query string")
    if parsed.path != expected_path:
        raise ValueError("url does not match the expected canonical path")
    if expected_fragment is None:
        if parsed.fragment:
            raise ValueError("url must not contain a fragment")
    elif isinstance(expected_fragment, re.Pattern):
        if expected_fragment.fullmatch(parsed.fragment) is None:
            raise ValueError("url does not match the expected fragment")
    elif parsed.fragment != expected_fragment:
        raise ValueError("url does not match the expected fragment")


def extract_repository_item_number(
    url: object,
    *,
    identity: RepositoryIdentity,
    resource: str,
) -> int:
    """Extract an issue or pull-request number from a canonical GitHub API URL.

    Repository-wide comment endpoints identify each comment's parent through
    `issue_url` or `pull_request_url`. Validate that URL against the selected
    repository before using the extracted number to classify the comment.
    """
    if resource not in {"issues", "pulls"}:
        raise ValueError("resource must be 'issues' or 'pulls'")
    if not isinstance(url, str):
        raise ValueError("url must be a string")

    parsed = urlsplit(url)
    path_pattern = re.compile(
        rf"/repos/{re.escape(identity.owner)}/{re.escape(identity.name)}/"
        rf"{resource}/([1-9]\d*)"
    )
    match = path_pattern.fullmatch(parsed.path)
    if match is None:
        raise ValueError("url does not identify an item in the selected repository")
    validate_github_url(
        url,
        hostname=TRUSTED_API_HOSTNAME,
        expected_path=parsed.path,
        expected_fragment=None,
    )
    return int(match.group(1))


class GitHubIssueCommentResponse(BaseModel):
    """The subset of the GitHub "List issue comments" REST response that is
    used, for both a pull request's general comments and a standalone
    issue's comments: GitHub represents both with the identical "issue
    comment" object, so this one model and its validation are shared by
    `pull_request.parse_pull_request_general_comment` and
    `issue.parse_issue_comment`.

    `body` and `user` are required keys that may legitimately hold an
    explicit `null` (no description text, or a since-deleted author).
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    body: str | None
    html_url: str = Field(min_length=1)
    url: str = Field(min_length=1)
    issue_url: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    user: GitHubUserResponse | None
    author_association: str = Field(min_length=1)

    @field_validator("author_association")
    @classmethod
    def author_association_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author_association must not be blank")
        return value

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)

    @model_validator(mode="after")
    def updated_at_must_not_precede_created_at(self) -> "GitHubIssueCommentResponse":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


def parse_and_validate_issue_comment(
    raw_item: object,
    *,
    identity: RepositoryIdentity,
    html_path_segment: str,
    item_number: int,
) -> tuple[GitHubIssueCommentResponse | None, str | None]:
    """Validate one "issue comment" record and its canonical URLs.

    `html_path_segment` is the one part of the canonical URL contract that
    actually differs between the two callers: `"pull"` for a pull
    request's general comments, `"issues"` for a standalone issue's
    comments.

    Returns `(parsed, malformed_reason)` — exactly one is `None`. Never
    raises itself: the caller raises `GitHubMalformedResponse` once this
    function's own `try`/`except` has fully exited, keeping the same
    deferred-raise discipline every parser in this adapter uses.
    """
    malformed_reason: str | None = None
    parsed: GitHubIssueCommentResponse | None = None
    try:
        parsed = GitHubIssueCommentResponse.model_validate(raw_item)
    except ValidationError:
        malformed_reason = "The GitHub API issue-comment response failed validation."

    if malformed_reason is None and parsed is not None:
        try:
            validate_github_url(
                parsed.html_url,
                hostname=TRUSTED_HTML_HOSTNAME,
                expected_path=(
                    f"/{identity.owner}/{identity.name}/"
                    f"{html_path_segment}/{item_number}"
                ),
                expected_fragment=f"issuecomment-{parsed.id}",
            )
            validate_github_url(
                parsed.url,
                hostname=TRUSTED_API_HOSTNAME,
                expected_path=(
                    f"/repos/{identity.owner}/{identity.name}/issues/comments/"
                    f"{parsed.id}"
                ),
                expected_fragment=None,
            )
            validate_github_url(
                parsed.issue_url,
                hostname=TRUSTED_API_HOSTNAME,
                expected_path=(
                    f"/repos/{identity.owner}/{identity.name}/issues/{item_number}"
                ),
                expected_fragment=None,
            )
        except ValueError:
            malformed_reason = (
                "The GitHub API issue-comment URLs are not canonical for "
                "this repository and item."
            )

    return parsed, malformed_reason
