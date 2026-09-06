"""Integration tests for the application preflight workflow.

Covers the full path from a user-supplied `owner/repository` reference
through GitHub repository lookup to a `PreflightResult`, or to a
`PreflightUnavailable` operational failure. Only a mocked `httpx` transport
and deterministic fixtures are used; no real network access or `GITHUB_TOKEN`
is used anywhere in this file.
"""

import httpx
import pytest
from github_test_support import json_response, load_fixture, mock_transport

from reporationale.adapters.github import GitHubClient
from reporationale.application import (
    PreflightUnavailable,
    preflight_repository_reference,
)
from reporationale.domain import PreflightResult

_SECRET_TOKEN = "super-secret-test-token-value"


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(token=_SECRET_TOKEN, transport=handler)


def test_valid_public_repository_reports_indexing_required() -> None:
    """A valid, accessible public repository resolves to `indexing_required`."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )

    result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(handler)
    )

    assert result.status == "indexing_required"
    assert result.repository is not None
    assert result.repository.owner == "octo-org"
    assert result.repository.name == "example-repo"
    assert result.reason_code is None
    assert result.message is None


def test_renamed_repository_reports_its_canonical_identity() -> None:
    """A renamed/transferred repository's canonical identity is returned,
    not the one the user originally typed."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_renamed.json"))
    )

    result = preflight_repository_reference(
        "requested-owner/requested-repo", github_client=_client(handler)
    )

    assert result.status == "indexing_required"
    assert result.repository is not None
    assert result.repository.owner == "new-owner-name"
    assert result.repository.name == "new-repo-name"


def test_malformed_reference_reports_unsupported_without_a_request() -> None:
    """A malformed reference is rejected before any HTTP request is made."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("no request should be sent for a malformed reference")

    result = preflight_repository_reference(
        "not-a-valid-reference", github_client=_client(mock_transport(handler))
    )

    assert result.status == "unsupported"
    assert result.repository is None
    assert result.reason_code == "missing_separator"
    assert result.message
    assert calls == []


def test_missing_repository_reports_unsupported_with_stable_reason() -> None:
    """A repository GitHub cannot find or access is reported as unsupported."""
    handler = mock_transport(
        lambda request: json_response(404, {"message": "Not Found"})
    )

    result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(handler)
    )

    assert result.status == "unsupported"
    assert result.repository is None
    assert result.reason_code == "repository_not_found_or_inaccessible"
    assert result.message


def test_private_repository_reports_unsupported_with_stable_reason() -> None:
    """An accessible but private repository is reported as unsupported."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_private.json"))
    )

    result = preflight_repository_reference(
        "octo-org/internal-repo", github_client=_client(handler)
    )

    assert result.status == "unsupported"
    assert result.repository is None
    assert result.reason_code == "private_repository"
    assert result.message


def test_authentication_failure_raises_preflight_unavailable() -> None:
    """A GitHub authentication failure is an operational failure, not an
    unsupported-repository outcome."""
    handler = mock_transport(lambda request: json_response(401, {"message": "Bad"}))

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_authentication_failed"
    assert excinfo.value.message


def test_primary_rate_limit_raises_preflight_unavailable_with_timing() -> None:
    """A primary rate limit surfaces as an operational failure with safe
    reset timing."""
    handler = mock_transport(
        lambda request: json_response(
            403,
            {"message": "API rate limit exceeded"},
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1735689600",
            },
        )
    )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_rate_limited"
    assert excinfo.value.reset_at is not None
    assert excinfo.value.reset_at.timestamp() == 1735689600


