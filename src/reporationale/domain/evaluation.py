"""Provider-independent offline evaluation-harness contracts.

This module defines the versioned evaluation question-set contract and the
typed records one reproducible evaluation run is built from: indexing
measurements, raw per-case retrieval results, and per-case answer results.
It also defines the aggregate contracts (`RetrieverAggregateMetrics`,
`AnswerAggregateMetrics`, `EvaluationSummary`) those raw results are
reduced to. Aggregate computation itself lives in
`reporationale.application.evaluation_metrics`, never here: this module
only defines shapes.

Answer results deliberately reuse the existing provider-neutral
`AnswerOutcome`/`RunTrace` contracts from `reporationale.domain.answering`
rather than duplicating a second answering-result shape; the only new
answer-side additions here are the evaluation-specific cost estimate and
human review fields evaluation.md requires that cannot be computed from an
`AnsweringRunResult` alone.

Nothing here makes a GitHub, Voyage, Chroma, or Anthropic call, and no
model in this module carries a credential, HTTP header, environment
value, or raw provider response object -- only identifiers, versions,
counts, timings, and cost estimates.
"""

import math
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from reporationale.domain.answering import AnswerOutcome, RunTrace
from reporationale.domain.repository_identity import RepositoryIdentity
from reporationale.domain.source_document import require_timezone_aware

_StrictPositiveInt = Annotated[int, Field(strict=True, gt=0)]
_StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]

_GIT_OBJECT_ID_PATTERN = re.compile(r"[0-9a-f]{40,64}")


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _require_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


def _require_nonnegative_int_values(value: object) -> object:
    """Reject a boolean or numeric-string count before pydantic's own
    lenient `int` coercion could otherwise accept either and erase the
    distinction this validator needs to reject (mirrors
    `SnapshotManifest.counts_must_be_raw_nonnegative_ints`)."""
    if isinstance(value, dict):
        for key, count in value.items():
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{key!r} must be a nonnegative integer")
    return value


def _require_valid_git_object_id(value: str) -> str:
    if _GIT_OBJECT_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a valid lowercase-hex Git object id")
    return value


# --- Versioned evaluation question set -----------------------------------

# Bumped whenever `EvaluationCase`/`EvaluationQuestionSet`'s own shape
# changes incompatibly. Deliberately separate from `question_set_version`,
# which identifies a specific edition of question content under this same
# schema (mirrors the manifest/source schema-version split in
# `reporationale.domain.snapshot`).
QUESTION_SET_SCHEMA_VERSION = 1

EvaluationSplit = Literal["development", "held_out"]
ExpectedOutcome = Literal["answered", "insufficient_evidence"]


