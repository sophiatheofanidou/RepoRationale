"""Application workflows that combine domain contracts with adapters."""

from reporationale.application.answering_workflow import (
    AnsweringModel,
    AnsweringProtocolError,
    AnsweringWorkflowError,
    CitationMarkerMismatchError,
    SearchHistoryCapability,
    UnknownCitationEvidenceError,
    answer_question,
)
from reporationale.application.corpus import (
    CorpusAssemblyError,
    RepositoryCorpus,
    assemble_repository_corpus,
)
from reporationale.application.preflight import (
    PreflightInspection,
    PreflightUnavailable,
    inspect_repository_reference,
    preflight_repository_reference,
)

__all__ = [
    "AnsweringModel",
    "AnsweringProtocolError",
    "AnsweringWorkflowError",
    "CitationMarkerMismatchError",
    "CorpusAssemblyError",
    "PreflightInspection",
    "PreflightUnavailable",
    "RepositoryCorpus",
    "SearchHistoryCapability",
    "UnknownCitationEvidenceError",
    "answer_question",
    "assemble_repository_corpus",
    "inspect_repository_reference",
    "preflight_repository_reference",
]
