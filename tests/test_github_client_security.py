"""Regression tests for the GitHub adapter's trusted-origin, redirect, and
outgoing-request security boundary (A1, A2, and A5).

No real network access is used; every response comes from an inline mock
handler over a mocked `httpx` transport, and only a synthetic token is ever
used.
"""

import httpx
import pytest
from github_test_support import json_response, load_fixture, mock_transport

from reporationale.adapters.github import (
    GitHubClient,
    GitHubRedirectCycleDetected,
    GitHubTooManyRedirects,
    GitHubUntrustedOriginRejected,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_ISSUES_PATH = "/repos/octo-org/example-repo/issues"


def _client(
    handler: httpx.MockTransport, *, token: str | None = _TOKEN
) -> GitHubClient:
    return GitHubClient(token=token, transport=handler)


# --- Configured base_url validation (A1) -----------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.com",
        "https://evil.example.com",
        "https://user:pass@api.github.com",
        "https://api.github.com:8443",
        "https://api.github.com/some/path",
        "https://api.github.com?query=1",
        "https://api.github.com#fragment",
        "https://API.GITHUB.COM.evil.example.com",
    ],
)
def test_invalid_base_url_is_rejected_before_any_request(base_url: str) -> None:
    """An untrusted or malformed configured origin fails at construction,
    before any request can possibly be sent."""
    with pytest.raises(ValueError):
        GitHubClient(token=_TOKEN, base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    ["https://api.github.com", "https://API.GITHUB.COM", "https://api.github.com:443/"],
)
def test_valid_base_url_variants_are_accepted(base_url: str) -> None:
    """Case-insensitive hostname, an explicit default port, and a trailing
    slash are all tolerated."""
    handler = mock_transport(
        lambda request: json_response(200, load_fixture("repository_public.json"))
    )
    client = GitHubClient(token=_TOKEN, base_url=base_url, transport=handler)

    metadata = client.get_repository(_IDENTITY)

    assert metadata.identity.owner == "octo-org"


# --- Initial request-target validation (A1) --------------------------------


@pytest.mark.parametrize(
    "initial_url",
    [
        "https://evil.example.com/items",
        "http://api.github.com/repos/octo-org/example-repo/issues",
        "//evil.example.com/items",
    ],
)
def test_get_paginated_collection_rejects_untrusted_initial_url(
    initial_url: str,
) -> None:
    """An external, HTTP-downgraded, or scheme-relative initial pagination
    URL is rejected before any request is sent, and never receives the
    bearer token."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("no request should be sent for an untrusted initial URL")

    with pytest.raises(GitHubUntrustedOriginRejected):
        _client(mock_transport(handler)).get_paginated_collection(initial_url)

    assert calls == []


def test_get_paginated_collection_accepts_trusted_relative_initial_path() -> None:
    """A genuinely relative initial path still works, resolved against the
    trusted configured origin."""
    handler = mock_transport(lambda request: json_response(200, [{"id": 1}]))

    items = _client(handler).get_paginated_collection(_ISSUES_PATH)

    assert items == [{"id": 1}]


def test_get_paginated_collection_accepts_trusted_absolute_initial_url() -> None:
    """An absolute initial URL that is already the trusted GitHub API origin
    is accepted and requested normally."""
    handler = mock_transport(lambda request: json_response(200, [{"id": 1}]))

    items = _client(handler).get_paginated_collection(
        f"https://api.github.com{_ISSUES_PATH}"
    )

    assert items == [{"id": 1}]


def test_untrusted_initial_url_never_receives_authorization() -> None:
    """Even if a handler were reached, no path in the client can attach the
    bearer token before the trusted-origin check runs; this asserts the
    check raises strictly before `_send_once`/`_auth_headers` execute."""
    calls: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("Authorization")))
        return json_response(200, [])

    with pytest.raises(GitHubUntrustedOriginRejected):
        _client(mock_transport(handler)).get_paginated_collection(
            "https://evil.example.com/items"
        )

    assert calls == []


# --- Redirect trusted-origin enforcement -----------------------------------


def test_same_origin_relative_redirect_is_followed() -> None:
    """A same-origin, relative redirect (the common rename/transfer case) is
    followed to completion."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/octo-org/example-repo":
            return httpx.Response(
                301, headers={"Location": "/repos/new-owner/new-repo"}
            )
        return json_response(200, load_fixture("repository_renamed.json"))

    metadata = _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert calls == ["/repos/octo-org/example-repo", "/repos/new-owner/new-repo"]
    assert metadata.identity.owner == "new-owner-name"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/repos/octo-org/example-repo",
        "http://api.github.com/repos/octo-org/example-repo",
        "https://user:pass@api.github.com/repos/octo-org/example-repo",
        "https://api.github.com:8443/repos/octo-org/example-repo",
    ],
)
def test_untrusted_redirect_target_is_rejected_and_never_requested(
    location: str,
) -> None:
    """An external host, an HTTP downgrade, userinfo, and an unexpected port
    are all rejected before a second request is ever sent."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(301, headers={"Location": location})

    with pytest.raises(GitHubUntrustedOriginRejected) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert excinfo.value.url == location
    # Only the first (legitimate, same-origin) request was ever sent; the
    # rejected target received zero requests and therefore no token.
    assert len(calls) == 1


def test_redirect_cycle_is_detected() -> None:
    """A redirect chain that returns to an already-visited target is rejected
    rather than looping forever."""
    calls: list[str] = []
    target = "https://api.github.com/repos/new-owner/new-repo"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(301, headers={"Location": target})

    with pytest.raises(GitHubRedirectCycleDetected) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert excinfo.value.url == target
    assert len(calls) == 2


def test_redirect_limit_is_enforced_for_a_non_repeating_chain() -> None:
    """A long, non-repeating redirect chain is rejected once the small
    explicit redirect bound is exceeded, even without an exact cycle."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        current_hop = int(url.rsplit("hop=", 1)[1]) if "hop=" in url else 0
        if current_hop >= 6:
            raise AssertionError(
                f"unexpected request for hop={current_hop}; "
                "the redirect limit should have been enforced earlier"
            )
        next_hop = current_hop + 1
        return httpx.Response(
            301,
            headers={
                "Location": (
                    f"https://api.github.com/repos/octo-org/example-repo?hop={next_hop}"
                )
            },
        )

    with pytest.raises(GitHubTooManyRedirects) as excinfo:
        _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert excinfo.value.limit == 5
    # Six requests are actually sent (the initial one plus hop=1..5); the
    # seventh target (hop=6) is rejected before ever being requested.
    assert len(calls) == 6


