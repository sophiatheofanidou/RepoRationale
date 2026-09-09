"""Tests for aggregate evaluation-metric computation: Hit@5, MRR@5, source
Recall@5, and answer outcome-accuracy/cost aggregation, always computed
from raw case results rather than accepted as independent values.
"""

import pytest

from reporationale.application.evaluation_metrics import (
    build_evaluation_summary,
    compute_answer_aggregate_metrics,
    compute_case_retrieval_metrics,
    compute_retriever_aggregate_metrics,
)
from reporationale.domain.answering import (
    AnsweredOutcome,
    Citation,
    InsufficientEvidenceOutcome,
    RunTrace,
)
from reporationale.domain.evaluation import (
    AnswerCaseResult,
    AnswerReview,
    EvaluationCase,
    EvaluationQuestionSet,
    ExpectedSource,
    RetrievalCaseResult,
)


def _source(number: int) -> ExpectedSource:
    return ExpectedSource(
        source_id=f"github:google/gson:issue:{number}",
        source_url=f"https://github.com/google/gson/issues/{number}",
    )


def _answered_case(
    case_id: str,
    *expected_source_numbers: int,
    category: str = "direct_retrieval",
    split: str = "development",
) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        split=split,
        question=f"Why was {case_id} changed?",
        category=category,
        expected_outcome="answered",
        expected_sources=tuple(_source(number) for number in expected_source_numbers),
        review_note="Confirmed against the linked thread.",
    )


def _insufficient_case(case_id: str, *, split: str = "held_out") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        split=split,
        question=f"Why was {case_id} changed?",
        category="unanswerable_control",
        expected_outcome="insufficient_evidence",
        review_note="No matching rationale found.",
    )


def _question_set(*cases: EvaluationCase) -> EvaluationQuestionSet:
    return EvaluationQuestionSet(
        question_set_schema_version=1, question_set_version=1, cases=cases
    )


# --- compute_case_retrieval_metrics -----------------------------------------


def test_hit_at_5_and_mrr_at_5_for_a_first_rank_hit() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=("github:google/gson:issue:1",),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is True
    assert metrics.mrr_at_5 == pytest.approx(1.0)
    assert metrics.source_recall_at_5 is None


def test_mrr_at_5_reflects_the_matching_rank() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=(
            "github:google/gson:issue:99",
            "github:google/gson:issue:98",
            "github:google/gson:issue:1",
        ),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is True
    assert metrics.mrr_at_5 == pytest.approx(1.0 / 3.0)


def test_miss_yields_zero_mrr_and_no_hit() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=("github:google/gson:issue:2",),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is False
    assert metrics.mrr_at_5 == 0.0


def test_source_recall_only_defined_for_multi_source_cases() -> None:
    case = _answered_case("case-1", 1, 2, 3)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=(
            "github:google/gson:issue:1",
            "github:google/gson:issue:2",
        ),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is True
    assert metrics.source_recall_at_5 == pytest.approx(2 / 3)


def test_repeated_chunks_from_one_source_do_not_inflate_recall() -> None:
    case = _answered_case("case-1", 1, 2)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=(
            "github:google/gson:issue:1",
            "github:google/gson:issue:1",
            "github:google/gson:issue:1",
        ),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.source_recall_at_5 == pytest.approx(0.5)


def _filler_ids(count: int) -> tuple[str, ...]:
    """Distinct source IDs that never match any expected source, used to
    push a relevant result past a specific rank."""
    return tuple(f"github:google/gson:issue:{900 + index}" for index in range(count))


# --- top-5 boundary: only the first five retrieved source IDs count --------


def test_relevant_source_at_rank_5_counts_as_a_hit() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=(*_filler_ids(4), "github:google/gson:issue:1"),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is True
    assert metrics.mrr_at_5 == pytest.approx(1.0 / 5.0)


def test_relevant_source_at_rank_6_does_not_count_as_a_hit() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        retrieved_source_ids=(*_filler_ids(5), "github:google/gson:issue:1"),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is False