class ExpectedSource(BaseModel):
    """One human-confirmed piece of expected supporting evidence for an
    `answered` evaluation case: a stable source ID and its original URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*:\S+$")
    source_url: AnyHttpUrl


class EvaluationCase(BaseModel):
    """One reviewed evaluation case, recorded before the system is run.

    An `answered` case must declare at least one expected supporting
    source: an answerable case without expected documentation is
    ambiguous and is rejected. An `insufficient_evidence` case must
    declare none, since no supporting evidence is expected to exist for
    it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    split: EvaluationSplit
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_outcome: ExpectedOutcome
    expected_sources: tuple[ExpectedSource, ...] = Field(default_factory=tuple)
    review_note: str = Field(min_length=1)

    @field_validator("case_id", "question", "category", "review_note")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @model_validator(mode="after")
    def expected_sources_must_have_unique_ids(self) -> "EvaluationCase":
        source_ids = [source.source_id for source in self.expected_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("expected_sources must not repeat a source_id")
        return self

    @model_validator(mode="after")
    def expected_sources_must_match_expected_outcome(self) -> "EvaluationCase":
        if self.expected_outcome == "answered" and not self.expected_sources:
            raise ValueError(
                "an 'answered' case must declare at least one expected source"
            )
        if self.expected_outcome == "insufficient_evidence" and self.expected_sources:
            raise ValueError(
                "an 'insufficient_evidence' case must not declare expected sources"
            )
        return self


class EvaluationQuestionSet(BaseModel):
    """One versioned, immutable evaluation question set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_set_schema_version: _StrictPositiveInt
    question_set_version: _StrictPositiveInt
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_must_be_unique(self) -> "EvaluationQuestionSet":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("cases must not repeat a case_id")
        return self


# --- Indexing measurements -------------------------------------------------


class PhaseTiming(BaseModel):
    """Wall-clock time spent in one named indexing phase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: str = Field(min_length=1)
    seconds: float = Field(ge=0)

    @field_validator("phase")
    @classmethod
    def phase_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("seconds")
    @classmethod
    def seconds_must_be_finite(cls, value: float) -> float:
        return _require_finite(value)


class SkippedItem(BaseModel):
    """One item an indexing run skipped or failed to ingest, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identifier: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("identifier", "reason")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class IndexingMeasurement(BaseModel):
    """Operational measurements for one repeatable indexing run, recorded
    independently of retrieval or answer quality. Carries no credential,
    HTTP header, environment value, or provider response object -- only
    counts, timings, and cost estimates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: RepositoryIdentity
    resolved_commit_sha: str
    phase_timings: tuple[PhaseTiming, ...] = Field(default_factory=tuple)
    github_request_count: _StrictNonNegativeInt
    source_count_by_type: dict[str, int] = Field(default_factory=dict)
    chunk_count: _StrictNonNegativeInt
    embedding_request_count: _StrictNonNegativeInt | None = None
    embedding_token_count: _StrictNonNegativeInt | None = None
    estimated_embedding_cost_usd: float | None = Field(default=None, ge=0)
    snapshot_size_bytes: _StrictNonNegativeInt | None = None
    skipped_items: tuple[SkippedItem, ...] = Field(default_factory=tuple)

    @field_validator("resolved_commit_sha")
    @classmethod
    def resolved_commit_sha_must_be_valid_object_id(cls, value: str) -> str:
        return _require_valid_git_object_id(value)

    @field_validator("source_count_by_type", mode="before")
    @classmethod
    def source_counts_must_be_raw_nonnegative_ints(cls, value: object) -> object:
        return _require_nonnegative_int_values(value)

    @field_validator("estimated_embedding_cost_usd")
    @classmethod
    def estimated_embedding_cost_usd_must_be_finite(
        cls, value: float | None
    ) -> float | None:
        if value is not None:
            _require_finite(value)
        return value


# --- Retrieval results and metrics ----------------------------------------


class RetrievalCaseResult(BaseModel):
    """One retriever's raw ranked evidence for one evaluation case: the
    retrieved chunks' source IDs in rank order. A source may repeat across
    ranks when multiple chunks of it were retrieved; this is not
    deduplicated here. `retrieved_source_ids` may hold more than five
    entries (a retriever's own raw output), but Hit@5/MRR@5/source-Recall@5
    are always computed from only the first five, and always from this raw
    ordering plus the question set's expected sources (see
    `reporationale.application.evaluation_metrics`), never accepted as an
    independent value on this record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    retriever: str = Field(min_length=1)
    retrieved_source_ids: tuple[str, ...] = Field(default_factory=tuple)
    latency_seconds: float = Field(ge=0)

    @field_validator("case_id", "retriever")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("retrieved_source_ids")
    @classmethod
    def retrieved_source_ids_must_not_contain_blanks(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        for source_id in value:
            _require_non_blank(source_id)
        return value

    @field_validator("latency_seconds")
    @classmethod
    def latency_seconds_must_be_finite(cls, value: float) -> float:
        return _require_finite(value)


class CaseRetrievalMetrics(BaseModel):
    """One case's Hit@5/MRR@5/source-Recall@5, computed deterministically
    from a `RetrievalCaseResult` and its case's expected sources.
    `source_recall_at_5` is `None` for a case with fewer than two expected
    sources, matching the metric's definition (only for questions that
    require multiple expected sources)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    retriever: str = Field(min_length=1)
    hit_at_5: bool
    mrr_at_5: float = Field(ge=0, le=1)
    source_recall_at_5: float | None = Field(default=None, ge=0, le=1)


class RetrieverAggregateMetrics(BaseModel):
    """Aggregate Hit@5/MRR@5/source-Recall@5/mean latency for one retriever
    across every answerable case it was run against, computed from
    `per_case` and the raw per-case retrieval latencies -- never accepted
    as an independent value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retriever: str = Field(min_length=1)
    case_count: _StrictNonNegativeInt
    hit_rate_at_5: float = Field(ge=0, le=1)
    mean_mrr_at_5: float = Field(ge=0, le=1)
    mean_source_recall_at_5: float | None = Field(default=None, ge=0, le=1)
    mean_latency_seconds: float = Field(ge=0)
    per_case: tuple[CaseRetrievalMetrics, ...] = Field(default_factory=tuple)

    @field_validator("mean_latency_seconds")
    @classmethod
    def mean_latency_seconds_must_be_finite(cls, value: float) -> float:
        return _require_finite(value)

    @model_validator(mode="after")
    def case_count_must_match_per_case(self) -> "RetrieverAggregateMetrics":
        if self.case_count != len(self.per_case):
            raise ValueError(
                "case_count must equal the number of recorded per_case metrics"
            )
        return self


# --- Answer results and metrics -------------------------------------------

FailureStage = Literal[
    "admission_or_indexing_failure",
    "expected_evidence_not_retrieved",
    "evidence_retrieved_but_judged_insufficient",
    "answered_when_should_have_abstained",
    "abstained_despite_sufficient_evidence",
    "unsupported_or_incomplete_claim_support",
    "invalid_or_malformed_citation",
    "retrieval_loop_exhausted_without_resolution",
    "external_api_or_provider_failure",
]


class AnswerReview(BaseModel):
    """The human-reviewed quality and grounding judgments that cannot be
    computed automatically from `AnswerCaseResult.outcome`/`trace` alone.
    Every field stays `None` pending review; a related-but-not-supporting
    citation must never be recorded as adequate claim support."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refinement_useful: bool | None = None
    search_efficient: bool | None = None
    claim_support_adequate: bool | None = None
    citation_completeness_adequate: bool | None = None
    primary_failure_stage: FailureStage | None = None
    reviewer_note: str | None = None

    @field_validator("reviewer_note")
    @classmethod
    def reviewer_note_must_not_be_blank_when_present(
        cls, value: str | None
    ) -> str | None:
        if value is not None:
            _require_non_blank(value)
        return value


