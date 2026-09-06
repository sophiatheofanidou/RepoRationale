"""Tests for GitHub-specific `owner/repository` reference parsing.

This behind-the-adapter parser performs no network access; it only
validates the format of a user-supplied reference.
"""

import pytest

from reporationale.adapters.github import (
    RepositoryReferenceRejected,
    parse_github_repository_reference,
)
from reporationale.domain import RepositoryIdentity


def test_valid_owner_repository_reference_parses() -> None:
    """A well-formed owner/repository reference resolves to a GitHub identity."""
    identity = parse_github_repository_reference("psf/black")

    assert identity == RepositoryIdentity(platform="github", owner="psf", name="black")
    assert identity.repository == "psf/black"


@pytest.mark.parametrize(
    "raw",
    [
        "octo/hello-world",
        "octo-org/hello_world",
        "octo/hello.world",
        "  octo/hello-world  ",
        "a/b",
    ],
)
def test_valid_reference_variants_are_accepted(raw: str) -> None:
    """Surrounding whitespace is tolerated; GitHub's naming rules are honored."""
    identity = parse_github_repository_reference(raw)

    assert identity.platform == "github"
    assert identity.owner and identity.name


@pytest.mark.parametrize(
    ("raw", "reason_code"),
    [
        ("", "empty_reference"),
        ("   ", "empty_reference"),
        ("octo", "missing_separator"),
        ("octo/repo/extra", "too_many_segments"),
        ("octo /repo", "invalid_reference_format"),
        ("octo/ repo", "invalid_reference_format"),
        ("-octo/repo", "invalid_owner_format"),
        ("octo-/repo", "invalid_owner_format"),
        ("oc--to/repo", "invalid_owner_format"),
        ("octo/.", "invalid_repository_name_format"),
        ("octo/..", "invalid_repository_name_format"),
        ("octo/repo.git", "invalid_repository_name_format"),
        ("octo/repo!", "invalid_repository_name_format"),
    ],
)
def test_malformed_references_are_rejected_with_reason(
    raw: str, reason_code: str
) -> None:
    """Malformed owner/repository input is rejected with a specific reason."""
    with pytest.raises(RepositoryReferenceRejected) as excinfo:
        parse_github_repository_reference(raw)

    assert excinfo.value.reason_code == reason_code
    assert excinfo.value.message


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/octo/hello-world",
        "http://github.com/octo/hello-world",
        "git@github.com:octo/hello-world.git",
        "ssh://git@github.com/octo/hello-world.git",
    ],
)
def test_unsupported_url_style_references_are_rejected(raw: str) -> None:
    """The MVP accepts owner/repository text only, not URLs or clone addresses."""
    with pytest.raises(RepositoryReferenceRejected) as excinfo:
        parse_github_repository_reference(raw)

    assert excinfo.value.reason_code == "unexpected_url_format"


def test_github_owner_length_boundary() -> None:
    """GitHub's 39-character owner limit is enforced only by the parser."""
    owner_39 = "a" * 39
    owner_40 = "a" * 40

    identity = parse_github_repository_reference(f"{owner_39}/repo")
    assert identity.owner == owner_39

    with pytest.raises(RepositoryReferenceRejected) as excinfo:
        parse_github_repository_reference(f"{owner_40}/repo")
    assert excinfo.value.reason_code == "invalid_owner_format"


def test_github_repository_name_length_boundary() -> None:
    """GitHub's 100-character repository-name limit is enforced by the parser."""
    name_100 = "a" * 100
    name_101 = "a" * 101

    identity = parse_github_repository_reference(f"octo/{name_100}")
    assert identity.name == name_100

    with pytest.raises(RepositoryReferenceRejected) as excinfo:
        parse_github_repository_reference(f"octo/{name_101}")
    assert excinfo.value.reason_code == "invalid_repository_name_format"


@pytest.mark.parametrize(
    ("raw", "reason_code"),
    [
        ("/repo", "invalid_owner_format"),
        ("octo/", "invalid_repository_name_format"),
    ],
)
def test_parse_rejects_empty_owner_or_repository_segment(
    raw: str, reason_code: str
) -> None:
    """An empty owner or repository segment is rejected with a clear reason."""
    with pytest.raises(RepositoryReferenceRejected) as excinfo:
        parse_github_repository_reference(raw)

    assert excinfo.value.reason_code == reason_code
