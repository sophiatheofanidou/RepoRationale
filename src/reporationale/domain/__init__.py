"""Core domain models shared across RepoRationale components.

Platform-independent only: this package must never import the GitHub
adapter, `httpx`, or any other provider-specific type.
"""

from reporationale.domain.preflight import PreflightResult, PreflightStatus
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import SourceDocument

__all__ = [
    "PreflightResult",
    "PreflightStatus",
    "RepositoryIdentity",
    "SourceDocument",
]
