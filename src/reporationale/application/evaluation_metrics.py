"""Pure aggregate-metric computation over already-collected evaluation
case results.

Every aggregate here (`RetrieverAggregateMetrics`, `AnswerAggregateMetrics`,
`EvaluationSummary`) is derived strictly from the raw case-level results
already defined in `reporationale.domain.evaluation`
(`RetrievalCaseResult`, `AnswerCaseResult`) plus the reviewed
`EvaluationQuestionSet` those results were run against. Nothing in this
module accepts an aggregate metric as an independent input value: a
caller can never supply a Hit@5/MRR@5/outcome-accuracy number that
disagrees with the underlying case results.

Deliberately provider-independent: no GitHub, Voyage, Chroma, or
Anthropic call, and no filesystem access.
"""

from collections.abc import Sequence

from reporationale.domain.evaluation import (
    AnswerAggregateMetrics,
    AnswerCaseResult,
    CaseRetrievalMetrics,
    EvaluationCase,
    EvaluationQuestionSet,
    EvaluationSplit,
    EvaluationSummary,
    RetrievalCaseResult,
    RetrieverAggregateMetrics,
)

# The product's fixed retrieval result limit (see `search_history` in
# `reporationale.application.vector_retrieval`): Hit@5/MRR@5/source-Recall@5
# are metrics over the top five results specifically, not over however many
# entries a `RetrievalCaseResult` happens to carry.
_RETRIEVAL_TOP_K = 5


def compute_case_retrieval_metrics(
    case: EvaluationCase, result: RetrievalCaseResult
) -> CaseRetrievalMetrics:
    """Hit@5, MRR@5, and source Recall@5 for one case's retrieved
    evidence, judged at source level against `case.expected_sources` and
    considering only the first `_RETRIEVAL_TOP_K` (5) entries of
    `result.retrieved_source_ids` -- a relevant source at rank 6 or later
    never counts as a hit, contributes nothing to MRR, and is excluded from
    source Recall@5. `source_recall_at_5` stays `None` for a case with
    fewer than two expected sources, matching the metric's definition in
    the project's evaluation plan (only for questions that require
    multiple expected sources).

    Raises `ValueError` if `case` is not an `answered` case with at least
    one expected source: retrieval metrics are only defined for
    answerable questions with declared ground truth.
    """
    if case.expected_outcome != "answered" or not case.expected_sources:
        raise ValueError(
            f"case {case.case_id!r} has no expected sources to score retrieval against"
        )
    expected_source_ids = {source.source_id for source in case.expected_sources}
    ranked_source_ids = result.retrieved_source_ids[:_RETRIEVAL_TOP_K]

    hit = any(source_id in expected_source_ids for source_id in ranked_source_ids)
    reciprocal_rank = 0.0
    for rank, source_id in enumerate(ranked_source_ids, start=1):
        if source_id in expected_source_ids:
            reciprocal_rank = 1.0 / rank
            break

    source_recall: float | None = None
    if len(expected_source_ids) > 1:
        found = {
            source_id
            for source_id in expected_source_ids
            if source_id in ranked_source_ids
        }
        source_recall = len(found) / len(expected_source_ids)

    return CaseRetrievalMetrics(
        case_id=case.case_id,
        retriever=result.retriever,
        hit_at_5=hit,
        mrr_at_5=reciprocal_rank,
        source_recall_at_5=source_recall,
    )


