"""Tests for the platform-independent `RepositoryIdentity` model.

GitHub-specific reference parsing (format rules, length limits, URL-style
rejection) lives behind the adapter boundary and is tested in
`test_github_reference.py`.
"""

import json

import pytest
from pydantic import ValidationError

from reporationale.domain import RepositoryIdentity


def test_repository_identity_accepts_valid_direct_construction() -> None:
    """Direct construction accepts a well-formed platform-independent identity."""
    identity = RepositoryIdentity(platform="github", owner="octo", name="hello-world")

    assert identity.owner == "octo"
    assert identity.name == "hello-world"
    assert identity.repository == "octo/hello-world"


@pytest.mark.parametrize("field_name", ["owner", "name"])
@pytest.mark.parametrize(
    "value",
    ["", "   ", "\t", "octo/repo", "oct o", " octo", "octo "],
)
def test_repository_identity_rejects_invalid_segments(
    field_name: str, value: str
) -> None:
    """Blank, whitespace-only, whitespace-containing, or '/'-containing
    segments are rejected regardless of platform, independent of any
    GitHub-specific length or character rule."""
    values: dict[str, object] = {"platform": "github", "owner": "octo", "name": "repo"}
    values[field_name] = value

    with pytest.raises(ValidationError):
        RepositoryIdentity.model_validate(values)


def test_repository_identity_model_dump_round_trip() -> None:
    """`model_dump()` and `model_dump_json()` preserve a valid identity."""
    identity = RepositoryIdentity(platform="github", owner="psf", name="black")

    dumped = identity.model_dump()
    assert dumped == {"platform": "github", "owner": "psf", "name": "black"}

    dumped_json = identity.model_dump_json()
    assert json.loads(dumped_json) == dumped
    assert RepositoryIdentity.model_validate(json.loads(dumped_json)) == identity
