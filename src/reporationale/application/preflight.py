"""Application-level preflight workflow: parse, look up, and classify.

This module is the only place that combines the platform-independent
`PreflightResult` contract (`reporationale.domain.preflight`) with the
GitHub adapter (`reporationale.adapters.github`). It depends on both; the
domain package must not depend on either this module or the adapter.

`snapshot_root` and `admission_limits` are optional. When `snapshot_root` is
omitted, `preflight_repository_reference` behaves exactly as the original
bare repository-lookup preflight always has (never returning `ready`, and
never checking for an existing snapshot or evaluating admission) — this
keeps every pre-existing caller and test working unchanged; no default
snapshot directory is ever invented here. When `snapshot_root` is supplied,
the workflow additionally resolves the revision once, reuses a compatible
normalized-source snapshot when one exists, and otherwise estimates and
applies admission limits using the caller's `admission_limits` when given, or
the shared
`reporationale.application.admission.DEFAULT_ADMISSION_LIMITS` otherwise.
Even then, `ready` is still never returned: a `sources_complete` normalized
snapshot is real ingestion progress, but the chunk/embedding/vector-index
artifacts `ready` requires do not exist yet.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reporationale.adapters.github import (
    GitHubAdapterError,
    GitHubAuthenticationFailed,
    GitHubClient,
    GitHubMalformedResponse,
    GitHubRateLimited,
    GitHubRepositoryMetadata,
    GitHubRepositoryNotFound,
    GitHubRepositoryPrivate,
    GitHubTransportError,
    RepositoryReferenceRejected,
    parse_github_repository_reference,
)
from reporationale.adapters.snapshot_store import look_up_normalized_source_snapshot
from reporationale.application.admission import (
    DEFAULT_ADMISSION_LIMITS,
    AdmissionLimits,
    estimate_repository_workload,
    evaluate_admission,
)
from reporationale.domain.preflight import PreflightResult


class PreflightUnavailable(Exception):
    """Preflight could not currently be completed for an operational reason.

    This is distinct from an `unsupported` `PreflightResult`: it does not
    claim anything about whether the repository is supported, only that
    GitHub could not be reached or did not answer reliably right now. It
    never carries a raw `httpx` object, a GitHub response, or a request
    header; only a stable reason code, a safe message, and (for rate
    limiting) plain timing values are stored.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        reset_at: datetime | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.reset_at = reset_at
        self.retry_after_seconds = retry_after_seconds


def _map_adapter_error_to_unavailable(
    error: GitHubAdapterError,
) -> PreflightUnavailable:
    """Map any operational `GitHubAdapterError` to the matching
    `PreflightUnavailable`, with the exact reason codes/messages this
    workflow has always used. Shared by every call site in this module
    that can raise one (the repository lookup, revision resolution, and
    admission-estimate requests), so their mapping cannot silently drift
    apart from each other.
    """
    if isinstance(error, GitHubAuthenticationFailed):
        return PreflightUnavailable(
            "github_authentication_failed",
            "GitHub rejected the supplied credentials.",
        )
    if isinstance(error, GitHubRateLimited):
        return PreflightUnavailable(
            "github_rate_limited",
            "The GitHub API rate limit was reached; try again later.",
            reset_at=error.reset_at,
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, GitHubTransportError):
        return PreflightUnavailable(
            "github_unavailable",
            "GitHub could not be reached due to a network or timeout error.",
        )
    if isinstance(error, GitHubMalformedResponse):
        return PreflightUnavailable(
            "github_malformed_response",
            "GitHub returned a response that could not be understood.",
        )
    # GitHubUnexpectedResponse and any other adapter-owned operational
    # failure not given a more specific mapping above — including
    # redirect/origin protocol failures such as an untrusted redirect
    # target, a redirect cycle, or too many redirects — proves nothing
    # about repository support, so it is treated the same as an unexpected
    # GitHub response rather than crossing the application boundary as a
    # raw adapter exception.
    return PreflightUnavailable(
        "github_unexpected_response",
        "GitHub returned an unexpected response.",
    )