def test_mrr_is_zero_when_first_relevant_source_is_beyond_rank_5() -> None:
    case = _answered_case("case-1", 1)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        # The relevant source sits at rank 7, well past the top-five cutoff.
        retrieved_source_ids=(*_filler_ids(6), "github:google/gson:issue:1"),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is False
    assert metrics.mrr_at_5 == 0.0


def test_source_recall_at_5_ignores_expected_sources_only_seen_past_rank_5() -> None:
    case = _answered_case("case-1", 1, 2)
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="bm25",
        # source 1 is within the top five; source 2 only appears at rank 6.
        retrieved_source_ids=(
            "github:google/gson:issue:1",
            *_filler_ids(4),
            "github:google/gson:issue:2",
        ),
        latency_seconds=0.01,
    )
    metrics = compute_case_retrieval_metrics(case, result)
    assert metrics.hit_at_5 is True
    assert metrics.source_recall_at_5 == pytest.approx(0.5)


def test_retrieval_metrics_reject_insufficient_evidence_case() -> None:
    case = _insufficient_case("case-1")
    result = RetrievalCaseResult(
        case_id="case-1", retriever="bm25", retrieved_source_ids=(), latency_seconds=0.0
    )
    with pytest.raises(ValueError, match="no expected sources"):
        compute_case_retrieval_metrics(case, result)


# --- compute_retriever_aggregate_metrics -------------------------------------


def test_aggregate_metrics_grouped_by_retriever_and_averaged() -> None:
    question_set = _question_set(
        _answered_case("case-1", 1), _answered_case("case-2", 2)
    )
    results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.01,
        ),
        RetrievalCaseResult(
            case_id="case-2",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:99",),
            latency_seconds=0.01,
        ),
        RetrievalCaseResult(
            case_id="case-1",
            retriever="vector",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.05,
        ),
        RetrievalCaseResult(
            case_id="case-2",
            retriever="vector",
            retrieved_source_ids=("github:google/gson:issue:98",),
            latency_seconds=0.03,
        ),
    ]

    aggregates = compute_retriever_aggregate_metrics(
        question_set, results, split="development"
    )
    by_retriever = {aggregate.retriever: aggregate for aggregate in aggregates}

    assert by_retriever["bm25"].case_count == 2
    assert by_retriever["bm25"].hit_rate_at_5 == pytest.approx(0.5)
    assert by_retriever["bm25"].mean_mrr_at_5 == pytest.approx(0.5)
    assert by_retriever["bm25"].mean_latency_seconds == pytest.approx(0.01)
    assert by_retriever["vector"].case_count == 2
    assert by_retriever["vector"].hit_rate_at_5 == pytest.approx(0.5)
    assert by_retriever["vector"].mean_latency_seconds == pytest.approx(0.04)


def test_aggregate_metrics_reject_unknown_case_id() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    results = [
        RetrievalCaseResult(
            case_id="does-not-exist",
            retriever="bm25",
            retrieved_source_ids=(),
            latency_seconds=0.0,
        )
    ]
    with pytest.raises(ValueError, match="unknown case_id"):
        compute_retriever_aggregate_metrics(question_set, results, split="development")


def test_aggregate_metrics_reject_duplicate_case_retriever_pair() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.0,
        ),
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=(),
            latency_seconds=0.0,
        ),
    ]
    with pytest.raises(ValueError, match="duplicate retrieval result"):
        compute_retriever_aggregate_metrics(question_set, results, split="development")


def test_aggregate_metrics_reject_case_from_other_split() -> None:
    """A retrieval result for a case that exists in the question set but
    belongs to a different split than the one selected must never be
    silently scored or ignored."""
    question_set = _question_set(
        _answered_case("case-1", 1, split="development"),
        _answered_case("case-2", 2, split="held_out"),
    )
    results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.0,
        ),
        RetrievalCaseResult(
            case_id="case-2",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:2",),
            latency_seconds=0.0,
        ),
    ]
    with pytest.raises(ValueError, match="belongs to split 'held_out'"):
        compute_retriever_aggregate_metrics(question_set, results, split="development")


