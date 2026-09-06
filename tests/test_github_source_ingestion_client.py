"""Client-level integration tests for the GitHub source-type endpoints:
pull-request roots/general comments/reviews/inline review comments,
standalone issues/comments, commits, and Markdown files.

Exercises pagination, duplicate detection, all-or-nothing failure, and
(for Markdown) truncated-tree fallback traversal, against a mocked
`httpx` transport. No real network access or `GITHUB_TOKEN` is used. Each
source type keeps one representative case per risk (pagination/dedup,
duplicate identity, no-partial-success on a later malformed page) rather
than repeating the full matrix for every endpoint; token-exposure and
identity/number-guard checks are exercised once, not per endpoint (see
`test_no_new_endpoint_ever_exposes_the_token` below and
`test_github_pull_request.py`/`test_github_issue.py` for the parser-level
guards).
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import httpx
import pytest
from github_test_support import json_response, load_fixture, mock_transport

from reporationale.adapters.github import (
    GitHubClient,
    GitHubMalformedResponse,
    GitHubRequestBudgetExceeded,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")


def _client(handler: httpx.MockTransport) -> GitHubClient:
    return GitHubClient(token=_SECRET_TOKEN, transport=handler)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- Pull-request roots --------------------------------------------------


def test_list_closed_pull_requests_paginates_state_and_deduplicates() -> None:
    merged = load_fixture("pull_request_merged.json")
    unmerged = load_fixture("pull_request_closed_unmerged.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls?state=closed&page=2"
    )
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, [unmerged])
        captured["state"] = request.url.params.get("state", "")
        captured["per_page"] = request.url.params.get("per_page", "")
        return json_response(
            200, [merged], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    documents = _client(mock_transport(handler)).list_closed_pull_requests(_IDENTITY)

    assert captured["state"] == "closed"
    assert captured["per_page"] == "100"
    assert [d.item_number for d in documents] == [42, 7]


def test_list_closed_pull_requests_rejects_duplicate_identity_across_pages() -> None:
    merged = load_fixture("pull_request_merged.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls?state=closed&page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, [merged])
        return json_response(
            200, [merged], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_closed_pull_requests(_IDENTITY)


def test_list_closed_pull_requests_fails_entirely_on_malformed_later_page() -> None:
    """A malformed later page fails the whole operation; the valid first
    page's item is never returned as a partial result."""
    merged = load_fixture("pull_request_merged.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls?state=closed&page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, {"message": "not a list"})
        return json_response(
            200, [merged], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_closed_pull_requests(_IDENTITY)


# --- General comments (GitHub's shared "issue comment" object) -----------


def test_list_repository_issue_comments_maps_parents_and_paginates() -> None:
    pr_comment = load_fixture("pull_request_comment.json")
    issue_comment = load_fixture("issue_comment.json")
    empty_comment = {
        **pr_comment,
        "id": 2099,
        "body": "   ",
        "html_url": "https://github.com/octo-org/example-repo/pull/42#issuecomment-2099",
        "url": "https://api.github.com/repos/octo-org/example-repo/issues/comments/2099",
    }
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/issues/comments?page=2"
    )
    captured_path = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_path
        captured_path = request.url.path
        if "page=2" in str(request.url):
            return json_response(200, [empty_comment])
        return json_response(
            200,
            [pr_comment, issue_comment],
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    documents = _client(mock_transport(handler)).list_repository_issue_comments(
        _IDENTITY,
        standalone_issue_numbers={10},
        closed_pull_request_numbers={42},
    )

    assert captured_path == "/repos/octo-org/example-repo/issues/comments"
    assert [(d.source_type, d.item_number) for d in documents] == [
        ("pull_request_comment", 42),
        ("issue_comment", 10),
    ]


def test_list_repository_issue_comments_rejects_duplicate_id_via_skipped_comment() -> (
    None
):
    """A duplicate ID is rejected even when one occurrence is later skipped
    for an empty body — duplicate detection must not be bypassed by that."""
    comment = load_fixture("pull_request_comment.json")
    empty_comment = {**comment, "body": None}
    handler = mock_transport(
        lambda request: json_response(200, [comment, empty_comment])
    )

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).list_repository_issue_comments(
            _IDENTITY,
            standalone_issue_numbers=set(),
            closed_pull_request_numbers={42},
        )


# --- Reviews -----------------------------------------------------------


def test_list_pull_request_reviews_paginates_and_deduplicates() -> None:
    review = load_fixture("pull_request_review.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls/42/reviews?page=2"
    )

    second_review = {
        **review,
        "id": 502,
        "html_url": (
            "https://github.com/octo-org/example-repo/pull/42#pullrequestreview-502"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, [second_review])
        return json_response(
            200, [review], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    documents = _client(mock_transport(handler)).list_pull_request_reviews(
        _IDENTITY, 42
    )

    assert [d.metadata["github_id"] for d in documents] == [501, 502]


def test_list_pull_request_reviews_rejects_duplicate_id_across_pages() -> None:
    review = load_fixture("pull_request_review.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls/42/reviews?page=2"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, [review])
        return json_response(
            200, [review], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_pull_request_reviews(_IDENTITY, 42)


# --- Inline review comments/replies -------------------------------------


def test_list_repository_review_comments_resolves_reply_parent() -> None:
    top = load_fixture("pull_request_review_comment.json")
    reply = load_fixture("pull_request_review_comment_reply.json")
    handler = mock_transport(lambda request: json_response(200, [top, reply]))

    documents = _client(handler).list_repository_pull_request_review_comments(
        _IDENTITY, closed_pull_request_numbers={42}
    )

    assert len(documents) == 2
    reply_document = next(d for d in documents if d.metadata["github_id"] == 602)
    assert reply_document.parent_source_id == (
        "github:octo-org/example-repo:pull_request:42:review_comment:601"
    )


def test_list_repository_review_comments_rejects_dangling_reply_parent() -> None:
    """A reply whose parent is missing from the fully collected response
    fails the entire operation."""
    reply = {
        **load_fixture("pull_request_review_comment_reply.json"),
        "in_reply_to_id": 9999,
    }
    handler = mock_transport(lambda request: json_response(200, [reply]))

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).list_repository_pull_request_review_comments(
            _IDENTITY, closed_pull_request_numbers={42}
        )


def test_list_repository_review_comments_rejects_duplicate_id_on_one_page() -> None:
    top = load_fixture("pull_request_review_comment.json")
    handler = mock_transport(lambda request: json_response(200, [top, top]))

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).list_repository_pull_request_review_comments(
            _IDENTITY, closed_pull_request_numbers={42}
        )


# --- Standalone issues + comments ----------------------------------------


def test_list_standalone_issues_excludes_pr_shaped_items() -> None:
    issue = load_fixture("issue.json")
    pr_shaped = load_fixture("issue_pr_shaped.json")
    handler = mock_transport(lambda request: json_response(200, [issue, pr_shaped]))

    documents = _client(handler).list_standalone_issues(_IDENTITY)

    assert [d.item_number for d in documents] == [10]


def test_list_standalone_issues_uses_state_all_and_per_page_100() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["state"] = request.url.params.get("state", "")
        captured["per_page"] = request.url.params.get("per_page", "")
        captured["path"] = request.url.path
        return json_response(200, [])

    _client(mock_transport(handler)).list_standalone_issues(_IDENTITY)

    assert captured["state"] == "all"
    assert captured["per_page"] == "100"
    assert captured["path"] == "/repos/octo-org/example-repo/issues"


def test_list_standalone_issues_rejects_duplicate_identity_including_pr_shaped() -> (
    None
):
    pr_shaped = load_fixture("issue_pr_shaped.json")
    handler = mock_transport(lambda request: json_response(200, [pr_shaped, pr_shaped]))

    with pytest.raises(GitHubMalformedResponse):
        _client(handler).list_standalone_issues(_IDENTITY)


# --- Commits -------------------------------------------------------------


def test_list_commits_uses_default_branch_and_deduplicates() -> None:
    commit = load_fixture("commit.json")
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["sha_param"] = request.url.params.get("sha", "")
        return json_response(200, [commit])

    documents = _client(mock_transport(handler)).list_commits(_IDENTITY, "main")

    assert captured["sha_param"] == "main"
    assert len(documents) == 1


def test_list_commits_rejects_duplicate_sha_across_pages() -> None:
    commit = load_fixture("commit.json")
    next_url = "https://api.github.com/repos/octo-org/example-repo/commits?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return json_response(200, [commit])
        return json_response(
            200, [commit], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_commits(_IDENTITY, "main")


# --- Commit/tree revision resolution ---------------------------------------


def test_resolve_commit_and_tree_returns_commit_and_root_tree_sha() -> None:
    commit_sha = "c" * 40
    tree_sha = "d" * 40
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return json_response(
            200, {"sha": commit_sha, "commit": {"tree": {"sha": tree_sha}}}
        )

    resolved_commit_sha, resolved_tree_sha = _client(
        mock_transport(handler)
    ).resolve_commit_and_tree(_IDENTITY, "main")

    assert captured["path"] == "/repos/octo-org/example-repo/commits/main"
    assert resolved_commit_sha == commit_sha
    assert resolved_tree_sha == tree_sha


# --- Markdown --------------------------------------------------------------
#
# `list_markdown_documents` takes an already-resolved `commit_sha`/`tree_sha`
# pair (see `resolve_commit_and_tree` above and Finding 3 in the corpus
# workflow); it performs no branch resolution of its own, so these tests
# supply that pair directly rather than mocking a `/commits/{branch}` call.


def test_list_markdown_documents_end_to_end() -> None:
    commit_sha = "c" * 40
    tree_sha = "d" * 40
    readme_sha = "e" * 40
    empty_sha = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{tree_sha}"):
            return json_response(
                200,
                {
                    "sha": tree_sha,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": readme_sha,
                        },
                        {
                            "path": "EMPTY.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": empty_sha,
                        },
                        {
                            "path": "app.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": "1" * 40,
                        },
                    ],
                },
            )
        if path.endswith(f"/git/blobs/{readme_sha}"):
            return json_response(
                200,
                {"sha": readme_sha, "content": _b64("# Hello"), "encoding": "base64"},
            )
        if path.endswith(f"/git/blobs/{empty_sha}"):
            return json_response(
                200, {"sha": empty_sha, "content": _b64("   "), "encoding": "base64"}
            )
        raise AssertionError(f"unexpected request: {path}")

    documents = _client(mock_transport(handler)).list_markdown_documents(
        _IDENTITY, commit_sha=commit_sha, tree_sha=tree_sha
    )

    assert [d.source_id for d in documents] == [
        "github:octo-org/example-repo:markdown:README.md"
    ]


def test_list_markdown_documents_falls_back_when_tree_is_truncated() -> None:
    root_tree = "d" * 40
    sub_tree = "a" * 40
    readme_sha = "e" * 40
    guide_sha = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{root_tree}") and request.url.params.get(
            "recursive"
        ):
            return json_response(200, {"sha": root_tree, "truncated": True, "tree": []})
        if path.endswith(f"/git/trees/{root_tree}"):
            return json_response(
                200,
                {
                    "sha": root_tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": readme_sha,
                        },
                        {
                            "path": "docs",
                            "mode": "040000",
                            "type": "tree",
                            "sha": sub_tree,
                        },
                    ],
                },
            )
        if path.endswith(f"/git/trees/{sub_tree}"):
            return json_response(
                200,
                {
                    "sha": sub_tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "guide.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": guide_sha,
                        }
                    ],
                },
            )
        if path.endswith(f"/git/blobs/{readme_sha}"):
            return json_response(
                200,
                {
                    "sha": readme_sha,
                    "content": _b64("root readme"),
                    "encoding": "base64",
                },
            )
        if path.endswith(f"/git/blobs/{guide_sha}"):
            return json_response(
                200,
                {"sha": guide_sha, "content": _b64("guide text"), "encoding": "base64"},
            )
        raise AssertionError(f"unexpected request: {path}")

    documents = _client(mock_transport(handler)).list_markdown_documents(
        _IDENTITY, commit_sha="c" * 40, tree_sha=root_tree
    )

    assert sorted(d.source_id for d in documents) == [
        "github:octo-org/example-repo:markdown:README.md",
        "github:octo-org/example-repo:markdown:docs/guide.md",
    ]


