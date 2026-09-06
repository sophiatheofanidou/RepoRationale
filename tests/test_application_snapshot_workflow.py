"""Tests for the normalized-source build/reuse/rebuild workflow and its preflight
integration.

Uses only a mocked `httpx` transport and existing deterministic fixtures;
no real network access or `GITHUB_TOKEN` is used, and no snapshot is ever
written into the repository (every test writes under `tmp_path`).
"""

import base64
from pathlib import Path

import httpx
import pytest
from github_test_support import json_response, load_fixture, mock_transport

from reporationale.adapters.github import (
    GitHubClient,
    GitHubMalformedResponse,
    GitHubRepositoryMetadata,
)
from reporationale.adapters.snapshot_store import publish_snapshot
from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    AdmissionLimits,
    RuntimeIngestionLimitExceeded,
    RuntimeIngestionLimits,
)
from reporationale.application.preflight import preflight_repository_reference
from reporationale.application.snapshot_workflow import (
    NormalizedSourceBuildRejected,
    build_normalized_source_snapshot,
    rebuild_normalized_source_snapshot,
)
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_SECRET_TOKEN = "super-secret-test-token-value"
_IDENTITY = RepositoryIdentity(platform="github", owner="octo-org", name="example-repo")
_COMMIT_SHA = "c" * 40
_TREE_SHA = "d" * 40
_README_SHA = "e" * 40

_GENEROUS_LIMITS = AdmissionLimits(
    max_all_issues_and_pull_requests=1000,
    max_closed_pull_requests=1000,
    max_commits=1000,
    max_tree_entries=1000,
)
_GENEROUS_RUNTIME_LIMITS = RuntimeIngestionLimits(
    max_source_count=1000, max_github_request_count=1000
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _metadata() -> GitHubRepositoryMetadata:
    return GitHubRepositoryMetadata(
        identity=_IDENTITY,
        github_id=123456,
        html_url="https://github.com/octo-org/example-repo",  # type: ignore[arg-type]
        private=False,
        default_branch="main",
    )


def _full_transport() -> httpx.MockTransport:
    """A handler answering every request `build_normalized_source_snapshot`
    makes (admission estimation and complete corpus assembly alike), with
    one item of each supported source type, reusing existing fixtures."""
    pr = load_fixture("pull_request_merged.json")
    pr_comment = load_fixture("pull_request_comment.json")
    review = load_fixture("pull_request_review.json")
    review_comment = load_fixture("pull_request_review_comment.json")
    issue = load_fixture("issue.json")
    issue_comment = load_fixture("issue_comment.json")
    commit = load_fixture("commit.json")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
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
                            "sha": _README_SHA,
                        }
                    ],
                },
            )
        if path == f"/repos/octo-org/example-repo/git/blobs/{_README_SHA}":
            return json_response(
                200,
                {"sha": _README_SHA, "content": _b64("# Readme"), "encoding": "base64"},
            )
        raise AssertionError(f"unexpected request: {path}")

    return mock_transport(handler)


def test_build_normalized_source_snapshot_reuses_then_explicit_rebuild(
    tmp_path: Path,
) -> None:
    metadata = _metadata()

    result1 = build_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_full_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )
    assert result1.reused_existing_snapshot is False

    result2 = build_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_full_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )
    assert result2.reused_existing_snapshot is True
    assert result2.manifest == result1.manifest
    assert result2.documents == result1.documents

    result3 = rebuild_normalized_source_snapshot(
        metadata,
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_full_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )
    assert result3.reused_existing_snapshot is False
    assert result3.manifest.resolved_commit_sha == result1.manifest.resolved_commit_sha
    # Fresh and reused results share the exact same shape.
    assert type(result1) is type(result2) is type(result3)


def test_build_normalized_source_snapshot_resolves_revision_exactly_once(
    tmp_path: Path,
) -> None:
    """The revision must be resolved exactly once per build, and that same
    resolved commit/tree pair — never the moving branch name — is what
    admission estimation, corpus assembly, and the published manifest all
    actually use."""
    branch_resolution_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal branch_resolution_calls
        path = request.url.path
        if path == "/repos/octo-org/example-repo/commits/main":
            branch_resolution_calls += 1
            assert branch_resolution_calls == 1, (
                "the revision must be resolved exactly once per build"
            )
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        if path in (
            "/repos/octo-org/example-repo/issues",
            "/repos/octo-org/example-repo/pulls",
        ):
            return json_response(200, [])
        if path == "/repos/octo-org/example-repo/commits":
            assert request.url.params.get("sha") == _COMMIT_SHA, (
                "commit-history collection must use the resolved commit sha, "
                "never the moving branch name"
            )
            return json_response(200, [])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    result = build_normalized_source_snapshot(
        _metadata(),
        github_client=GitHubClient(
            token=_SECRET_TOKEN, transport=mock_transport(handler)
        ),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )

    assert branch_resolution_calls == 1
    assert result.manifest.resolved_commit_sha == _COMMIT_SHA


