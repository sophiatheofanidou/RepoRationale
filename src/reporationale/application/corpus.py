"""Unified deterministic assembly of the currently supported GitHub corpus
for one already-validated public repository, at one already-resolved
revision.

Combines closed pull-request roots/general-comments/review-summaries/
inline-review-comments, standalone issues/comments, commit messages, and
Markdown files into one deterministically ordered, duplicate-free list of
`SourceDocument`s, plus a small summary of counts.

Does not persist anything (no snapshot/`sources.jsonl` in this batch). Any
request, pagination, validation, duplicate, decoding, or completeness
failure aborts the entire operation; nothing partial is ever returned.

Each merged pull request's merge commit SHA and merge state are already
preserved in its own normalized `metadata` (see `pull_request.
parse_closed_pull_request`); this module does not derive or persist a
separate pull-request/commit relationship record.
"""

import re
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from reporationale.adapters.github import GitHubClient
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

# A Git object ID is lowercase hex; 40 characters (SHA-1) is standard today,
# but this deliberately does not hard-code exactly 40 (a future SHA-256
# repository would use 64). Mirrors the same tolerance already established
# for the Markdown adapter's own tree/blob/commit identities.
_GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")
_PULL_REQUEST_REVIEW_WORKERS = 8


class CorpusAssemblyError(Exception):
    """A duplicate `source_id` was returned across different source types.

    Distinct from adapter-level failures (`GitHubMalformedResponse`,
    `GitHubRateLimited`, and similar), which propagate unchanged from the
    `GitHubClient` calls this function makes; either kind of failure aborts
    the complete assembly.
    """


