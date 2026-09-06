"""Tests for repository-admission workload estimation, preflight limit
evaluation, and runtime ingestion-limit enforcement."""

import httpx
import pytest
from github_test_support import json_response, mock_transport

from reporationale.adapters.github import GitHubClient, GitHubRepositoryMetadata
from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    DEFAULT_RUNTIME_INGESTION_LIMITS,
    AdmissionLimits,
    RepositoryWorkloadEstimate,
    RuntimeIngestionLimitExceeded,
    RuntimeIngestionLimits,
    check_runtime_ingestion_limits,
    estimate_repository_workload,
    evaluate_admission,
)
from reporationale.application.corpus import RepositoryCorpus
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_TREE_SHA = "d" * 40
_COMMIT_SHA = "c" * 40


def _metadata() -> GitHubRepositoryMetadata:
    return GitHubRepositoryMetadata(
        identity=_IDENTITY,
        github_id=123456,
        html_url="https://github.com/octo-org/example-repo",  # type: ignore[arg-type]
        private=False,
        default_branch="main",
        size_kb=1234,
    )


def test_shared_defaults_hold_exactly_the_accepted_values() -> None:
    """The accepted initial MVP envelope, defined once as shared
    application policy and reused (never re-hard-coded) by preflight,
    snapshot building, and the rebuild CLI."""
    assert DEFAULT_ADMISSION_LIMITS == AdmissionLimits(
        max_all_issues_and_pull_requests=700,
        max_closed_pull_requests=500,
        max_commits=1100,
        max_tree_entries=100,
    )
    assert DEFAULT_RUNTIME_INGESTION_LIMITS == RuntimeIngestionLimits(
        max_source_count=3000,
        max_github_request_count=750,
    )


