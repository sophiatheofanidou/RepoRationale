"""Normalization of everything the MVP ingests for a closed pull request:
its root record, general comments, review summaries, and inline review
comments/replies — into citation-addressable `SourceDocument` records.

Grouped in one module because all four record types describe the same
pull request and share the same identity/URL conventions; each keeps its
own strict response model and its own `parse_*` function, so a defect in
one cannot silently change another's contract.

Reply-parent resolution for inline review comments (a reply's
`in_reply_to_id` must reference a comment actually present in the fully
collected response) requires the complete, paginated result and is
therefore performed by
`GitHubClient.list_repository_pull_request_review_comments`, not by
`parse_pull_request_review_comment`.
"""

import re
from datetime import datetime
from typing import Literal

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
    TRUSTED_API_HOSTNAME,
    TRUSTED_HTML_HOSTNAME,
    GitHubUserResponse,
    parse_and_validate_issue_comment,
    parse_github_timestamp,
    require_github_platform,
    require_strict_positive_int,
    validate_github_url,
)
from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.models import validate_canonical_github_url
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

# --- Pull-request root -------------------------------------------------


class _GitHubPullRequestResponse(BaseModel):
    """The subset of the GitHub "List pull requests" REST response that is
    used.

    Matches the documented list-endpoint payload exactly: it has `id`,
    `number`, and `merged_at`, but — unlike the single-pull-request "Get a
    pull request" response — no `merged` boolean, so merge status is
    derived from `merged_at` alone (see `parse_closed_pull_request`) rather
    than requiring a per-item detail request.

    `body`, `merged_at`, `merge_commit_sha`, and `user` are required keys
    that may legitimately hold a JSON `null` (no description, an unmerged
    PR, or a since-deleted author).
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    number: PositiveInt
    state: str
    title: str = Field(min_length=1)
    body: str | None
    html_url: str = Field(min_length=1)
    created_at: datetime
    updated_at: datetime
    closed_at: datetime
    merged_at: datetime | None
    merge_commit_sha: str | None
    user: GitHubUserResponse | None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("state")
    @classmethod
    def state_must_be_closed(cls, value: str) -> str:
        if value != "closed":
            raise ValueError("expected a closed pull request")
        return value

    @field_validator(
        "created_at", "updated_at", "closed_at", "merged_at", mode="before"
    )
    @classmethod
    def parse_timestamp(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)

    @field_validator("merge_commit_sha", mode="before")
    @classmethod
    def normalize_legacy_empty_merge_commit_sha(cls, value: object) -> object:
        # Very old closed PRs may use an empty string where current GitHub
        # responses use JSON null (observed on pallets/markupsafe PR #4).
        # Normalize only that exact legacy sentinel; whitespace remains
        # malformed rather than silently erasing unexpected content.
        if value == "":
            return None
        if isinstance(value, str) and not value.strip():
            raise ValueError("merge_commit_sha must not be blank when present")
        return value


def _render_text(title: str, body: str | None) -> str:
    """Title alone when the body is missing or blank; otherwise the title
    followed by a blank line and the original body, verbatim."""
    if body is None or not body.strip():
        return title
    return f"{title}\n\n{body}"


def parse_closed_pull_request(
    raw_item: object, *, identity: RepositoryIdentity
) -> SourceDocument:
    """Validate and normalize one closed pull-request root record.

    Raises `GitHubMalformedResponse` for any type, value, state, or
    canonical-URL mismatch, only after the originating `except` block has
    fully exited.
    """
    require_github_platform(identity)

    malformed_reason: str | None = None
    parsed: _GitHubPullRequestResponse | None = None

    try:
        parsed = _GitHubPullRequestResponse.model_validate(raw_item)
    except ValidationError:
        malformed_reason = "The GitHub API pull-request response failed validation."

    if malformed_reason is None and parsed is not None:
        expected_path = f"/{identity.owner}/{identity.name}/pull/{parsed.number}"
        try:
            validate_canonical_github_url(parsed.html_url, expected_path=expected_path)
        except ValueError:
            malformed_reason = (
                "The GitHub API pull-request html_url is not canonical for "
                "this repository."
            )

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    metadata: dict[str, JsonValue] = {
        "github_id": parsed.id,
        "state": parsed.state,
        "merged": parsed.merged_at is not None,
        "closed_at": parsed.closed_at.isoformat(),
        "merged_at": parsed.merged_at.isoformat()
        if parsed.merged_at is not None
        else None,
        "merge_commit_sha": parsed.merge_commit_sha,
        "author_login": parsed.user.login if parsed.user is not None else None,
        "author_type": parsed.user.type if parsed.user is not None else None,
    }

    return SourceDocument(
        source_id=(
            f"github:{identity.owner}/{identity.name}:"
            f"pull_request:{parsed.number}:description"
        ),
        platform="github",
        repository=identity.repository,
        source_type="pull_request",
        text=_render_text(parsed.title, parsed.body),
        source_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        parent_source_id=None,
        item_number=parsed.number,
        title=parsed.title,
        metadata=metadata,
    )


# --- General comments (GitHub's "issue comment" object) -----------------


def parse_pull_request_general_comment(
    raw_item: object, *, identity: RepositoryIdentity, pull_request_number: int
) -> tuple[str, SourceDocument | None]:
    """Validate one PR general-comment record and normalize it if non-empty.

    Always returns the comment's deterministic `source_id`, even when
    skipped for a `null`, empty, or whitespace-only body, so the caller can
    detect a duplicate identity across pages either way.
    """
    require_github_platform(identity)
    require_strict_positive_int(pull_request_number, name="pull_request_number")

    parsed, malformed_reason = parse_and_validate_issue_comment(
        raw_item,
        identity=identity,
        html_path_segment="pull",
        item_number=pull_request_number,
    )
    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = (
        f"github:{identity.owner}/{identity.name}:"
        f"pull_request:{pull_request_number}:comment:{parsed.id}"
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
        source_type="pull_request_comment",
        text=body,
        source_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        parent_source_id=(
            f"github:{identity.owner}/{identity.name}:"
            f"pull_request:{pull_request_number}:description"
        ),
        item_number=pull_request_number,
        title=None,
        metadata=metadata,
    )
    return source_id, document


# --- Review summaries -----------------------------------------------------

# Documented GitHub pull-request review states. "PENDING" denotes a review
# still being drafted by its own author; the list endpoint does not return
# other users' pending reviews, but a caller's own pending review can appear.
_DOCUMENTED_REVIEW_STATES = frozenset(
    {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}
)


class _GitHubReviewResponse(BaseModel):
    """The subset of the GitHub "List reviews for a pull request" REST
    response that is used.

    `body` and `user` may hold an explicit `null` (no summary text, or a
    since-deleted author). `commit_id` and `submitted_at` are nullable for
    a `PENDING` review that has not yet been submitted.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    body: str | None
    state: str
    html_url: str = Field(min_length=1)
    commit_id: str | None
    submitted_at: datetime | None
    user: GitHubUserResponse | None
    author_association: str = Field(min_length=1)

    @field_validator("state")
    @classmethod
    def state_must_be_documented(cls, value: str) -> str:
        if value not in _DOCUMENTED_REVIEW_STATES:
            raise ValueError(f"unexpected pull-request review state: {value!r}")
        return value

    @field_validator("commit_id")
    @classmethod
    def commit_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("commit_id must not be blank when present")
        return value

    @field_validator("author_association")
    @classmethod
    def author_association_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author_association must not be blank")
        return value

    @field_validator("submitted_at", mode="before")
    @classmethod
    def parse_submitted_at(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)


