"""GitHub REST adapter: repository metadata lookup, the complete supported
source collection (pull requests, issues, commits, Markdown), revision
resolution, cheap admission-estimate signals, and reusable pagination.

Keeps raw HTTP and GitHub response handling inside this adapter boundary;
only `RepositoryIdentity`, `GitHubRepositoryMetadata`, and `SourceDocument`
cross into the rest of the application.

Security notes:

- The configured `base_url` is validated once, at construction time, to be
  the trusted HTTPS `api.github.com` origin with no userinfo, unexpected
  port, path, query, or fragment. Construction fails before any request can
  be sent if it is not.
- Every request target — the initial URL passed to `_send` (whether a
  relative path or an absolute URL a caller supplies to
  `get_paginated_collection`), a pagination `Link: rel="next"` target, and
  every redirect target — is validated against that trusted origin before
  `_auth_headers()` is ever called for it, so the bearer token can never be
  sent to an unexpected host, a plaintext HTTP origin, a non-default port,
  or a URL carrying userinfo.
- Redirects are not followed automatically by `httpx`. This client follows
  only the redirect statuses GitHub uses for a renamed/transferred
  repository, and only after validating the resolved target the same way.
- Every place that converts a lower-level failure (an `httpx` exception, an
  invalid response body, a Pydantic validation error) into a typed adapter
  error raises the new error only after the originating `except` block has
  fully exited, so neither `__cause__` nor `__context__` on the raised
  error can reach the original exception, a `JSONDecodeError`'s retained
  response body, its `httpx.Request`/`httpx.Response`, or the Authorization
  header. See `errors.py` for the same note on the exception classes.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    SecretStr,
    StrictBool,
    ValidationError,
    field_validator,
)

from reporationale.adapters.github._shared import (
    extract_repository_item_number,
    require_github_platform,
    require_strict_positive_int,
)
from reporationale.adapters.github.commits import parse_commit
from reporationale.adapters.github.errors import (
    GitHubAuthenticationFailed,
    GitHubMalformedResponse,
    GitHubPaginationCycleDetected,
    GitHubRateLimited,
    GitHubRedirectCycleDetected,
    GitHubRepositoryNotFound,
    GitHubRepositoryPrivate,
    GitHubRequestBudgetExceeded,
    GitHubTooManyRedirects,
    GitHubTransportError,
    GitHubUnexpectedResponse,
    GitHubUntrustedOriginRejected,
)
from reporationale.adapters.github.issue import (
    parse_issue_comment,
    parse_standalone_issue,
)
from reporationale.adapters.github.markdown import (
    GitTreeEntry,
    decode_markdown_blob,
    is_markdown_path,
    is_regular_file_blob,
    parse_commit_reference,
    parse_git_tree,
    parse_markdown_document,
)
from reporationale.adapters.github.models import (
    GitHubRepositoryMetadata,
    validate_canonical_github_url,
)
from reporationale.adapters.github.pagination import parse_link_header
from reporationale.adapters.github.pull_request import (
    parse_closed_pull_request,
    parse_pull_request_general_comment,
    parse_pull_request_review,
    parse_pull_request_review_comment,
)
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

_DEFAULT_BASE_URL = "https://api.github.com"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_USER_AGENT = "RepoRationale-GitHub-Adapter/0.1"
_API_VERSION = "2022-11-28"

_TRUSTED_SCHEME = "https"
_TRUSTED_HTTPS_PORT = 443
_TRUSTED_API_HOSTNAME = "api.github.com"

# GitHub uses 301 Moved Permanently for a renamed/transferred repository
# lookup. No other redirect status is required by this adapter.
_ALLOWED_REDIRECT_STATUSES = frozenset({301})
_MAX_REDIRECTS = 5

_RATE_LIMIT_MESSAGE_MARKER = "rate limit"

_PULL_REQUESTS_PER_PAGE = "100"
_COMMENTS_PER_PAGE = "100"
_ITEMS_PER_PAGE = "100"


def _validate_base_url(base_url: str) -> str:
    """Validate the configured GitHub API origin before any request is sent.

    The MVP supports GitHub.com only: exactly `https://api.github.com` (an
    explicit default port and a trailing slash are tolerated). Anything
    else is a configuration error raised at construction time, not a
    runtime adapter failure.
    """
    candidate = base_url.rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain userinfo")
    if parsed.scheme.lower() != _TRUSTED_SCHEME:
        raise ValueError("base_url must use https")
    if (parsed.hostname or "").lower() != _TRUSTED_API_HOSTNAME:
        raise ValueError(f"base_url must use host {_TRUSTED_API_HOSTNAME!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port") from exc
    if port is not None and port != _TRUSTED_HTTPS_PORT:
        raise ValueError("base_url must use the default HTTPS port")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("base_url must not contain a path")
    return candidate


def _looks_absolute(url: str) -> bool:
    """A URL counts as absolute (and therefore needs trusted-origin
    validation) if it carries a scheme or an authority component, so a
    scheme-relative URL (`//host/path`) cannot slip through as a plain
    relative path."""
    parsed = urlsplit(url)
    return bool(parsed.scheme) or bool(parsed.netloc)


@dataclass(frozen=True)
class BoundedPaginationCount:
    """The result of `GitHubClient.count_paginated_collection_bounded`.

    `observed_count` is the exact total when `complete` is `True`. When
    `complete` is `False`, counting stopped early because rejection
    against the caller's `max_items` threshold was already certain;
    `observed_count` is then only a lower bound on the true total and
    must never be presented as exact.
    """

    observed_count: int
    complete: bool


class _GitHubRepositoryOwnerResponse(BaseModel):
    """The subset of a GitHub repository response owner object that is used."""

    model_config = ConfigDict(extra="ignore", strict=True)

    login: str = Field(min_length=1)

    @field_validator("login")
    @classmethod
    def login_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("owner login must not be blank")
        return value


class _GitHubRepositoryResponse(BaseModel):
    """The subset of a GitHub repository REST response that is used.

    Strict typing rejects coerced values (a numeric ID string, a string in
    place of a boolean, and similar) that a lax parse would otherwise accept.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    id: PositiveInt
    name: str = Field(min_length=1)
    html_url: str = Field(min_length=1)
    private: StrictBool
    default_branch: str = Field(min_length=1)
    owner: _GitHubRepositoryOwnerResponse
    # Optional: older fixtures and some documented responses omit it, and
    # a repository lookup must keep working without it.
    size: int | None = None

    @field_validator("name", "default_branch")
    @classmethod
    def value_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("size")
    @classmethod
    def size_must_be_nonnegative_when_present(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError("size must be a nonnegative integer when present")
        return value


class GitHubClient:
    """A small, adapter-owned client for the versioned GitHub REST API."""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = SecretStr(token) if token else None
        self._base_url = _validate_base_url(base_url)
        self._expected_host = _TRUSTED_API_HOSTNAME
        self._request_count = 0
        self._request_budget_limit: int | None = None
        self._request_budget_remaining: int | None = None
        self._request_state_lock = Lock()
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION,
                "User-Agent": _USER_AGENT,
            },
        )

    @property
    def request_count(self) -> int:
        """The number of actual HTTP requests sent so far (one per
        redirect hop included), for phase-level measurement reporting.
        Never exposes headers, credentials, or the requests themselves."""
        with self._request_state_lock:
            return self._request_count

    @contextmanager
    def limit_requests(self, max_requests: int) -> Iterator[None]:
        """Enforce a hard cap of `max_requests` actual HTTP attempts —
        pagination pages and redirect hops included, since every one of
        them goes through `_send_once` — for the duration of this `with`
        block.

        Checked before each request is sent: requests 1 through
        `max_requests` may go out; request `max_requests + 1` is rejected
        with `GitHubRequestBudgetExceeded` and never sent. Scoped strictly
        to this block — the previous budget (including no budget at all)
        is restored on exit, whether the block succeeds or raises, so it
        can never leak into a caller's later, unrelated use of this client.
        """
        require_strict_positive_int(max_requests, name="max_requests")
        with self._request_state_lock:
            previous_limit = self._request_budget_limit
            previous_remaining = self._request_budget_remaining
            self._request_budget_limit = max_requests
            self._request_budget_remaining = max_requests
        try:
            yield
        finally:
            with self._request_state_lock:
                self._request_budget_limit = previous_limit
                self._request_budget_remaining = previous_remaining

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_repository(self, identity: RepositoryIdentity) -> GitHubRepositoryMetadata:
        """Look up and normalize public repository metadata.

        Raises `GitHubRepositoryPrivate` if the repository is accessible but
        private, even when the supplied token could read it, since private
        repositories are outside the MVP scope.
        """
        require_github_platform(identity)

        path = (
            f"/repos/{quote(identity.owner, safe='')}/{quote(identity.name, safe='')}"
        )
        response = self._send("GET", path)
        self._raise_for_status(response)
        return self._parse_repository_metadata(response)

    def list_closed_pull_requests(
        self, identity: RepositoryIdentity
    ) -> list[SourceDocument]:
        """Fetch and normalize every closed pull-request root record.

        Includes both merged and closed-unmerged pull requests; excludes
        open pull requests via `state=closed`. Normalizes only the PR root
        title/body/lifecycle facts into one `SourceDocument` per pull
        request — comments, reviews, and diffs are out of scope here.

        Fails the entire collection (returning nothing) if any page or
        item is malformed, or if GitHub returns a repeated pull-request
        identity across pages, rather than returning a partial corpus.
        """
        require_github_platform(identity)

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/pulls"
        )
        raw_items = self.get_paginated_collection(
            path, params={"state": "closed", "per_page": _PULL_REQUESTS_PER_PAGE}
        )

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for raw_item in raw_items:
            document = parse_closed_pull_request(raw_item, identity=identity)
            if document.source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated pull-request identity across pages."
                )
            seen_source_ids.add(document.source_id)
            documents.append(document)
        return documents

    @staticmethod
    def _extract_comment_parent_number(
        raw_item: Mapping[str, JsonValue],
        *,
        field_name: str,
        identity: RepositoryIdentity,
        resource: str,
    ) -> int:
        """Read and validate a repository-wide comment's parent URL."""
        parent_number: int | None = None
        malformed = False
        try:
            parent_number = extract_repository_item_number(
                raw_item.get(field_name), identity=identity, resource=resource
            )
        except ValueError:
            malformed = True
        if malformed:
            raise GitHubMalformedResponse(
                "The GitHub API repository-wide comment response has an "
                "invalid parent URL."
            )
        if parent_number is None:
            raise AssertionError("unreachable: parent number must be set")
        return parent_number

    def list_repository_issue_comments(
        self,
        identity: RepositoryIdentity,
        *,
        standalone_issue_numbers: set[int],
        closed_pull_request_numbers: set[int],
    ) -> list[SourceDocument]:
        """Fetch all repository issue-style comments in one paginated stream.

        GitHub represents both standalone-issue comments and a pull
        request's general conversation comments as "issue comments". The
        response's canonical `issue_url` assigns each record to one of the
        already-collected parent-number sets. Comments for parents outside
        the supported corpus (for example an open PR) are ignored.
        """
        require_github_platform(identity)
        for number in standalone_issue_numbers:
            require_strict_positive_int(number, name="standalone_issue_number")
        for number in closed_pull_request_numbers:
            require_strict_positive_int(number, name="closed_pull_request_number")
        if standalone_issue_numbers & closed_pull_request_numbers:
            raise ValueError("issue and pull-request parent numbers must be disjoint")
        if not standalone_issue_numbers and not closed_pull_request_numbers:
            return []

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/issues/comments"
        )
        raw_items = self.get_paginated_collection(
            path, params={"per_page": _COMMENTS_PER_PAGE}
        )

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for raw_item in raw_items:
            parent_number = self._extract_comment_parent_number(
                raw_item,
                field_name="issue_url",
                identity=identity,
                resource="issues",
            )
            if parent_number in standalone_issue_numbers:
                source_id, document = parse_issue_comment(
                    raw_item, identity=identity, issue_number=parent_number
                )
            elif parent_number in closed_pull_request_numbers:
                source_id, document = parse_pull_request_general_comment(
                    raw_item,
                    identity=identity,
                    pull_request_number=parent_number,
                )
            else:
                continue
            if source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated issue-comment identity across pages."
                )
            seen_source_ids.add(source_id)
            if document is not None:
                documents.append(document)
        return documents

    def list_pull_request_reviews(
        self, identity: RepositoryIdentity, pull_request_number: int
    ) -> list[SourceDocument]:
        """Fetch and normalize a pull request's non-empty review summaries.

        Collects `GET /repos/{owner}/{repo}/pulls/{number}/reviews`. Only
        the review's own summary body is normalized here; inline review
        comments/replies are a separate source type.

        Fails the entire collection if any page or review is malformed, or
        if GitHub returns a repeated review identity across pages
        (including one later skipped for an empty body).
        """
        require_github_platform(identity)
        require_strict_positive_int(pull_request_number, name="pull_request_number")

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/pulls/{pull_request_number}/reviews"
        )
        raw_items = self.get_paginated_collection(
            path, params={"per_page": _ITEMS_PER_PAGE}
        )

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for raw_item in raw_items:
            source_id, document = parse_pull_request_review(
                raw_item, identity=identity, pull_request_number=pull_request_number
            )
            if source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated pull-request review identity "
                    "across pages."
                )
            seen_source_ids.add(source_id)
            if document is not None:
                documents.append(document)
        return documents

    def list_repository_pull_request_review_comments(
        self,
        identity: RepositoryIdentity,
        *,
        closed_pull_request_numbers: set[int],
    ) -> list[SourceDocument]:
        """Fetch all repository inline review comments in one stream.

        The response's canonical `pull_request_url` assigns each record to
        an already-collected closed PR. Comments for open PRs are ignored.
        Reply-parent validation still covers the complete retained stream.
        """
        require_github_platform(identity)
        for number in closed_pull_request_numbers:
            require_strict_positive_int(number, name="closed_pull_request_number")
        if not closed_pull_request_numbers:
            return []

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/pulls/comments"
        )
        raw_items = self.get_paginated_collection(
            path, params={"per_page": _ITEMS_PER_PAGE}
        )

        seen_source_ids: set[str] = set()
        seen_comment_ids: set[int] = set()
        records: list[tuple[int, int | None, SourceDocument | None]] = []
        for raw_item in raw_items:
            pull_request_number = self._extract_comment_parent_number(
                raw_item,
                field_name="pull_request_url",
                identity=identity,
                resource="pulls",
            )
            if pull_request_number not in closed_pull_request_numbers:
                continue
            source_id, comment_id, in_reply_to_id, document = (
                parse_pull_request_review_comment(
                    raw_item,
                    identity=identity,
                    pull_request_number=pull_request_number,
                )
            )
            if source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated pull-request review-comment "
                    "identity across pages."
                )
            seen_source_ids.add(source_id)
            seen_comment_ids.add(comment_id)
            records.append((comment_id, in_reply_to_id, document))

        for _, in_reply_to_id, _document in records:
            if in_reply_to_id is not None and in_reply_to_id not in seen_comment_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a review-comment reply whose referenced "
                    "parent comment is missing from the collected response."
                )

        return [document for _, _, document in records if document is not None]

    def list_standalone_issues(
        self, identity: RepositoryIdentity
    ) -> list[SourceDocument]:
        """Fetch and normalize every standalone issue root record.

        Collects `GET /repos/{owner}/{repo}/issues?state=all`, which also
        returns pull requests; an item carrying the documented
        `pull_request` marker is excluded (PR roots are collected via
        `list_closed_pull_requests`). Includes open and closed issues.

        Fails the entire collection if any page or item is malformed, or
        if GitHub returns a repeated issue/PR-shaped identity across pages.
        """
        require_github_platform(identity)

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/issues"
        )
        raw_items = self.get_paginated_collection(
            path, params={"state": "all", "per_page": _ITEMS_PER_PAGE}
        )

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for raw_item in raw_items:
            source_id, is_pull_request, document = parse_standalone_issue(
                raw_item, identity=identity
            )
            if source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated issue identity across pages."
                )
            seen_source_ids.add(source_id)
            if not is_pull_request and document is not None:
                documents.append(document)
        return documents

    def list_commits(
        self, identity: RepositoryIdentity, revision: str
    ) -> list[SourceDocument]:
        """Fetch and normalize every commit message reachable from `revision`.

        Collects `GET /repos/{owner}/{repo}/commits`, without path, author,
        or date filtering. `revision` is passed as GitHub's `sha` query
        parameter, which accepts either a branch name or an exact commit
        sha; the supported corpus-assembly workflow always resolves the
        default branch to an exact commit sha first (see
        `resolve_commit_and_tree`) and passes that, so that a branch moving
        between requests cannot make this collection and a separately
        resolved Markdown tree disagree about which revision they describe.

        Fails the entire collection if any page or commit is malformed, or
        if GitHub returns a repeated commit SHA across pages.
        """
        require_github_platform(identity)

        path = (
            f"/repos/{quote(identity.owner, safe='')}/"
            f"{quote(identity.name, safe='')}/commits"
        )
        raw_items = self.get_paginated_collection(
            path,
            params={"sha": revision, "per_page": _ITEMS_PER_PAGE},
        )

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for raw_item in raw_items:
            document = parse_commit(raw_item, identity=identity)
            if document.source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated commit identity across pages."
                )
            seen_source_ids.add(document.source_id)
            documents.append(document)
        return documents

    def resolve_commit_and_tree(
        self, identity: RepositoryIdentity, ref: str
    ) -> tuple[str, str]:
        """Resolve `ref` (typically a branch name) to its exact commit sha
        and root-tree sha, via `GET /repos/{owner}/{repo}/commits/{ref}`.

        This is the single place a moving branch name is ever resolved.
        The supported corpus-assembly workflow calls this exactly once and
        reuses both returned values (the commit sha for commit-history
        collection, the tree sha for Markdown collection) so a branch that
        moves between requests cannot make those two collections describe
        different revisions.
        """
        require_github_platform(identity)

        owner = quote(identity.owner, safe="")
        name = quote(identity.name, safe="")
        ref_path = f"/repos/{owner}/{name}/commits/{quote(ref, safe='')}"
        ref_response = self._send("GET", ref_path)
        self._raise_for_status(ref_response)
        return parse_commit_reference(
            self._response_json(ref_response, context="a commit reference")
        )

    def list_markdown_documents(
        self, identity: RepositoryIdentity, *, commit_sha: str, tree_sha: str
    ) -> list[SourceDocument]:
        """Fetch and normalize every non-empty Markdown file at the exact,
        already-resolved `commit_sha`/`tree_sha` revision.

        Both identities must come from the same prior `resolve_commit_and_tree`
        call; this method performs no branch resolution of its own (a moving
        branch name is not a valid argument here), which is what keeps this
        collection and a separately collected commit history anchored to
        the same immutable revision. Collects the complete tree at
        `tree_sha` (falling back to a manual non-recursive walk if GitHub
        reports the recursive tree as truncated), and fetches every `.md`/
        `.markdown` blob individually (there is no list endpoint that
        returns file contents).

        Fails the entire operation if the tree cannot be completely
        resolved, if any blob is malformed, or if GitHub returns a repeated
        Markdown path/identity.
        """
        require_github_platform(identity)

        owner = quote(identity.owner, safe="")
        name = quote(identity.name, safe="")

        entries = self._collect_complete_tree(identity, tree_sha)

        documents: list[SourceDocument] = []
        seen_source_ids: set[str] = set()
        for entry in entries:
            if not is_regular_file_blob(entry) or not is_markdown_path(entry.path):
                continue

            blob_path = f"/repos/{owner}/{name}/git/blobs/{quote(entry.sha, safe='')}"
            blob_response = self._send("GET", blob_path)
            self._raise_for_status(blob_response)
            content = decode_markdown_blob(
                self._response_json(blob_response, context="a Markdown blob"),
                expected_sha=entry.sha,
            )

            source_id, document = parse_markdown_document(
                identity=identity,
                commit_sha=commit_sha,
                blob_sha=entry.sha,
                path=entry.path,
                content=content,
            )
            if source_id in seen_source_ids:
                raise GitHubMalformedResponse(
                    "GitHub returned a repeated Markdown file identity."
                )
            seen_source_ids.add(source_id)
            if document is not None:
                documents.append(document)

        return documents

    def _fetch_tree(
        self, identity: RepositoryIdentity, tree_sha: str, *, recursive: bool
    ) -> tuple[list[GitTreeEntry], bool]:
        """Fetch one "Get a tree" response and validate its identity.

        Raises `GitHubMalformedResponse` if the response's own `sha` does
        not match `tree_sha` — the exact tree object that was requested —
        so a response that silently describes a different tree can never
        be trusted as the answer to this request.
        """
        owner = quote(identity.owner, safe="")
        name = quote(identity.name, safe="")
        path = f"/repos/{owner}/{name}/git/trees/{quote(tree_sha, safe='')}"
        params = {"recursive": "1"} if recursive else None
        response = self._send("GET", path, params=params)
        self._raise_for_status(response)
        returned_sha, entries, truncated = parse_git_tree(
            self._response_json(response, context="a Git tree")
        )
        if returned_sha != tree_sha:
            raise GitHubMalformedResponse(
                "The GitHub API tree response sha does not match the "
                "requested tree sha."
            )
        return entries, truncated

    def _collect_complete_tree(
        self, identity: RepositoryIdentity, tree_sha: str
    ) -> list[GitTreeEntry]:
        """Return the complete, flattened tree at `tree_sha` with
        repository-relative paths.

        Tries one recursive fetch first. If GitHub reports that response as
        `truncated`, falls back to a manual, non-recursive walk that fails
        safely (rather than silently accepting an incomplete tree) if it is
        still too large to resolve that way.

        Since Git tree objects are content-addressed, the exact same
        subtree sha can legitimately be referenced from more than one
        repository path (for example, two directories with identical
        contents); that is not a cycle and must not be rejected. Only a
        genuine cycle — a tree sha reappearing among its own ancestors
        along one traversal branch — is rejected. A duplicate *resolved*
        path (the same final path produced by two different branches of
        the traversal) is separately rejected, since a well-formed tree
        cannot legitimately produce that.
        """
        entries, truncated = self._fetch_tree(identity, tree_sha, recursive=True)
        if not truncated:
            return entries

        collected: list[GitTreeEntry] = []
        seen_full_paths: set[str] = set()

        def walk(current_sha: str, prefix: str, ancestry: frozenset[str]) -> None:
            if current_sha in ancestry:
                raise GitHubMalformedResponse(
                    "GitHub returned a tree traversal cycle while resolving a "
                    "truncated tree."
                )
            sub_entries, sub_truncated = self._fetch_tree(
                identity, current_sha, recursive=False
            )
            if sub_truncated:
                raise GitHubMalformedResponse(
                    "A non-recursive GitHub tree response was still truncated; "
                    "the tree could not be safely resolved as complete."
                )

            child_ancestry = ancestry | {current_sha}
            for entry in sub_entries:
                full_path = f"{prefix}{entry.path}"
                if full_path in seen_full_paths:
                    raise GitHubMalformedResponse(
                        "GitHub returned a duplicate resolved repository path "
                        "while resolving a truncated tree."
                    )
                seen_full_paths.add(full_path)
                if entry.type == "tree":
                    walk(entry.sha, f"{full_path}/", child_ancestry)
                else:
                    collected.append(
                        GitTreeEntry(
                            path=full_path,
                            mode=entry.mode,
                            type=entry.type,
                            sha=entry.sha,
                        )
                    )

        walk(tree_sha, "", frozenset())
        return collected

    def get_tree_entry_overview(
        self, identity: RepositoryIdentity, tree_sha: str
    ) -> tuple[int, bool]:
        """Cheaply estimate a Git tree's size via exactly one recursive
        "Get a tree" request, without walking a truncated tree to
        completion the way `list_markdown_documents` does.

        Returns `(entry_count, truncated)`. When `truncated` is `True`,
        `entry_count` is only the count of entries GitHub returned in that
        one incomplete response — a lower bound, not the true total —
        since resolving the true total would require the same complete
        walk this estimate is meant to avoid paying for.
        """
        require_github_platform(identity)
        entries, truncated = self._fetch_tree(identity, tree_sha, recursive=True)
        return len(entries), truncated

    def count_via_last_page_link(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> int:
        """Cheaply estimate a paginated collection's total item count from
        GitHub's `Link: rel="last"` header with `per_page=1`, without
        collecting, validating, or normalizing any item body.

        Requests exactly one page of at most one item. If GitHub returns a
        `last` link, its `page` query parameter times the one item per page
        is the total count. If there is no `last` link, the collection is
        small enough to fit on the single requested page, so the count is
        the number of items in that one page's body (0 or 1) — never
        fabricated as zero for an unread collection.

        Raises `GitHubMalformedResponse` if the response is not the
        documented JSON array of objects, if a `last` link is present but
        its `page` parameter is missing or not a positive integer, or if
        the response carries a `next` link with no `last` link — GitHub's
        cursor-style pagination, which this `per_page=1`/`last`-page trick
        cannot answer; such a response is never treated as a complete
        single-page collection. Use `count_paginated_collection_bounded`
        for an endpoint that paginates this way.
        """
        merged_params: dict[str, str] = {**(params or {}), "per_page": "1"}
        response = self._send("GET", path, params=merged_params)
        self._raise_for_status(response)
        page = self._parse_collection_page(response)

        links = parse_link_header(response.headers.get("Link"))
        last_link = links.get("last")
        if last_link is None:
            if links.get("next") is not None:
                raise GitHubMalformedResponse(
                    "The GitHub API returned cursor-style pagination (a "
                    "'next' link with no 'last' link) for a collection "
                    "this cheap per_page=1 count cannot resolve."
                )
            return len(page)
        return self._extract_last_page_number(last_link)

    def count_paginated_collection_bounded(
        self, path: str, *, params: Mapping[str, str] | None = None, max_items: int
    ) -> "BoundedPaginationCount":
        """Bound a paginated collection's size without following cursor-
        style pagination (no `last` link, only `next`) to completion when
        it is already clearly over `max_items`.

        Unlike `count_via_last_page_link`, this requests normal large
        pages (the same `per_page=100` `get_paginated_collection` uses)
        and follows `rel="next"` — with the same trusted-origin validation
        and pagination-cycle protection — counting items as it goes.
        Stops as soon as the observed count exceeds `max_items`: at that
        point admission rejection against a limit of `max_items` is
        already certain, so no further page is ever requested, and the
        returned count is reported as a lower bound, never as exact.
        Otherwise continues until pagination genuinely ends and returns
        the exact total.
        """
        require_strict_positive_int(max_items, name="max_items")
        observed_count = 0
        visited: set[str] = set()
        current_url = path
        current_params: Mapping[str, str] | None = {
            **(params or {}),
            "per_page": _ITEMS_PER_PAGE,
        }

        while True:
            if current_url in visited:
                raise GitHubPaginationCycleDetected(current_url)
            visited.add(current_url)

            response = self._send("GET", current_url, params=current_params)
            self._raise_for_status(response)
            observed_count += len(self._parse_collection_page(response))
            current_params = None

            next_link = parse_link_header(response.headers.get("Link")).get("next")
            if observed_count > max_items:
                # Rejection against `max_items` is already certain; never
                # request another page. If this same response happened to
                # be the last page anyway (no `next` link), the count is
                # in fact exact — only report a lower bound when a further
                # page was deliberately left unrequested.
                return BoundedPaginationCount(
                    observed_count=observed_count, complete=next_link is None
                )
            if next_link is None:
                return BoundedPaginationCount(
                    observed_count=observed_count, complete=True
                )
            self._validate_trusted_origin(next_link)
            current_url = next_link

    @staticmethod
    def _extract_last_page_number(last_link: str) -> int:
        query = parse_qs(urlsplit(last_link).query)
        page_values = query.get("page")
        page_number: int | None = None
        if page_values:
            try:
                page_number = int(page_values[0])
            except ValueError:
                page_number = None
        if page_number is None or page_number <= 0:
            raise GitHubMalformedResponse(
                "The GitHub API pagination Link header's 'last' URL does not "
                "carry a positive integer page parameter."
            )
        return page_number

    @staticmethod
    def _response_json(response: httpx.Response, *, context: str) -> object:
        """Parse a response body as JSON, raising `GitHubMalformedResponse`
        safely (only after the originating `except` block has fully
        exited) if it is not valid JSON."""
        malformed = False
        payload: object = None
        try:
            payload = response.json()
        except json.JSONDecodeError:
            malformed = True
        if malformed:
            raise GitHubMalformedResponse(
                f"The GitHub API returned invalid JSON for {context}."
            )
        return payload

    def get_paginated_collection(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> list[dict[str, JsonValue]]:
        """Follow `Link: rel="next"` pagination until it is exhausted.

        Returns every item from every page, in API order, as a flat list.
        This is a reusable primitive for later issue, comment, review, and
        commit collection; it performs no source-specific parsing itself.

        `path` may be a relative path (resolved against the trusted
        configured origin) or an absolute URL; an absolute URL is validated
        against the same trusted origin, by `_send`, before any request is
        sent for it.
        """
        items: list[dict[str, JsonValue]] = []
        visited: set[str] = set()
        next_url: str | None = path
        next_params: Mapping[str, str] | None = params

        while next_url is not None:
            if next_url in visited:
                raise GitHubPaginationCycleDetected(next_url)
            visited.add(next_url)

            response = self._send("GET", next_url, params=next_params)
            self._raise_for_status(response)
            items.extend(self._parse_collection_page(response))

            next_params = None
            next_link = parse_link_header(response.headers.get("Link")).get("next")
            if next_link is None:
                break
            self._validate_trusted_origin(next_link)
            next_url = next_link

        return items

    def _send(
        self, method: str, url: str, *, params: Mapping[str, str] | None = None
    ) -> httpx.Response:
        """Send one request, following only trusted-origin redirects.

        Validates `url` itself first when it is absolute (an initial
        caller-supplied absolute URL, as opposed to a path that will be
        resolved against the trusted configured origin), so an untrusted
        initial target is rejected before `_auth_headers()` is ever called.
        Redirects are handled here (rather than by `httpx`) so every
        redirect target can be validated the same way before it is ever
        requested.
        """
        if _looks_absolute(url):
            self._validate_trusted_origin(url)

        current_url = url
        current_params = params
        redirects_followed = 0
        visited_redirects: set[str] = set()

        while True:
            response = self._send_once(method, current_url, params=current_params)

            if response.status_code not in _ALLOWED_REDIRECT_STATUSES:
                return response

            location = response.headers.get("Location")
            if not location:
                # A redirect status with no Location is a protocol anomaly;
                # let `_raise_for_status` report it as unexpected.
                return response

            target = urljoin(str(response.url), location)
            self._validate_trusted_origin(target)

            if target in visited_redirects:
                raise GitHubRedirectCycleDetected(target)
            visited_redirects.add(target)

            redirects_followed += 1
            if redirects_followed > _MAX_REDIRECTS:
                raise GitHubTooManyRedirects(target, _MAX_REDIRECTS)

            current_url = target
            current_params = None

    def _send_once(
        self, method: str, url: str, *, params: Mapping[str, str] | None
    ) -> httpx.Response:
        """Send exactly one request, translating transport failures safely.

        The raised `GitHubTransportError` is constructed and raised only
        after the `except` block below has fully exited, so it never
        carries the original `httpx` exception (or the authenticated
        request it may reference) on `__cause__` or `__context__`.

        When a scoped request budget is active (see `limit_requests`), it
        is checked here, before this request is counted or sent — the
        request that would exceed it is never dispatched.
        """
        with self._request_state_lock:
            if self._request_budget_remaining is not None:
                if self._request_budget_remaining <= 0:
                    if self._request_budget_limit is None:
                        raise AssertionError(
                            "unreachable: a request budget limit must be set "
                            "whenever remaining budget is tracked"
                        )
                    raise GitHubRequestBudgetExceeded(self._request_budget_limit)
                self._request_budget_remaining -= 1

            self._request_count += 1
        response: httpx.Response | None = None
        try:
            response = self._client.request(
                method, url, params=params, headers=self._auth_headers()
            )
        except httpx.HTTPError:
            response = None

        if response is None:
            raise GitHubTransportError(
                "The GitHub API request failed due to a network or timeout error."
            )
        return response

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            return {}
        return {"Authorization": f"Bearer {self._token.get_secret_value()}"}

    def _validate_trusted_origin(self, url: str) -> None:
        """Require HTTPS, the configured GitHub API host, and a default port.

        Applied to every initial absolute request URL, every pagination
        `next` link, and every redirect target before it is requested, so
        the bearer token can never be sent to an unexpected host, a
        plaintext HTTP origin, or a non-default port.
        """
        parsed = urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            raise GitHubUntrustedOriginRejected(url, "must not contain userinfo")
        if parsed.scheme.lower() != _TRUSTED_SCHEME:
            raise GitHubUntrustedOriginRejected(url, "must use https")
        hostname = (parsed.hostname or "").lower()
        if hostname != self._expected_host:
            raise GitHubUntrustedOriginRejected(
                url, f"must use host {self._expected_host!r}"
            )
        try:
            port = parsed.port
        except ValueError:
            raise GitHubUntrustedOriginRejected(url, "has an invalid port") from None
        if port is not None and port != _TRUSTED_HTTPS_PORT:
            raise GitHubUntrustedOriginRejected(url, "must use the default HTTPS port")

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status == 401:
            raise GitHubAuthenticationFailed(status)
        if status == 404:
            raise GitHubRepositoryNotFound(status)
        if status == 429 or (status == 403 and self._is_rate_limited(response)):
            raise GitHubRateLimited.from_response(response)
        raise GitHubUnexpectedResponse(status)

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        """Classify a `403` as rate-limited (primary or secondary).

        A primary rate limit exhausts `X-RateLimit-Remaining`. A secondary
        rate limit may instead only carry a `Retry-After` header, or only a
        response body message naming a rate limit; either is enough.
        """
        remaining: str | None = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            return True
        if response.headers.get("Retry-After") is not None:
            return True
        return GitHubClient._response_message_indicates_rate_limit(response)

    @staticmethod
    def _response_message_indicates_rate_limit(response: httpx.Response) -> bool:
        """Safely check a JSON body's `message` field for rate-limit wording.

        Never raises, and never exposes the body beyond this boolean; a
        response that cannot be parsed as a JSON object is treated as not
        rate-limit-related.
        """
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        message = payload.get("message")
        if not isinstance(message, str):
            return False
        return _RATE_LIMIT_MESSAGE_MARKER in message.lower()

    @staticmethod
    def _parse_repository_metadata(
        response: httpx.Response,
    ) -> GitHubRepositoryMetadata:
        """Parse and validate a repository lookup response.

        Any JSON-decoding or validation failure is raised as
        `GitHubMalformedResponse` only after its originating `except` block
        (and the `JSONDecodeError`'s reference to the raw response body, or
        the `ValidationError`) has gone out of scope, so neither is
        reachable from the raised error's `__cause__` or `__context__`.
        """
        malformed_reason: str | None = None
        metadata: GitHubRepositoryMetadata | None = None

        try:
            payload = response.json()
        except json.JSONDecodeError:
            malformed_reason = (
                "The GitHub API returned invalid JSON for a repository lookup."
            )
        else:
            try:
                parsed = _GitHubRepositoryResponse.model_validate(payload)
                validate_canonical_github_url(
                    parsed.html_url,
                    expected_path=f"/{parsed.owner.login}/{parsed.name}",
                )
                identity = RepositoryIdentity(
                    platform="github", owner=parsed.owner.login, name=parsed.name
                )
                metadata = GitHubRepositoryMetadata(
                    identity=identity,
                    github_id=parsed.id,
                    html_url=AnyHttpUrl(parsed.html_url),
                    private=parsed.private,
                    default_branch=parsed.default_branch,
                    size_kb=parsed.size,
                )
            except (ValidationError, ValueError):
                malformed_reason = (
                    "The GitHub API repository response failed validation."
                )

        if malformed_reason is not None:
            raise GitHubMalformedResponse(malformed_reason)
        if metadata is None:
            raise AssertionError(
                "unreachable: metadata or malformed_reason must be set"
            )

        if metadata.private:
            raise GitHubRepositoryPrivate(metadata.identity.repository)
        return metadata

    @staticmethod
    def _parse_collection_page(response: httpx.Response) -> list[dict[str, JsonValue]]:
        """Parse and validate one paginated collection page's JSON body.

        Same deferred-raise discipline as `_parse_repository_metadata`: no
        raise happens while a `JSONDecodeError` is being handled.
        """
        malformed_reason: str | None = None
        page: list[dict[str, JsonValue]] = []

        try:
            payload = response.json()
        except json.JSONDecodeError:
            malformed_reason = (
                "The GitHub API returned invalid JSON for a collection page."
            )
        else:
            if not isinstance(payload, list):
                malformed_reason = (
                    "Expected a JSON array for a paginated GitHub collection page."
                )
            else:
                for item in payload:
                    if not isinstance(item, dict):
                        malformed_reason = (
                            "Expected each paginated collection item to be a "
                            "JSON object."
                        )
                        break
                    page.append(item)

        if malformed_reason is not None:
            raise GitHubMalformedResponse(malformed_reason)
        return page
