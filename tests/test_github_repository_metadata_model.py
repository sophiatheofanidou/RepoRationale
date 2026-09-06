"""Direct model tests for `GitHubRepositoryMetadata` (A4).

The exported normalized model must independently enforce its own contract,
not rely only on the adapter's private pre-validation of the raw GitHub API
response, since it can be constructed directly.
"""

import json

import pytest
from pydantic import AnyHttpUrl, ValidationError

from reporationale.adapters.github import GitHubRepositoryMetadata
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_VALID_HTML_URL = AnyHttpUrl("https://github.com/octo-org/example-repo")


def _metadata(**overrides: object) -> GitHubRepositoryMetadata:
    values: dict[str, object] = {
        "identity": _IDENTITY,
        "github_id": 123456,
        "html_url": _VALID_HTML_URL,
        "private": False,
        "default_branch": "main",
    }
    values.update(overrides)
    return GitHubRepositoryMetadata.model_validate(values)


def test_valid_metadata_constructs_successfully() -> None:
    """A well-formed, canonical metadata value constructs without error."""
    metadata = _metadata()

    assert metadata.identity == _IDENTITY
    assert metadata.github_id == 123456
    assert metadata.default_branch == "main"
    assert metadata.private is False


@pytest.mark.parametrize("default_branch", ["", "   ", "\t"])
def test_blank_default_branch_is_rejected(default_branch: str) -> None:
    """A whitespace-only or empty default branch is rejected directly on
    the exported model, not only by the adapter's private response model."""
    with pytest.raises(ValidationError):
        _metadata(default_branch=default_branch)


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/wrong-owner/example-repo",
        "https://github.com/octo-org/wrong-repo",
        "http://github.com/octo-org/example-repo",
        "https://evil.example.com/octo-org/example-repo",
        "https://github.com/octo-org/example-repo?ref=1",
        "https://github.com/octo-org/example-repo#readme",
        "https://github.com/octo-org/example-repo/extra",
        "https://user:pass@github.com/octo-org/example-repo",
        "https://github.com:8443/octo-org/example-repo",
    ],
)
def test_non_canonical_html_url_is_rejected(html_url: str) -> None:
    """An `html_url` unrelated to (or not exactly matching) `identity` is
    rejected directly on the exported model."""
    with pytest.raises(ValidationError):
        _metadata(html_url=html_url)


@pytest.mark.parametrize("github_id", [True, False, "123456", 0, -1])
def test_github_id_must_be_a_positive_strict_integer(github_id: object) -> None:
    """A boolean, numeric string, zero, or negative value is rejected for
    the GitHub numeric ID."""
    with pytest.raises(ValidationError):
        _metadata(github_id=github_id)


@pytest.mark.parametrize("private", ["false", "true", 0, 1, None])
def test_private_must_be_a_strict_boolean(private: object) -> None:
    """A string, integer, or `None` is rejected for the strict boolean
    `private` field."""
    with pytest.raises(ValidationError):
        _metadata(private=private)


def test_metadata_rejects_unknown_fields() -> None:
    """An unexpected extra field is rejected rather than silently ignored."""
    with pytest.raises(ValidationError):
        _metadata(unexpected_field="value")


def test_metadata_is_frozen() -> None:
    """A constructed metadata value cannot be mutated in place."""
    metadata = _metadata()

    with pytest.raises(ValidationError):
        metadata.default_branch = "develop"  # type: ignore[misc]


def test_metadata_model_dump_round_trip() -> None:
    """`model_dump()` and `model_dump_json()` preserve a valid metadata value."""
    metadata = _metadata()

    dumped = metadata.model_dump()
    assert GitHubRepositoryMetadata.model_validate(dumped) == metadata

    dumped_json = metadata.model_dump_json()
    assert GitHubRepositoryMetadata.model_validate(json.loads(dumped_json)) == metadata
    assert GitHubRepositoryMetadata.model_validate_json(dumped_json) == metadata
