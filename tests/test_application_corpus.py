"""Tests for the unified deterministic corpus-assembly workflow.

`assemble_repository_corpus` takes an already-resolved revision (see
`test_application_snapshot_workflow.py` for the one-time-resolution
regression test spanning the complete build workflow); these tests
exercise it in isolation with a fixed, directly-supplied
`resolved_commit_sha`/`resolved_tree_sha` pair.
"""

import base64
import json
from threading import Event, Lock

import httpx
import pytest
from github_test_support import json_response, load_fixture, mock_transport
from pydantic import ValidationError

from reporationale.adapters.github import GitHubClient, GitHubMalformedResponse
from reporationale.application import RepositoryCorpus, assemble_repository_corpus
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40
_TREE_SHA = "d" * 40


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _assemble(github_client: GitHubClient) -> RepositoryCorpus:
    return assemble_repository_corpus(
        _IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        resolved_tree_sha=_TREE_SHA,
        github_client=github_client,
    )


def _build_handler() -> httpx.MockTransport:
    """A handler that satisfies every endpoint `assemble_repository_corpus`
    calls, with one item of each supported source type."""
    pr = load_fixture("pull_request_merged.json")
    pr_comment = load_fixture("pull_request_comment.json")
    review = load_fixture("pull_request_review.json")
    review_comment = load_fixture("pull_request_review_comment.json")
    issue = load_fixture("issue.json")
    issue_comment = load_fixture("issue_comment.json")
    commit = load_fixture("commit.json")
    readme_sha = "e" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, [pr])
        if path == "/repos/octo-org/example-repo/pulls/42/reviews":
            return json_response(200, [review])
        if path == "/repos/octo-org/example-repo/pulls/comments":
            return json_response(200, [review_comment])
        if path == "/repos/octo-org/example-repo/issues":
            return json_response(200, [issue])
        if path == "/repos/octo-org/example-repo/issues/comments":
            return json_response(200, [pr_comment, issue_comment])
        if path == "/repos/octo-org/example-repo/commits":
            return json_response(200, [commit])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200,
                {
                    "sha": _TREE_SHA,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": readme_sha,
                        }
                    ],
                },
            )
        if path == f"/repos/octo-org/example-repo/git/blobs/{readme_sha}":
            return json_response(
                200,
                {"sha": readme_sha, "content": _b64("# Readme"), "encoding": "base64"},
            )
        raise AssertionError(f"unexpected request: {path}")

    return mock_transport(handler)


def test_assemble_repository_corpus_includes_every_supported_source_type() -> None:
    client = GitHubClient(token=_SECRET_TOKEN, transport=_build_handler())

    corpus = _assemble(client)

    assert isinstance(corpus, RepositoryCorpus)
    assert corpus.counts_by_source_type == {
        "pull_request": 1,
        "pull_request_comment": 1,
        "pull_request_review": 1,
        "pull_request_review_comment": 1,
        "issue": 1,
        "issue_comment": 1,
        "commit": 1,
        "markdown": 1,
    }
    assert len(corpus.documents) == 8
    assert corpus.resolved_commit_sha == _COMMIT_SHA
    assert corpus.default_branch == "main"


def test_assemble_repository_corpus_orders_documents_deterministically_by_source_id() -> (
    None
):
    client = GitHubClient(token=_SECRET_TOKEN, transport=_build_handler())

    corpus = _assemble(client)

    source_ids = [document.source_id for document in corpus.documents]
    assert source_ids == sorted(source_ids)


def test_pull_request_reviews_use_at_most_eight_concurrent_requests() -> None:
    template = load_fixture("pull_request_merged.json")
    pull_requests = [
        {
            **template,
            "id": 9000 + number,
            "number": number,
            "html_url": f"https://github.com/octo-org/example-repo/pull/{number}",
        }
        for number in range(1, 10)
    ]
    state_lock = Lock()
    eight_started = Event()
    active = 0
    maximum_active = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum_active
        path = request.url.path
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, pull_requests)
        if path.endswith("/reviews"):
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 8:
                    eight_started.set()
            assert eight_started.wait(timeout=2)
            with state_lock:
                active -= 1
            return json_response(200, [])
        if path in {
            "/repos/octo-org/example-repo/issues",
            "/repos/octo-org/example-repo/issues/comments",
            "/repos/octo-org/example-repo/pulls/comments",
            "/repos/octo-org/example-repo/commits",
        }:
            return json_response(200, [])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    corpus = _assemble(
        GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))
    )

    assert maximum_active == 8
    assert corpus.counts_by_source_type == {"pull_request": 9}


