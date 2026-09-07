"""Normalization of default-branch commit messages into `SourceDocument`.

Validates each record returned by GitHub's "List commits" REST endpoint
strictly, and maps each non-blank-message commit to one platform-independent
`SourceDocument` while preserving platform-native terminology. A commit with
a blank message documents no rationale and is skipped rather than
normalized (see `parse_commit`). Deliberately does not model or persist the
commit author's email address.
"""

import re
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from reporationale.adapters.github._shared import (
    TRUSTED_API_HOSTNAME,
    GitHubUserResponse,
    describe_validation_error,
    parse_github_timestamp,
    require_github_platform,
    validate_github_url,
)
from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.models import validate_canonical_github_url
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

# A commit SHA is a lowercase-hex Git object ID. 40 hex characters (SHA-1) is
# standard today; this deliberately does not hard-code exactly 40, since a
# future SHA-256 repository would use 64.
_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")


class _GitHubGitAuthorResponse(BaseModel):
    """The subset of a commit's nested Git author object that is used.

    Deliberately excludes `email`: commit-author email addresses are not
    persisted.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    name: str = Field(min_length=1)
    date: datetime

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("commit author name must not be blank")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> datetime | None:
        return parse_github_timestamp(value)


class _GitHubCommitDetailResponse(BaseModel):
    """The subset of a commit's nested `commit` object that is used.

    `message` may legitimately be blank or whitespace-only: Git itself
    permits an empty commit message (for example, one created with `git
    commit --allow-empty-message`), and this has been observed on a real,
    otherwise well-formed commit record. Such a commit documents no
    rationale, so `parse_commit` skips it rather than treating it as a
    malformed response -- the same way an empty issue/PR comment or review
    body is already skipped elsewhere in this adapter.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    message: str
    author: _GitHubGitAuthorResponse


class _GitHubCommitResponse(BaseModel):
    """The subset of the GitHub "List commits" REST response that is used.

    `author` (the linked GitHub account, distinct from the free-text Git
    author name in `commit.author`) is a required key that may legitimately
    hold an explicit `null` when the commit's author email is not linked to
    a GitHub account.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    sha: str = Field(min_length=1)
    html_url: str = Field(min_length=1)
    url: str = Field(min_length=1)
    commit: _GitHubCommitDetailResponse
    author: GitHubUserResponse | None

    @field_validator("sha")
    @classmethod
    def sha_must_be_a_valid_object_id(cls, value: str) -> str:
        if not _COMMIT_SHA_PATTERN.fullmatch(value):
            raise ValueError("sha must be a valid lowercase-hex Git object identifier")
        return value


def parse_commit(
    raw_item: object, *, identity: RepositoryIdentity
) -> tuple[str, SourceDocument | None]:
    """Validate one commit-list record and normalize it if its message is
    non-blank. Always returns the commit's deterministic `source_id`, even
    when skipped for a blank message -- mirroring
    `reporationale.adapters.github.issue.parse_issue_comment` -- so the
    caller's cross-page duplicate-identity check still covers a skipped
    commit.

    Rejects a non-GitHub `identity` immediately. Raises
    `GitHubMalformedResponse` for any type, value, or canonical-URL
    mismatch (including a SHA that does not match the one embedded in
    `html_url`/`url`), only after the originating `except` block has fully
    exited. A schema mismatch's message includes a per-field diagnostic
    summary (see `describe_validation_error`) -- the failing field path,
    reason, and error type for every issue, but never the raw offending
    response value.
    """
    require_github_platform(identity)

    malformed_reason: str | None = None
    parsed: _GitHubCommitResponse | None = None

    try:
        parsed = _GitHubCommitResponse.model_validate(raw_item)
    except ValidationError as error:
        malformed_reason = (
            "The GitHub API commit response failed validation: "
            + describe_validation_error(error)
        )

    if malformed_reason is None and parsed is not None:
        try:
            validate_canonical_github_url(
                parsed.html_url,
                expected_path=(
                    f"/{identity.owner}/{identity.name}/commit/{parsed.sha}"
                ),
            )
            validate_github_url(
                parsed.url,
                hostname=TRUSTED_API_HOSTNAME,
                expected_path=(
                    f"/repos/{identity.owner}/{identity.name}/commits/{parsed.sha}"
                ),
                expected_fragment=None,
            )
        except ValueError:
            malformed_reason = (
                "The GitHub API commit URLs are not canonical for this "
                "repository and commit SHA."
            )

    if malformed_reason is not None:
        raise GitHubMalformedResponse(malformed_reason)
    if parsed is None:
        raise AssertionError("unreachable: parsed or malformed_reason must be set")

    source_id = f"github:{identity.owner}/{identity.name}:commit:{parsed.sha}"

    if not parsed.commit.message.strip():
        return source_id, None

    metadata: dict[str, JsonValue] = {
        "sha": parsed.sha,
        "author_date": parsed.commit.author.date.isoformat(),
        "commit_author_name": parsed.commit.author.name,
        "author_login": parsed.author.login if parsed.author is not None else None,
        "author_type": parsed.author.type if parsed.author is not None else None,
    }

    document = SourceDocument(
        source_id=source_id,
        platform="github",
        repository=identity.repository,
        source_type="commit",
        text=parsed.commit.message,
        source_url=parsed.html_url,
        created_at=parsed.commit.author.date,
        updated_at=None,
        parent_source_id=None,
        item_number=None,
        title=None,
        metadata=metadata,
    )
    return source_id, document
