"""Tests for `GitHubClient.get_repository` against a mocked HTTP transport.

No real network access or real `GITHUB_TOKEN` is used anywhere in this file;
every response comes from a deterministic fixture or an inline mock handler.
"""

import httpx
import pytest
from github_test_support import (
    json_response,
    load_fixture,
    mock_transport,
    raw_response,
)

from reporationale.adapters.github import (
    GitHubAuthenticationFailed,
    GitHubClient,
    GitHubMalformedResponse,
    GitHubRateLimited,
    GitHubRepositoryNotFound,
    GitHubRepositoryPrivate,
    GitHubTransportError,
    GitHubUnexpectedResponse,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(token=_SECRET_TOKEN, transport=handler)


def test_get_repository_returns_normalized_public_metadata() -> None:
    """A successful lookup normalizes the required public repository fields."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )

    metadata = _client(handler).get_repository(_IDENTITY)

    assert metadata.identity == RepositoryIdentity(
        platform="github", owner="octo-org", name="example-repo"
    )
    assert metadata.github_id == 123456
    assert str(metadata.html_url) == "https://github.com/octo-org/example-repo"
    assert metadata.private is False
    assert metadata.default_branch == "main"


def test_get_repository_normalizes_canonical_owner_and_name() -> None:
    """The result uses GitHub's canonical owner/name, not the requested ones."""
    requested = RepositoryIdentity(
        platform="github", owner="requested-owner", name="requested-repo"
    )
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_renamed.json"))
    )

    metadata = _client(handler).get_repository(requested)

    assert metadata.identity.owner == "new-owner-name"
    assert metadata.identity.name == "new-repo-name"


def test_get_repository_follows_redirect_for_renamed_repository() -> None:
    """A 301 redirect to the new canonical location is followed transparently."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/octo-org/example-repo":
            return httpx.Response(
                301, headers={"Location": "/repos/new-owner-name/new-repo-name"}
            )
        if request.url.path == "/repos/new-owner-name/new-repo-name":
            return json_response(200, load_fixture("repository_renamed.json"))
        raise AssertionError(f"unexpected request path: {request.url.path}")

    metadata = _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert calls == [
        "/repos/octo-org/example-repo",
        "/repos/new-owner-name/new-repo-name",
    ]
    assert metadata.identity.owner == "new-owner-name"
    assert metadata.identity.name == "new-repo-name"


def test_before_request_hook_can_stop_before_sending_any_request() -> None:
    calls: list[str] = []

    class StopRequested(Exception):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return json_response(200, load_fixture("repository_public.json"))

    def stop_before_request() -> None:
        raise StopRequested

    client = GitHubClient(
        token=_SECRET_TOKEN,
        transport=mock_transport(handler),
        before_request=stop_before_request,
    )

    with pytest.raises(StopRequested):
        client.get_repository(_IDENTITY)

    assert calls == []
    assert client.request_count == 0


def test_before_request_hook_runs_for_every_redirect_hop() -> None:
    checks = 0
    calls: list[str] = []

    def before_request() -> None:
        nonlocal checks
        checks += 1

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return raw_response(
                301,
                b"",
                headers={"Location": "/repos/new-owner-name/new-repo-name"},
            )
        return json_response(200, load_fixture("repository_renamed.json"))

    client = GitHubClient(
        token=_SECRET_TOKEN,
        transport=mock_transport(handler),
        before_request=before_request,
    )
    client.get_repository(_IDENTITY)

    assert checks == 2
    assert len(calls) == 2


def test_get_repository_rejects_non_github_identity_before_any_request() -> None:
    """An identity for a platform other than GitHub is rejected without a call."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("no request should be sent for a non-GitHub identity")

    other_platform_identity = RepositoryIdentity(
        platform="gitlab", owner="octo-org", name="example-repo"
    )

    with pytest.raises(ValueError, match="platform must be 'github'"):
        _client(mock_transport(handler)).get_repository(other_platform_identity)

    assert calls == []