# --- Outgoing request shape (headers, auth, timeout, path encoding) --------


def test_outgoing_request_has_required_headers_and_bearer_token() -> None:
    """A trusted request carries the expected Accept, API version, User-Agent,
    and Authorization headers, and only a synthetic token is ever used."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers.get("accept", "")
        captured["api-version"] = request.headers.get("x-github-api-version", "")
        captured["user-agent"] = request.headers.get("user-agent", "")
        captured["authorization"] = request.headers.get("authorization", "")
        return json_response(200, load_fixture("repository_public.json"))

    _client(mock_transport(handler)).get_repository(_IDENTITY)

    assert captured["accept"] == "application/vnd.github+json"
    assert captured["api-version"] == "2022-11-28"
    assert captured["user-agent"] == "RepoRationale-GitHub-Adapter/0.1"
    assert captured["authorization"] == f"Bearer {_TOKEN}"


def test_outgoing_request_omits_authorization_without_a_token() -> None:
    """No Authorization header is sent when the client has no token."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["has_authorization"] = str("authorization" in request.headers)
        return json_response(200, load_fixture("repository_public.json"))

    _client(mock_transport(handler), token=None).get_repository(_IDENTITY)

    assert captured["has_authorization"] == "False"


def test_client_uses_the_configured_explicit_timeout() -> None:
    """The client is configured with the given explicit timeout, not an
    implicit library default."""
    handler = mock_transport(lambda request: json_response(200, {}))
    client = GitHubClient(timeout=3.5, transport=handler)

    assert client._client.timeout == httpx.Timeout(3.5)


@pytest.mark.parametrize(
    ("owner", "name", "expected_path"),
    [
        ("octo#org", "example-repo", "/repos/octo%23org/example-repo"),
        ("octo-org", "repo?name", "/repos/octo-org/repo%3Fname"),
        ("octo's", "repo", "/repos/octo%27s/repo"),
    ],
)
def test_owner_and_name_are_percent_encoded_in_the_path(
    owner: str, name: str, expected_path: str
) -> None:
    """Owner and repository name are percent-encoded into the URL path, so a
    character with special meaning in a URL cannot alter its structure.

    The mock response is unrelated to the requested identity: this test only
    checks what path was actually requested on the wire, not response
    normalization (covered separately in `test_github_repository_lookup.py`).
    """
    identity = RepositoryIdentity(platform="github", owner=owner, name=name)
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path.decode()
        return json_response(200, load_fixture("repository_public.json"))

    _client(mock_transport(handler)).get_repository(identity)

    assert captured["raw_path"].split("?", 1)[0] == expected_path
