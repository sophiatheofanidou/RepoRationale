"""Application workflows that combine domain contracts with adapters."""

from reporationale.application.corpus import (
    CorpusAssemblyError,
    RepositoryCorpus,
    assemble_repository_corpus,
)
from reporationale.application.preflight import (
    PreflightUnavailable,
    preflight_repository_reference,
)

__all__ = [
    "CorpusAssemblyError",
    "PreflightUnavailable",
    "RepositoryCorpus",
    "assemble_repository_corpus",
    "preflight_repository_reference",
]