def test_estimate_repository_workload_computes_documented_fields() -> None:
    """Each dimension comes from a cheap, documented signal: bounded,
    cursor-pagination-aware counting for the combined issue/PR count (here,
    a single page with no further `next` link, so the count is exact —
    see `test_estimate_repository_workload_bounds_cursor_paginated_issue_
    count` below for the cursor-pagination case that cannot be counted
    this way), the `Link: rel="last"` page-count trick for pulls/commits,
    and one recursive tree request (whose truncation is preserved, not
    resolved away, when it happens)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/issues":
            return json_response(200, [{}] * 7)
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, [{}])
        if path == "/repos/octo-org/example-repo/commits":
            last_url = (
                "https://api.github.com/repos/octo-org/example-repo/commits"
                "?sha=c&per_page=1&page=42"
            )
            return json_response(
                200, [{}], headers={"Link": f'<{last_url}>; rel="last"'}
            )
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200,
                {
                    "sha": _TREE_SHA,
                    "truncated": True,
                    "tree": [
                        {
                            "path": "a",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "a" * 40,
                        },
                        {
                            "path": "b",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "b" * 40,
                        },
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {path}")

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    estimate = estimate_repository_workload(
        metadata=_metadata(),
        resolved_commit_sha=_COMMIT_SHA,
        resolved_tree_sha=_TREE_SHA,
        admission_limits=_LIMITS,
        github_client=client,
    )

    assert estimate.repository_size_kb == 1234
    assert estimate.all_issues_and_pull_requests_count == 7
    assert estimate.all_issues_and_pull_requests_count_is_exact is True
    assert estimate.closed_pull_request_count == 1
    assert estimate.commit_count == 42
    assert estimate.tree_entry_count == 2
    assert estimate.tree_truncated is True
    assert _SECRET_TOKEN not in estimate.model_dump_json()


def test_count_paginated_collection_bounded_completes_across_cursor_pages() -> None:
    """A realistic cursor-paginated response (only `next` links, never a
    `last` link — the shape GitHub now serves for `/issues` on large
    repositories such as `psf/black`, 5,238 items over 53 pages) is
    followed until it genuinely ends, returning an exact count."""
    page_two_url = (
        "https://api.github.com/repos/octo-org/example-repo/issues"
        "?state=all&per_page=100&after=cursor1&page=2"
    )
    page_three_url = (
        "https://api.github.com/repos/octo-org/example-repo/issues"
        "?state=all&per_page=100&after=cursor2&page=3"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "page=3" in url:
            return json_response(200, [{}] * 38)
        if "page=2" in url:
            return json_response(
                200, [{}] * 100, headers={"Link": f'<{page_three_url}>; rel="next"'}
            )
        return json_response(
            200, [{}] * 100, headers={"Link": f'<{page_two_url}>; rel="next"'}
        )

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    result = client.count_paginated_collection_bounded(
        "/repos/octo-org/example-repo/issues",
        params={"state": "all"},
        max_items=1000,
    )

    assert result.observed_count == 238
    assert result.complete is True


def test_count_paginated_collection_bounded_stops_after_threshold_without_another_request() -> (
    None
):
    """Once the observed count already exceeds `max_items`, admission
    rejection against that threshold is already certain, so no further
    page is ever requested — proven here by counting actual transport
    calls, not merely asserting the returned count."""
    request_count = 0
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/issues"
        "?state=all&per_page=100&page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return json_response(
            200, [{}] * 100, headers={"Link": f'<{next_url}>; rel="next"'}
        )

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    result = client.count_paginated_collection_bounded(
        "/repos/octo-org/example-repo/issues",
        params={"state": "all"},
        max_items=5,
    )

    assert request_count == 1
    assert result.observed_count == 100
    assert result.complete is False


def _estimate(**overrides: object) -> RepositoryWorkloadEstimate:
    base: dict[str, object] = {
        "repository": _IDENTITY,
        "resolved_commit_sha": _COMMIT_SHA,
        "all_issues_and_pull_requests_count": 1,
        "all_issues_and_pull_requests_count_is_exact": True,
        "closed_pull_request_count": 1,
        "commit_count": 1,
        "tree_entry_count": 1,
        "tree_truncated": False,
    }
    base.update(overrides)
    return RepositoryWorkloadEstimate(**base)  # type: ignore[arg-type]


_LIMITS = AdmissionLimits(
    max_all_issues_and_pull_requests=5,
    max_closed_pull_requests=5,
    max_commits=5,
    max_tree_entries=5,
)


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({}, "within_admission_limits"),
        (
            {"all_issues_and_pull_requests_count": 6},
            "exceeds_all_issues_and_pull_requests_limit",
        ),
        ({"closed_pull_request_count": 6}, "exceeds_closed_pull_request_limit"),
        ({"commit_count": 6}, "exceeds_commit_limit"),
        ({"tree_entry_count": 6}, "exceeds_tree_entry_limit"),
        ({"tree_truncated": True}, "markdown_tree_truncated_at_estimate_time"),
        # Truncation is rejected before any numeric comparison, regardless
        # of how small the (necessarily incomplete) entry count is — an
        # uncertain/truncated dimension is never silently treated as zero
        # or as within limits.
        (
            {"tree_truncated": True, "tree_entry_count": 1},
            "markdown_tree_truncated_at_estimate_time",
        ),
    ],
)
def test_evaluate_admission_reason_codes(
    overrides: dict[str, object], expected_reason: str
) -> None:
    decision = evaluate_admission(_estimate(**overrides), _LIMITS)
    assert decision.reason_code == expected_reason
    assert decision.admitted == (expected_reason == "within_admission_limits")


def test_evaluate_admission_rejects_threshold_exceeded_inexact_issue_count() -> None:
    """A cursor-paginated issue count that was stopped early for already
    exceeding the limit (a lower bound, not the true total) must still
    reject admission, and the rejected estimate must never claim that
    bound is exact."""
    estimate = _estimate(
        all_issues_and_pull_requests_count=5238,
        all_issues_and_pull_requests_count_is_exact=False,
    )

    decision = evaluate_admission(estimate, _LIMITS)

    assert decision.admitted is False
    assert decision.reason_code == "exceeds_all_issues_and_pull_requests_limit"
    assert decision.estimate.all_issues_and_pull_requests_count_is_exact is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_all_issues_and_pull_requests": 0},
        {"max_commits": -1},
        {"max_tree_entries": True},
    ],
)
def test_admission_limits_reject_non_positive_or_boolean_values(
    overrides: dict[str, object],
) -> None:
    base = {
        "max_all_issues_and_pull_requests": 5,
        "max_closed_pull_requests": 5,
        "max_commits": 5,
        "max_tree_entries": 5,
    }
    with pytest.raises(Exception, match="greater than 0|valid integer"):
        AdmissionLimits(**{**base, **overrides})  # type: ignore[arg-type]


def _corpus(document_count: int) -> RepositoryCorpus:
    documents = tuple(
        SourceDocument(
            source_id=f"github:octo-org/example-repo:markdown:{index}.md",
            platform="github",
            repository="octo-org/example-repo",
            source_type="markdown",
            text="hello",
            source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
        )
        for index in range(document_count)
    )
    return RepositoryCorpus(
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=documents,
        counts_by_source_type={"markdown": document_count} if document_count else {},
    )


def test_check_runtime_ingestion_limits_accepts_within_bounds() -> None:
    check_runtime_ingestion_limits(
        _corpus(2),
        request_count=3,
        limits=RuntimeIngestionLimits(max_source_count=5, max_github_request_count=5),
    )


def test_check_runtime_ingestion_limits_rejects_source_count_over_limit() -> None:
    with pytest.raises(RuntimeIngestionLimitExceeded) as excinfo:
        check_runtime_ingestion_limits(
            _corpus(3),
            request_count=1,
            limits=RuntimeIngestionLimits(
                max_source_count=2, max_github_request_count=100
            ),
        )
    assert excinfo.value.reason_code == "exceeds_actual_source_count_limit"


def test_check_runtime_ingestion_limits_rejects_request_count_over_limit() -> None:
    with pytest.raises(RuntimeIngestionLimitExceeded) as excinfo:
        check_runtime_ingestion_limits(
            _corpus(1),
            request_count=100,
            limits=RuntimeIngestionLimits(
                max_source_count=100, max_github_request_count=2
            ),
        )
    assert excinfo.value.reason_code == "exceeds_actual_request_count_limit"


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_source_count": 0},
        {"max_github_request_count": -1},
        {"max_source_count": True},
    ],
)
def test_runtime_ingestion_limits_reject_non_positive_or_boolean_values(
    overrides: dict[str, object],
) -> None:
    base = {"max_source_count": 10, "max_github_request_count": 10}
    with pytest.raises(ValueError):
        RuntimeIngestionLimits(**{**base, **overrides})  # type: ignore[arg-type]
