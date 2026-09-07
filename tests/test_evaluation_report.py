"""Tests for the short, human-readable Markdown evaluation report."""

from datetime import UTC, datetime

from reporationale.application.evaluation_report import render_markdown_report
from reporationale.domain.evaluation import (
    AnswerAggregateMetrics,
    CaseRetrievalMetrics,
    EvaluationSummary,
    IndexingMeasurement,
    PhaseTiming,
    RetrieverAggregateMetrics,
    RunManifest,
    SkippedItem,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="google", name="gson")
_COMMIT_SHA = "a" * 40


def _manifest() -> RunManifest:
    return RunManifest(
        run_manifest_schema_version=1,
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


def _summary() -> EvaluationSummary:
    return EvaluationSummary(
        run_id="gson-2026-09-10",
        question_set_version=1,
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
        manifest=_manifest(), indexing=_indexing(), summary=_summary()
    )

    assert report.startswith("# Evaluation run `gson-2026-09-10`")
    assert "## Indexing" in report
    assert "## Retrieval" in report
    assert "## Answering" in report
    assert "google/gson" in report
    assert _COMMIT_SHA in report
    assert "bm25" in report
    assert "100.0%" in report  # hit_rate_at_5 formatted as a percentage
    assert "voyage-4" in report
    assert "claude-opus-5" in report
    assert "Embedding requests: 42" in report
    assert "Snapshot size: 204800 bytes" in report
    assert "Total phase time: 123.70 s" in report  # 120.5 + 3.2
    assert "0.343" in report  # mean_latency_seconds for the bm25 retriever
    assert report.endswith("\n")


def test_report_handles_missing_answer_metrics() -> None:
    summary = EvaluationSummary(
        run_id="gson-2026-09-10",
        question_set_version=1,
        case_count=1,
        retrieval_metrics=(),
        answer_metrics=None,
    )
    report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), summary=summary
    )
    assert "No retrieval results recorded for this run." in report
    assert "No answer results recorded for this run." in report