def test_get_repository_rejects_accessible_private_repository() -> None:
    """A private repository is rejected even though the lookup succeeded."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_private.json"))
    )

    with pytest.raises(GitHubRepositoryPrivate) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.repository == "octo-org/internal-repo"


def test_get_repository_raises_not_found_for_404() -> None:
    """A 404 response is reported as not-found or inaccessible."""
    handler = mock_transport(
        lambda request: json_response(404, {"message": "Not Found"})
    )

    with pytest.raises(GitHubRepositoryNotFound) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.status_code == 404


def test_get_repository_raises_authentication_failed_for_401() -> None:
    """A 401 response is reported as an authentication failure."""
    handler = mock_transport(
        lambda request: json_response(401, {"message": "Bad credentials"})
    )

    with pytest.raises(GitHubAuthenticationFailed) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.status_code == 401


def test_get_repository_raises_rate_limited_for_403_with_rate_limit_headers() -> None:
    """A 403 with exhausted primary rate-limit headers is reported as rate-limited."""
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

    with pytest.raises(GitHubRateLimited) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.status_code == 403
    assert excinfo.value.reset_at is not None
    assert excinfo.value.reset_at.timestamp() == 1735689600


def test_get_repository_raises_rate_limited_for_429_with_retry_after() -> None:
    """A 429 secondary rate-limit response preserves the retry delay."""
    handler = mock_transport(
        lambda request: json_response(
            429, {"message": "secondary rate limit"}, headers={"Retry-After": "30"}
        )
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after_seconds == 30.0
    assert excinfo.value.reset_at is None


def test_get_repository_raises_rate_limited_for_secondary_403_with_retry_after() -> (
    None
):
    """A secondary-limit 403 with only `Retry-After` (no exhausted primary
    header) is still classified as rate-limited."""
    handler = mock_transport(
        lambda request: json_response(
            403,
            {"message": "You have exceeded a secondary rate limit"},
            headers={"Retry-After": "20"},
        )
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.retry_after_seconds == 20.0


def test_get_repository_raises_rate_limited_for_403_with_nonzero_remaining() -> None:
    """A secondary-limit 403 is still rate-limited even when
    `X-RateLimit-Remaining` is present but not exhausted."""
    handler = mock_transport(
        lambda request: json_response(
            403,
            {"message": "You have exceeded a secondary rate limit"},
            headers={"X-RateLimit-Remaining": "42"},
        )
    )

    with pytest.raises(GitHubRateLimited):
        _client(handler).get_repository(_IDENTITY)


def test_get_repository_raises_rate_limited_for_403_message_only() -> None:
    """A 403 whose body names a rate limit, with no rate-limit headers at
    all, is still classified as rate-limited rather than unexpected."""
    handler = mock_transport(
        lambda request: json_response(
            403, {"message": "API rate limit exceeded for user ID 123."}
        )
    )

    with pytest.raises(GitHubRateLimited) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.reset_at is None
    assert excinfo.value.retry_after_seconds is None


def test_get_repository_raises_unexpected_response_for_other_403() -> None:
    """A plain 403 without rate-limit headers is not mistaken for rate-limiting."""
    handler = mock_transport(
        lambda request: json_response(403, {"message": "Forbidden"})
    )

    with pytest.raises(GitHubUnexpectedResponse) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.status_code == 403


def test_get_repository_raises_malformed_response_for_invalid_json() -> None:
    """Invalid JSON in a 200 response is reported as a malformed response.

    Regression test (A3): the raised error must not carry the original
    `JSONDecodeError` (which retains the raw response body via `.doc`) on
    `__cause__` or `__context__`.
    """
    handler = mock_transport(lambda request: raw_response(200, b"not valid json{"))

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_get_repository_raises_malformed_response_for_missing_required_field() -> None:
    """A response missing a required field is reported as a malformed
    response, and the raised error does not carry the original Pydantic
    `ValidationError` on `__cause__` or `__context__`."""
    handler = mock_transport(
        lambda request: json_response(
            200, load_fixture("repository_missing_field.json")
        )
    )

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        _client(handler).get_repository(_IDENTITY)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "123456"},
        {"id": True},
        {"id": 0},
        {"id": -1},
        {"private": "false"},
        {"private": 0},
        {"default_branch": ""},
        {"default_branch": "   "},
        {"name": ""},
        {"owner": {"login": ""}},
        {"owner": {"login": "   "}},
    ],
)
def test_get_repository_rejects_malformed_field_types_and_values(
    overrides: dict[str, object],
) -> None:
    """A4 (strict validation): a coerced type or blank required value is
    rejected as malformed rather than silently accepted."""
    payload = {**load_fixture("repository_public.json"), **overrides}
    handler = mock_transport(lambda request: json_response(200, payload))

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).get_repository(_IDENTITY)


@pytest.mark.parametrize(
    "html_url",
    [
        "https://github.com/octo-org/wrong-repo-name",
        "https://github.com/wrong-owner/example-repo",
        "http://github.com/octo-org/example-repo",
        "https://evil.example.com/octo-org/example-repo",
        "https://github.com/octo-org/example-repo?ref=1",
        "https://github.com/octo-org/example-repo#readme",
        "https://github.com/octo-org/example-repo/extra",
        "https://user:pass@github.com/octo-org/example-repo",
        "https://github.com:8443/octo-org/example-repo",
    ],
)
def test_get_repository_rejects_non_canonical_html_url(html_url: str) -> None:
    """A4 (strict validation): `html_url` must be the exact canonical
    `https://github.com/<owner>/<name>` URL for the returned owner/name."""
    payload = {**load_fixture("repository_public.json"), "html_url": html_url}
    handler = mock_transport(lambda request: json_response(200, payload))

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).get_repository(_IDENTITY)


def test_get_repository_raises_transport_error_for_network_failure() -> None:
    """A connection failure is reported through the typed transport error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GitHubTransportError) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert excinfo.value.__cause__ is None


