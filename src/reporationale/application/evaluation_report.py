"""Render a short, human-readable Markdown report for one completed
evaluation run.

A pure function of already-computed typed contracts
(`reporationale.domain.evaluation`): it never recomputes a metric itself
and never touches the filesystem or a provider API. Aggregate rendering
therefore always agrees with `EvaluationSummary`, which is itself computed
from case results by `reporationale.application.evaluation_metrics`. The
report always displays the run's selected split and that split's own case
count, both sourced from `summary` rather than recomputed here. Voyage
usage is rendered from two separate raw sources -- document (build-time)
embedding from `indexing` and query (retrieval-time) embedding from
`query_usage` -- combined into one list-price total only at render time,
never accepted as an independent input.
"""

from reporationale.domain.evaluation import (
    EvaluationSummary,
    IndexingMeasurement,
    RetrievalQueryUsage,
    RunManifest,
)


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_optional_percent(value: float | None) -> str:
    return _format_percent(value) if value is not None else "n/a"


def _format_optional_cost(value: float | None) -> str:
    return f"${value:.4f}" if value is not None else "n/a"


def _format_optional_cost_precise(value: float | None) -> str:
    """Eight-decimal formatting for a query-embedding-scale cost, where a
    typical few-dozen-token query values in the fourth decimal place and
    below would otherwise round away to `$0.0000`."""
    return f"${value:.8f}" if value is not None else "n/a"