@dataclass(frozen=True)
class PreflightInspection:
    """`preflight_repository_reference`'s outcome, plus the GitHub
    repository metadata and resolved commit sha this workflow already
    looked up/resolved internally to produce it, when it got that far.

    Lets a caller that needs the same metadata or revision for its next
    step (building a normalized-source snapshot, for instance) reuse them
    directly instead of repeating either GitHub call. `metadata` and
    `resolved_commit_sha` are populated together whenever `result.status`
    is not `"unsupported"`; both stay `None` for an `"unsupported"`
    result, since no repository lookup ever completed successfully.
    `resolved_commit_sha` also stays `None` when `snapshot_root` was
    omitted, since no revision is resolved on that path (see
    `preflight_repository_reference`'s docstring).
    """

    result: PreflightResult
    metadata: GitHubRepositoryMetadata | None
    resolved_commit_sha: str | None


def preflight_repository_reference(
    raw: str,
    *,
    github_client: GitHubClient,
    snapshot_root: Path | None = None,
    admission_limits: AdmissionLimits | None = None,
) -> PreflightResult:
    """Validate a bare `owner/repository` reference and look it up on GitHub.

    The caller injects an already-configured `GitHubClient`; this function
    never constructs one from settings itself. Outcomes:

    - a malformed or out-of-scope reference (format only, no request made)
      -> `unsupported`, preserving the parser's specific reason and message;
    - the repository does not exist or is not accessible
      -> `unsupported` with reason `repository_not_found_or_inaccessible`;
    - the repository is accessible but private
      -> `unsupported` with reason `private_repository`;
    - a successful public lookup, with no `snapshot_root` supplied, or a
      compatible normalized-source snapshot already found
      -> `indexing_required`, using GitHub's canonical returned identity;
    - a successful public lookup whose estimated admission workload exceeds
      `admission_limits` (or the shared `DEFAULT_ADMISSION_LIMITS` when
      `admission_limits` is omitted; only evaluated when `snapshot_root` is
      supplied and no compatible snapshot was found)
      -> `unsupported`, with the specific exceeded-limit reason code.

    `ready` is never returned by this function (see the module docstring).

    Authentication failures, rate limiting, transport/timeout failures,
    and malformed or unexpected GitHub responses are operational failures,
    not admission outcomes, at every stage of this workflow. They raise
    `PreflightUnavailable` instead of returning an `unsupported` result, so
    a temporary GitHub problem is never mistaken for "this repository is
    unsupported".

    A thin wrapper around `inspect_repository_reference` for callers that
    only need the `PreflightResult`; see that function to also reuse the
    metadata/revision this workflow resolves along the way.
    """
    return inspect_repository_reference(
        raw,
        github_client=github_client,
        snapshot_root=snapshot_root,
        admission_limits=admission_limits,
    ).result


