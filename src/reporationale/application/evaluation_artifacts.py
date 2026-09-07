"""Application workflow for complete, semantically validated evaluation runs.

This module owns evaluation semantics: it derives summary/report artifacts from
raw records and a reviewed question set, validates their cross-artifact
relationships, and delegates only filesystem persistence to the evaluation
store adapter.
"""

from collections.abc import Sequence
from pathlib import Path

from reporationale.adapters.evaluation_store import (
    EvaluationRunCorrupted,
    LoadedEvaluationRun,
    load_evaluation_artifacts,
    publish_evaluation_artifacts,
)
from reporationale.application.evaluation_metrics import build_evaluation_summary
from reporationale.application.evaluation_report import render_markdown_report
from reporationale.domain.evaluation import (
    QUESTION_SET_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
    AnswerCaseResult,
    EvaluationQuestionSet,
    IndexingMeasurement,
    RetrievalCaseResult,
    RunManifest,
)


def _cross_artifact_mismatch(
    *,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    question_set: EvaluationQuestionSet,
) -> str | None:
    if manifest.repository != indexing.repository:
        return "the run manifest and indexing measurement disagree about repository"
    if manifest.resolved_commit_sha != indexing.resolved_commit_sha:
        return "the run manifest and indexing measurement disagree about commit"
    if manifest.question_set_version != question_set.question_set_version:
        return "the run manifest's question_set_version does not match the question set"
    return None


def _validate_inputs(
    *,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    question_set: EvaluationQuestionSet,
) -> None:
    if manifest.run_manifest_schema_version != RUN_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            "manifest.run_manifest_schema_version "
            f"({manifest.run_manifest_schema_version}) is not supported by "
            f"this build (expected {RUN_MANIFEST_SCHEMA_VERSION})"
        )
    if question_set.question_set_schema_version != QUESTION_SET_SCHEMA_VERSION:
        raise ValueError(
            "question_set.question_set_schema_version "
            f"({question_set.question_set_schema_version}) is not supported by "
            f"this build (expected {QUESTION_SET_SCHEMA_VERSION})"
        )
    mismatch = _cross_artifact_mismatch(
        manifest=manifest, indexing=indexing, question_set=question_set
    )
    if mismatch is not None:
        raise ValueError(f"Cannot use an inconsistent evaluation run: {mismatch}.")


def publish_evaluation_run(
    *,
    root: Path,
    run_id: str,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    question_set: EvaluationQuestionSet,
    retrieval_results: Sequence[RetrievalCaseResult],
    answer_results: Sequence[AnswerCaseResult],
    force: bool = False,
) -> Path:
    """Derive and atomically publish one complete evaluation run."""
    if manifest.run_id != run_id:
        raise ValueError("manifest.run_id must equal run_id")
    _validate_inputs(manifest=manifest, indexing=indexing, question_set=question_set)
    summary = build_evaluation_summary(
        run_id=run_id,
        question_set=question_set,
        retrieval_results=retrieval_results,
        answer_results=answer_results,
    )
    report_markdown = render_markdown_report(
        manifest=manifest, indexing=indexing, summary=summary
    )
    return publish_evaluation_artifacts(
        root=root,
        run_id=run_id,
        manifest=manifest,
        indexing=indexing,
        retrieval_results=retrieval_results,
        answer_results=answer_results,
        summary=summary,
        report_markdown=report_markdown,
        force=force,
    )


def load_evaluation_run(
    directory: Path, *, question_set: EvaluationQuestionSet
) -> LoadedEvaluationRun:
    """Load a run and validate it semantically against its question set."""
    loaded = load_evaluation_artifacts(directory)
    try:
        _validate_inputs(
            manifest=loaded.manifest,
            indexing=loaded.indexing,
            question_set=question_set,
        )
        recomputed_summary = build_evaluation_summary(
            run_id=loaded.manifest.run_id,
            question_set=question_set,
            retrieval_results=loaded.retrieval_results,
            answer_results=loaded.answer_results,
        )
    except ValueError as error:
        raise EvaluationRunCorrupted(
            f"The evaluation run and question set do not match: {error}"
        ) from None
    if loaded.summary != recomputed_summary:
        raise EvaluationRunCorrupted(
            "The persisted summary does not match the aggregate computed from "
            "the run's raw results."
        )
    recomputed_report = render_markdown_report(
        manifest=loaded.manifest,
        indexing=loaded.indexing,
        summary=recomputed_summary,
    )
    if loaded.report_markdown != recomputed_report:
        raise EvaluationRunCorrupted(
            "The persisted report does not match the report rendered from the run."
        )
    return loaded
