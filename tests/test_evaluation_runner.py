"""Tests for the evaluation-runner slice that connects the offline
evaluation-harness foundation to the real BM25, vector-retrieval, and
answering workflows.

Every test uses small fake `LexicalRetriever`/`VectorSearchService`/
`AnsweringModel`/`SearchHistoryCapability` doubles and a deterministic fake
clock; no network, GitHub, Voyage, Chroma, or Anthropic call is ever made.
"""

import itertools
from collections.abc import Callable, Iterator

import pytest

from reporationale.application.evaluation_runner import (
    PaidModeNotConfirmed,
    run_answering,
    run_offline_lexical_retrieval,
    run_vector_retrieval,
)
from reporationale.domain.answering import (
    AnsweredOutcome,
    CitedReference,
    ConversationContext,
    FinalAnswer,
    FinalInsufficientEvidence,
    InsufficientEvidenceOutcome,
    ModelTurn,
)
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.evaluation import EvaluationCase, ExpectedSource
from reporationale.domain.retrieval import RankedEvidence

_MODEL_ID = "fake-model/1"


def _chunk(evidence_id: str, *, source_id: str, text: str) -> SourceChunk:
    _source_id, _, chunk_index_text = evidence_id.rpartition(":chunk:")
    return SourceChunk(
        chunk_id=evidence_id,
        source_id=source_id,
        chunk_index=int(chunk_index_text),
        text=text,
        platform="github",
        repository="google/gson",
        source_type="issue",
        source_url="https://github.com/google/gson/issues/1",
    )


def _evidence(
    evidence_id: str, *, source_id: str, rank: int = 1, text: str = "because of X"
) -> RankedEvidence:
    return RankedEvidence(
        evidence_id=evidence_id,
        rank=rank,
        score=0.5,
        score_kind="bm25",
        chunk=_chunk(evidence_id, source_id=source_id, text=text),
    )


def _answerable_case(case_id: str = "case-1", question: str = "why?") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        split="development",
        question=question,
        category="direct_retrieval",
        expected_outcome="answered",
        expected_sources=(
            ExpectedSource(
                source_id="github:google/gson:issue:1",
                source_url="https://github.com/google/gson/issues/1",
            ),
        ),
        review_note="Confirmed against the linked thread.",
    )


def _insufficient_case(case_id: str = "case-2") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        split="development",
        question="was this ever discussed?",
        category="unanswerable_control",
        expected_outcome="insufficient_evidence",
        review_note="No matching decision found after a manual search.",
    )


class _FakeLexicalRetriever:
    def __init__(self, results: dict[str, tuple[RankedEvidence, ...]]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int) -> tuple[RankedEvidence, ...]:
        self.calls.append((query, limit))
        return self._results[query]


class _FakeVectorSearchService:
    def __init__(self, results: dict[str, tuple[RankedEvidence, ...]]) -> None:
        self._results = results
        self.calls: list[str] = []

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]:
        self.calls.append(query)
        return self._results[query]


class _FakeSearchHistory:
    def __init__(self, results_by_query: dict[str, tuple[RankedEvidence, ...]]) -> None:
        self._results_by_query = results_by_query
        self.calls: list[str] = []

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]:
        self.calls.append(query)
        return self._results_by_query[query]


class _FakeModel:
    model_id = _MODEL_ID

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns: Iterator[ModelTurn] = iter(turns)

    def start(
        self,
        *,
        question: str,
        evidence: tuple[RankedEvidence, ...],
        search_available: bool,
        context: ConversationContext | None = None,
    ) -> ModelTurn:
        return next(self._turns)

    def submit_evidence(
        self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
    ) -> ModelTurn:
        return next(self._turns)


def _make_clock() -> Callable[[], float]:
    values: Iterator[float] = iter(itertools.count(0.0, 1.0))

    def _clock() -> float:
        return next(values)

    return _clock