def inspect_repository_reference(
    raw: str,
    *,
    github_client: GitHubClient,
    snapshot_root: Path | None = None,
    admission_limits: AdmissionLimits | None = None,
) -> PreflightInspection:
    """Same outcomes as `preflight_repository_reference` (see its
    docstring), returned alongside whichever `GitHubRepositoryMetadata`
    and resolved commit sha this workflow already obtained while producing
    them -- so a caller that needs those next (the Streamlit composition
    root building a normalized-source snapshot after a successful check,
    for instance) never has to look the repository up or resolve its
    revision a second time.
    """
    try:
        identity = parse_github_repository_reference(raw)
    except RepositoryReferenceRejected as rejected:
        return PreflightInspection(
            result=PreflightResult(
                status="unsupported",
                reason_code=rejected.reason_code,
                message=rejected.message,
            ),
            metadata=None,
            resolved_commit_sha=None,
        )

    unavailable: PreflightUnavailable | None = None
    unsupported_reason: tuple[str, str] | None = None
    metadata: GitHubRepositoryMetadata | None = None

    try:
        metadata = github_client.get_repository(identity)
    except GitHubRepositoryNotFound:
        unsupported_reason = (
            "repository_not_found_or_inaccessible",
            "The repository was not found or is not accessible.",
        )
    except GitHubRepositoryPrivate:
        unsupported_reason = (
            "private_repository",
            "This repository is private. RepoRationale currently supports public "
            "repositories only.",
        )
    except GitHubAdapterError as error:
        unavailable = _map_adapter_error_to_unavailable(error)

    if unavailable is not None:
        raise unavailable
    if unsupported_reason is not None:
        reason_code, message = unsupported_reason
        return PreflightInspection(
            result=PreflightResult(
                status="unsupported", reason_code=reason_code, message=message
            ),
            metadata=None,
            resolved_commit_sha=None,
        )
    if metadata is None:
        raise AssertionError(
            "unreachable: get_repository must either return metadata or raise "
            "one of the exceptions handled above"
        )

    if snapshot_root is None:
        return PreflightInspection(
            result=PreflightResult(
                status="indexing_required", repository=metadata.identity
            ),
            metadata=metadata,
            resolved_commit_sha=None,
        )

    result, resolved_commit_sha = _preflight_with_snapshot_and_admission(
        metadata,
        github_client=github_client,
        snapshot_root=snapshot_root,
        admission_limits=(
            admission_limits
            if admission_limits is not None
            else DEFAULT_ADMISSION_LIMITS
        ),
    )
    return PreflightInspection(
        result=result, metadata=metadata, resolved_commit_sha=resolved_commit_sha
    )


def _preflight_with_snapshot_and_admission(
    metadata: GitHubRepositoryMetadata,
    *,
    github_client: GitHubClient,
    snapshot_root: Path,
    admission_limits: AdmissionLimits,
) -> tuple[PreflightResult, str]:
    unavailable: PreflightUnavailable | None = None
    resolved_commit_sha: str | None = None
    resolved_tree_sha: str | None = None
    try:
        resolved_commit_sha, resolved_tree_sha = github_client.resolve_commit_and_tree(
            metadata.identity, metadata.default_branch
        )
    except GitHubAdapterError as error:
        unavailable = _map_adapter_error_to_unavailable(error)
    if unavailable is not None:
        raise unavailable
    if resolved_commit_sha is None or resolved_tree_sha is None:
        raise AssertionError(
            "unreachable: resolve_commit_and_tree must either return both "
            "identities or raise"
        )

    lookup = look_up_normalized_source_snapshot(
        root=snapshot_root,
        identity=metadata.identity,
        resolved_commit_sha=resolved_commit_sha,
    )
    if lookup.kind == "compatible":
        # A sources_complete normalized-source snapshot is real, reusable
        # ingestion progress, but the chunk/embedding/vector-index artifacts
        # `ready` requires do not exist yet, so `indexing_required` is
        # still the accurate outcome here.
        return (
            PreflightResult(status="indexing_required", repository=metadata.identity),
            resolved_commit_sha,
        )

    unavailable = None
    estimate = None
    try:
        estimate = estimate_repository_workload(
            metadata=metadata,
            resolved_commit_sha=resolved_commit_sha,
            resolved_tree_sha=resolved_tree_sha,
            admission_limits=admission_limits,
            github_client=github_client,
        )
    except GitHubAdapterError as error:
        unavailable = _map_adapter_error_to_unavailable(error)
    if unavailable is not None:
        raise unavailable
    if estimate is None:
        raise AssertionError(
            "unreachable: estimate_repository_workload must either return an "
            "estimate or raise"
        )

    decision = evaluate_admission(estimate, admission_limits)
    if not decision.admitted:
        return (
            PreflightResult(
                status="unsupported",
                reason_code=decision.reason_code,
                message=decision.message,
            ),
            resolved_commit_sha,
        )
    return (
        PreflightResult(status="indexing_required", repository=metadata.identity),
        resolved_commit_sha,
    )