def test_pull_request_review_comments_run_concurrently_with_review_summaries() -> None:
    """The scoped concurrency change: repository-wide PR review comments
    must start before every per-PR review-summary call has finished,
    proving the two run side by side rather than strictly one after the
    other. Under the old sequential order, review comments were only
    requested after all review summaries had already completed, so the
    review-summary handler below (which waits for the review-comments
    request to start) would hang until timeout instead of proceeding.
    """
    template = load_fixture("pull_request_merged.json")
    pull_requests = [
        {
            **template,
            "id": 9100 + number,
            "number": number,
            "html_url": f"https://github.com/octo-org/example-repo/pull/{number}",
        }
        for number in range(1, 4)
    ]
    review_comments_started = Event()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, pull_requests)
        if path.endswith("/reviews"):
            assert review_comments_started.wait(timeout=2), (
                "a review-summary call completed before repository-wide "
                "review-comment collection started -- they are no longer "
                "running concurrently"
            )
            return json_response(200, [])
        if path == "/repos/octo-org/example-repo/pulls/comments":
            review_comments_started.set()
            return json_response(200, [])
        if path in {
            "/repos/octo-org/example-repo/issues",
            "/repos/octo-org/example-repo/issues/comments",
            "/repos/octo-org/example-repo/commits",
        }:
            return json_response(200, [])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    corpus = _assemble(
        GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))
    )

    assert corpus.counts_by_source_type == {"pull_request": 3}


def test_assemble_repository_corpus_aborts_when_review_comments_stage_fails() -> None:
    """A failure in the concurrently-run repository-wide review-comments
    collection must abort the whole assembly -- not be silently absorbed
    while the review-summary fetch running alongside it succeeds."""
    template = load_fixture("pull_request_merged.json")
    pull_requests = [{**template, "id": 9200, "number": 1}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, pull_requests)
        if path.endswith("/reviews"):
            return json_response(200, [])
        if path == "/repos/octo-org/example-repo/pulls/comments":
            return json_response(200, {"message": "not a list"})
        raise AssertionError(f"unexpected request: {path}")

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    with pytest.raises(GitHubMalformedResponse):
        _assemble(client)


def test_assemble_repository_corpus_aborts_on_a_duplicate_within_one_source_type() -> (
    None
):
    """A duplicate identity returned by one endpoint (here, the same commit
    twice) aborts the whole assembly rather than silently deduplicating or
    returning a partial corpus."""
    duplicate_commit = load_fixture("commit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in (
            "/repos/octo-org/example-repo/pulls",
            "/repos/octo-org/example-repo/issues",
        ):
            return json_response(200, [])
        if path == "/repos/octo-org/example-repo/commits":
            return json_response(200, [duplicate_commit, duplicate_commit])
        raise AssertionError(f"unexpected request: {path}")

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    with pytest.raises(GitHubMalformedResponse):
        _assemble(client)