def parse_pull_request_review(
    raw_item: object, *, identity: RepositoryIdentity, pull_request_number: int
) -> tuple[str, SourceDocument | None]:
    """Validate one pull-request review record and normalize it if its
    summary body is non-empty. Always returns the review's deterministic
    `source_id`, even when skipped for an empty body."""
    require_github_platform(identity)
    require_strict_positive_int(pull_request_number, name="pull_request_number")

    malformed_reason: str | None = None
    parsed: _GitHubReviewResponse | None = None

    try:
        parsed = _GitHubReviewResponse.model_validate(raw_item)
    except ValidationError:
        malformed_reason = (
            "The GitHub API pull-request review response failed validation."
        )

    if malformed_reason is None and parsed is not None:
        try:
            validate_github_url(
                parsed.html_url,
                hostname=TRUSTED_HTML_HOSTNAME,
                expected_path=(
                    f"/{identity.owner}/{identity.name}/pull/{pull_request_number}"
                ),
                expected_fragment=f"pullrequestreview-{parsed.id}",
            )
        except ValueError:
            malformed_reason = (
                "The GitHub API pull-request review html_url is not canonical "
                "for this repository and pull request."
            )

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = (
        f"github:{identity.owner}/{identity.name}:"
        f"pull_request:{pull_request_number}:review:{parsed.id}"
    )

    body = parsed.body
    if body is None or not body.strip():
        return source_id, None

    metadata: dict[str, JsonValue] = {
        "github_id": parsed.id,
        "state": parsed.state,
        "submitted_at": parsed.submitted_at.isoformat()
        if parsed.submitted_at is not None
        else None,
        "commit_id": parsed.commit_id,
        "author_login": parsed.user.login if parsed.user is not None else None,
        "author_type": parsed.user.type if parsed.user is not None else None,
        "author_association": parsed.author_association,
    }

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="pull_request_review",
        text=body,
        source_url=parsed.html_url,
        created_at=parsed.submitted_at,
        updated_at=None,
        parent_source_id=(
            f"github:{identity.owner}/{identity.name}:"
            f"pull_request:{pull_request_number}:description"
        ),
        item_number=pull_request_number,
        title=None,
        metadata=metadata,
    )
    return source_id, document


