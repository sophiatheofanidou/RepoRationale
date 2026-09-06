"""Tests for `GitHubClient.get_paginated_collection` and Link-header parsing.

No real network access is used; every response comes from an inline mock
handler over a mocked `httpx` transport.
"""

import httpx
import pytest
from github_test_support import json_response, mock_transport

from reporationale.adapters.github import (
    GitHubMalformedResponse,
    GitHubPaginationCycleDetected,
    GitHubRateLimited,
    GitHubUntrustedOriginRejected,
)
from reporationale.adapters.github.client import GitHubClient
from reporationale.adapters.github.pagination import parse_link_header

_ISSUES_PATH = "/repos/octo-org/example-repo/issues"


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(transport=handler)


def test_get_paginated_collection_returns_empty_for_empty_page() -> None:
    """An empty single page yields an empty collection."""
    handler = mock_transport(lambda request: json_response(200, []))

    items = _client(handler).get_paginated_collection(_ISSUES_PATH)

    assert items == []


def test_get_paginated_collection_returns_single_page_items() -> None:
    """A single page with no `next` link yields exactly its own items."""
    handler = mock_transport(lambda request: json_response(200, [{"id": 10}]))

    items = _client(handler).get_paginated_collection(_ISSUES_PATH)

    assert items == [{"id": 10}]


def test_get_paginated_collection_follows_multiple_pages_in_order() -> None:
    """Items from every page are concatenated in API order."""
    next_url = "https://api.github.com/repos/octo-org/example-repo/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("page=2"):
            return json_response(200, [{"id": 3}])
        return json_response(
            200, [{"id": 1}, {"id": 2}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    items = _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert items == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_get_paginated_collection_rejects_non_array_page_shape() -> None:
    """A page whose JSON body is not a list is reported as malformed, and
    (A3) the raised error carries neither `__cause__` nor `__context__`."""
    handler = mock_transport(
        lambda request: json_response(200, {"message": "not a list"})
    )

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        _client(handler).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_get_paginated_collection_rejects_non_object_page_items() -> None:
    """A page whose array contains a non-object item is reported as
    malformed."""
    handler = mock_transport(
        lambda request: json_response(200, [{"id": 1}, "not-an-object"])
    )

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).get_paginated_collection(_ISSUES_PATH)


def test_get_paginated_collection_rejects_invalid_json_without_context_leak() -> None:
    """Regression test (A3): invalid JSON in a collection page must not
    leave the original `JSONDecodeError` reachable via `__cause__` or
    `__context__`, since it retains the raw response body via `.doc`."""
    handler = mock_transport(
        lambda request: httpx.Response(200, content=b"not valid json{")
    )

    with pytest.raises(GitHubMalformedResponse) as excinfo:
        _client(handler).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_get_paginated_collection_rejects_a_pagination_cycle() -> None:
    """A `next` link that repeats an already-fetched page is rejected."""
    next_url = "https://api.github.com/repos/octo-org/example-repo/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubPaginationCycleDetected) as excinfo:
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.url == next_url


def test_get_paginated_collection_rejects_unexpected_external_host_link() -> None:
    """A `next` link to a host other than the GitHub API host is refused."""
    calls: list[str] = []
    external_url = "https://evil.example.com/repos/octo-org/example-repo/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{external_url}>; rel="next"'}
        )

    with pytest.raises(GitHubUntrustedOriginRejected) as excinfo:
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.url == external_url
    assert len(calls) == 1


def test_get_paginated_collection_rejects_http_downgrade_to_same_host() -> None:
    """A `next` link that downgrades to plain HTTP, even to the same
    hostname, is refused before any request is sent to it."""
    calls: list[str] = []
    downgraded_url = "http://api.github.com/repos/octo-org/example-repo/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{downgraded_url}>; rel="next"'}
        )

    with pytest.raises(GitHubUntrustedOriginRejected) as excinfo:
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.url == downgraded_url
    assert len(calls) == 1


def test_get_paginated_collection_rejects_unexpected_port_link() -> None:
    """A `next` link to an unexpected port on the trusted host is refused."""
    calls: list[str] = []
    unexpected_port_url = (
        "https://api.github.com:8443/repos/octo-org/example-repo/issues?page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{unexpected_port_url}>; rel="next"'}
        )

    with pytest.raises(GitHubUntrustedOriginRejected):
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert len(calls) == 1


def test_get_paginated_collection_rejects_userinfo_in_link() -> None:
    """A `next` link containing userinfo is refused before any request."""
    calls: list[str] = []
    userinfo_url = (
        "https://user:pass@api.github.com/repos/octo-org/example-repo/issues?page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{userinfo_url}>; rel="next"'}
        )

    with pytest.raises(GitHubUntrustedOriginRejected):
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert len(calls) == 1


def test_get_paginated_collection_surfaces_rate_limit_on_a_later_page() -> None:
    """A rate limit reached mid-pagination surfaces the same typed failure."""
    next_url = "https://api.github.com/repos/octo-org/example-repo/issues?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("page=2"):
            return httpx.Response(
                429,
                json={"message": "secondary rate limit"},
                headers={"Retry-After": "15"},
            )
        return json_response(
            200, [{"id": 1}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubRateLimited) as excinfo:
        _client(mock_transport(handler)).get_paginated_collection(_ISSUES_PATH)

    assert excinfo.value.retry_after_seconds == 15.0


def test_parse_link_header_returns_empty_mapping_for_missing_header() -> None:
    """A missing or empty `Link` header parses to no links."""
    assert parse_link_header(None) == {}
    assert parse_link_header("") == {}


def test_parse_link_header_parses_a_single_relation() -> None:
    """A single-relation header parses to one mapping entry."""
    header = '<https://api.github.com/resource?page=2>; rel="next"'

    assert parse_link_header(header) == {
        "next": "https://api.github.com/resource?page=2"
    }


def test_parse_link_header_parses_multiple_relations() -> None:
    """A multi-relation header preserves every relation."""
    header = (
        '<https://api.github.com/resource?page=2>; rel="next", '
        '<https://api.github.com/resource?page=9>; rel="last"'
    )

    assert parse_link_header(header) == {
        "next": "https://api.github.com/resource?page=2",
        "last": "https://api.github.com/resource?page=9",
    }


def test_parse_link_header_ignores_malformed_segments() -> None:
    """A segment without the expected `<url>; rel="..."` shape is ignored."""
    assert parse_link_header("not-a-valid-link-header") == {}