def compute_retriever_aggregate_metrics(
    question_set: EvaluationQuestionSet,
    retrieval_results: Sequence[RetrievalCaseResult],
    *,
    split: EvaluationSplit,
) -> tuple[RetrieverAggregateMetrics, ...]:
    """Group `retrieval_results` by retriever and compute each retriever's
    aggregate Hit@5/MRR@5/source-Recall@5 across its own recorded cases,
    ordered deterministically by retriever name. Every result is judged
    against `split`: a retrieval result for a case belonging to the other
    split is rejected outright rather than silently scored or ignored, and
    every retriever that appears at all in `retrieval_results` must supply
    exactly one result for every `answered` case in `split` -- no more, no
    fewer.

    Raises `ValueError` for a result referencing a case_id absent from
    `question_set`, a result for a case belonging to a split other than
    `split`, a retriever whose results do not exactly cover every
    answerable case in `split`, or a repeated `(case_id, retriever)` pair.
    """
    cases_by_id = {case.case_id: case for case in question_set.cases}
    answerable_case_ids_in_split = {
        case.case_id
        for case in question_set.cases
        if case.split == split and case.expected_outcome == "answered"
    }
    grouped: dict[str, list[RetrievalCaseResult]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for result in retrieval_results:
        case = cases_by_id.get(result.case_id)
        if case is None:
            raise ValueError(
                f"retrieval result references unknown case_id {result.case_id!r}"
            )
        if case.split != split:
            raise ValueError(
                f"retrieval result for case_id {result.case_id!r} belongs to "
                f"split {case.split!r}, not the selected split {split!r}"
            )
        pair = (result.case_id, result.retriever)
        if pair in seen_pairs:
            raise ValueError(
                f"duplicate retrieval result for case_id={result.case_id!r}, "
                f"retriever={result.retriever!r}"
            )
        seen_pairs.add(pair)
        grouped.setdefault(result.retriever, []).append(result)

    aggregates: list[RetrieverAggregateMetrics] = []
    for retriever in sorted(grouped):
        results = grouped[retriever]
        result_case_ids = {result.case_id for result in results}
        if result_case_ids != answerable_case_ids_in_split:
            missing = sorted(answerable_case_ids_in_split - result_case_ids)
            extra = sorted(result_case_ids - answerable_case_ids_in_split)
            raise ValueError(
                f"retriever {retriever!r} does not have exactly one result "
                f"for every answerable case in split {split!r} "
                f"(missing={missing}, extra={extra})"
            )
        per_case = tuple(
            compute_case_retrieval_metrics(cases_by_id[result.case_id], result)
            for result in results
        )
        case_count = len(per_case)
        hit_rate = sum(1 for metric in per_case if metric.hit_at_5) / case_count
        mean_mrr = sum(metric.mrr_at_5 for metric in per_case) / case_count
        recall_values = [
            metric.source_recall_at_5
            for metric in per_case
            if metric.source_recall_at_5 is not None
        ]
        mean_recall = sum(recall_values) / len(recall_values) if recall_values else None
        # Latency is per retrieval call, not per top-K result, so it is
        # computed from the full raw results rather than the top-K slice
        # used for Hit@5/MRR@5/source-Recall@5.
        mean_latency = sum(result.latency_seconds for result in results) / case_count
        aggregates.append(
            RetrieverAggregateMetrics(
                retriever=retriever,
                case_count=case_count,
                hit_rate_at_5=hit_rate,
                mean_mrr_at_5=mean_mrr,
                mean_source_recall_at_5=mean_recall,
                mean_latency_seconds=mean_latency,
                per_case=per_case,
            )
        )
    return tuple(aggregates)


def compute_answer_aggregate_metrics(
    question_set: EvaluationQuestionSet,
    answer_results: Sequence[AnswerCaseResult],
    *,
    split: EvaluationSplit,
) -> AnswerAggregateMetrics:
    """Outcome accuracy, token/cost totals, mean latency, and failure-stage
    counts across `answer_results`, judged against each case's own
    `expected_outcome`. Every result is judged against `split`: a result
    for a case belonging to the other split is rejected outright, and
    `answer_results` must supply exactly one result for every case in
    `split` -- both `answered` and `insufficient_evidence` controls --
    whenever this function is called at all (an entirely retrieval-only
    run never calls it; see `build_evaluation_summary`).

    Raises `ValueError` for a result referencing a case_id absent from
    `question_set`, a result for a case belonging to a split other than
    `split`, a repeated case_id, or a set of results that does not exactly
    cover every case in `split`.
    """
    cases_by_id = {case.case_id: case for case in question_set.cases}
    split_case_ids = {case.case_id for case in question_set.cases if case.split == split}
    seen_case_ids: set[str] = set()

    correct = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost = 0.0
    any_cost_recorded = False
    total_latency = 0.0
    failure_stage_counts: dict[str, int] = {}

    for result in answer_results:
        case = cases_by_id.get(result.case_id)
        if case is None:
            raise ValueError(
                f"answer result references unknown case_id {result.case_id!r}"
            )
        if case.split != split:
            raise ValueError(
                f"answer result for case_id {result.case_id!r} belongs to "
                f"split {case.split!r}, not the selected split {split!r}"
            )
        if result.case_id in seen_case_ids:
            raise ValueError(f"duplicate answer result for case_id {result.case_id!r}")
        seen_case_ids.add(result.case_id)

        if result.outcome.status == case.expected_outcome:
            correct += 1
        total_input_tokens += result.trace.input_tokens
        total_output_tokens += result.trace.output_tokens
        total_latency += result.trace.total_latency_seconds
        if result.estimated_cost_usd is not None:
            total_cost += result.estimated_cost_usd
            any_cost_recorded = True
        failure_stage = result.review.primary_failure_stage
        if failure_stage is not None:
            failure_stage_counts[failure_stage] = (
                failure_stage_counts.get(failure_stage, 0) + 1
            )

    if seen_case_ids != split_case_ids:
        missing = sorted(split_case_ids - seen_case_ids)
        raise ValueError(
            f"answer results do not cover every case in split {split!r} "
            f"(missing={missing})"
        )

    case_count = len(answer_results)
    return AnswerAggregateMetrics(
        case_count=case_count,
        outcome_accuracy=(correct / case_count) if case_count else 0.0,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_estimated_cost_usd=total_cost if any_cost_recorded else None,
        mean_total_latency_seconds=(total_latency / case_count) if case_count else 0.0,
        failure_stage_counts=failure_stage_counts,
    )


def build_evaluation_summary(
    *,
    run_id: str,
    question_set: EvaluationQuestionSet,
    split: EvaluationSplit,
    retrieval_results: Sequence[RetrievalCaseResult],
    answer_results: Sequence[AnswerCaseResult],
) -> EvaluationSummary:
    """Build the complete `EvaluationSummary` for one run from its raw
    case-level retrieval and answer results, scoped to `split`.
    `case_count` is the number of cases in `split` only -- never the full
    question set -- so a development-only run can never report the
    complete question set's case count. `answer_metrics` stays `None` when
    `answer_results` is empty (a retrieval-only run).

    Raises `ValueError` if `question_set` has no case belonging to
    `split`.
    """
    split_case_count = sum(1 for case in question_set.cases if case.split == split)
    if split_case_count == 0:
        raise ValueError(f"the question set has no cases for split {split!r}")
    retrieval_metrics = compute_retriever_aggregate_metrics(
        question_set, retrieval_results, split=split
    )
    answer_metrics = (
        compute_answer_aggregate_metrics(question_set, answer_results, split=split)
        if answer_results
        else None
    )
    return EvaluationSummary(
        run_id=run_id,
        question_set_version=question_set.question_set_version,
        split=split,
        case_count=split_case_count,
        retrieval_metrics=retrieval_metrics,
        answer_metrics=answer_metrics,
    )