class AnswerCaseResult(BaseModel):
    """One completed answering run for one evaluation case: the
    provider-neutral structured outcome and execution trace already
    defined by `reporationale.domain.answering` (never duplicated here),
    plus the evaluation-specific cost estimate and human review. Carries
    no credential, HTTP header, environment value, or provider response
    object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    outcome: AnswerOutcome
    trace: RunTrace
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    review: AnswerReview = Field(default_factory=AnswerReview)

    @field_validator("case_id")
    @classmethod
    def case_id_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("estimated_cost_usd")
    @classmethod
    def estimated_cost_usd_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None:
            _require_finite(value)
        return value


class AnswerAggregateMetrics(BaseModel):
    """Aggregate answering-quality and cost measurements across every
    `AnswerCaseResult` in a run, computed from those case results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_count: _StrictNonNegativeInt
    outcome_accuracy: float = Field(ge=0, le=1)
    total_input_tokens: _StrictNonNegativeInt
    total_output_tokens: _StrictNonNegativeInt
    total_estimated_cost_usd: float | None = Field(default=None, ge=0)
    mean_total_latency_seconds: float = Field(ge=0)
    failure_stage_counts: dict[str, int] = Field(default_factory=dict)

    @field_validator("failure_stage_counts", mode="before")
    @classmethod
    def failure_stage_counts_must_be_raw_nonnegative_ints(cls, value: object) -> object:
        return _require_nonnegative_int_values(value)


# --- Aggregate run summary and reproducible run manifest -------------------


class EvaluationSummary(BaseModel):
    """The aggregate result of one complete evaluation run: retrieval
    metrics per retriever and, once answering was evaluated, answer
    quality and cost metrics. Every aggregate here is computed from the
    run's own case results
    (`reporationale.application.evaluation_metrics.build_evaluation_summary`),
    never accepted as an independent value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    question_set_version: _StrictPositiveInt
    case_count: _StrictNonNegativeInt
    retrieval_metrics: tuple[RetrieverAggregateMetrics, ...] = Field(
        default_factory=tuple
    )
    answer_metrics: AnswerAggregateMetrics | None = None

    @field_validator("run_id")
    @classmethod
    def run_id_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


# Bumped whenever `RunManifest`'s own shape changes incompatibly.
RUN_MANIFEST_SCHEMA_VERSION = 1


class RunManifest(BaseModel):
    """Everything needed to reproduce one evaluation run, per the
    reproducibility fields fixed in the project's evaluation plan:
    repository and resolved commit, snapshot/schema/question-set
    versions, chunking parameters and K, embedding/answering-model
    identifiers, agent version, relevant library versions, run date, and
    the documented command used. Carries no credential, HTTP header,
    environment value, or provider response object -- only identifiers,
    versions, and the exact command string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_manifest_schema_version: _StrictPositiveInt
    run_id: str = Field(min_length=1)
    created_at: datetime
    command: str = Field(min_length=1)
    repository: RepositoryIdentity
    resolved_commit_sha: str
    source_schema_version: _StrictPositiveInt
    chunk_schema_version: _StrictPositiveInt
    chunker_algorithm_version: _StrictPositiveInt
    max_chars: _StrictPositiveInt
    retrieval_result_limit: _StrictPositiveInt
    question_set_version: _StrictPositiveInt
    embedding_model: str | None = None
    answering_model: str | None = None
    agent_version: str | None = None
    library_versions: dict[str, str] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def created_at_must_include_timezone(cls, value: datetime) -> datetime:
        require_timezone_aware(value)
        return value

    @field_validator("run_id", "command")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("resolved_commit_sha")
    @classmethod
    def resolved_commit_sha_must_be_valid_object_id(cls, value: str) -> str:
        return _require_valid_git_object_id(value)

    @field_validator("embedding_model", "answering_model", "agent_version")
    @classmethod
    def optional_identifiers_must_not_be_blank_when_present(
        cls, value: str | None
    ) -> str | None:
        if value is not None:
            _require_non_blank(value)
        return value
