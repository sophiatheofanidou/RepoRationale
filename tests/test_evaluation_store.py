"""Tests for the evaluation-harness persistence adapter: question-set
loading, schema-version enforcement, deterministic serialization, atomic
run publication, cross-artifact consistency, crash recovery, and cleanup
on failure.

Every test writes under `tmp_path`; no generated evaluation run is ever
written into the repository.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import reporationale.adapters.evaluation_store as evaluation_store_module
from reporationale.adapters.evaluation_store import (
    EvaluationRunAlreadyExists,
    EvaluationRunCorrupted,
    EvaluationRunIncompatible,
    EvaluationRunNotFound,
    QuestionSetIncompatible,
    QuestionSetInvalid,
    QuestionSetNotFound,
    evaluation_run_directory,
    load_question_set,
)
from reporationale.adapters.snapshot_store import write_file_durably
from reporationale.application.evaluation_artifacts import (
    load_evaluation_run,
    publish_evaluation_run,
)
from reporationale.application.evaluation_metrics import build_evaluation_summary
from reporationale.application.evaluation_report import render_markdown_report
from reporationale.domain.answering import AnsweredOutcome, Citation, RunTrace
from reporationale.domain.evaluation import (
    AnswerCaseResult,
    EvaluationCase,
    EvaluationQuestionSet,
    ExpectedSource,
    IndexingMeasurement,
    RetrievalCaseResult,
    RunManifest,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="google", name="gson")
_COMMIT_SHA = "a" * 40


def _question_set(question_set_version: int = 1) -> EvaluationQuestionSet:
    case = EvaluationCase(
        case_id="case-1",
        split="development",
        question="Why was X changed?",
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
    return EvaluationQuestionSet(
        question_set_schema_version=1,
        question_set_version=question_set_version,
        cases=(case,),
    )


def _manifest(
    run_id: str = "gson-2026-09-10",
    *,
    resolved_commit_sha: str = _COMMIT_SHA,
    question_set_version: int = 1,
) -> RunManifest:
    return RunManifest(
        run_manifest_schema_version=1,
        run_id=run_id,
        created_at=datetime(2026, 9, 10, tzinfo=UTC),
        command="uv run python -m reporationale.evaluation " + run_id,
        repository=_IDENTITY,
        resolved_commit_sha=resolved_commit_sha,
        source_schema_version=1,
        chunk_schema_version=1,
        chunker_algorithm_version=1,
        max_chars=2000,
        retrieval_result_limit=5,
        question_set_version=question_set_version,
    )


def _indexing(*, resolved_commit_sha: str = _COMMIT_SHA) -> IndexingMeasurement:
    return IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=resolved_commit_sha,
        github_request_count=750,
        chunk_count=10,
    )


def _retrieval_results() -> tuple[RetrievalCaseResult, ...]:
    return (
        RetrievalCaseResult(
            case_id="case-1",
            retriever="bm25",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.01,
        ),
    )


def _answer_outcome() -> AnsweredOutcome:
    citation = Citation(
        number=1,
        evidence_id="github:google/gson:issue:1:chunk:0",
        source_id="github:google/gson:issue:1",
        source_title="Some issue",
        source_url="https://github.com/google/gson/issues/1",
        excerpt="because of X",
    )
    return AnsweredOutcome(answer="Because of X [1].", citations=(citation,))


def _trace() -> RunTrace:
    return RunTrace(
        model="claude-opus-5",
        agent_version="test-agent/1",
        searches=(),
        model_call_latencies_seconds=(0.2,),
        total_search_count=0,
        total_latency_seconds=0.3,
        input_tokens=100,
        output_tokens=50,
    )


def _answer_results(case_id: str = "case-1") -> tuple[AnswerCaseResult, ...]:
    return (
        AnswerCaseResult(case_id=case_id, outcome=_answer_outcome(), trace=_trace()),
    )


def _publish(root: Path, run_id: str = "gson-2026-09-10", **overrides: object) -> Path:
    kwargs: dict[str, object] = {
        "root": root,
        "run_id": run_id,
        "manifest": _manifest(run_id),
        "indexing": _indexing(),
        "question_set": _question_set(),
        "retrieval_results": _retrieval_results(),
        "answer_results": _answer_results(),
    }
    kwargs.update(overrides)
    return publish_evaluation_run(**kwargs)  # type: ignore[arg-type]


def _rewrite_json_file(path: Path, mutate: object) -> None:
    """Load `path` as JSON, apply `mutate` (a callable) to the parsed
    object, and write the result back. Used to simulate a persisted
    artifact drifting from what publication actually wrote, independent
    of the store's own write path."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)  # type: ignore[operator]
    path.write_text(json.dumps(data), encoding="utf-8")


