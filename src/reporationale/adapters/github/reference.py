"""GitHub-specific `owner/repository` reference validation.

This is GitHub-specific business logic (naming rules, URL-format detection)
rather than a platform-independent contract, so it lives behind the GitHub
adapter boundary rather than in the domain package. It performs
no network access itself; `GitHubClient.get_repository` performs the actual
remote lookup for an already-parsed `RepositoryIdentity`.
"""

import re

from reporationale.domain.repository_identity import RepositoryIdentity

_GITHUB_OWNER_PATTERN = re.compile(r"[A-Za-z0-9-]+")
_GITHUB_REPOSITORY_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_RESERVED_REPOSITORY_NAMES = {".", ".."}
_MAX_OWNER_LENGTH = 39
_MAX_REPOSITORY_NAME_LENGTH = 100


class RepositoryReferenceRejected(Exception):
    """A user-supplied repository reference could not be accepted."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _is_valid_github_owner(owner: str) -> bool:
    """Approximate GitHub's public username/organization naming rules."""
    if not 1 <= len(owner) <= _MAX_OWNER_LENGTH:
        return False
    if owner.startswith("-") or owner.endswith("-") or "--" in owner:
        return False
    return _GITHUB_OWNER_PATTERN.fullmatch(owner) is not None


def _is_valid_github_repository_name(name: str) -> bool:
    """Approximate GitHub's public repository naming rules."""
    if not 1 <= len(name) <= _MAX_REPOSITORY_NAME_LENGTH:
        return False
    if name in _RESERVED_REPOSITORY_NAMES or name.endswith(".git"):
        return False
    return _GITHUB_REPOSITORY_NAME_PATTERN.fullmatch(name) is not None


def parse_github_repository_reference(raw: str) -> RepositoryIdentity:
    """Validate the format of a public GitHub `owner/repository` reference.

    Accepts only a bare `owner/repository` reference.
    This performs no network access; remote existence, public access, and
    corpus size remain later preflight stages.
    """
    candidate = raw.strip()
    if not candidate:
        raise RepositoryReferenceRejected(
            "empty_reference", "Enter a repository as owner/repository."
        )

    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://", "git@", "ssh://")):
        raise RepositoryReferenceRejected(
            "unexpected_url_format",
            "Enter owner/repository, not a URL or clone address.",
        )

    if any(character.isspace() for character in candidate):
        raise RepositoryReferenceRejected(
            "invalid_reference_format",
            "owner/repository must not contain internal whitespace.",
        )

    segments = candidate.split("/")
    if len(segments) < 2:
        raise RepositoryReferenceRejected(
            "missing_separator",
            "Enter owner/repository — the repository name is missing.",
        )
    if len(segments) > 2:
        raise RepositoryReferenceRejected(
            "too_many_segments",
            "Enter exactly one repository as owner/repository, "
            "without extra path segments.",
        )

    owner, name = segments
    if not _is_valid_github_owner(owner):
        raise RepositoryReferenceRejected(
            "invalid_owner_format",
            "owner must be a valid GitHub user or organization name.",
        )
    if not _is_valid_github_repository_name(name):
        raise RepositoryReferenceRejected(
            "invalid_repository_name_format",
            "repository must be a valid GitHub repository name.",
        )

    return RepositoryIdentity(platform="github", owner=owner, name=name)
