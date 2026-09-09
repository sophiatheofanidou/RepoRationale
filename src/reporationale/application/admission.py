"""Repository-admission workload estimation and ingestion-limit enforcement.

Two deliberately different checks, at two different points in the
workflow:

- **Preflight admission** (`RepositoryWorkloadEstimate`/`AdmissionLimits`/
  `evaluate_admission`) uses only a few cheap, reliably measurable signals
  — repository size, combined issue/PR root counts, closed PR root counts,
  commit count, and tree entries — obtained via one `per_page=1` request
  (using GitHub's `Link: rel="last"` header) per dimension, or one
  recursive tree request. It performs no paid embedding/generation call
  and no GraphQL request, and it does not attempt to estimate comment,
  review, or total-request volume: those cannot be measured cheaply and
  reliably, and a fixed multiplier for them is not defensible.
- **Runtime ingestion limits** (`RuntimeIngestionLimits`/
  `check_runtime_ingestion_limits`) instead bound the *actual* normalized
  source count and *actual* GitHub request count once the complete corpus
  has actually been collected, catching whatever the cheap estimate could
  not predict. Exceeding either aborts the build before anything is
  published.

Neither check invents or hard-codes a production number: the exact
thresholds are selected later from a live measured benchmark.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from reporationale.adapters.github import GitHubClient, GitHubRepositoryMetadata
from reporationale.application.corpus import RepositoryCorpus
from reporationale.domain.repository_identity import RepositoryIdentity

_GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")


class RepositoryWorkloadEstimate(BaseModel):
    """A cheap, explicit estimate of one repository's admission-relevant
    size at one resolved revision. None of these fields claim to be what
    `assemble_repository_corpus` would eventually collect in full.

    - `repository_size_kb`: GitHub's own repository `size` metadata field
      (kibibytes on GitHub's storage), when the repository lookup provided
      it. A coarse platform storage metric, not a measure of ingestable
      text volume; `None` when GitHub did not report it.
    - `all_issues_and_pull_requests_count` /
      `all_issues_and_pull_requests_count_is_exact`: the item count from
      `GET /issues?state=all`, which GitHub documents as returning both
      standalone issues and pull requests — an upper bound on standalone-
      issue volume, not an exact issue count by itself. Some repositories
      serve this endpoint with cursor-style pagination (a `next` link and
      no `last` link), which cannot be counted with one cheap request; the
      count is then only bounded against `AdmissionLimits.
      max_all_issues_and_pull_requests` (see `count_paginated_collection_
      bounded`), and `all_issues_and_pull_requests_count_is_exact` is
      `False` — the value is a lower bound, already known to exceed the
      limit, not the true total.
    - `closed_pull_request_count`: the exact total item count from
      `GET /pulls?state=closed`.
    - `commit_count`: the exact total item count from
      `GET /commits?sha={resolved_commit_sha}`.
    - `tree_entry_count` / `tree_truncated`: the entry count from exactly
      one recursive "Get a tree" request at the resolved root tree. When
      `tree_truncated` is `True`, `tree_entry_count` is only a lower bound
      (the entries GitHub returned before truncating) — never fabricated
      as the true, complete total.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: RepositoryIdentity
    resolved_commit_sha: str
    repository_size_kb: int | None = Field(default=None, strict=True, ge=0)
    all_issues_and_pull_requests_count: int = Field(strict=True, ge=0)
    all_issues_and_pull_requests_count_is_exact: StrictBool
    closed_pull_request_count: int = Field(strict=True, ge=0)
    commit_count: int = Field(strict=True, ge=0)
    tree_entry_count: int = Field(strict=True, ge=0)
    tree_truncated: StrictBool

    @model_validator(mode="after")
    def resolved_commit_sha_must_be_valid_object_id(
        self,
    ) -> "RepositoryWorkloadEstimate":
        if _GIT_OBJECT_ID_PATTERN.fullmatch(self.resolved_commit_sha) is None:
            raise ValueError(
                "resolved_commit_sha must be a valid lowercase-hex Git object id"
            )
        return self


class AdmissionLimits(BaseModel):
    """Explicit, caller-supplied preflight admission thresholds.

    No permanent production default is defined here or anywhere in this
    module: the canonical documents require the exact numbers to follow
    measured benchmarks, so tests inject small synthetic limits and the
    real values are selected later by Codex/the user.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_all_issues_and_pull_requests: int = Field(strict=True, gt=0)
    max_closed_pull_requests: int = Field(strict=True, gt=0)
    max_commits: int = Field(strict=True, gt=0)
    max_tree_entries: int = Field(strict=True, gt=0)


# The accepted MVP admission envelope, expanded after the complete Gson
# evaluation build succeeded within these measured ceilings. This remains
# the one canonical definition shared by preflight, snapshot building, and
# the rebuild CLI; it is a tested local-MVP envelope, not a claim of
# production-scale or unlimited-repository support.
DEFAULT_ADMISSION_LIMITS = AdmissionLimits(
    max_all_issues_and_pull_requests=3500,
    max_closed_pull_requests=1500,
    max_commits=2500,
    max_tree_entries=500,
)


AdmissionReasonCode = Literal[
    "within_admission_limits",
    "exceeds_all_issues_and_pull_requests_limit",
    "exceeds_closed_pull_request_limit",
    "exceeds_commit_limit",
    "exceeds_tree_entry_limit",
    "markdown_tree_truncated_at_estimate_time",
]


class AdmissionDecision(BaseModel):
    """The deterministic outcome of comparing one `RepositoryWorkloadEstimate`
    against one `AdmissionLimits`, with the specific measured limit that was
    exceeded when `admitted` is `False`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admitted: StrictBool
    reason_code: AdmissionReasonCode
    message: str = Field(min_length=1)
    estimate: RepositoryWorkloadEstimate
    limits: AdmissionLimits

    @model_validator(mode="after")
    def admitted_must_agree_with_reason_code(self) -> "AdmissionDecision":
        if self.admitted and self.reason_code != "within_admission_limits":
            raise ValueError(
                "an admitted decision must use reason_code 'within_admission_limits'"
            )
        if not self.admitted and self.reason_code == "within_admission_limits":
            raise ValueError(
                "a non-admitted decision must not use reason_code "
                "'within_admission_limits'"
            )
        return self


def estimate_repository_workload(
    *,
    metadata: GitHubRepositoryMetadata,
    resolved_commit_sha: str,
    resolved_tree_sha: str,
    admission_limits: AdmissionLimits,
    github_client: GitHubClient,
) -> RepositoryWorkloadEstimate:
    """Estimate `metadata.identity`'s admission-relevant size at the
    already-resolved `resolved_commit_sha`/`resolved_tree_sha` revision.

    `admission_limits.max_all_issues_and_pull_requests` bounds the
    combined issue/pull-request count: some repositories serve
    `GET /issues?state=all` with cursor-style pagination, which cannot be
    counted with one cheap request, so it is instead counted only far
    enough to prove admission would reject it — see
    `GitHubClient.count_paginated_collection_bounded`.

    Raises whatever `GitHubClient` raises for a request, pagination, or
    malformed-response failure (propagated unchanged) — a temporary GitHub
    failure here is an operational failure, not an admission outcome.
    """
    identity = metadata.identity
    owner = quote(identity.owner, safe="")
    name = quote(identity.name, safe="")

    issues_count = github_client.count_paginated_collection_bounded(
        f"/repos/{owner}/{name}/issues",
        params={"state": "all"},
        max_items=admission_limits.max_all_issues_and_pull_requests,
    )
    closed_pull_request_count = github_client.count_via_last_page_link(
        f"/repos/{owner}/{name}/pulls", params={"state": "closed"}
    )
    commit_count = github_client.count_via_last_page_link(
        f"/repos/{owner}/{name}/commits", params={"sha": resolved_commit_sha}
    )
    tree_entry_count, tree_truncated = github_client.get_tree_entry_overview(
        identity, resolved_tree_sha
    )

    return RepositoryWorkloadEstimate(
        repository=identity,
        resolved_commit_sha=resolved_commit_sha,
        repository_size_kb=metadata.size_kb,
        all_issues_and_pull_requests_count=issues_count.observed_count,
        all_issues_and_pull_requests_count_is_exact=issues_count.complete,
        closed_pull_request_count=closed_pull_request_count,
        commit_count=commit_count,
        tree_entry_count=tree_entry_count,
        tree_truncated=tree_truncated,
    )


def evaluate_admission(
    estimate: RepositoryWorkloadEstimate, limits: AdmissionLimits
) -> AdmissionDecision:
    """Deterministically compare `estimate` against `limits`.

    A truncated Markdown tree is rejected outright, before any numeric
    comparison against `max_tree_entries`: its true entry count is
    unknown, and an unknown or truncated required dimension must never be
    silently treated as within limits. Each other dimension is compared in
    a fixed order, so the first exceeded limit is always the one reported.
    """
    if estimate.tree_truncated:
        return AdmissionDecision(
            admitted=False,
            reason_code="markdown_tree_truncated_at_estimate_time",
            message=(
                "The repository's Markdown tree is too large to estimate "
                "completely without a full traversal; its true size "
                "relative to the tree-entry limit is unknown."
            ),
            estimate=estimate,
            limits=limits,
        )

    checks: tuple[tuple[int, int, AdmissionReasonCode, str], ...] = (
        (
            estimate.all_issues_and_pull_requests_count,
            limits.max_all_issues_and_pull_requests,
            "exceeds_all_issues_and_pull_requests_limit",
            "Combined issues and pull requests",
        ),
        (
            estimate.closed_pull_request_count,
            limits.max_closed_pull_requests,
            "exceeds_closed_pull_request_limit",
            "Closed pull requests",
        ),
        (
            estimate.commit_count,
            limits.max_commits,
            "exceeds_commit_limit",
            "Commits",
        ),
        (
            estimate.tree_entry_count,
            limits.max_tree_entries,
            "exceeds_tree_entry_limit",
            "Git tree entries",
        ),
    )

    for measured, limit, reason_code, label in checks:
        if measured > limit:
            qualifier = (
                "at least "
                if reason_code == "exceeds_all_issues_and_pull_requests_limit"
                and not estimate.all_issues_and_pull_requests_count_is_exact
                else ""
            )
            return AdmissionDecision(
                admitted=False,
                reason_code=reason_code,
                message=(
                    f"{label} — estimated: {qualifier}{measured:,}. "
                    f"Current limit: {limit:,}."
                ),
                estimate=estimate,
                limits=limits,
            )

    return AdmissionDecision(
        admitted=True,
        reason_code="within_admission_limits",
        message="The repository's estimated workload is within the supplied limits.",
        estimate=estimate,
        limits=limits,
    )


# --- Runtime ingestion limits: enforced after the complete corpus is ------
# --- actually collected, not merely estimated.                           -


@dataclass(frozen=True)
class RuntimeIngestionLimits:
    """Hard caps on the actually-collected corpus, applied after complete
    corpus collection and before anything is published. Unlike
    `AdmissionLimits`, these bound measured reality, not an estimate."""

    max_source_count: int
    max_github_request_count: int

    def __post_init__(self) -> None:
        for name in ("max_source_count", "max_github_request_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a strict positive integer")


# The accepted MVP runtime envelope, expanded after Gson completed with
# 13,893 normalized sources and 1,371 source-collection requests. The
# rounded ceilings preserve measured headroom while remaining hard stops
# before publication; the request cap is not a substitute for GitHub's own
# rate-limit enforcement.
DEFAULT_RUNTIME_INGESTION_LIMITS = RuntimeIngestionLimits(
    max_source_count=15000,
    max_github_request_count=2000,
)


RuntimeIngestionReasonCode = Literal[
    "exceeds_actual_source_count_limit",
    "exceeds_actual_request_count_limit",
]


class RuntimeIngestionLimitExceeded(Exception):
    """The complete, already-collected corpus exceeded a runtime ingestion
    limit. Raised before publication; the caller must not publish any
    part of the corpus that triggered this."""

    def __init__(
        self,
        *,
        reason_code: RuntimeIngestionReasonCode,
        message: str,
        measured: int,
        limit: int,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.measured = measured
        self.limit = limit


def check_runtime_ingestion_limits(
    corpus: RepositoryCorpus, *, request_count: int, limits: RuntimeIngestionLimits
) -> None:
    """Raise `RuntimeIngestionLimitExceeded` if the actually-collected
    `corpus` or the actual `request_count` spent collecting it exceeds
    `limits`. Called after `assemble_repository_corpus` returns and before
    the corpus is published, so a partial snapshot is never published."""
    source_count = len(corpus.documents)
    if source_count > limits.max_source_count:
        raise RuntimeIngestionLimitExceeded(
            reason_code="exceeds_actual_source_count_limit",
            message=(
                "The complete collected corpus exceeds the runtime source-count limit."
            ),
            measured=source_count,
            limit=limits.max_source_count,
        )
    if request_count > limits.max_github_request_count:
        raise RuntimeIngestionLimitExceeded(
            reason_code="exceeds_actual_request_count_limit",
            message=(
                "Collecting the complete corpus exceeded the runtime GitHub "
                "request-count limit."
            ),
            measured=request_count,
            limit=limits.max_github_request_count,
        )
