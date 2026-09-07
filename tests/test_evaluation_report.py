"""Tests for the short, human-readable Markdown evaluation report."""

from datetime import UTC, date, datetime

from reporationale.application.evaluation_report import render_markdown_report
from reporationale.domain.evaluation import (
    AnswerAggregateMetrics,
    CaseRetrievalMetrics,
    EvaluationSummary,
    IndexingMeasurement,
    MeasurementLimitation,
    PhaseTiming,
    PricingBasis,
    RetrievalQueryUsage,
    RetrieverAggregateMetrics,
    RunManifest,
    SkippedItem,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="google", name="gson")
_COMMIT_SHA = "a" * 40


def _manifest() -> RunManifest:
    return RunManifest(
        run_manifest_schema_version=2,
        run_id="gson-2026-09-10",
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        command="uv run python -m reporationale.evaluation gson-2026-09-10",
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        source_schema_version=1,
        chunk_schema_version=1,
        chunker_algorithm_version=1,
        max_chars=2000,
        retrieval_result_limit=5,
        question_set_version=1,
        split="development",
        embedding_model="voyage-4",
        answering_model="claude-opus-5",
        agent_version="test-agent/1",
    )


def _indexing() -> IndexingMeasurement:
    return IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        phase_timings=(
            PhaseTiming(phase="collection", seconds=120.5),
            PhaseTiming(phase="chunking", seconds=3.2),
        ),
        github_request_count=750,
        source_count_by_type={"issue": 10, "pull_request": 5},
        chunk_count=42,
        embedding_request_count=42,
        embedding_token_count=12345,
        estimated_embedding_cost_usd=0.0074,
        snapshot_size_bytes=204800,
        skipped_items=(SkippedItem(identifier="issue:9999", reason="malformed body"),),
    )


def _query_usage(**overrides: object) -> RetrievalQueryUsage:
    fields: dict[str, object] = {
        "query_embedding_request_count": 4,
        "query_embedding_token_count": 49,
        "estimated_query_embedding_cost_usd": 0.00000294,
        "includes_refinement_diagnostic": True,
    }
    fields.update(overrides)
    return RetrievalQueryUsage(**fields)


def _summary() -> EvaluationSummary:
    return EvaluationSummary(
        run_id="gson-2026-09-10",
        question_set_version=1,
        split="development",
        case_count=1,
        retrieval_metrics=(
            RetrieverAggregateMetrics(
                retriever="bm25",
                case_count=1,
                hit_rate_at_5=1.0,
                mean_mrr_at_5=1.0,
                mean_source_recall_at_5=None,
                mean_latency_seconds=0.343,
                per_case=(
                    CaseRetrievalMetrics(
                        case_id="case-1", retriever="bm25", hit_at_5=True, mrr_at_5=1.0
                    ),
                ),
            ),
        ),
        answer_metrics=AnswerAggregateMetrics(
            case_count=1,
            outcome_accuracy=1.0,
            total_input_tokens=100,
            total_output_tokens=50,
            total_estimated_cost_usd=0.01,
            mean_total_latency_seconds=1.5,
            failure_stage_counts={},
        ),
    )


def test_report_is_readable_markdown_with_expected_sections() -> None:
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), query_usage=_query_usage(), summary=_summary()
    )

    assert report.startswith("# Evaluation run `gson-2026-09-10`")
    assert "## Indexing" in report
    assert "## Voyage usage" in report
    assert "## Retrieval" in report
    assert "## Answering" in report
    assert "google/gson" in report
    assert _COMMIT_SHA in report
    assert "bm25" in report
    assert "100.0%" in report  # hit_rate_at_5 formatted as a percentage
    assert "voyage-4" in report
    assert "claude-opus-5" in report
    assert "Document embedding requests: 42" in report
    assert "Snapshot size: 204800 bytes" in report
    assert "Total phase time: 123.70 s" in report  # 120.5 + 3.2
    assert "0.343" in report  # mean_latency_seconds for the bm25 retriever
    assert "**Split:** development" in report
    assert report.endswith("\n")


def test_report_shows_query_usage_and_combined_voyage_cost() -> None:
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), query_usage=_query_usage(), summary=_summary()
    )

    assert "Query embedding requests: 4" in report
    assert "Query embedding tokens: 49" in report
    assert "Estimated query embedding cost: $0.00000294" in report
    assert "Query usage includes the refinement diagnostic: yes" in report
    # document $0.0074 + query $0.00000294 = $0.00740294
    assert "Combined Voyage list-price cost (document + query): $0.00740294" in report


def test_report_shows_query_usage_without_refinement_diagnostic() -> None:
    report = render_markdown_report(
        manifest=_manifest(),
        indexing=_indexing(),
        query_usage=_query_usage(includes_refinement_diagnostic=False),
        summary=_summary(),
    )
    assert "Query usage includes the refinement diagnostic: no" in report


def test_report_shows_measurement_limitations_separately_from_skipped_items() -> None:
    indexing = IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        github_request_count=750,
        chunk_count=42,
        skipped_items=(),
        measurement_limitations=(
            MeasurementLimitation(
                description=(
                    "Blank-message commits were excluded as non-rationale "
                    "content during source ingestion, but their exact count "
                    "and identities cannot be reconstructed from the "
                    "completed normalized snapshot."
                )
            ),
        ),
    )
    report = render_markdown_report(
        manifest=_manifest(), indexing=indexing, query_usage=_query_usage(), summary=_summary()
    )
    assert "Measurement limitations:" in report
    assert (
        "Blank-message commits were excluded as non-rationale content "
        "during source ingestion" in report
    )
    assert "Skipped items:" not in report


def test_report_omits_measurement_limitations_section_when_none_are_known() -> None:
    """An empty `measurement_limitations` tuple (no known gap) must not be
    rendered as if a limitation exists; the section must simply be
    absent, distinct from ever claiming zero limitations are known."""
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), query_usage=_query_usage(), summary=_summary()
    )
    assert "Measurement limitations:" not in report


def test_report_omits_pricing_basis_section_when_none_are_recorded() -> None:
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), query_usage=_query_usage(), summary=_summary()
    )
    assert "## Pricing basis" not in report


def test_report_shows_pricing_basis_when_recorded() -> None:
    manifest = _manifest().model_copy(
        update={
            "pricing_bases": (
                PricingBasis(
                    provider="anthropic",
                    model="claude-opus-5",
                    input_price_per_million_usd=5.0,
                    output_price_per_million_usd=25.0,
                    verified_on=date(2026, 9, 7),
                ),
                PricingBasis(
                    provider="voyage",
                    model="voyage-4",
                    input_price_per_million_usd=0.06,
                    verified_on=date(2026, 9, 7),
                ),
            )
        }
    )
    report = render_markdown_report(
        manifest=manifest, indexing=_indexing(), query_usage=_query_usage(), summary=_summary()
    )
    assert "## Pricing basis" in report
    assert "| anthropic | claude-opus-5 | 5.00 | 25.00 | 2026-09-07 |" in report
    assert "| voyage | voyage-4 | 0.06 | n/a | 2026-09-07 |" in report


def test_report_handles_missing_answer_metrics() -> None:
    summary = EvaluationSummary(
        run_id="gson-2026-09-10",
        question_set_version=1,
        split="held_out",
        case_count=1,
        retrieval_metrics=(),
        answer_metrics=None,
    )
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), query_usage=_query_usage(), summary=summary
    )
    assert "**Split:** held_out" in report
    assert "No retrieval results recorded for this run." in report
    assert "No answer results recorded for this run." in report