def test_build_normalized_source_snapshot_raises_when_over_admission_limit(
    tmp_path: Path,
) -> None:
    tight_limits = AdmissionLimits(
        max_all_issues_and_pull_requests=1000,
        max_closed_pull_requests=1,
        max_commits=1000,
        max_tree_entries=1000,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        if path == "/repos/octo-org/example-repo/issues":
            return json_response(200, [{}])
        if path == "/repos/octo-org/example-repo/pulls":
            return json_response(200, [{}, {}])
        if path == "/repos/octo-org/example-repo/commits":
            return json_response(200, [{}])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(NormalizedSourceBuildRejected) as excinfo:
        build_normalized_source_snapshot(
            _metadata(),
            github_client=GitHubClient(
                token=_SECRET_TOKEN, transport=mock_transport(handler)
            ),
            snapshot_root=tmp_path,
            admission_limits=tight_limits,
            runtime_limits=_GENEROUS_RUNTIME_LIMITS,
        )

    assert excinfo.value.decision.reason_code == "exceeds_closed_pull_request_limit"
    assert list(tmp_path.rglob("*")) == []


def test_build_normalized_source_snapshot_uses_shared_defaults_when_omitted(
    tmp_path: Path,
) -> None:
    """With `admission_limits` omitted, the build must use exactly the
    shared `DEFAULT_ADMISSION_LIMITS` object — proven both by the rejected
    decision carrying that exact object and by a closed-PR count (501) that
    is over the default's 500 but would fit comfortably under every other
    dimension's much larger default. An explicit override still wins over
    the default for the same estimate."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        if path == "/repos/octo-org/example-repo/issues":
            return json_response(200, [{}])
        if path == "/repos/octo-org/example-repo/pulls":
            last_url = (
                "https://api.github.com/repos/octo-org/example-repo/pulls"
                "?state=closed&per_page=1&page=501"
            )
            return json_response(
                200, [{}], headers={"Link": f'<{last_url}>; rel="last"'}
            )
        if path == "/repos/octo-org/example-repo/commits":
            return json_response(200, [{}])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    with pytest.raises(NormalizedSourceBuildRejected) as excinfo:
        build_normalized_source_snapshot(
            _metadata(),
            github_client=GitHubClient(
                token=_SECRET_TOKEN, transport=mock_transport(handler)
            ),
            snapshot_root=tmp_path,
            # admission_limits and runtime_limits both omitted.
        )

    assert excinfo.value.decision.limits == DEFAULT_ADMISSION_LIMITS
    assert excinfo.value.decision.reason_code == "exceeds_closed_pull_request_limit"
    assert list(tmp_path.rglob("*")) == []

    # An explicit override raising just the exceeded dimension above the
    # shared default must make admission pass for the identical estimate:
    # the next failure is a malformed-PR-item parse error from collection
    # (this handler's placeholder `{}` items), never
    # `NormalizedSourceBuildRejected` — proving the override, not the
    # default, governed the admission comparison.
    override_limits = AdmissionLimits(
        max_all_issues_and_pull_requests=(
            DEFAULT_ADMISSION_LIMITS.max_all_issues_and_pull_requests
        ),
        max_closed_pull_requests=600,
        max_commits=DEFAULT_ADMISSION_LIMITS.max_commits,
        max_tree_entries=DEFAULT_ADMISSION_LIMITS.max_tree_entries,
    )
    with pytest.raises(GitHubMalformedResponse):
        build_normalized_source_snapshot(
            _metadata(),
            github_client=GitHubClient(
                token=_SECRET_TOKEN, transport=mock_transport(handler)
            ),
            snapshot_root=tmp_path,
            admission_limits=override_limits,
            runtime_limits=_GENEROUS_RUNTIME_LIMITS,
        )


def test_build_normalized_source_snapshot_raises_when_over_runtime_limit(
    tmp_path: Path,
) -> None:
    """A runtime ingestion limit bounds what was *actually* collected, after
    admission already passed; exceeding it aborts before any publish, so
    no partial snapshot is ever written."""
    tight_runtime_limits = RuntimeIngestionLimits(
        max_source_count=1, max_github_request_count=1000
    )

    with pytest.raises(Exception) as excinfo:
        build_normalized_source_snapshot(
            _metadata(),
            github_client=GitHubClient(
                token=_SECRET_TOKEN, transport=_full_transport()
            ),
            snapshot_root=tmp_path,
            admission_limits=_GENEROUS_LIMITS,
            runtime_limits=tight_runtime_limits,
        )

    assert excinfo.value.reason_code == "exceeds_actual_source_count_limit"  # type: ignore[attr-defined]
    assert list(tmp_path.rglob("*")) == []


def test_build_normalized_source_snapshot_raises_when_request_budget_exceeded(
    tmp_path: Path,
) -> None:
    """The GitHub request budget scoped to source collection stops a build
    mid-collection — not merely after the complete corpus was already
    fetched — and surfaces the same `exceeds_actual_request_count_limit`
    reason, with no snapshot published."""
    tight_runtime_limits = RuntimeIngestionLimits(
        max_source_count=1000, max_github_request_count=1
    )

    with pytest.raises(RuntimeIngestionLimitExceeded) as excinfo:
        build_normalized_source_snapshot(
            _metadata(),
            github_client=GitHubClient(
                token=_SECRET_TOKEN, transport=_full_transport()
            ),
            snapshot_root=tmp_path,
            admission_limits=_GENEROUS_LIMITS,
            runtime_limits=tight_runtime_limits,
        )

    assert excinfo.value.reason_code == "exceeds_actual_request_count_limit"
    assert list(tmp_path.rglob("*")) == []


def test_measurement_summary_excludes_credentials_and_counts_requests(
    tmp_path: Path,
) -> None:
    result = build_normalized_source_snapshot(
        _metadata(),
        github_client=GitHubClient(token=_SECRET_TOKEN, transport=_full_transport()),
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
        runtime_limits=_GENEROUS_RUNTIME_LIMITS,
    )

    assert result.measurement.total_github_request_count > 0
    assert result.measurement.total_elapsed_seconds >= 0
    assert {phase.phase for phase in result.measurement.phases} == {
        "revision_resolution",
        "admission_estimation",
        "source_collection",
        "snapshot_publication",
        "snapshot_validation",
    }
    for phase in result.measurement.phases:
        assert _SECRET_TOKEN not in repr(phase)


def test_preflight_uses_shared_admission_defaults_when_omitted(tmp_path: Path) -> None:
    """Preflight with `snapshot_root` supplied but `admission_limits`
    omitted must reject using exactly the shared
    `DEFAULT_ADMISSION_LIMITS` — proven by a closed-PR count (501) over the
    default's 500 but comfortably under every other dimension's much larger
    default. An explicit override for just that dimension then admits the
    identical estimate."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo":
            return json_response(200, load_fixture("repository_public.json"))
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        if path == "/repos/octo-org/example-repo/issues":
            return json_response(200, [{}])
        if path == "/repos/octo-org/example-repo/pulls":
            last_url = (
                "https://api.github.com/repos/octo-org/example-repo/pulls"
                "?state=closed&per_page=1&page=501"
            )
            return json_response(
                200, [{}], headers={"Link": f'<{last_url}>; rel="last"'}
            )
        if path == "/repos/octo-org/example-repo/commits":
            return json_response(200, [{}])
        if path == f"/repos/octo-org/example-repo/git/trees/{_TREE_SHA}":
            return json_response(
                200, {"sha": _TREE_SHA, "truncated": False, "tree": []}
            )
        raise AssertionError(f"unexpected request: {path}")

    result = preflight_repository_reference(
        "octo-org/example-repo",
        github_client=GitHubClient(
            token=_SECRET_TOKEN, transport=mock_transport(handler)
        ),
        snapshot_root=tmp_path,
        # admission_limits omitted: must fall back to DEFAULT_ADMISSION_LIMITS.
    )

    assert result.status == "unsupported"
    assert result.reason_code == "exceeds_closed_pull_request_limit"

    override_limits = AdmissionLimits(
        max_all_issues_and_pull_requests=(
            DEFAULT_ADMISSION_LIMITS.max_all_issues_and_pull_requests
        ),
        max_closed_pull_requests=600,
        max_commits=DEFAULT_ADMISSION_LIMITS.max_commits,
        max_tree_entries=DEFAULT_ADMISSION_LIMITS.max_tree_entries,
    )
    admitted_result = preflight_repository_reference(
        "octo-org/example-repo",
        github_client=GitHubClient(
            token=_SECRET_TOKEN, transport=mock_transport(handler)
        ),
        snapshot_root=tmp_path,
        admission_limits=override_limits,
    )
    assert admitted_result.status == "indexing_required"


def test_preflight_with_snapshot_and_admission_end_to_end(tmp_path: Path) -> None:
    """A compatible `sources_complete` snapshot at the exact revision
    preflight itself resolves must yield `indexing_required`, never
    `ready`, proving both revision consistency and the source/index readiness
    boundary in one workflow."""
    document = SourceDocument(
        source_id="github:octo-org/example-repo:markdown:README.md",
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        text="# Readme",
        source_url=(
            f"https://github.com/octo-org/example-repo/blob/{_COMMIT_SHA}/README.md"
        ),
    )
    publish_snapshot(
        root=tmp_path,
        repository=_IDENTITY,
        default_branch="main",
        resolved_commit_sha=_COMMIT_SHA,
        documents=(document,),
        counts_by_source_type={"markdown": 1},
        producer_version="test/0",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/octo-org/example-repo":
            return json_response(200, load_fixture("repository_public.json"))
        if path == "/repos/octo-org/example-repo/commits/main":
            return json_response(
                200, {"sha": _COMMIT_SHA, "commit": {"tree": {"sha": _TREE_SHA}}}
            )
        raise AssertionError(f"unexpected request: {path}")

    client = GitHubClient(token=_SECRET_TOKEN, transport=mock_transport(handler))

    result = preflight_repository_reference(
        "octo-org/example-repo",
        github_client=client,
        snapshot_root=tmp_path,
        admission_limits=_GENEROUS_LIMITS,
    )

    assert result.status in ("ready", "indexing_required", "unsupported")
    assert result.status == "indexing_required"
    assert result.repository is not None
    assert result.repository.owner == "octo-org"
