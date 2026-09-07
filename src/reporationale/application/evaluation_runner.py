"""Connects the offline evaluation-harness foundation to the real,
ingestion-independent retrieval and answering workflows.

Runs a validated `EvaluationQuestionSet`'s cases through the existing BM25,
Voyage/Chroma, and bounded-answering services and turns each run's raw
output into the existing `RetrievalCaseResult`/`AnswerCaseResult` records,
ready for `reporationale.application.evaluation_artifacts.publish_evaluation_run`.
This module does not compute a metric, aggregate a summary, render a
report, or persist anything itself -- it only produces the raw per-case
records those steps already consume.

Three independent modes, matching the project's separately-gated
evaluation stages:

- `run_offline_lexical_retrieval`: the free BM25 baseline. Always
  runnable; makes no network call.
- `run_vector_retrieval`: the paid Voyage/Chroma product retrieval path.
- `run_answering`: the paid bounded answering agent.

This module never constructs a provider client and never reads a
credential or environment value itself: every retriever, search service,
and answering-model instance is injected by the caller as an already-built
object satisfying a narrow structural `Protocol`, so a real paid call can
never happen here merely because a credential happens to be configured
somewhere. The two paid modes additionally refuse to run at all unless the
caller explicitly passes `confirm_paid_mode=True`, and reject a `cases`
sequence spanning more than one evaluation split -- both checked before
anything else executes, including before either a model or a search/
embedding provider is ever touched. Every mode runs its cases strictly
sequentially, exactly once each, with no retry of any kind: a failed call
propagates immediately and an unfavorable outcome is recorded as-is, never
silently replaced by a repeated attempt.
"""

import time
from collections.abc import Callable, Sequence
from typing import Protocol

from reporationale.application.answering_workflow import (
    AnsweringModel,
    SearchHistoryCapability,
    answer_question,
)
from reporationale.domain.answering import RunTrace
from reporationale.domain.evaluation import (
    AnswerCaseResult,
    EvaluationCase,
    RetrievalCaseResult,
)
from reporationale.domain.retrieval import RankedEvidence

Clock = Callable[[], float]

# The product's vector-retrieval path's well-known retriever name, used as
# `run_vector_retrieval`'s default and referenced by
# `reporationale.application.evaluation_artifacts` to cross-validate a raw
# `RetrievalQueryUsage` record against retrieval results carrying this
# retriever name. A caller may still pass a different `retriever_name`; the
# cross-validation simply does not apply to results under a different name.
DEFAULT_VECTOR_RETRIEVER_NAME = "voyage_chroma"


class PaidModeNotConfirmed(Exception):
    """A paid retrieval or answering mode was called without the caller
    explicitly passing `confirm_paid_mode=True`. Always raised before
    anything else runs, so an unconfirmed call never reaches a provider."""


class LexicalRetriever(Protocol):
    """The exact surface `run_offline_lexical_retrieval` depends on --
    satisfied structurally by
    `reporationale.application.lexical_retrieval.BM25Retriever` and by any
    fake test double."""

    def search(self, query: str, *, limit: int) -> tuple[RankedEvidence, ...]: ...


class VectorSearchService(Protocol):
    """The exact surface `run_vector_retrieval` depends on -- satisfied
    structurally by
    `reporationale.application.vector_retrieval.SearchHistoryService` and
    by any fake test double."""

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]: ...


def _require_single_split(cases: Sequence[EvaluationCase]) -> None:
    """Raise `ValueError` if `cases` spans more than one evaluation split,
    before anything else in a paid mode runs: a paid retrieval or
    answering call must be scoped to exactly one split, so a caller
    mistake can never mix development and held-out cases into the same
    run. Always checked before `model_factory`, `search_service`, or
    `search_history` is ever touched."""
    splits = {case.split for case in cases}
    if len(splits) > 1:
        raise ValueError(
            "cases span multiple evaluation splits "
            f"({sorted(splits)}); a paid retrieval or answering run must "
            "be scoped to a single split before any provider is called"
        )


def _answerable_cases(cases: Sequence[EvaluationCase]) -> tuple[EvaluationCase, ...]:
    """Only an `answered` case declares the expected sources retrieval
    metrics are computed against (see
    `reporationale.application.evaluation_metrics.compute_case_retrieval_metrics`
    and `compute_retriever_aggregate_metrics`, which reject a retrieval
    result for any other case outright); an `insufficient_evidence` case
    is therefore never run through a retrieval mode here."""
    return tuple(case for case in cases if case.expected_outcome == "answered")


def _retrieval_case_result(
    case: EvaluationCase,
    *,
    retriever: str,
    evidence: tuple[RankedEvidence, ...],
    latency_seconds: float,
) -> RetrievalCaseResult:
    return RetrievalCaseResult(
        case_id=case.case_id,
        retriever=retriever,
        retrieved_source_ids=tuple(result.chunk.source_id for result in evidence),
        latency_seconds=latency_seconds,
    )


