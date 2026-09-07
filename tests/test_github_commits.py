"""Unit tests for commit-message normalization."""

import pytest
from github_test_support import load_fixture

from reporationale.adapters.github import GitHubMalformedResponse
from reporationale.adapters.github.commits import parse_commit
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_SHA = "b" * 40
_SOURCE_ID = f"github:octo-org/example-repo:commit:{_SHA}"


def test_parse_commit() -> None:
    raw = load_fixture("commit.json")

    source_id, document = parse_commit(raw, identity=_IDENTITY)

    assert source_id == _SOURCE_ID
    assert document is not None
    assert document.source_id == _SOURCE_ID
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

    _source_id, document = parse_commit(raw, identity=_IDENTITY)

    assert document is not None
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


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_skips_a_commit_with_a_blank_message_without_raising(message: str) -> None:
    """A commit with a fully blank message is real, valid Git data (for
    example, one created with `git commit --allow-empty-message`) that
    documents no rationale. It must be skipped like an empty comment or
    review body, not treated as a malformed response."""
    raw = {
        **load_fixture("commit.json"),
        "commit": {**load_fixture("commit.json")["commit"], "message": message},
    }

    source_id, document = parse_commit(raw, identity=_IDENTITY)

    assert source_id == _SOURCE_ID
    assert document is None


def test_rejects_blank_author_name() -> None:
    raw = {
        **load_fixture("commit.json"),
        "commit": {
            **load_fixture("commit.json")["commit"],
            "author": {**load_fixture("commit.json")["commit"]["author"], "name": ""},
        },
    }

    with pytest.raises(GitHubMalformedResponse):
        parse_commit(raw, identity=_IDENTITY)


def test_malformed_response_message_names_a_missing_field() -> None:
    raw = {
        key: value for key, value in load_fixture("commit.json").items() if key != "sha"
    }

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_commit(raw, identity=_IDENTITY)

    assert "sha" in str(excinfo.value)
    assert "missing" in str(excinfo.value)


def test_malformed_response_message_names_a_nested_field_path() -> None:
    raw = {
        **load_fixture("commit.json"),
        "commit": {**load_fixture("commit.json")["commit"], "message": 12345},
    }

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_commit(raw, identity=_IDENTITY)

    assert "commit.message" in str(excinfo.value)


def test_malformed_response_message_never_includes_the_raw_offending_value() -> None:
    """The diagnostic message must name the failing field, never echo the
    raw response value it rejected -- that value could be arbitrary
    repository content (here, a distinctive marker embedded in the
    ill-typed value that fails validation)."""
    marker = "UNIQUE-SECRET-LOOKING-COMMIT-BODY-MARKER-42"
    raw = {
        **load_fixture("commit.json"),
        "commit": {
            **load_fixture("commit.json")["commit"],
            # A dict is not a valid `message` (must be `str`), so this value
            # itself becomes the failing field's rejected input.
            "message": {"unexpected_shape": marker},
        },
    }

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        parse_commit(raw, identity=_IDENTITY)

    assert marker not in str(excinfo.value)
    assert "commit.message" in str(excinfo.value)


def test_rejects_non_github_identity_directly() -> None:
    raw = load_fixture("commit.json")
    non_github = RepositoryIdentity(platform="gitlab", owner="o", name="r")

    with pytest.raises(ValueError, match="platform must be 'github'"):
        parse_commit(raw, identity=non_github)
