"""Unit tests for commit-message normalization."""

import pytest
from github_test_support import load_fixture

from reporationale.adapters.github import GitHubMalformedResponse
from reporationale.adapters.github.commits import parse_commit
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_SHA = "b" * 40


def test_parse_commit() -> None:
    raw = load_fixture("commit.json")

    document = parse_commit(raw, identity=_IDENTITY)

    assert document.source_id == f"github:octo-org/example-repo:commit:{_SHA}"
    assert document.source_type == "commit"
    assert document.parent_source_id is None
    assert document.item_number is None
    assert document.text == "Fix the retry bug\n\nDetailed explanation of the fix."
    assert str(document.source_url) == (
        f"https://github.com/octo-org/example-repo/commit/{_SHA}"
    )
    assert document.metadata["sha"] == _SHA
    assert document.metadata["commit_author_name"] == "Jane Doe"
    assert document.metadata["author_login"] == "janedoe"
    assert "email" not in document.metadata
    assert "commit_author_email" not in document.metadata


def test_commit_without_a_linked_github_account_preserves_null_author() -> None:
    raw = {**load_fixture("commit.json"), "author": None}

    document = parse_commit(raw, identity=_IDENTITY)

    assert document.metadata["author_login"] is None
    assert document.metadata["author_type"] is None
    assert document.metadata["commit_author_name"] == "Jane Doe"


@pytest.mark.parametrize(
    "sha",
    [
        "b" * 39,
        "B" * 40,
        "not-a-valid-sha",
        "",
    ],
)
def test_rejects_malformed_sha(sha: str) -> None:
    raw = {**load_fixture("commit.json"), "sha": sha}

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_commit(raw, identity=_IDENTITY)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_rejects_sha_mismatched_with_urls() -> None:
    """The sha embedded in html_url/url must match the object's own sha."""
    other_sha = "c" * 40
    raw = {
        **load_fixture("commit.json"),
        "sha": other_sha,
    }

    with pytest.raises(GitHubMalformedResponse):
        parse_commit(raw, identity=_IDENTITY)


@pytest.mark.parametrize(
    "overrides",
    [
        {"commit": {**load_fixture("commit.json")["commit"], "message": ""}},
        {"commit": {**load_fixture("commit.json")["commit"], "message": "   "}},
        {
            "commit": {
                **load_fixture("commit.json")["commit"],
                "author": {
                    **load_fixture("commit.json")["commit"]["author"],
                    "name": "",
                },
            }
        },
    ],
)
def test_rejects_blank_message_or_author_name(overrides: dict[str, object]) -> None:
    raw = {**load_fixture("commit.json"), **overrides}

    with pytest.raises(GitHubMalformedResponse):
        parse_commit(raw, identity=_IDENTITY)


def test_rejects_non_github_identity_directly() -> None:
    raw = load_fixture("commit.json")
    non_github = RepositoryIdentity(platform="gitlab", owner="o", name="r")

    with pytest.raises(ValueError, match="platform must be 'github'"):
        parse_commit(raw, identity=non_github)