def test_aggregate_metrics_reject_incomplete_retriever_coverage() -> None:
    """A retriever must supply exactly one result for every answerable
    case in the selected split; a partial development-only run must never
    be accepted as if it covered the whole split."""
    question_set = _question_set(
        _answered_case("case-1", 1), _answered_case("case-2", 2)
    )
    results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.0,
        ),
    ]
    with pytest.raises(ValueError, match="does not have exactly one result"):
        compute_retriever_aggregate_metrics(question_set, results, split="development")


def test_aggregate_metrics_reject_insufficient_evidence_case_in_coverage() -> None:
    """An `insufficient_evidence` case in the selected split must never
    appear among a retriever's results: retrieval metrics only exist for
    `answered` cases, so its presence is an extra, uncovered case_id."""
    question_set = _question_set(
        _answered_case("case-1", 1), _insufficient_case("case-2", split="development")
    )
    results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.0,
        ),
        RetrievalCaseResult(
            case_id="case-2",
            retriever="bm25",
            retrieved_source_ids=(),
            latency_seconds=0.0,
        ),
    ]
    with pytest.raises(ValueError, match="does not have exactly one result"):
        compute_retriever_aggregate_metrics(question_set, results, split="development")


# --- compute_answer_aggregate_metrics ----------------------------------------


def _trace(input_tokens: int = 100, output_tokens: int = 50) -> RunTrace:
    return RunTrace(
        model="claude-opus-5",
        agent_version="test-agent/1",
        searches=(),
        model_call_latencies_seconds=(0.2,),
        total_search_count=0,
        total_latency_seconds=0.3,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _answered_outcome() -> AnsweredOutcome:
    citation = Citation(
        number=1,
        evidence_id="github:google/gson:issue:1:chunk:0",
        source_id="github:google/gson:issue:1",
        source_title="Some issue",
        source_url="https://github.com/google/gson/issues/1",
        excerpt="because of X",
    )
    return AnsweredOutcome(answer="Because of X [1].", citations=(citation,))


def test_answer_aggregate_metrics_computes_outcome_accuracy_and_totals() -> None:
    question_set = _question_set(
        _answered_case("case-1", 1),
        _insufficient_case("case-2", split="development"),
    )
    results = [
        AnswerCaseResult(
            case_id="case-1",
            outcome=_answered_outcome(),
            trace=_trace(input_tokens=100, output_tokens=50),
            estimated_cost_usd=0.01,
        ),
        AnswerCaseResult(
            case_id="case-2",
            outcome=AnsweredOutcome(
                answer="X",
                citations=(
                    Citation(
                        number=1,
                        evidence_id="e1",
                        source_id="github:google/gson:issue:9",
                        source_title=None,
                        source_url="https://github.com/google/gson/issues/9",
                        excerpt="e",
                    ),
                ),
            ),
            trace=_trace(input_tokens=200, output_tokens=75),
            estimated_cost_usd=0.02,
            review=AnswerReview(
                primary_failure_stage="answered_when_should_have_abstained"
            ),
        ),
    ]

    metrics = compute_answer_aggregate_metrics(
        question_set, results, split="development"
    )
    assert metrics.case_count == 2
    assert metrics.outcome_accuracy == pytest.approx(0.5)
    assert metrics.total_input_tokens == 300
    assert metrics.total_output_tokens == 125
    assert metrics.total_estimated_cost_usd == pytest.approx(0.03)
    assert metrics.failure_stage_counts == {"answered_when_should_have_abstained": 1}


def test_answer_aggregate_metrics_correct_abstention_counts_as_accurate() -> None:
    question_set = _question_set(_insufficient_case("case-1"))
    results = [
        AnswerCaseResult(
            case_id="case-1",
            outcome=InsufficientEvidenceOutcome(explanation="No documented rationale."),
            trace=_trace(),
        )
    ]
    metrics = compute_answer_aggregate_metrics(question_set, results, split="held_out")
    assert metrics.outcome_accuracy == pytest.approx(1.0)
    assert metrics.total_estimated_cost_usd is None


def test_answer_aggregate_metrics_reject_duplicate_case_id() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    result = AnswerCaseResult(
        case_id="case-1", outcome=_answered_outcome(), trace=_trace()
    )
    with pytest.raises(ValueError, match="duplicate answer result"):
        compute_answer_aggregate_metrics(
            question_set, [result, result], split="development"
        )


def test_answer_aggregate_metrics_reject_unknown_case_id() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    result = AnswerCaseResult(
        case_id="does-not-exist", outcome=_answered_outcome(), trace=_trace()
    )
    with pytest.raises(ValueError, match="unknown case_id"):
        compute_answer_aggregate_metrics(question_set, [result], split="development")


def test_answer_aggregate_metrics_reject_case_from_other_split() -> None:
    question_set = _question_set(
        _answered_case("case-1", 1, split="development"),
        _insufficient_case("case-2", split="held_out"),
    )
    result = AnswerCaseResult(
        case_id="case-2",
        outcome=InsufficientEvidenceOutcome(explanation="No documented rationale."),
        trace=_trace(),
    )
    with pytest.raises(ValueError, match="belongs to split 'held_out'"):
        compute_answer_aggregate_metrics(question_set, [result], split="development")


def test_answer_aggregate_metrics_reject_incomplete_split_coverage() -> None:
    """Answer results, when present, must cover every case in the
    selected split -- including insufficient-evidence controls -- or the
    aggregate must be rejected rather than silently computed over a subset."""
    question_set = _question_set(
        _answered_case("case-1", 1),
        _insufficient_case("case-2", split="development"),
    )
    result = AnswerCaseResult(
        case_id="case-1", outcome=_answered_outcome(), trace=_trace()
    )
    with pytest.raises(ValueError, match="do not cover every case in split"):
        compute_answer_aggregate_metrics(question_set, [result], split="development")


# --- build_evaluation_summary -------------------------------------------------


def test_build_evaluation_summary_combines_retrieval_and_answer_metrics() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    retrieval_results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.01,
        )
    ]
    answer_results = [
        AnswerCaseResult(case_id="case-1", outcome=_answered_outcome(), trace=_trace())
    ]

    summary = build_evaluation_summary(
        run_id="run-1",
        question_set=question_set,
        split="development",
        retrieval_results=retrieval_results,
        answer_results=answer_results,
    )
    assert summary.run_id == "run-1"
    assert summary.split == "development"
    assert summary.case_count == 1
    assert len(summary.retrieval_metrics) == 1
    assert summary.answer_metrics is not None
    assert summary.answer_metrics.outcome_accuracy == pytest.approx(1.0)


