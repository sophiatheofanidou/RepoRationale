"""Local filesystem persistence for the evaluation-harness artifact
contract: a versioned question-set input file, and one complete,
atomically published evaluation run directory.

Isolates all filesystem access for this contract, the same way
`reporationale.adapters.snapshot_store` isolates the normalized-source and
derived-chunk artifact layout. Reuses that module's generic
staged-directory publication helpers (`write_file_durably`,
`publish_staged_directory`, `recover_interrupted_replacement`,
`remove_directory`) so a run directory is published and recovered through
the exact same safe protocol as every other derived artifact in this
project.

The caller always supplies the output root: this module never assumes a
`.local/` directory or any other personal path of its own.

A complete evaluation run directory contains exactly seven files:
`run-manifest.json`, `indexing.json`, `query-usage.json`,
`retrieval-results.jsonl`, `answer-results.jsonl`, `summary.json`, and
`report.md`. Nothing here calls GitHub, Voyage, Chroma, or Anthropic.

Semantic derivation and validation of `summary.json` and `report.md` belong
to `reporationale.application.evaluation_artifacts`. This adapter only
serializes, atomically publishes, reloads, and structurally validates the
typed artifacts supplied by that workflow.
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from pydantic import ValidationError

from reporationale.adapters.snapshot_store import (
    publish_staged_directory,
    recover_interrupted_replacement,
    remove_directory,
    write_file_durably,
)
from reporationale.domain.evaluation import (
    QUESTION_SET_SCHEMA_VERSION,
    RUN_MANIFEST_SCHEMA_VERSION,
    AnswerCaseResult,
    EvaluationQuestionSet,
    EvaluationSummary,
    IndexingMeasurement,
    RetrievalCaseResult,
    RetrievalQueryUsage,
    RunManifest,
)

_RUN_MANIFEST_FILENAME = "run-manifest.json"
_INDEXING_FILENAME = "indexing.json"
_QUERY_USAGE_FILENAME = "query-usage.json"
_RETRIEVAL_RESULTS_FILENAME = "retrieval-results.jsonl"
_ANSWER_RESULTS_FILENAME = "answer-results.jsonl"
_SUMMARY_FILENAME = "summary.json"
_REPORT_FILENAME = "report.md"

_SAFE_PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


class EvaluationStoreError(Exception):
    """Base class for every typed evaluation-harness persistence failure."""


class QuestionSetNotFound(EvaluationStoreError):
    """No question-set file exists at the requested path."""


class QuestionSetInvalid(EvaluationStoreError):
    """A question-set file exists but fails the versioned contract's own
    validation (malformed JSON, a blank/duplicate case field, or an
    answerable case without expected documentation)."""


class QuestionSetIncompatible(EvaluationStoreError):
    """A question-set file is well-formed but declares a
    `question_set_schema_version` this build does not support."""


class EvaluationRunNotFound(EvaluationStoreError):
    """No complete evaluation run exists at the derived directory."""


class EvaluationRunCorrupted(EvaluationStoreError):
    """An evaluation run directory exists but its content is malformed,
    unreadable, or internally inconsistent -- including a persisted
    summary or report that does not match the aggregate/rendering
    deterministically computed from the run's own raw records, or a
    manifest that disagrees with the indexing measurement or supplied
    question set about repository, commit, or question-set version."""


class EvaluationRunIncompatible(EvaluationStoreError):
    """An evaluation run directory exists and is well-formed, but its
    manifest declares a `run_manifest_schema_version` this build does not
    support."""


class EvaluationRunAlreadyExists(EvaluationStoreError):
    """A completed evaluation run already exists at the target directory
    and `force=True` was not passed to explicitly replace it."""


def _validate_safe_path_segment(value: str, *, name: str) -> str:
    """Reject anything that could make a path segment unsafe: blank,
    `.`/`..`, or a character outside a strict allowlist (mirrors
    `reporationale.adapters.snapshot_store._validate_safe_path_segment`)."""
    if value in ("", ".", ".."):
        raise ValueError(f"{name} must not be empty, '.', or '..'")
    if _SAFE_PATH_SEGMENT_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{name} contains characters unsafe for a filesystem path segment"
        )
    return value


def load_question_set(path: Path) -> EvaluationQuestionSet:
    """Load and strictly validate one versioned evaluation question-set
    JSON file. Raises `QuestionSetNotFound` for a missing/unreadable file;
    `QuestionSetInvalid` for anything that fails `EvaluationQuestionSet`'s
    own validation: malformed JSON, a blank case field, a duplicate
    case_id, an answerable case without expected documentation, or any
    other contract violation; and `QuestionSetIncompatible` for a
    well-formed file whose `question_set_schema_version` is not
    `QUESTION_SET_SCHEMA_VERSION` -- an unknown future (or otherwise
    unsupported) schema version is rejected outright rather than accepted
    merely because it is a positive integer.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        raise QuestionSetNotFound(f"No question-set file exists at {path}.") from None

    try:
        question_set = EvaluationQuestionSet.model_validate_json(raw_text)
    except ValidationError as error:
        raise QuestionSetInvalid(
            f"The question-set file at {path} is invalid: {error}"
        ) from None

    if question_set.question_set_schema_version != QUESTION_SET_SCHEMA_VERSION:
        raise QuestionSetIncompatible(
            f"The question-set file at {path} declares schema version "
            f"{question_set.question_set_schema_version}, which this build "
            f"does not support (expected {QUESTION_SET_SCHEMA_VERSION})."
        )
    return question_set


