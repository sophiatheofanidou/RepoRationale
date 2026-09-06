"""Unit tests for `reporationale.adapters.github.issue`: standalone-issue
roots, PR-shaped discrimination, and issue comments. Client-level
pagination/ingestion behaviour is covered in
`test_github_source_ingestion_client.py`.
"""

import pytest
from github_test_support import load_fixture

from reporationale.adapters.github.errors import GitHubMalformedResponse
from reporationale.adapters.github.issue import (
    parse_issue_comment,
    parse_standalone_issue,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_ISSUE_NUMBER = 10


def test_rejects_non_github_identity_directly() -> None:
    non_github = RepositoryIdentity(platform="gitlab", owner="o", name="r")
    with pytest.raises(ValueError, match="platform must be 'github'"):
        parse_standalone_issue(load_fixture("issue.json"), identity=non_github)


@pytest.mark.parametrize("issue_number", [True, 0, -1, "10"])
def test_rejects_invalid_issue_number_directly(issue_number: object) -> None:
    raw = load_fixture("issue_comment.json")
    with pytest.raises(ValueError, match="issue_number"):
        parse_issue_comment(raw, identity=_IDENTITY, issue_number=issue_number)  # type: ignore[arg-type]


# --- Issue root -------------------------------------------------------


def test_parse_closed_issue_with_completed_reason() -> None:
    raw = load_fixture("issue.json")

    source_id, is_pull_request, document = parse_standalone_issue(
        raw, identity=_IDENTITY
    )

    assert source_id == "github:octo-org/example-repo:issue:10:description"
    assert is_pull_request is False
    assert document is not None
    assert document.source_type == "issue"
    assert document.metadata["state"] == "closed"
    assert document.metadata["state_reason"] == "completed"


def test_parse_open_issue_with_null_body_and_state_reason() -> None:
    raw = load_fixture("issue_open.json")

    _source_id, is_pull_request, document = parse_standalone_issue(
        raw, identity=_IDENTITY
    )

    assert is_pull_request is False
    assert document is not None
    assert document.text == "Feature request: dark mode"
    assert document.metadata["state_reason"] is None
    assert document.metadata["closed_at"] is None


def test_pr_shaped_item_is_excluded_from_normalization() -> None:
    """An item carrying the documented `pull_request` marker is excluded;
    its deterministic identity is still returned for duplicate detection."""
    raw = load_fixture("issue_pr_shaped.json")

    source_id, is_pull_request, document = parse_standalone_issue(
        raw, identity=_IDENTITY
    )

    assert source_id == "github:octo-org/example-repo:issue:42:description"
    assert is_pull_request is True
    assert document is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "701"},
        {"id": 0},
        {"title": ""},
        {"state": "unknown"},
        {"state_reason": "invalid_reason"},
        {"created_at": "2024-01-01T00:00:00"},
        {"state": "closed", "closed_at": None},
    ],
)
def test_issue_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    """Malformed IDs, blank required strings, undocumented state values,
    and a closed issue missing `closed_at` are all rejected."""
    raw = {**load_fixture("issue.json"), **overrides}
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_standalone_issue(raw, identity=_IDENTITY)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("key", ["id", "body", "state_reason", "closed_at", "user"])
def test_issue_rejects_missing_required_nullable_key(key: str) -> None:
    raw = load_fixture("issue.json")
    del raw[key]
    with pytest.raises(GitHubMalformedResponse):
        parse_standalone_issue(raw, identity=_IDENTITY)


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/example-repo/issues/99",
        "http://github.com/octo-org/example-repo/issues/10",
    ],
)
def test_issue_rejects_non_canonical_html_url(html_url: str) -> None:
    raw = {**load_fixture("issue.json"), "html_url": html_url}
    with pytest.raises(GitHubMalformedResponse):
        parse_standalone_issue(raw, identity=_IDENTITY)


def test_issue_rejects_non_object_input() -> None:
    with pytest.raises(GitHubMalformedResponse):
        parse_standalone_issue("not-an-object", identity=_IDENTITY)


# --- Issue comments (GitHub's shared "issue comment" object) ------------


def test_parse_non_empty_issue_comment() -> None:
    raw = load_fixture("issue_comment.json")

    source_id, document = parse_issue_comment(
        raw, identity=_IDENTITY, issue_number=_ISSUE_NUMBER
    )

    assert source_id == "github:octo-org/example-repo:issue:10:comment:801"
    assert document is not None
    assert document.source_type == "issue_comment"
    assert (
        document.parent_source_id == "github:octo-org/example-repo:issue:10:description"
    )
    assert document.metadata["author_login"] == "another-user"


@pytest.mark.parametrize("body", [None, "", "  \t "])
def test_issue_comment_empty_body_is_skipped(body: object) -> None:
    raw = {**load_fixture("issue_comment.json"), "body": body}

    source_id, document = parse_issue_comment(
        raw, identity=_IDENTITY, issue_number=_ISSUE_NUMBER
    )

    assert source_id
    assert document is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "801"},
        {"id": 0},
        {"author_association": ""},
        {"updated_at": "2024-01-01T04:00:00Z"},
    ],
)
def test_issue_comment_rejects_malformed_fields(overrides: dict[str, object]) -> None:
    raw = {**load_fixture("issue_comment.json"), **overrides}
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_issue_comment(raw, identity=_IDENTITY, issue_number=_ISSUE_NUMBER)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("key", ["body", "user"])
def test_issue_comment_rejects_missing_required_nullable_key(key: str) -> None:
    raw = load_fixture("issue_comment.json")
    del raw[key]
    with pytest.raises(GitHubMalformedResponse):
        parse_issue_comment(raw, identity=_IDENTITY, issue_number=_ISSUE_NUMBER)


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/example-repo/issues/10#issuecomment-999",
        "https://github.com/octo-org/example-repo/pull/10#issuecomment-801",
    ],
)
def test_issue_comment_rejects_non_canonical_html_url(html_url: str) -> None:
    raw = {**load_fixture("issue_comment.json"), "html_url": html_url}
    with pytest.raises(GitHubMalformedResponse):
        parse_issue_comment(raw, identity=_IDENTITY, issue_number=_ISSUE_NUMBER)