def test_build_evaluation_summary_answer_metrics_none_for_retrieval_only_run() -> None:
    question_set = _question_set(_answered_case("case-1", 1))
    retrieval_results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.01,
        )
    ]
    summary = build_evaluation_summary(
        run_id="run-1",
        question_set=question_set,
        split="development",
        retrieval_results=retrieval_results,
        answer_results=[],
    )
    assert summary.answer_metrics is None


def test_build_evaluation_summary_case_count_is_scoped_to_the_selected_split() -> None:
    """`case_count` must reflect only the selected split's cases, never
    the full question set -- otherwise a partial development-only run's
    summary could be mistaken for a complete run over every case."""
    question_set = _question_set(
        _answered_case("case-1", 1, split="development"),
        _answered_case("case-2", 2, split="held_out"),
    )
    retrieval_results = [
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.01,
        )
    ]
    summary = build_evaluation_summary(
        run_id="run-1",
        question_set=question_set,
        split="development",
        retrieval_results=retrieval_results,
        answer_results=[],
    )
    assert summary.case_count == 1


def test_build_evaluation_summary_rejects_split_with_no_cases() -> None:
    question_set = _question_set(_answered_case("case-1", 1, split="development"))
    with pytest.raises(ValueError, match="no cases for split"):
        build_evaluation_summary(
            run_id="run-1",
            question_set=question_set,
            split="held_out",
            retrieval_results=[],
            answer_results=[],
        )