def test_secondary_rate_limit_raises_preflight_unavailable_with_retry_after() -> None:
    """A secondary rate limit surfaces as an operational failure with a safe
    retry delay."""
    handler = mock_transport(
        lambda request: json_response(
            403,
            {"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "42"},
        )
    )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_rate_limited"
    assert excinfo.value.retry_after_seconds == 42.0


def test_network_failure_raises_preflight_unavailable() -> None:
    """A transport-level failure is an operational failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(mock_transport(handler))
        )

    assert excinfo.value.reason_code == "github_unavailable"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_malformed_github_response_raises_preflight_unavailable() -> None:
    """A malformed GitHub response is an operational failure."""
    handler = mock_transport(
        lambda request: httpx.Response(200, content=b"not valid json{")
    )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_malformed_response"


def test_unexpected_github_response_raises_preflight_unavailable() -> None:
    """A non-success response not otherwise classified is an operational
    failure."""
    handler = mock_transport(lambda request: json_response(500, {"message": "oops"}))

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_unexpected_response"


def test_untrusted_redirect_maps_to_preflight_unavailable() -> None:
    """A redirect to an untrusted origin is an adapter-level protocol
    failure, not an unsupported-repository outcome, so it must not escape
    the application workflow as a raw adapter exception."""
    handler = mock_transport(
        lambda request: httpx.Response(
            301, headers={"Location": "https://evil.example.com/repos/x/y"}
        )
    )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_unexpected_response"


def test_redirect_cycle_maps_to_preflight_unavailable() -> None:
    """A redirect cycle is an operational failure, not an unsupported
    outcome."""
    target = "https://api.github.com/repos/new-owner/new-repo"
    handler = mock_transport(
        lambda request: httpx.Response(301, headers={"Location": target})
    )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )

    assert excinfo.value.reason_code == "github_unexpected_response"


def test_too_many_redirects_maps_to_preflight_unavailable() -> None:
    """A redirect chain exceeding the small explicit bound is an operational
    failure, not an unsupported outcome."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        current_hop = int(url.rsplit("hop=", 1)[1]) if "hop=" in url else 0
        next_hop = current_hop + 1
        return httpx.Response(
            301,
            headers={
                "Location": (
                    f"https://api.github.com/repos/octo-org/example-repo?hop={next_hop}"
                )
            },
        )

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(mock_transport(handler))
        )

    assert excinfo.value.reason_code == "github_unexpected_response"


def test_ready_is_never_returned_by_the_workflow() -> None:
    """This slice has no snapshot mechanism, so `ready` is never produced,
    for either a successful or a rejected reference."""
    success_handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    success_result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(success_handler)
    )
    assert success_result.status != "ready"

    unsupported_handler = mock_transport(
        lambda request: json_response(404, {"message": "Not Found"})
    )
    unsupported_result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(unsupported_handler)
    )
    assert unsupported_result.status != "ready"


def test_only_the_three_agreed_statuses_are_ever_returned() -> None:
    """Every possible outcome from this workflow is a valid `PreflightResult`
    whose status is one of exactly the three agreed values."""
    public_handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    not_found_handler = mock_transport(
        lambda request: json_response(404, {"message": "x"})
    )

    for handler in (public_handler, not_found_handler):
        result = preflight_repository_reference(
            "octo-org/example-repo", github_client=_client(handler)
        )
        assert isinstance(result, PreflightResult)
        assert result.status in ("ready", "indexing_required", "unsupported")


def test_no_result_or_exception_exposes_the_token() -> None:
    """Neither a `PreflightResult` nor a `PreflightUnavailable` (nor its
    exception chain) ever exposes the synthetic token."""
    success_handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(success_handler)
    )
    assert _SECRET_TOKEN not in repr(result)
    assert _SECRET_TOKEN not in result.model_dump_json()

    def network_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {_SECRET_TOKEN}"
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo",
            github_client=_client(mock_transport(network_handler)),
        )

    error = excinfo.value
    assert _SECRET_TOKEN not in str(error)
    assert _SECRET_TOKEN not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert not hasattr(error, "request")
    assert not hasattr(error, "response")


def test_no_httpx_object_crosses_the_application_boundary() -> None:
    """The application layer never returns or raises a raw `httpx` type."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    result = preflight_repository_reference(
        "octo-org/example-repo", github_client=_client(handler)
    )
    assert not isinstance(result, httpx.Response)
    assert not isinstance(result, httpx.Request)

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(PreflightUnavailable) as excinfo:
        preflight_repository_reference(
            "octo-org/example-repo",
            github_client=_client(mock_transport(error_handler)),
        )
    for attribute_value in vars(excinfo.value).values():
        assert not isinstance(attribute_value, httpx.Request)
        assert not isinstance(attribute_value, httpx.Response)