def test_list_markdown_documents_allows_repeated_subtree_sha_at_different_paths() -> (
    None
):
    """A content-addressed subtree sha (e.g. two directories with identical
    contents) can legitimately appear at more than one repository path;
    that is not a cycle and must not be rejected."""
    root_tree = "d" * 40
    shared_sub_tree = "a" * 40
    file_sha = "e" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{root_tree}") and request.url.params.get(
            "recursive"
        ):
            return json_response(200, {"sha": root_tree, "truncated": True, "tree": []})
        if path.endswith(f"/git/trees/{root_tree}"):
            return json_response(
                200,
                {
                    "sha": root_tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "dir-a",
                            "mode": "040000",
                            "type": "tree",
                            "sha": shared_sub_tree,
                        },
                        {
                            "path": "dir-b",
                            "mode": "040000",
                            "type": "tree",
                            "sha": shared_sub_tree,
                        },
                    ],
                },
            )
        if path.endswith(f"/git/trees/{shared_sub_tree}"):
            return json_response(
                200,
                {
                    "sha": shared_sub_tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "note.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": file_sha,
                        }
                    ],
                },
            )
        if path.endswith(f"/git/blobs/{file_sha}"):
            return json_response(
                200, {"sha": file_sha, "content": _b64("hello"), "encoding": "base64"}
            )
        raise AssertionError(f"unexpected request: {path}")

    documents = _client(mock_transport(handler)).list_markdown_documents(
        _IDENTITY, commit_sha="c" * 40, tree_sha=root_tree
    )

    assert sorted(d.source_id for d in documents) == [
        "github:octo-org/example-repo:markdown:dir-a/note.md",
        "github:octo-org/example-repo:markdown:dir-b/note.md",
    ]