# --- run_offline_lexical_retrieval -----------------------------------------


def test_offline_lexical_retrieval_skips_insufficient_evidence_cases() -> None:
    evidence_id = "github:google/gson:issue:1:chunk:0"
    retriever = _FakeLexicalRetriever(
        {"why?": (_evidence(evidence_id, source_id="github:google/gson:issue:1"),)}
    )
    cases = (_answerable_case(), _insufficient_case())

    results = run_offline_lexical_retrieval(
        cases, retriever, result_limit=5, clock=_make_clock()
    )

    assert len(results) == 1
    assert results[0].case_id == "case-1"
    assert results[0].retriever == "bm25"
    assert results[0].retrieved_source_ids == ("github:google/gson:issue:1",)
    assert results[0].latency_seconds == 1.0
    assert retriever.calls == [("why?", 5)]


def test_offline_lexical_retrieval_uses_custom_retriever_name() -> None:
    retriever = _FakeLexicalRetriever({"why?": ()})
    results = run_offline_lexical_retrieval(
        (_answerable_case(),),
        retriever,
        result_limit=5,
        retriever_name="bm25-dev",
        clock=_make_clock(),
    )
    assert results[0].retriever == "bm25-dev"
    assert results[0].retrieved_source_ids == ()


# --- run_vector_retrieval ---------------------------------------------------


def test_vector_retrieval_refuses_without_explicit_confirmation() -> None:
    search_service = _FakeVectorSearchService({"why?": ()})
    with pytest.raises(PaidModeNotConfirmed):
        run_vector_retrieval(
            (_answerable_case(),), search_service, confirm_paid_mode=False
        )
    assert search_service.calls == []


def test_vector_retrieval_rejects_mixed_split_cases_before_any_provider_call() -> None:
    """A mixed-split case sequence must be rejected before the search
    service is ever touched -- even when paid mode is confirmed."""
    search_service = _FakeVectorSearchService({"why?": ()})
    held_out_case = _answerable_case("case-held", "why?")
    held_out_case = held_out_case.model_copy(update={"split": "held_out"})
    cases = (_answerable_case("case-dev"), held_out_case)

    with pytest.raises(ValueError, match="multiple evaluation splits"):
        run_vector_retrieval(cases, search_service, confirm_paid_mode=True)
    assert search_service.calls == []


def test_vector_retrieval_runs_answerable_cases_when_confirmed() -> None:
    evidence_id = "github:google/gson:issue:1:chunk:0"
    search_service = _FakeVectorSearchService(
        {"why?": (_evidence(evidence_id, source_id="github:google/gson:issue:1"),)}
    )
    cases = (_answerable_case(), _insufficient_case())

    results = run_vector_retrieval(
        cases, search_service, confirm_paid_mode=True, clock=_make_clock()
    )

    assert len(results) == 1
    assert results[0].retriever == "voyage_chroma"
    assert results[0].retrieved_source_ids == ("github:google/gson:issue:1",)
    assert search_service.calls == ["why?"]


# --- run_answering -----------------------------------------------------------


def _answered_turns(evidence_id: str) -> list[ModelTurn]:
    return [
        ModelTurn(
            action=FinalAnswer(
                answer="Because of X. [1]",
                citations=(CitedReference(evidence_id=evidence_id),),
            ),
            input_tokens=20,
            output_tokens=15,
        ),
    ]


def test_answering_refuses_without_explicit_confirmation() -> None:
    search = _FakeSearchHistory({})
    calls = {"count": 0}

    def model_factory() -> _FakeModel:
        calls["count"] += 1
        return _FakeModel([])

    with pytest.raises(PaidModeNotConfirmed):
        run_answering(
            (_answerable_case(),),
            model_factory=model_factory,
            search_history=search,
            confirm_paid_mode=False,
        )
    assert calls["count"] == 0
    assert search.calls == []


