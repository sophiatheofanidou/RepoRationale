"""Tests for the platform-independent preflight result contract.

The workflow that produces a `PreflightResult` for a user-supplied
reference (parsing it and looking it up on GitHub) lives in
`reporationale.application.preflight` and is tested in
`test_application_preflight.py`, since it depends on the GitHub adapter.
"""

import json

import pytest
from pydantic import ValidationError

from reporationale.domain import PreflightResult, RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="psf", name="black")


def test_ready_result_requires_repository_identity() -> None:
    """The `ready` outcome must carry the identified repository."""
    PreflightResult(status="ready", repository=_IDENTITY)

    with pytest.raises(ValidationError, match="requires a repository identity"):
        PreflightResult(status="ready")


def test_indexing_required_result_requires_repository_identity() -> None:
    """The `indexing_required` outcome must carry the identified repository."""
    PreflightResult(status="indexing_required", repository=_IDENTITY)

    with pytest.raises(ValidationError, match="requires a repository identity"):
        PreflightResult(status="indexing_required")


def test_unsupported_result_requires_reason_and_message() -> None:
    """The `unsupported` outcome must explain why the repository was rejected."""
    PreflightResult(status="unsupported", reason_code="empty_reference", message="x")

    with pytest.raises(ValidationError, match="requires a reason_code and message"):
        PreflightResult(status="unsupported")


def test_ready_result_rejects_a_rejection_reason() -> None:
    """A successful outcome must not also carry rejection details."""
    with pytest.raises(ValidationError, match="must not carry a rejection reason"):
        PreflightResult(
            status="ready",
            repository=_IDENTITY,
            reason_code="empty_reference",
            message="x",
        )


@pytest.mark.parametrize("reason_code", ["", "   ", "\t"])
def test_unsupported_result_rejects_blank_reason_code(reason_code: str) -> None:
    """A whitespace-only or empty reason_code is not a valid machine reason."""
    with pytest.raises(ValidationError):
        PreflightResult(status="unsupported", reason_code=reason_code, message="x")


@pytest.mark.parametrize("message", ["", "   ", "\t"])
def test_unsupported_result_rejects_blank_message(message: str) -> None:
    """A whitespace-only or empty message does not explain the rejection."""
    with pytest.raises(ValidationError):
        PreflightResult(
            status="unsupported", reason_code="empty_reference", message=message
        )


@pytest.mark.parametrize(
    "reason_code",
    ["Empty_Reference", "empty-reference", "1empty_reference", "empty reference"],
)
def test_unsupported_result_rejects_non_snake_case_reason_code(
    reason_code: str,
) -> None:
    """Reason codes must stay machine-readable lower-snake-case identifiers."""
    with pytest.raises(ValidationError):
        PreflightResult(status="unsupported", reason_code=reason_code, message="x")


def test_only_three_preflight_statuses_are_accepted() -> None:
    """The contract preserves exactly the three agreed outcomes."""
    PreflightResult(status="ready", repository=_IDENTITY)
    PreflightResult(status="indexing_required", repository=_IDENTITY)
    PreflightResult(status="unsupported", reason_code="empty_reference", message="x")

    with pytest.raises(ValidationError):
        PreflightResult.model_validate({"status": "rejected", "repository": None})


def test_preflight_result_model_dump_round_trip() -> None:
    """`model_dump()` and `model_dump_json()` preserve a nested identity."""
    result = PreflightResult(status="indexing_required", repository=_IDENTITY)

    dumped = result.model_dump()
    assert dumped == {
        "status": "indexing_required",
        "repository": {"platform": "github", "owner": "psf", "name": "black"},
        "reason_code": None,
        "message": None,
    }

    dumped_json = result.model_dump_json()
    assert json.loads(dumped_json) == dumped
    assert PreflightResult.model_validate(json.loads(dumped_json)) == result
