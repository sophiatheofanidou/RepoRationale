"""GitHub REST adapter boundary: repository lookup, the complete supported
source collection, reference parsing, and reusable pagination."""

from reporationale.adapters.github.client import BoundedPaginationCount, GitHubClient
from reporationale.adapters.github.errors import (
    GitHubAdapterError,
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
from reporationale.adapters.github.models import GitHubRepositoryMetadata
from reporationale.adapters.github.reference import (
    RepositoryReferenceRejected,
    parse_github_repository_reference,
)

__all__ = [
    "BoundedPaginationCount",
    "GitHubAdapterError",
    "GitHubAuthenticationFailed",
    "GitHubClient",
    "GitHubMalformedResponse",
    "GitHubPaginationCycleDetected",
    "GitHubRateLimited",
    "GitHubRedirectCycleDetected",
    "GitHubRepositoryMetadata",
    "GitHubRepositoryNotFound",
    "GitHubRepositoryPrivate",
    "GitHubRequestBudgetExceeded",
    "GitHubTooManyRedirects",
    "GitHubTransportError",
    "GitHubUnexpectedResponse",
    "GitHubUntrustedOriginRejected",
    "RepositoryReferenceRejected",
    "parse_github_repository_reference",
]