def test_get_repository_raises_transport_error_for_timeout() -> None:
    """A request timeout is reported through the same typed transport error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(GitHubTransportError) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert excinfo.value.__cause__ is None


def test_transport_error_chain_cannot_expose_the_authenticated_request() -> None:
    """Regression test (A1): the raised error's exception chain must not carry
    the original authenticated `httpx.Request` (and therefore not the bearer
    token) through `__cause__`, `__context__`, or any attribute."""

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx attaches the request that was actually sent (with our
        # Authorization header) to transport-level exceptions it raises.
        assert request.headers.get("Authorization") == f"Bearer {_SECRET_TOKEN}"
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GitHubTransportError) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    error = excinfo.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert vars(error) == {}
    assert _SECRET_TOKEN not in str(error)
    assert _SECRET_TOKEN not in repr(error)

    # Walk any exception chain that might still exist, defensively: even if
    # a future change reintroduced chaining, no reachable object may carry
    # the token.
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None:
        seen.append(current)
        current = current.__cause__ or current.__context__
    for exc in seen:
        assert _SECRET_TOKEN not in str(exc)
        assert _SECRET_TOKEN not in repr(exc)
        request_obj = getattr(exc, "request", None)
        if request_obj is not None:
            raise AssertionError(
                "an authenticated request object is reachable from the error chain"
            )


def test_token_never_appears_in_results_or_exception_text() -> None:
    """Neither a successful result nor a raised failure ever exposes the token."""
    success_handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    metadata = _client(success_handler).get_repository(_IDENTITY)

    assert not isinstance(metadata, httpx.Response)
    assert _SECRET_TOKEN not in repr(metadata)
    assert _SECRET_TOKEN not in str(metadata)
    assert _SECRET_TOKEN not in metadata.model_dump_json()

    failure_cases: list[tuple[httpx.MockTransport, type[Exception]]] = [
        (
            mock_transport(
                lambda request: json_response(404, {"message": "Not Found"})
            ),
            GitHubRepositoryNotFound,
        ),
        (
            mock_transport(lambda request: json_response(401, {"message": "Bad"})),
            GitHubAuthenticationFailed,
        ),
        (
            mock_transport(
                lambda request: json_response(
                    403,
                    {"message": "rate limited"},
                    headers={"X-RateLimit-Remaining": "0"},
                )
            ),
            GitHubRateLimited,
        ),
        (
            mock_transport(lambda request: raw_response(200, b"not json")),
            GitHubMalformedResponse,
        ),
        (
            mock_transport(
                lambda request: json_response(
                    200, load_fixture("repository_private.json")
                )
            ),
            GitHubRepositoryPrivate,
        ),
    ]
    for handler, expected_error in failure_cases:
        with pytest.raises(expected_error) as excinfo:
            _client(handler).get_repository(_IDENTITY)
        assert _SECRET_TOKEN not in str(excinfo.value)
        assert _SECRET_TOKEN not in repr(excinfo.value)
