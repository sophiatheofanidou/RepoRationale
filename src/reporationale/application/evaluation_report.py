"""Render a short, human-readable Markdown report for one completed
evaluation run.

A pure function of already-computed typed contracts
(`reporationale.domain.evaluation`): it never recomputes a metric itself
and never touches the filesystem or a provider API. Aggregate rendering
therefore always agrees with `EvaluationSummary`, which is itself computed
from case results by `reporationale.application.evaluation_metrics`.
"""

from reporationale.domain.evaluation import (
    EvaluationSummary,
    IndexingMeasurement,
    RunManifest,
)


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_optional_percent(value: float | None) -> str:
    return _format_percent(value) if value is not None else "n/a"


def _format_optional_cost(value: float | None) -> str:
    return f"${value:.4f}" if value is not None else "n/a"


def render_markdown_report(
    *,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    summary: EvaluationSummary,
) -> str:
    """Render the fixed run manifest, indexing measurements, and computed
    `EvaluationSummary` as a single short Markdown document."""
    lines: list[str] = []
    lines.append(f"# Evaluation run `{manifest.run_id}`")
    lines.append("")
    lines.append(f"- **Repository:** `{manifest.repository.repository}`")
    lines.append(f"- **Resolved commit:** `{manifest.resolved_commit_sha}`")
    lines.append(f"- **Created at:** {manifest.created_at.isoformat()}")
    lines.append(f"- **Question-set version:** {manifest.question_set_version}")
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
    if indexing.embedding_request_count is not None:
        lines.append(f"- Embedding requests: {indexing.embedding_request_count}")
    if indexing.embedding_token_count is not None:
        lines.append(f"- Embedding tokens: {indexing.embedding_token_count}")
    if indexing.estimated_embedding_cost_usd is not None:
        lines.append(
            "- Estimated embedding cost: "
            f"{_format_optional_cost(indexing.estimated_embedding_cost_usd)}"
        )
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