def test_answering_rejects_mixed_split_cases_before_any_provider_call() -> None:
    """A mixed-split case sequence must be rejected before `model_factory`
    or `search_history` is ever touched -- even when paid mode is
    confirmed."""
    search = _FakeSearchHistory({})
    held_out_case = _insufficient_case("case-held").model_copy(
        update={"split": "held_out"}
    )
    cases = (_answerable_case("case-dev"), held_out_case)
    calls = {"count": 0}

    def model_factory() -> _FakeModel:
        calls["count"] += 1
        return _FakeModel([])

    with pytest.raises(ValueError, match="multiple evaluation splits"):
        run_answering(
            cases,
            model_factory=model_factory,
            search_history=search,
            confirm_paid_mode=True,
        )
    assert calls["count"] == 0
    assert search.calls == []


def test_answering_runs_every_case_with_a_fresh_model_instance() -> None:
    evidence_id = "github:google/gson:issue:1:chunk:0"
    search = _FakeSearchHistory(
        {
            "why?": (_evidence(evidence_id, source_id="github:google/gson:issue:1"),),
            "was this ever discussed?": (),
        }
    )
    model_factories_called: list[_FakeModel] = []

    def model_factory() -> _FakeModel:
        if not model_factories_called:
            model = _FakeModel(_answered_turns(evidence_id))
        else:
            model = _FakeModel(
                [
                    ModelTurn(
                        action=FinalInsufficientEvidence(
                            explanation="No matching decision was found."
                        ),
                        input_tokens=6,
                        output_tokens=3,
                    ),
                ]
            )
        model_factories_called.append(model)
        return model

    cases = (_answerable_case(), _insufficient_case())

    results = run_answering(
        cases,
        model_factory=model_factory,
        search_history=search,
        confirm_paid_mode=True,
        estimate_cost_usd=lambda trace: trace.input_tokens * 0.001,
        clock=_make_clock(),
    )

    assert len(model_factories_called) == 2
    assert len(results) == 2
    assert results[0].case_id == "case-1"
    assert isinstance(results[0].outcome, AnsweredOutcome)
    assert results[0].estimated_cost_usd == pytest.approx(0.02)
    assert results[1].case_id == "case-2"
    assert isinstance(results[1].outcome, InsufficientEvidenceOutcome)
    assert results[1].estimated_cost_usd == pytest.approx(0.006)


def test_answering_without_cost_estimator_leaves_cost_unset() -> None:
    evidence_id = "github:google/gson:issue:1:chunk:0"
    search = _FakeSearchHistory(
        {"why?": (_evidence(evidence_id, source_id="github:google/gson:issue:1"),)}
    )
    model = _FakeModel(_answered_turns(evidence_id))

    results = run_answering(
        (_answerable_case(),),
        model_factory=lambda: model,
        search_history=search,
        confirm_paid_mode=True,
        clock=_make_clock(),
    )

    assert results[0].estimated_cost_usd is None


def test_answering_propagates_a_case_failure_without_retrying() -> None:
    """A provider/protocol failure on one case stops the run immediately;
    it must never be caught, retried, or silently replaced with a more
    favorable attempt."""
    evidence_id = "github:google/gson:issue:1:chunk:0"
    search = _FakeSearchHistory(
        {"why?": (_evidence(evidence_id, source_id="github:google/gson:issue:1"),)}
    )
    calls = {"count": 0}

    def failing_model_factory() -> _FakeModel:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated provider failure")
        return _FakeModel(_answered_turns(evidence_id))

    cases = (
        _answerable_case("case-1"),
        _answerable_case("case-2"),
        _answerable_case("case-3"),
    )

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        run_answering(
            cases,
            model_factory=failing_model_factory,
            search_history=search,
            confirm_paid_mode=True,
            clock=_make_clock(),
        )

    # The third case's model must never have been requested: the failure
    # on the second case stopped the run rather than continuing past it.
    assert calls["count"] == 2