class RepositoryCorpus(BaseModel):
    """The complete, deterministically ordered, currently supported corpus
    for one repository at one resolved revision.

    Independently enforces its own contract (a non-blank branch, a valid
    resolved-commit object id, nonnegative counts that are not coerced from
    a boolean or a numeric string, and deterministically ordered,
    duplicate-free documents that all belong to this corpus's own
    repository) during direct construction and deserialization alike,
    rather than relying only on `assemble_repository_corpus` to have
    produced a consistent value. An empty corpus is not rejected here; a
    future admission-preflight stage owns that decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: RepositoryIdentity
    default_branch: str
    resolved_commit_sha: str
    documents: tuple[SourceDocument, ...]
    counts_by_source_type: dict[str, int]

    @field_validator("default_branch")
    @classmethod
    def default_branch_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("default_branch must not be blank")
        return value

    @field_validator("resolved_commit_sha")
    @classmethod
    def resolved_commit_sha_must_be_valid_object_id(cls, value: str) -> str:
        if _GIT_OBJECT_ID_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "resolved_commit_sha must be a valid lowercase-hex Git object id"
            )
        return value

    @field_validator("counts_by_source_type", mode="before")
    @classmethod
    def counts_must_be_raw_nonnegative_ints(cls, value: object) -> object:
        """Reject a boolean or numeric-string count before pydantic's own
        lenient `int` coercion could otherwise silently accept either (a
        `bool` is coerced to `0`/`1`, and a digit string to its integer
        value) and erase the distinction this validator needs to reject."""
        if isinstance(value, dict):
            for source_type, count in value.items():
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError(
                        f"counts_by_source_type[{source_type!r}] must be a "
                        "nonnegative integer"
                    )
        return value

    @model_validator(mode="after")
    def documents_must_be_ordered_deduplicated_and_own_repository(
        self,
    ) -> "RepositoryCorpus":
        previous_source_id: str | None = None
        for document in self.documents:
            if previous_source_id is not None:
                if document.source_id == previous_source_id:
                    raise ValueError(
                        f"duplicate document source_id: {document.source_id!r}"
                    )
                if document.source_id < previous_source_id:
                    raise ValueError(
                        "documents must be ordered deterministically by source_id"
                    )
            previous_source_id = document.source_id

            if document.platform != self.repository.platform:
                raise ValueError(
                    f"document {document.source_id!r} platform does not match "
                    "the corpus repository"
                )
            if document.repository != self.repository.repository:
                raise ValueError(
                    f"document {document.source_id!r} repository does not "
                    "match the corpus repository"
                )
        return self

    @model_validator(mode="after")
    def counts_by_source_type_must_match_documents(self) -> "RepositoryCorpus":
        recomputed_counts: dict[str, int] = {}
        for document in self.documents:
            recomputed_counts[document.source_type] = (
                recomputed_counts.get(document.source_type, 0) + 1
            )
        if self.counts_by_source_type != recomputed_counts:
            raise ValueError(
                "counts_by_source_type must exactly equal the counts "
                "recomputed from documents"
            )
        return self


def _require_item_number(document: SourceDocument, *, context: str) -> int:
    if document.item_number is None:
        raise AssertionError(
            f"unreachable: a {context} root document must always carry an item_number"
        )
    return document.item_number


def _collect_pull_request_reviews(
    identity: RepositoryIdentity,
    *,
    pull_request_numbers: set[int],
    github_client: GitHubClient,
) -> list[SourceDocument]:
    """Collect independent per-PR review summaries with eight workers.

    GitHub has no repository-wide REST collection for review summaries.
    Eight workers reduce their latency while remaining below the measured
    REST point rate; failures cancel work that has not started. Results are
    flattened by PR number and the complete corpus is sorted by source ID
    later, so scheduling cannot affect persisted order.
    """
    if not pull_request_numbers:
        return []

    executor = ThreadPoolExecutor(max_workers=_PULL_REQUEST_REVIEW_WORKERS)
    futures: dict[Future[list[SourceDocument]], int] = {
        executor.submit(
            github_client.list_pull_request_reviews, identity, number
        ): number
        for number in sorted(pull_request_numbers)
    }
    reviews_by_number: dict[int, list[SourceDocument]] = {}
    try:
        for future in as_completed(futures):
            reviews_by_number[futures[future]] = future.result()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    executor.shutdown(wait=True)

    return [
        review
        for number in sorted(reviews_by_number)
        for review in reviews_by_number[number]
    ]


def assemble_repository_corpus(
    identity: RepositoryIdentity,
    *,
    default_branch: str,
    resolved_commit_sha: str,
    resolved_tree_sha: str,
    github_client: GitHubClient,
) -> RepositoryCorpus:
    """Assemble the complete supported corpus for `identity` at the
    already-resolved `resolved_commit_sha`/`resolved_tree_sha` revision.

    The caller resolves the revision exactly once (see
    `GitHubClient.resolve_commit_and_tree`) and passes it in here; this
    function performs no branch resolution of its own, so a branch that
    moves between requests cannot make this corpus's commit history,
    Markdown collection, and `resolved_commit_sha` describe different
    revisions.

    Raises whatever `GitHubClient` raises for any request, pagination, or
    per-item validation failure (propagated unchanged), or
    `CorpusAssemblyError` for a cross-source-type duplicate `source_id`.
    Nothing is returned unless the complete corpus was assembled
    successfully.
    """
    documents: list[SourceDocument] = []

    pull_requests = github_client.list_closed_pull_requests(identity)
    documents.extend(pull_requests)
    pull_request_numbers = {
        _require_item_number(pull_request, context="pull-request")
        for pull_request in pull_requests
    }
    documents.extend(
        _collect_pull_request_reviews(
            identity,
            pull_request_numbers=pull_request_numbers,
            github_client=github_client,
        )
    )

    issues = github_client.list_standalone_issues(identity)
    documents.extend(issues)
    issue_numbers = {_require_item_number(issue, context="issue") for issue in issues}

    documents.extend(
        github_client.list_repository_issue_comments(
            identity,
            standalone_issue_numbers=issue_numbers,
            closed_pull_request_numbers=pull_request_numbers,
        )
    )
    documents.extend(
        github_client.list_repository_pull_request_review_comments(
            identity,
            closed_pull_request_numbers=pull_request_numbers,
        )
    )

    documents.extend(github_client.list_commits(identity, resolved_commit_sha))

    documents.extend(
        github_client.list_markdown_documents(
            identity, commit_sha=resolved_commit_sha, tree_sha=resolved_tree_sha
        )
    )

    seen_source_ids: set[str] = set()
    for document in documents:
        if document.source_id in seen_source_ids:
            raise CorpusAssemblyError(
                f"Duplicate source_id across source types: {document.source_id!r}"
            )
        seen_source_ids.add(document.source_id)

    documents.sort(key=lambda document: document.source_id)

    counts_by_source_type: dict[str, int] = {}
    for document in documents:
        counts_by_source_type[document.source_type] = (
            counts_by_source_type.get(document.source_type, 0) + 1
        )

    return RepositoryCorpus(
        repository=identity,
        default_branch=default_branch,
        resolved_commit_sha=resolved_commit_sha,
        documents=tuple(documents),
        counts_by_source_type=counts_by_source_type,
    )