# --- Inline review comments/replies ---------------------------------------

# GitHub's documented review-comment `html_url` anchor uses one of two
# families: the conversation-timeline anchor `discussion_r{id}`, whose
# embedded ID must equal the comment's own `id`, or the diff-view anchor
# `discussion-diff-{id}`, which identifies a position within the diff
# rather than the comment itself and so is accepted for any positive
# integer. No other fragment is documented.
_DISCUSSION_DIFF_FRAGMENT_PATTERN = r"discussion-diff-[1-9]\d*"


class _GitHubReviewCommentResponse(BaseModel):
    """The subset of the GitHub "List review comments on a pull request"
    REST response that is used.

    `body`, `user`, `in_reply_to_id`, `pull_request_review_id`, `position`,
    `original_position`, and `original_line` may legitimately hold an
    explicit `null` (no body, a since-deleted author, a top-level comment,
    a review still pending, a comment no longer positioned in the current
    diff, or a *file*-subject comment, which documents no line/position
    fields at all).

    `in_reply_to_id` also documents `null` for a top-level comment, but
    GitHub's real endpoint (observed on `pallets/itsdangerous` PR 419) may
    omit the key entirely for the same case rather than sending it as
    `null`; a missing key is treated identically to an explicit `null` —
    both mean "top-level comment", normalized to the pull request's own
    description as the parent.

    `subject_type` is documented as `"line"` or `"file"`, but GitHub's own
    documented example response for this endpoint omits the key entirely
    (a legacy, line-only shape predating the `file`-subject feature); a
    missing key is treated the same as an absent/unknown subject.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    pull_request_review_id: int | None
    path: str = Field(min_length=1)
    body: str | None
    commit_id: str = Field(min_length=1)
    original_commit_id: str = Field(min_length=1)
    in_reply_to_id: int | None = None
    user: GitHubUserResponse | None
    created_at: datetime
    updated_at: datetime
    html_url: str = Field(min_length=1)
    url: str = Field(min_length=1)
    pull_request_url: str = Field(min_length=1)
    author_association: str = Field(min_length=1)
    position: int | None
    original_position: int | None
    line: int | None
    original_line: int | None
    start_line: int | None
    original_start_line: int | None
    side: Literal["LEFT", "RIGHT"]
    start_side: Literal["LEFT", "RIGHT"] | None
    subject_type: Literal["line", "file"] | None = None

    @field_validator("author_association")
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)

    @field_validator("pull_request_review_id", "in_reply_to_id")
    @classmethod
    def id_must_be_positive_when_present(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError("must be a strict positive integer when present")
        return value

    @field_validator(
        "position",
        "original_position",
        "line",
        "original_line",
        "start_line",
        "original_start_line",
    )
    @classmethod
    def location_field_must_be_strict_positive_when_present(
        cls, value: int | None
    ) -> int | None:
        """Reject a boolean, zero, or negative value. Strict mode already
        rejects a numeric string or float, but not a `bool` (a Python
        `bool` is an `int` subclass), so that case is checked explicitly."""
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise ValueError("must be a strict positive integer when present")
        return value

    @model_validator(mode="after")
    def reply_must_not_be_self_referential(
        self,
    ) -> "_GitHubReviewCommentResponse":
        if self.in_reply_to_id is not None and self.in_reply_to_id == self.id:
            raise ValueError("in_reply_to_id must not reference its own comment")
        return self

    @model_validator(mode="after")
    def updated_at_must_not_precede_created_at(
        self,
    ) -> "_GitHubReviewCommentResponse":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


def parse_pull_request_review_comment(
    raw_item: object, *, identity: RepositoryIdentity, pull_request_number: int
) -> tuple[str, int, int | None, SourceDocument | None]:
    """Validate one inline review-comment/reply record and normalize it if
    non-empty.

    Returns `(source_id, comment_id, in_reply_to_id, document_or_none)`:
    the first three are always returned, even when the comment is skipped
    for an empty body, so the caller can detect duplicate identities and
    resolve/validate reply-parent references across the complete,
    paginated result.
    """
    require_github_platform(identity)
    require_strict_positive_int(pull_request_number, name="pull_request_number")

    malformed_reason: str | None = None
    parsed: _GitHubReviewCommentResponse | None = None

    try:
        parsed = _GitHubReviewCommentResponse.model_validate(raw_item)
    except ValidationError:
        malformed_reason = (
            "The GitHub API pull-request review-comment response failed validation."
        )

    if malformed_reason is None and parsed is not None:
        try:
            allowed_fragment_pattern = re.compile(
                rf"discussion_r{parsed.id}|{_DISCUSSION_DIFF_FRAGMENT_PATTERN}"
            )
            validate_github_url(
                parsed.html_url,
                hostname=TRUSTED_HTML_HOSTNAME,
                expected_path=(
                    f"/{identity.owner}/{identity.name}/pull/{pull_request_number}"
                ),
                expected_fragment=allowed_fragment_pattern,
            )
            validate_github_url(
                parsed.url,
                hostname=TRUSTED_API_HOSTNAME,
                expected_path=(
                    f"/repos/{identity.owner}/{identity.name}/pulls/comments/"
                    f"{parsed.id}"
                ),
                expected_fragment=None,
            )
            validate_github_url(
                parsed.pull_request_url,
                hostname=TRUSTED_API_HOSTNAME,
                expected_path=(
                    f"/repos/{identity.owner}/{identity.name}/pulls/"
                    f"{pull_request_number}"
                ),
                expected_fragment=None,
            )
        except ValueError:
            malformed_reason = (
                "The GitHub API review-comment URLs are not canonical for "
                "this repository and pull request."
            )

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = (
        f"github:{identity.owner}/{identity.name}:"
        f"pull_request:{pull_request_number}:review_comment:{parsed.id}"
    )

    body = parsed.body
    if body is None or not body.strip():
        return source_id, parsed.id, parsed.in_reply_to_id, None

    if parsed.in_reply_to_id is not None:
        parent_source_id = (
            f"github:{identity.owner}/{identity.name}:pull_request:"
            f"{pull_request_number}:review_comment:{parsed.in_reply_to_id}"
        )
    else:
        parent_source_id = (
            f"github:{identity.owner}/{identity.name}:"
            f"pull_request:{pull_request_number}:description"
        )

    # A null `position` reliably means "outdated" only for a *line*-subject
    # comment; a *file*-subject comment always has a null position by
    # definition, and an absent `subject_type` gives no signal at all.
    # Report `None` (unknown) rather than fabricating a boolean.
    is_outdated: bool | None
    if parsed.subject_type == "line":
        is_outdated = parsed.position is None
    else:
        is_outdated = None

    metadata: dict[str, JsonValue] = {
        "github_id": parsed.id,
        "review_id": parsed.pull_request_review_id,
        "in_reply_to_id": parsed.in_reply_to_id,
        "path": parsed.path,
        "commit_id": parsed.commit_id,
        "original_commit_id": parsed.original_commit_id,
        "position": parsed.position,
        "original_position": parsed.original_position,
        "line": parsed.line,
        "original_line": parsed.original_line,
        "start_line": parsed.start_line,
        "original_start_line": parsed.original_start_line,
        "side": parsed.side,
        "start_side": parsed.start_side,
        "subject_type": parsed.subject_type,
        "is_outdated": is_outdated,
        "author_login": parsed.user.login if parsed.user is not None else None,
        "author_type": parsed.user.type if parsed.user is not None else None,
        "author_association": parsed.author_association,
    }

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="pull_request_review_comment",
        text=body,
        source_url=parsed.html_url,
        created_at=parsed.created_at,
        updated_at=parsed.updated_at,
        parent_source_id=parent_source_id,
        item_number=pull_request_number,
        title=None,
        metadata=metadata,
    )
    return source_id, parsed.id, parsed.in_reply_to_id, document