def render_markdown_report(
    *,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    query_usage: RetrievalQueryUsage,
    summary: EvaluationSummary,
) -> str:
    """Render the fixed run manifest, indexing measurements, retrieval-query
    usage, and computed `EvaluationSummary` as a single short Markdown
    document."""
    lines: list[str] = []
    lines.append(f"# Evaluation run `{manifest.run_id}`")
    lines.append("")
    lines.append(f"- **Repository:** `{manifest.repository.repository}`")
    lines.append(f"- **Resolved commit:** `{manifest.resolved_commit_sha}`")
    lines.append(f"- **Created at:** {manifest.created_at.isoformat()}")
    lines.append(f"- **Question-set version:** {manifest.question_set_version}")
    lines.append(f"- **Split:** {summary.split}")
    lines.append(f"- **Case count:** {summary.case_count}")
    lines.append(f"- **Chunk size:** {manifest.max_chars} characters")
    lines.append(f"- **Retrieval result limit (K):** {manifest.retrieval_result_limit}")
    if manifest.embedding_model is not None:
        lines.append(f"- **Embedding model:** `{manifest.embedding_model}`")
    if manifest.answering_model is not None:
        lines.append(f"- **Answering model:** `{manifest.answering_model}`")
    if manifest.agent_version is not None:
        lines.append(f"- **Agent version:** `{manifest.agent_version}`")
    lines.append(f"- **Command:** `{manifest.command}`")
    lines.append("")

    lines.append("## Indexing")
    lines.append("")
    lines.append(f"- GitHub requests: {indexing.github_request_count}")
    lines.append(f"- Chunk count: {indexing.chunk_count}")
    if indexing.source_count_by_type:
        lines.append("- Sources by type:")
        for source_type in sorted(indexing.source_count_by_type):
            count = indexing.source_count_by_type[source_type]
            lines.append(f"  - `{source_type}`: {count}")
    if indexing.snapshot_size_bytes is not None:
        lines.append(f"- Snapshot size: {indexing.snapshot_size_bytes} bytes")
    if indexing.phase_timings:
        total_phase_seconds = sum(phase.seconds for phase in indexing.phase_timings)
        lines.append(f"- Total phase time: {total_phase_seconds:.2f} s")
        lines.append("")
        lines.append("| Phase | Seconds |")
        lines.append("| --- | ---: |")
        for phase in indexing.phase_timings:
            lines.append(f"| {phase.phase} | {phase.seconds:.2f} |")
    if indexing.skipped_items:
        lines.append("")
        lines.append("Skipped items:")
        for item in indexing.skipped_items:
            lines.append(f"- `{item.identifier}`: {item.reason}")
    if indexing.measurement_limitations:
        lines.append("")
        lines.append("Measurement limitations:")
        for limitation in indexing.measurement_limitations:
            lines.append(f"- {limitation.description}")
    lines.append("")

    lines.append("## Voyage usage")
    lines.append("")
    lines.append(
        f"- Document embedding requests: {indexing.embedding_request_count}"
        if indexing.embedding_request_count is not None
        else "- Document embedding requests: n/a"
    )
    lines.append(
        f"- Document embedding tokens: {indexing.embedding_token_count}"
        if indexing.embedding_token_count is not None
        else "- Document embedding tokens: n/a"
    )
    lines.append(
        "- Estimated document embedding cost: "
        f"{_format_optional_cost_precise(indexing.estimated_embedding_cost_usd)}"
    )
    lines.append(
        f"- Query embedding requests: {query_usage.query_embedding_request_count}"
    )
    lines.append(f"- Query embedding tokens: {query_usage.query_embedding_token_count}")
    lines.append(
        "- Estimated query embedding cost: "
        f"{_format_optional_cost_precise(query_usage.estimated_query_embedding_cost_usd)}"
    )
    lines.append(
        "- Query usage includes the refinement diagnostic: "
        + ("yes" if query_usage.includes_refinement_diagnostic else "no")
    )
    if (
        indexing.estimated_embedding_cost_usd is not None
        or query_usage.estimated_query_embedding_cost_usd is not None
    ):
        combined_cost = (indexing.estimated_embedding_cost_usd or 0.0) + (
            query_usage.estimated_query_embedding_cost_usd or 0.0
        )
        lines.append(
            "- Combined Voyage list-price cost (document + query): "
            f"{_format_optional_cost_precise(combined_cost)}"
        )
    lines.append("")

    lines.append("## Retrieval")
    lines.append("")
    if summary.retrieval_metrics:
        lines.append(
            "| Retriever | Cases | Hit@5 | MRR@5 | Source Recall@5 | Mean Latency (s) |"
        )
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for retriever_metrics in summary.retrieval_metrics:
            lines.append(
                f"| {retriever_metrics.retriever} "
                f"| {retriever_metrics.case_count} "
                f"| {_format_percent(retriever_metrics.hit_rate_at_5)} "
                f"| {retriever_metrics.mean_mrr_at_5:.3f} "
                "| "
                f"{_format_optional_percent(retriever_metrics.mean_source_recall_at_5)} "
                f"| {retriever_metrics.mean_latency_seconds:.3f} |"
            )
    else:
        lines.append("No retrieval results recorded for this run.")
    lines.append("")

    lines.append("## Answering")
    lines.append("")
    if summary.answer_metrics is not None:
        answer_metrics = summary.answer_metrics
        lines.append(f"- Cases: {answer_metrics.case_count}")
        lines.append(
            f"- Outcome accuracy: {_format_percent(answer_metrics.outcome_accuracy)}"
        )
        lines.append(f"- Input tokens: {answer_metrics.total_input_tokens}")
        lines.append(f"- Output tokens: {answer_metrics.total_output_tokens}")
        lines.append(
            "- Estimated cost: "
            f"{_format_optional_cost(answer_metrics.total_estimated_cost_usd)}"
        )
        lines.append(
            f"- Mean total latency: {answer_metrics.mean_total_latency_seconds:.2f} s"
        )
        if answer_metrics.failure_stage_counts:
            lines.append("")
            lines.append("Failure stages:")
            for stage in sorted(answer_metrics.failure_stage_counts):
                count = answer_metrics.failure_stage_counts[stage]
                lines.append(f"- `{stage}`: {count}")
    else:
        lines.append("No answer results recorded for this run.")
    lines.append("")

    return "\n".join(lines) + "\n"