def evaluation_run_directory(*, root: Path, run_id: str) -> Path:
    """Deterministically derive the exact directory for one evaluation
    run: `<root>/<run_id>`. `run_id` is validated against a strict
    allowlist and the final path is confirmed to still resolve under
    `root`, exactly like
    `reporationale.adapters.snapshot_store.snapshot_directory`."""
    segment = _validate_safe_path_segment(run_id, name="run_id")
    candidate = root / segment

    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if (
        resolved_candidate != resolved_root
        and resolved_root not in resolved_candidate.parents
    ):
        raise ValueError(
            "derived evaluation-run path escapes the configured output root"
        )
    return candidate


def _serialize_retrieval_results_jsonl(
    records: Sequence[RetrievalCaseResult],
) -> bytes:
    """The same deterministic JSONL convention used throughout this
    project (see `reporationale.adapters.snapshot_store.serialize_sources_jsonl`):
    one compact JSON object per line, UTF-8, exactly one trailing newline
    (none for an empty sequence)."""
    if not records:
        return b""
    lines = [record.model_dump_json() for record in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _serialize_answer_results_jsonl(records: Sequence[AnswerCaseResult]) -> bytes:
    if not records:
        return b""
    lines = [record.model_dump_json() for record in records]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _parse_retrieval_results_jsonl(
    raw_bytes: bytes,
) -> tuple[RetrievalCaseResult, ...]:
    if not raw_bytes:
        return ()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise EvaluationRunCorrupted(
            "The retrieval-results file is not valid UTF-8."
        ) from None
    if not text.endswith("\n"):
        raise EvaluationRunCorrupted(
            "The retrieval-results file must end with exactly one newline."
        )

    results: list[RetrievalCaseResult] = []
    for line in text[:-1].split("\n"):
        if not line.strip():
            raise EvaluationRunCorrupted(
                "The retrieval-results file contains a blank line."
            )
        try:
            results.append(RetrievalCaseResult.model_validate_json(line))
        except ValidationError:
            raise EvaluationRunCorrupted(
                "The retrieval-results file contains an invalid record."
            ) from None
    return tuple(results)


def _parse_answer_results_jsonl(raw_bytes: bytes) -> tuple[AnswerCaseResult, ...]:
    if not raw_bytes:
        return ()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise EvaluationRunCorrupted(
            "The answer-results file is not valid UTF-8."
        ) from None
    if not text.endswith("\n"):
        raise EvaluationRunCorrupted(
            "The answer-results file must end with exactly one newline."
        )

    results: list[AnswerCaseResult] = []
    for line in text[:-1].split("\n"):
        if not line.strip():
            raise EvaluationRunCorrupted(
                "The answer-results file contains a blank line."
            )
        try:
            results.append(AnswerCaseResult.model_validate_json(line))
        except ValidationError:
            raise EvaluationRunCorrupted(
                "The answer-results file contains an invalid record."
            ) from None
    return tuple(results)


def _validate_no_duplicate_retrieval_pairs(
    results: Sequence[RetrievalCaseResult],
) -> None:
    seen: set[tuple[str, str]] = set()
    for result in results:
        pair = (result.case_id, result.retriever)
        if pair in seen:
            raise EvaluationRunCorrupted(
                "The retrieval-results file contains a duplicate "
                f"(case_id={result.case_id!r}, retriever={result.retriever!r}) pair."
            )
        seen.add(pair)


def _validate_no_duplicate_answer_case_ids(
    results: Sequence[AnswerCaseResult],
) -> None:
    seen: set[str] = set()
    for result in results:
        if result.case_id in seen:
            raise EvaluationRunCorrupted(
                "The answer-results file contains a duplicate case_id "
                f"{result.case_id!r}."
            )
        seen.add(result.case_id)


class LoadedEvaluationRun(NamedTuple):
    """A fully validated evaluation run: typed models, never a raw or
    partially-checked dictionary."""

    manifest: RunManifest
    indexing: IndexingMeasurement
    query_usage: RetrievalQueryUsage
    retrieval_results: tuple[RetrievalCaseResult, ...]
    answer_results: tuple[AnswerCaseResult, ...]
    summary: EvaluationSummary
    report_markdown: str


def _first_cross_artifact_mismatch(
    *,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    summary: EvaluationSummary,
) -> str | None:
    """Return the first structural disagreement among persisted records."""
    if manifest.repository != indexing.repository:
        return "the run manifest and indexing measurement disagree about repository"
    if manifest.resolved_commit_sha != indexing.resolved_commit_sha:
        return (
            "the run manifest and indexing measurement disagree about the "
            "resolved commit"
        )
    if manifest.run_id != summary.run_id:
        return "the run manifest and summary disagree about run_id"
    if manifest.question_set_version != summary.question_set_version:
        return "the run manifest and summary disagree about question_set_version"
    if manifest.split != summary.split:
        return "the run manifest and summary disagree about split"
    return None


def _load_evaluation_artifacts_from_directory(directory: Path) -> LoadedEvaluationRun:
    manifest_path = directory / _RUN_MANIFEST_FILENAME
    indexing_path = directory / _INDEXING_FILENAME
    query_usage_path = directory / _QUERY_USAGE_FILENAME
    retrieval_path = directory / _RETRIEVAL_RESULTS_FILENAME
    answer_path = directory / _ANSWER_RESULTS_FILENAME
    summary_path = directory / _SUMMARY_FILENAME
    report_path = directory / _REPORT_FILENAME

    required_files = (
        manifest_path,
        indexing_path,
        query_usage_path,
        retrieval_path,
        answer_path,
        summary_path,
        report_path,
    )
    if not all(path.is_file() for path in required_files):
        raise EvaluationRunNotFound(
            f"No complete evaluation run exists at {directory}."
        )

    try:
        manifest = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise EvaluationRunCorrupted(
            "The run manifest is missing, unreadable, or invalid."
        ) from None

    if manifest.run_manifest_schema_version != RUN_MANIFEST_SCHEMA_VERSION:
        raise EvaluationRunIncompatible(
            "The run manifest declares schema version "
            f"{manifest.run_manifest_schema_version}, which this build "
            f"does not support (expected {RUN_MANIFEST_SCHEMA_VERSION})."
        )

    try:
        indexing = IndexingMeasurement.model_validate_json(
            indexing_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise EvaluationRunCorrupted(
            "The indexing measurement file is missing, unreadable, or invalid."
        ) from None

    try:
        query_usage = RetrievalQueryUsage.model_validate_json(
            query_usage_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise EvaluationRunCorrupted(
            "The query-usage file is missing, unreadable, or invalid."
        ) from None

    try:
        retrieval_bytes = retrieval_path.read_bytes()
    except OSError:
        raise EvaluationRunCorrupted(
            "The retrieval-results file is unreadable."
        ) from None
    retrieval_results = _parse_retrieval_results_jsonl(retrieval_bytes)
    _validate_no_duplicate_retrieval_pairs(retrieval_results)

    try:
        answer_bytes = answer_path.read_bytes()
    except OSError:
        raise EvaluationRunCorrupted("The answer-results file is unreadable.") from None
    answer_results = _parse_answer_results_jsonl(answer_bytes)
    _validate_no_duplicate_answer_case_ids(answer_results)

    try:
        summary = EvaluationSummary.model_validate_json(
            summary_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError):
        raise EvaluationRunCorrupted(
            "The summary file is missing, unreadable, or invalid."
        ) from None

    try:
        report_markdown = report_path.read_text(encoding="utf-8")
    except OSError:
        raise EvaluationRunCorrupted("The report file is unreadable.") from None
    if not report_markdown.strip():
        raise EvaluationRunCorrupted("The report file must not be blank.")

    mismatch = _first_cross_artifact_mismatch(
        manifest=manifest, indexing=indexing, summary=summary
    )
    if mismatch is not None:
        raise EvaluationRunCorrupted(f"The evaluation run is inconsistent: {mismatch}.")

    return LoadedEvaluationRun(
        manifest=manifest,
        indexing=indexing,
        query_usage=query_usage,
        retrieval_results=retrieval_results,
        answer_results=answer_results,
        summary=summary,
        report_markdown=report_markdown,
    )


def load_evaluation_artifacts(directory: Path) -> LoadedEvaluationRun:
    """Load and structurally validate one published evaluation run.

    Semantic validation against the versioned question set is performed by
    the application workflow after this adapter returns the typed artifacts.
    """
    return _load_evaluation_artifacts_from_directory(directory)


def _is_valid_evaluation_run_directory(path: Path) -> bool:
    try:
        _load_evaluation_artifacts_from_directory(path)
    except EvaluationStoreError:
        return False
    return True


def publish_evaluation_artifacts(
    *,
    root: Path,
    run_id: str,
    manifest: RunManifest,
    indexing: IndexingMeasurement,
    query_usage: RetrievalQueryUsage,
    retrieval_results: Sequence[RetrievalCaseResult],
    answer_results: Sequence[AnswerCaseResult],
    summary: EvaluationSummary,
    report_markdown: str,
    force: bool = False,
) -> Path:
    """Atomically publish one complete evaluation run directory beneath
    the caller-supplied `root`.

    Summary/report derivation and semantic validation are performed by the
    application workflow. This adapter verifies the supplied typed artifacts,
    writes them atomically, and reloads the staged directory before publishing.

    Before anything is written, rejects (as `ValueError`, since nothing
    has touched the filesystem yet): a `manifest.run_id` not equal to
    `run_id`; an unsupported `manifest.run_manifest_schema_version` or
    `question_set.question_set_schema_version`; a `manifest.repository` or
    `manifest.resolved_commit_sha` that disagrees with `indexing`; a
    `manifest.question_set_version` that disagrees with `question_set`;
    and any retrieval/answer result referencing a case_id absent from
    `question_set`, or a repeated `(case_id, retriever)`/`case_id` pair
    (raised by `build_evaluation_summary` itself).

    Writes every one of the seven artifacts (`run-manifest.json`,
    `indexing.json`, `query-usage.json`, `retrieval-results.jsonl`,
    `answer-results.jsonl`, `summary.json`, `report.md`) to a staging
    directory created under `root`, flushes and fsyncs each file, then
    validates the staged
    directory by loading it back through `load_evaluation_run` before
    publishing anything -- the same protocol
    `reporationale.adapters.snapshot_store.publish_snapshot` uses for a
    normalized-source snapshot. Raises `EvaluationRunAlreadyExists` if a
    completed run already exists at the target and `force` is not `True`.

    On any failure (write, validation, or publication), the staging
    directory this call created is removed and nothing about the target
    changes: a failed or interrupted write never appears as a completed
    run, and a failed forced replacement leaves the previous completed
    run intact.
    """
    if manifest.run_id != run_id:
        raise ValueError("manifest.run_id must equal run_id")
    if manifest.run_manifest_schema_version != RUN_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            "manifest.run_manifest_schema_version "
            f"({manifest.run_manifest_schema_version}) is not supported by "
            f"this build (expected {RUN_MANIFEST_SCHEMA_VERSION})"
        )
    mismatch = _first_cross_artifact_mismatch(
        manifest=manifest, indexing=indexing, summary=summary
    )
    if mismatch is not None:
        raise ValueError(f"Cannot publish an inconsistent evaluation run: {mismatch}.")

    target = evaluation_run_directory(root=root, run_id=run_id)
    recover_interrupted_replacement(
        target,
        is_valid=_is_valid_evaluation_run_directory,
    )
    if target.exists() and not force:
        raise EvaluationRunAlreadyExists(
            f"A completed evaluation run already exists at {target}; pass "
            "force=True to replace it explicitly."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".staging-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        write_file_durably(
            staging / _RUN_MANIFEST_FILENAME,
            manifest.model_dump_json().encode("utf-8"),
        )
        write_file_durably(
            staging / _INDEXING_FILENAME,
            indexing.model_dump_json().encode("utf-8"),
        )
        write_file_durably(
            staging / _QUERY_USAGE_FILENAME,
            query_usage.model_dump_json().encode("utf-8"),
        )
        write_file_durably(
            staging / _RETRIEVAL_RESULTS_FILENAME,
            _serialize_retrieval_results_jsonl(retrieval_results),
        )
        write_file_durably(
            staging / _ANSWER_RESULTS_FILENAME,
            _serialize_answer_results_jsonl(answer_results),
        )
        write_file_durably(
            staging / _SUMMARY_FILENAME,
            summary.model_dump_json().encode("utf-8"),
        )
        write_file_durably(
            staging / _REPORT_FILENAME,
            report_markdown.encode("utf-8"),
        )

        # Validate the staged directory exactly the way a later load
        # would, before anything is published.
        loaded = _load_evaluation_artifacts_from_directory(staging)
        expected = LoadedEvaluationRun(
            manifest=manifest,
            indexing=indexing,
            query_usage=query_usage,
            retrieval_results=tuple(retrieval_results),
            answer_results=tuple(answer_results),
            summary=summary,
            report_markdown=report_markdown,
        )
        if loaded != expected:
            raise EvaluationRunCorrupted(
                "The staged evaluation artifacts did not reload exactly as written."
            )

        publish_staged_directory(staging, target)
    except BaseException:
        remove_directory(staging)
        raise
    return target