def run_offline_lexical_retrieval(
    cases: Sequence[EvaluationCase],
    retriever: LexicalRetriever,
    *,
    result_limit: int,
    retriever_name: str = "bm25",
    clock: Clock = time.monotonic,
) -> tuple[RetrievalCaseResult, ...]:
    """Run the free, offline BM25 baseline over every answerable case in
    `cases`, once each, in question-set order.

    Never touches GitHub, Voyage, Chroma, or Anthropic, so it needs no
    confirmation and no injected credential. `retriever.search` is called
    with the case's own `question` text as the query; a case whose
    `expected_outcome` is `insufficient_evidence` is skipped, since it
    declares no expected sources for a retrieval metric to be computed
    against.
    """
    results: list[RetrievalCaseResult] = []
    for case in _answerable_cases(cases):
        started = clock()
        evidence = retriever.search(case.question, limit=result_limit)
        latency_seconds = clock() - started
        results.append(
            _retrieval_case_result(
                case,
                retriever=retriever_name,
                evidence=evidence,
                latency_seconds=latency_seconds,
            )
        )
    return tuple(results)


def run_vector_retrieval(
    cases: Sequence[EvaluationCase],
    search_service: VectorSearchService,
    *,
    confirm_paid_mode: bool,
    retriever_name: str = DEFAULT_VECTOR_RETRIEVER_NAME,
    clock: Clock = time.monotonic,
) -> tuple[RetrievalCaseResult, ...]:
    """Run the paid Voyage/Chroma product retrieval path over every
    answerable case in `cases`, once each, in question-set order.

    Raises `ValueError` before calling `search_service` at all if `cases`
    spans more than one evaluation split. Raises `PaidModeNotConfirmed`
    before calling `search_service` at all unless the caller explicitly
    passes `confirm_paid_mode=True`. A case whose `expected_outcome` is
    `insufficient_evidence` is skipped, for the same reason as
    `run_offline_lexical_retrieval`.
    """
    _require_single_split(cases)
    if not confirm_paid_mode:
        raise PaidModeNotConfirmed(
            "run_vector_retrieval makes one paid Voyage query-embedding "
            "call per answerable case; pass confirm_paid_mode=True to "
            "proceed."
        )
    results: list[RetrievalCaseResult] = []
    for case in _answerable_cases(cases):
        started = clock()
        evidence = search_service.search_history(case.question)
        latency_seconds = clock() - started
        results.append(
            _retrieval_case_result(
                case,
                retriever=retriever_name,
                evidence=evidence,
                latency_seconds=latency_seconds,
            )
        )
    return tuple(results)


def run_answering(
    cases: Sequence[EvaluationCase],
    *,
    model_factory: Callable[[], AnsweringModel],
    search_history: SearchHistoryCapability,
    confirm_paid_mode: bool,
    estimate_cost_usd: Callable[[RunTrace], float | None] | None = None,
    clock: Clock = time.monotonic,
) -> tuple[AnswerCaseResult, ...]:
    """Run the paid bounded answering agent once over every case in
    `cases`, in question-set order (both `answered` and
    `insufficient_evidence` cases: unlike retrieval-only scoring, every
    case is a valid answering case).

    Calls `model_factory()` once per case to obtain a fresh `AnsweringModel`
    instance, since an `AnsweringModel` owns growing per-run message
    history and must never be reused across questions (see
    `reporationale.application.answering_workflow.answer_question`). The
    same `search_history` capability is reused across cases, matching how
    the product's `SearchHistoryService` is itself designed to be reused.

    Raises `ValueError` before calling `model_factory` or `search_history`
    at all if `cases` spans more than one evaluation split. Raises
    `PaidModeNotConfirmed` before calling `model_factory` or
    `search_history` at all unless the caller explicitly passes
    `confirm_paid_mode=True`. Every case runs exactly once: a raised
    provider or protocol error propagates immediately and stops the run
    rather than being caught, retried, or replaced with a more favorable
    attempt; ground truth (`cases`) is never read back or mutated.
    """
    _require_single_split(cases)
    if not confirm_paid_mode:
        raise PaidModeNotConfirmed(
            "run_answering makes paid Anthropic calls (and, through "
            "search_history, paid Voyage query calls) per case; pass "
            "confirm_paid_mode=True to proceed."
        )
    results: list[AnswerCaseResult] = []
    for case in cases:
        run_result = answer_question(
            case.question,
            search_history=search_history,
            model=model_factory(),
            clock=clock,
        )
        estimated_cost_usd = (
            estimate_cost_usd(run_result.trace)
            if estimate_cost_usd is not None
            else None
        )
        results.append(
            AnswerCaseResult(
                case_id=case.case_id,
                outcome=run_result.outcome,
                trace=run_result.trace,
                estimated_cost_usd=estimated_cost_usd,
            )
        )
    return tuple(results)