# --- load_question_set -------------------------------------------------------


def test_load_question_set_round_trips(tmp_path: Path) -> None:
    question_set = _question_set()
    path = tmp_path / "question-set.json"
    path.write_text(question_set.model_dump_json(), encoding="utf-8")

    loaded = load_question_set(path)
    assert loaded == question_set


def test_load_question_set_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(QuestionSetNotFound):
        load_question_set(tmp_path / "does-not-exist.json")


def test_load_question_set_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "question-set.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(QuestionSetInvalid):
        load_question_set(path)


def test_load_question_set_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    path = tmp_path / "question-set.json"
    path.write_text(
        '{"question_set_schema_version": 1, "question_set_version": 1, "cases": ['
        '{"case_id": "dup", "split": "development", "question": "q1", '
        '"category": "c", "expected_outcome": "insufficient_evidence", '
        '"review_note": "n1"},'
        '{"case_id": "dup", "split": "development", "question": "q2", '
        '"category": "c", "expected_outcome": "insufficient_evidence", '
        '"review_note": "n2"}'
        "]}",
        encoding="utf-8",
    )
    with pytest.raises(QuestionSetInvalid):
        load_question_set(path)


def test_load_question_set_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "question-set.json"
    path.write_text(
        json.dumps(
            {
                "question_set_schema_version": 999,
                "question_set_version": 1,
                "cases": [
                    {
                        "case_id": "case-1",
                        "split": "development",
                        "question": "q",
                        "category": "c",
                        "expected_outcome": "insufficient_evidence",
                        "review_note": "n",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(QuestionSetIncompatible):
        load_question_set(path)


# --- evaluation_run_directory -------------------------------------------------


@pytest.mark.parametrize("run_id", ["..", ".", "a/b", "a b"])
def test_evaluation_run_directory_rejects_unsafe_run_id(
    run_id: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        evaluation_run_directory(root=tmp_path, run_id=run_id)


# --- publish/load round trip and determinism ---------------------------------


def test_publish_and_load_evaluation_run_round_trip(tmp_path: Path) -> None:
    target = _publish(tmp_path)

    assert target == evaluation_run_directory(root=tmp_path, run_id="gson-2026-09-10")
    for filename in (
        "run-manifest.json",
        "indexing.json",
        "retrieval-results.jsonl",
        "answer-results.jsonl",
        "summary.json",
        "report.md",
    ):
        assert (target / filename).is_file()

    question_set = _question_set()
    loaded = load_evaluation_run(target, question_set=question_set)
    assert loaded.manifest == _manifest()
    assert loaded.indexing == _indexing()
    assert loaded.retrieval_results == _retrieval_results()
    assert loaded.answer_results == _answer_results()

    # The persisted summary/report are never independent inputs: they must
    # equal exactly what the pure application functions compute from the
    # same raw records and question set.
    expected_summary = build_evaluation_summary(
        run_id="gson-2026-09-10",
        question_set=question_set,
        retrieval_results=_retrieval_results(),
        answer_results=_answer_results(),
    )
    assert loaded.summary == expected_summary
    expected_report = render_markdown_report(
        manifest=_manifest(), indexing=_indexing(), summary=expected_summary
    )
    assert loaded.report_markdown == expected_report


def test_publish_is_deterministic_byte_for_byte(tmp_path: Path) -> None:
    _publish(tmp_path, run_id="run-a")
    _publish(tmp_path, run_id="run-b")

    directory_a = evaluation_run_directory(root=tmp_path, run_id="run-a")
    directory_b = evaluation_run_directory(root=tmp_path, run_id="run-b")

    for filename in (
        "indexing.json",
        "retrieval-results.jsonl",
        "answer-results.jsonl",
    ):
        assert (directory_a / filename).read_bytes() == (
            directory_b / filename
        ).read_bytes()


def test_load_evaluation_run_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(EvaluationRunNotFound):
        load_evaluation_run(tmp_path / "does-not-exist", question_set=_question_set())


def test_load_evaluation_run_rejects_question_set_missing_referenced_case(
    tmp_path: Path,
) -> None:
    """Loading a validly published run against a *different* question set
    that does not contain the case_id its results reference must be
    rejected rather than silently accepted."""
    target = _publish(tmp_path)
    other_question_set = EvaluationQuestionSet(
        question_set_schema_version=1,
        question_set_version=1,
        cases=(
            EvaluationCase(
                case_id="unrelated-case",
                split="held_out",
                question="Some other question?",
                category="unanswerable_control",
                expected_outcome="insufficient_evidence",
                review_note="Unrelated to case-1.",
            ),
        ),
    )
    with pytest.raises(EvaluationRunCorrupted, match="do not match"):
        load_evaluation_run(target, question_set=other_question_set)


# --- already-exists / forced replacement -------------------------------------


def test_publish_without_force_over_existing_run_raises(tmp_path: Path) -> None:
    _publish(tmp_path)
    with pytest.raises(EvaluationRunAlreadyExists):
        _publish(tmp_path)


def test_forced_replacement_succeeds_and_leaves_no_backup(tmp_path: Path) -> None:
    _publish(tmp_path)
    target = _publish(
        tmp_path,
        indexing=_indexing(),
        force=True,
    )
    loaded = load_evaluation_run(target, question_set=_question_set())
    assert loaded.answer_results == _answer_results()
    assert not (target.parent / f"{target.name}.backup").exists()


# --- publish-time input rejection (before anything is written) --------------


def test_publish_rejects_manifest_run_id_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _publish(tmp_path, manifest=_manifest("different-run-id"))


def test_publish_rejects_unsupported_manifest_schema_version(tmp_path: Path) -> None:
    bad_manifest = _manifest().model_copy(update={"run_manifest_schema_version": 999})
    with pytest.raises(ValueError, match="run_manifest_schema_version"):
        _publish(tmp_path, manifest=bad_manifest)


def test_publish_rejects_unsupported_question_set_schema_version(
    tmp_path: Path,
) -> None:
    bad_question_set = _question_set().model_copy(
        update={"question_set_schema_version": 999}
    )
    with pytest.raises(ValueError, match="question_set_schema_version"):
        _publish(tmp_path, question_set=bad_question_set)


def test_publish_rejects_manifest_repository_mismatch_with_indexing(
    tmp_path: Path,
) -> None:
    other_identity = RepositoryIdentity(platform="github", owner="other", name="repo")
    mismatched_indexing = _indexing().model_copy(update={"repository": other_identity})
    with pytest.raises(ValueError, match="repository"):
        _publish(tmp_path, indexing=mismatched_indexing)


def test_publish_rejects_manifest_commit_mismatch_with_indexing(tmp_path: Path) -> None:
    mismatched_indexing = _indexing(resolved_commit_sha="b" * 40)
    with pytest.raises(ValueError, match="commit"):
        _publish(tmp_path, indexing=mismatched_indexing)


def test_publish_rejects_manifest_question_set_version_mismatch(tmp_path: Path) -> None:
    mismatched_manifest = _manifest(question_set_version=2)
    with pytest.raises(ValueError, match="question_set_version"):
        _publish(tmp_path, manifest=mismatched_manifest)


# --- atomic publication and cleanup on failure -------------------------------


def test_failed_publish_cleans_up_staging_and_never_publishes_partial_run(
    tmp_path: Path,
) -> None:
    """Two answer results for the same case_id fail summary computation
    (a case can be answered at most once per run) before anything is
    written: `build_evaluation_summary` itself raises `ValueError`, so
    publication must fail and leave no trace of the attempt."""
    duplicate_answers = _answer_results() + _answer_results()

    with pytest.raises(ValueError, match="duplicate answer result"):
        _publish(tmp_path, answer_results=duplicate_answers)

    target = evaluation_run_directory(root=tmp_path, run_id="gson-2026-09-10")
    assert not target.exists()
    assert not any(entry.name.startswith(".staging-") for entry in tmp_path.iterdir())


def test_publish_cleans_up_staging_directory_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through writing the staged artifacts (for
    example a transient I/O error) must remove the staging directory and
    leave no completed or partial run behind -- the atomic-publication
    guarantee independent of any input-validation failure."""
    call_count = {"n": 0}

    def _flaky_write(path: Path, payload: bytes) -> None:
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise OSError("simulated write failure")
        write_file_durably(path, payload)

    monkeypatch.setattr(evaluation_store_module, "write_file_durably", _flaky_write)

    with pytest.raises(OSError, match="simulated write failure"):
        _publish(tmp_path)

    target = evaluation_run_directory(root=tmp_path, run_id="gson-2026-09-10")
    assert not target.exists()
    assert not any(entry.name.startswith(".staging-") for entry in tmp_path.iterdir())


def test_failed_forced_replacement_preserves_previous_run(tmp_path: Path) -> None:
    _publish(tmp_path)
    duplicate_answers = _answer_results() + _answer_results()

    with pytest.raises(ValueError, match="duplicate answer result"):
        _publish(tmp_path, answer_results=duplicate_answers, force=True)

    target = evaluation_run_directory(root=tmp_path, run_id="gson-2026-09-10")
    loaded = load_evaluation_run(target, question_set=_question_set())
    assert loaded.answer_results == _answer_results()
    assert not any(
        entry.name.startswith(".staging-") or entry.name.endswith(".backup")
        for entry in tmp_path.iterdir()
    )


# --- corrupted / inconsistent run directory variants -------------------------


@pytest.mark.parametrize(
    ("corrupt", "expected_error"),
    [
        ("missing_manifest", EvaluationRunNotFound),
        ("bad_json_manifest", EvaluationRunCorrupted),
        ("unsupported_manifest_schema_version", EvaluationRunIncompatible),
        ("blank_line_in_retrieval", EvaluationRunCorrupted),
        ("blank_report", EvaluationRunCorrupted),
        ("manifest_repository_mismatch", EvaluationRunCorrupted),
        ("manifest_commit_mismatch", EvaluationRunCorrupted),
        ("manifest_question_set_version_mismatch", EvaluationRunCorrupted),
        ("summary_run_id_mismatch", EvaluationRunCorrupted),
        ("summary_tampered", EvaluationRunCorrupted),
        ("report_tampered", EvaluationRunCorrupted),
    ],
)
def test_load_evaluation_run_rejects_corrupt_variants(
    corrupt: str, expected_error: type[Exception], tmp_path: Path
) -> None:
    target = _publish(tmp_path)

    if corrupt == "missing_manifest":
        (target / "run-manifest.json").unlink()
    elif corrupt == "bad_json_manifest":
        (target / "run-manifest.json").write_text("not json", encoding="utf-8")
    elif corrupt == "unsupported_manifest_schema_version":
        _rewrite_json_file(
            target / "run-manifest.json",
            lambda data: data.__setitem__("run_manifest_schema_version", 999),
        )
    elif corrupt == "blank_line_in_retrieval":
        path = target / "retrieval-results.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
    elif corrupt == "blank_report":
        (target / "report.md").write_text("   ", encoding="utf-8")
    elif corrupt == "manifest_repository_mismatch":
        _rewrite_json_file(
            target / "run-manifest.json",
            lambda data: data["repository"].__setitem__("name", "different-repo"),
        )
    elif corrupt == "manifest_commit_mismatch":
        _rewrite_json_file(
            target / "run-manifest.json",
            lambda data: data.__setitem__("resolved_commit_sha", "b" * 40),
        )
    elif corrupt == "manifest_question_set_version_mismatch":
        _rewrite_json_file(
            target / "run-manifest.json",
            lambda data: data.__setitem__("question_set_version", 2),
        )
    elif corrupt == "summary_run_id_mismatch":
        _rewrite_json_file(
            target / "summary.json",
            lambda data: data.__setitem__("run_id", "different-run-id"),
        )
    elif corrupt == "summary_tampered":
        _rewrite_json_file(
            target / "summary.json", lambda data: data.__setitem__("case_count", 2)
        )
    elif corrupt == "report_tampered":
        path = target / "report.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\nTAMPERED\n", encoding="utf-8"
        )

    with pytest.raises(expected_error):
        load_evaluation_run(target, question_set=_question_set())


def test_answer_case_result_validation_error_is_not_silently_swallowed(
    tmp_path: Path,
) -> None:
    """A record that merely fails pydantic validation (not this module's
    own duplicate-detection) still surfaces as an error rather than
    disappearing."""
    with pytest.raises(ValidationError):
        AnswerCaseResult.model_validate({"case_id": "", "outcome": {}, "trace": {}})