def test_assemble_repository_corpus_fails_entirely_when_markdown_stage_fails() -> None:
    """A late-stage failure (Markdown tree collection) must not return a
    partial corpus."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in (
            "/repos/octo-org/example-repo/pulls",
            "/repos/octo-org/example-repo/issues",
            "/repos/octo-org/example-repo/commits",
        ):
            return json_response(200, [])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(200, {"message": "not a tree"})
        raise AssertionError(f"unexpected request: {path}")

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    with pytest.raises(GitHubMalformedResponse):
        _assemble(client)


def test_repository_corpus_model_dump_round_trip() -> None:
    client = GitHubClient(token=_SECRET_TOKEN, transport=_build_handler())
    corpus = _assemble(client)

    dumped = corpus.model_dump()
    assert RepositoryCorpus.model_validate(dumped) == corpus

    dumped_json = corpus.model_dump_json()
    assert RepositoryCorpus.model_validate(json.loads(dumped_json)) == corpus
    assert RepositoryCorpus.model_validate_json(dumped_json) == corpus


def test_no_token_appears_in_corpus_or_exceptions() -> None:
    client = GitHubClient(token=_SECRET_TOKEN, transport=_build_handler())
    corpus = _assemble(client)

    assert _SECRET_TOKEN not in corpus.model_dump_json()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, [{"not": "valid"}])
        return json_response(200, [])

    bad_client = GitHubClient(
        token=_SECRET_TOKEN, transport=mock_transport(bad_handler)
    )
    try:
        _assemble(bad_client)
    except GitHubMalformedResponse as exc:
        assert _SECRET_TOKEN not in str(exc)
        assert _SECRET_TOKEN not in repr(exc)


# --- RepositoryCorpus contract enforcement --------------------------------
#
# These construct the exported model directly, independent of
# `assemble_repository_corpus`, since a frozen public model must reject
# contradictory state on any direct construction or deserialization.

_PR_SOURCE_ID = "github:octo-org/example-repo:pull_request:42:description"


def _pr_document() -> SourceDocument:
    return SourceDocument(
        source_id=_PR_SOURCE_ID,
        platform="github",
        repository="octo-org/example-repo",
        source_type="pull_request",
        text="Fix the bug.",
        source_url="https://github.com/octo-org/example-repo/pull/42",
    )


def _commit_document() -> SourceDocument:
    return SourceDocument(
        source_id=f"github:octo-org/example-repo:commit:{'a' * 40}",
        platform="github",
        repository="octo-org/example-repo",
        source_type="commit",
        text="Initial commit.",
        source_url=f"https://github.com/octo-org/example-repo/commit/{'a' * 40}",
    )


def _valid_corpus_kwargs() -> dict[str, object]:
    return {
        "repository": _IDENTITY,
        "default_branch": "main",
        "resolved_commit_sha": "b" * 40,
        "documents": (_pr_document(),),
        "counts_by_source_type": {"pull_request": 1},
    }


def test_repository_corpus_accepts_valid_construction() -> None:
    corpus = RepositoryCorpus(**_valid_corpus_kwargs())
    assert corpus.resolved_commit_sha == "b" * 40


def test_repository_corpus_accepts_empty_corpus() -> None:
    """Admission limits are a preflight concern; an empty but internally
    consistent corpus is not rejected here."""
    corpus = RepositoryCorpus(
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha="b" * 40,
        documents=(),
        counts_by_source_type={},
    )
    assert corpus.documents == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_branch": ""},
        {"resolved_commit_sha": "not-a-sha"},
        {"resolved_commit_sha": "A" * 40},
        {"counts_by_source_type": {"pull_request": -1}},
        {"counts_by_source_type": {"pull_request": True}},
        {"counts_by_source_type": {"pull_request": "1"}},
        {"counts_by_source_type": {}},
        {"counts_by_source_type": {"pull_request": 1, "commit": 0}},
    ],
)
def test_repository_corpus_rejects_invalid_direct_construction(
    overrides: dict[str, object],
) -> None:
    kwargs = {**_valid_corpus_kwargs(), **overrides}
    with pytest.raises(ValidationError):
        RepositoryCorpus(**kwargs)


def test_repository_corpus_rejects_unordered_documents() -> None:
    kwargs = _valid_corpus_kwargs()
    # "commit:" sorts before "pull_request:", so this is descending order.
    kwargs["documents"] = (_pr_document(), _commit_document())
    kwargs["counts_by_source_type"] = {"pull_request": 1, "commit": 1}

    with pytest.raises(ValidationError):
        RepositoryCorpus(**kwargs)


def test_repository_corpus_rejects_duplicate_documents() -> None:
    kwargs = _valid_corpus_kwargs()
    kwargs["documents"] = (_pr_document(), _pr_document())
    kwargs["counts_by_source_type"] = {"pull_request": 2}

    with pytest.raises(ValidationError):
        RepositoryCorpus(**kwargs)


def test_repository_corpus_rejects_document_repository_mismatch() -> None:
    mismatched = SourceDocument(
        source_id="github:other-org/other-repo:pull_request:1:description",
        platform="github",
        repository="other-org/other-repo",
        source_type="pull_request",
        text="Unrelated.",
        source_url="https://github.com/other-org/other-repo/pull/1",
    )
    kwargs = _valid_corpus_kwargs()
    kwargs["documents"] = (mismatched,)

    with pytest.raises(ValidationError):
        RepositoryCorpus(**kwargs)