def test_list_markdown_documents_rejects_actual_tree_cycle() -> None:
    """A tree sha reappearing among its own ancestors along one traversal
    branch is a genuine cycle, unlike a repeated sha at unrelated paths."""
    root_tree = "d" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{root_tree}") and request.url.params.get(
            "recursive"
        ):
            return json_response(200, {"sha": root_tree, "truncated": True, "tree": []})
        if path.endswith(f"/git/trees/{root_tree}"):
            return json_response(
                200,
                {
                    "sha": root_tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "self",
                            "mode": "040000",
                            "type": "tree",
                            "sha": root_tree,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_markdown_documents(
            _IDENTITY, commit_sha="c" * 40, tree_sha=root_tree
        )


def test_list_markdown_documents_rejects_returned_tree_sha_mismatch() -> None:
    """A tree response's own `sha` must match the tree sha that was
    actually requested, for the initial recursive fetch as much as for any
    fallback request."""
    tree_sha = "d" * 40
    unrequested_sha = "9" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{tree_sha}"):
            return json_response(
                200, {"sha": unrequested_sha, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_markdown_documents(
            _IDENTITY, commit_sha="c" * 40, tree_sha=tree_sha
        )


def test_list_markdown_documents_fails_if_non_recursive_fallback_still_truncated() -> (
    None
):
    root_tree = "d" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{root_tree}"):
            return json_response(200, {"sha": root_tree, "truncated": True, "tree": []})
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_markdown_documents(
            _IDENTITY, commit_sha="c" * 40, tree_sha=root_tree
        )


def test_list_markdown_documents_rejects_duplicate_paths() -> None:
    tree_sha = "d" * 40
    blob_sha = "e" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/git/trees/{tree_sha}"):
            return json_response(
                200,
                {
                    "sha": tree_sha,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_sha,
                        },
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_sha,
                        },
                    ],
                },
            )
        if path.endswith(f"/git/blobs/{blob_sha}"):
            return json_response(
                200, {"sha": blob_sha, "content": _b64("text"), "encoding": "base64"}
            )
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(GitHubMalformedResponse):
        _client(mock_transport(handler)).list_markdown_documents(
            _IDENTITY, commit_sha="c" * 40, tree_sha=tree_sha
        )


# --- Scoped request budget ------------------------------------------------


def test_limit_requests_stops_before_the_nth_plus_one_request() -> None:
    """With a scoped budget of N, request N is sent but request N + 1 (here,
    the second paginated page) is never dispatched to the transport."""
    merged = load_fixture("pull_request_merged.json")
    next_url = (
        "https://api.github.com/repos/octo-org/example-repo/pulls?state=closed&page=2"
    )
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return json_response(
            200, [merged], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    client = _client(mock_transport(handler))

    with pytest.raises(GitHubRequestBudgetExceeded):
        with client.limit_requests(1):
            client.list_closed_pull_requests(_IDENTITY)

    assert request_count == 1


def test_limit_requests_is_shared_across_concurrent_requests() -> None:
    request_count = 0
    counter_lock = Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        with counter_lock:
            request_count += 1
        return json_response(200, [])

    client = _client(mock_transport(handler))
    with client.limit_requests(2), ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(client.list_pull_request_reviews, _IDENTITY, number)
            for number in range(1, 5)
        ]

    failures = 0
    for future in futures:
        try:
            future.result()
        except GitHubRequestBudgetExceeded:
            failures += 1

    assert request_count == 2
    assert failures == 2


def test_no_new_endpoint_ever_exposes_the_token() -> None:
    review = load_fixture("pull_request_review.json")
    handler = mock_transport(lambda request: json_response(200, [review]))
    documents = _client(handler).list_pull_request_reviews(_IDENTITY, 42)
    for document in documents:
        assert _SECRET_TOKEN not in document.model_dump_json()

    malformed_handler = mock_transport(
        lambda request: json_response(200, [{"not": "valid"}])
    )
    with pytest.raises(GitHubMalformedResponse) as excinfo:
        _client(malformed_handler).list_pull_request_reviews(_IDENTITY, 42)
    assert _SECRET_TOKEN not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
