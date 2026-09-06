"""Typed failure contract for the GitHub REST adapter.

Every exception here carries only non-secret, useful detail (status codes,
rate-limit timing, URLs, and a short reason string). None of them accept or
store request/response objects or headers, so an Authorization value can
never reach an exception message, representation, or attribute. Call sites
that convert a lower-level failure into one of these must raise it after the
originating `except` block has fully exited (never `raise ... from exc`, and
never a bare `raise` while still handling the original exception), since
Python otherwise still records the original exception on `__context__` even
when `__cause__` is suppressed with `from None`.
"""

from datetime import UTC, datetime

import httpx


class GitHubAdapterError(Exception):
    """Base class for all typed GitHub REST adapter failures."""


class GitHubRepositoryNotFound(GitHubAdapterError):
    """The repository does not exist, or is not accessible with the supplied
    credentials."""

    def __init__(self, status_code: int) -> None:
        super().__init__("The repository was not found or is not accessible.")
        self.status_code = status_code


class GitHubRepositoryPrivate(GitHubAdapterError):
    """The repository exists and is accessible, but is private."""

    def __init__(self, repository: str) -> None:
        super().__init__(
            f"Repository '{repository}' is private; "
            "the MVP supports public repositories only."
        )
        self.repository = repository


class GitHubAuthenticationFailed(GitHubAdapterError):
    """The GitHub API rejected the supplied credentials."""

    def __init__(self, status_code: int) -> None:
        super().__init__("GitHub API authentication failed; check the supplied token.")
        self.status_code = status_code


class GitHubRateLimited(GitHubAdapterError):
    """The GitHub API reported that its rate limit was reached (primary or
    secondary)."""

    def __init__(
        self,
        *,
        status_code: int,
        reset_at: datetime | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"GitHub API rate limit reached (status {status_code}).")
        self.status_code = status_code
        self.reset_at = reset_at
        self.retry_after_seconds = retry_after_seconds

    @classmethod
    def from_response(cls, response: httpx.Response) -> "GitHubRateLimited":
        """Build the error from rate-limit response headers, when present.

        Only plain timing values are extracted; the response object itself
        is never stored on the returned error.
        """
        reset_at: datetime | None = None
        reset_header = response.headers.get("X-RateLimit-Reset")
        if reset_header is not None:
            try:
                reset_at = datetime.fromtimestamp(int(reset_header), tz=UTC)
            except ValueError:
                reset_at = None

        retry_after_seconds: float | None = None
        retry_after_header = response.headers.get("Retry-After")
        if retry_after_header is not None:
            try:
                retry_after_seconds = float(retry_after_header)
            except ValueError:
                retry_after_seconds = None

        return cls(
            status_code=response.status_code,
            reset_at=reset_at,
            retry_after_seconds=retry_after_seconds,
        )


class GitHubMalformedResponse(GitHubAdapterError):
    """The GitHub API returned a response that could not be parsed as expected."""


class GitHubTransportError(GitHubAdapterError):
    """A network or timeout failure occurred while calling the GitHub API."""


class GitHubUnexpectedResponse(GitHubAdapterError):
    """The GitHub API returned a non-success status not otherwise classified."""

    def __init__(self, status_code: int) -> None:
        super().__init__(
            f"GitHub API returned an unexpected status code {status_code}."
        )
        self.status_code = status_code


class GitHubPaginationCycleDetected(GitHubAdapterError):
    """A GitHub pagination `next` link repeated a page already fetched."""

    def __init__(self, url: str) -> None:
        super().__init__(
            "GitHub pagination returned a repeated page link, forming a cycle."
        )
        self.url = url


class GitHubUntrustedOriginRejected(GitHubAdapterError):
    """A URL derived from a GitHub response (a pagination `next` link or a
    redirect `Location`) did not match the client's trusted HTTPS origin.

    Used for both pagination links and redirect targets, since both must
    satisfy the same trusted-origin boundary before any request is sent.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(
            f"Refused to follow a GitHub link to an untrusted origin ({reason})."
        )
        self.url = url
        self.reason = reason


class GitHubRedirectCycleDetected(GitHubAdapterError):
    """A GitHub redirect chain returned to a target already visited."""

    def __init__(self, url: str) -> None:
        super().__init__("GitHub returned a redirect chain that forms a cycle.")
        self.url = url


class GitHubTooManyRedirects(GitHubAdapterError):
    """A GitHub redirect chain exceeded the small explicit redirect bound."""

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"GitHub redirect chain exceeded the limit of {limit}.")
        self.url = url
        self.limit = limit


class GitHubRequestBudgetExceeded(GitHubAdapterError):
    """A scoped request budget set by `GitHubClient.limit_requests` was
    exceeded: the request that would have exceeded it was never sent."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"The scoped GitHub request budget of {limit} request(s) was "
            "exceeded; the next request was not sent."
        )
        self.limit = limit
