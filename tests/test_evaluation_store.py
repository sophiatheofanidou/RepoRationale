"""Tests for the evaluation-harness persistence adapter: question-set
loading, schema-version enforcement, deterministic serialization, atomic
run publication, cross-artifact consistency, crash recovery, and cleanup
on failure.

Every test writes under `tmp_path`; no generated evaluation run is ever
written into the repository.
"""

import json
from datetime import UTC, date, datetime
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
from reporationale.domain.answering import (
    AnsweredOutcome,
    Citation,
    InsufficientEvidenceOutcome,
    RunTrace,
)
from reporationale.domain.evaluation import (
    AnswerCaseResult,
    EvaluationCase,
    EvaluationQuestionSet,
    ExpectedSource,
    IndexingMeasurement,
    MeasurementLimitation,
    PricingBasis,
    RetrievalCaseResult,
    RetrievalQueryUsage,
    RunManifest,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="google", name="gson")
_COMMIT_SHA = "a" * 40


def _question_set(
    question_set_version: int = 1, *, include_held_out_case: bool = False
) -> EvaluationQuestionSet:
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
    cases: tuple[EvaluationCase, ...] = (case,)
    if include_held_out_case:
        held_out_case = EvaluationCase(
            case_id="case-2",
            split="held_out",
            question="Why was Y changed?",
            category="unanswerable_control",
            expected_outcome="insufficient_evidence",
            review_note="No matching rationale found after a manual search.",
        )
        cases = (case, held_out_case)
    return EvaluationQuestionSet(
        question_set_schema_version=1,
        question_set_version=question_set_version,
        cases=cases,
    )


def _manifest(
    run_id: str = "gson-2026-09-10",
    *,
    resolved_commit_sha: str = _COMMIT_SHA,
    question_set_version: int = 1,
    split: str = "development",
    pricing_bases: tuple[PricingBasis, ...] = (),
) -> RunManifest:
    return RunManifest(
        run_manifest_schema_version=2,
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
        split=split,
        pricing_bases=pricing_bases,
    )


def _indexing(*, resolved_commit_sha: str = _COMMIT_SHA) -> IndexingMeasurement:
    return IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=resolved_commit_sha,
        github_request_count=750,
        chunk_count=10,
    )


def _query_usage(**overrides: object) -> RetrievalQueryUsage:
    fields: dict[str, object] = {
        "query_embedding_request_count": 0,
        "query_embedding_token_count": 0,
        "estimated_query_embedding_cost_usd": None,
        "includes_refinement_diagnostic": False,
    }
    fields.update(overrides)
    return RetrievalQueryUsage(**fields)


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
        "query_usage": _query_usage(),
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
        "query-usage.json",
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
    assert loaded.query_usage == _query_usage()
    assert loaded.retrieval_results == _retrieval_results()
    assert loaded.answer_results == _answer_results()

    # The persisted summary/report are never independent inputs: they must
    # equal exactly what the pure application functions compute from the
    # same raw records and question set.
    expected_summary = build_evaluation_summary(
        run_id="gson-2026-09-10",
        question_set=question_set,
        split="development",
        retrieval_results=_retrieval_results(),
        answer_results=_answer_results(),
    )
    assert loaded.summary == expected_summary
    expected_report = render_markdown_report(
        manifest=_manifest(),
        indexing=_indexing(),
        query_usage=_query_usage(),
        summary=expected_summary,
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


# --- retrieval-query usage and measurement limitations ------------------------


def test_publish_and_load_round_trip_persists_query_usage(tmp_path: Path) -> None:
    """Query-embedding usage is its own persisted raw artifact and must
    reload exactly as published, with the report rendering it from that
    raw source rather than from any other total."""
    query_usage = _query_usage(
        query_embedding_request_count=4,
        query_embedding_token_count=49,
        estimated_query_embedding_cost_usd=0.00000294,
        includes_refinement_diagnostic=True,
    )
    target = _publish(tmp_path, query_usage=query_usage)

    assert (target / "query-usage.json").is_file()
    loaded = load_evaluation_run(target, question_set=_question_set())
    assert loaded.query_usage == query_usage
    assert "Query embedding requests: 4" in loaded.report_markdown
    assert "Query embedding tokens: 49" in loaded.report_markdown
    assert "Query usage includes the refinement diagnostic: yes" in loaded.report_markdown


def test_publish_and_load_round_trip_persists_measurement_limitations(
    tmp_path: Path,
) -> None:
    limitation_text = (
        "Blank-message commits were excluded as non-rationale content "
        "during source ingestion, but their exact count and identities "
        "cannot be reconstructed from the completed normalized snapshot."
    )
    indexing = _indexing().model_copy(
        update={
            "measurement_limitations": (
                MeasurementLimitation(description=limitation_text),
            )
        }
    )
    target = _publish(tmp_path, indexing=indexing)

    loaded = load_evaluation_run(target, question_set=_question_set())
    assert len(loaded.indexing.measurement_limitations) == 1
    assert loaded.indexing.measurement_limitations[0].description == limitation_text
    assert limitation_text in loaded.report_markdown
    assert "Skipped items:" not in loaded.report_markdown


def test_publish_rejects_query_usage_below_vector_retrieval_count(
    tmp_path: Path,
) -> None:
    """A `RetrievalQueryUsage` record that understates the query calls its
    own run's vector-retrieval results required must be rejected before
    publication -- usage totals are cross-validated wherever applicable,
    never accepted as an independent, unsupported summary."""
    vector_results = (
        RetrievalCaseResult(
            case_id="case-1",
            retriever="voyage_chroma",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.4,
        ),
    )
    understated_usage = _query_usage(query_embedding_request_count=0)
    with pytest.raises(ValueError, match="less than the minimum implied"):
        _publish(
            tmp_path,
            retrieval_results=vector_results,
            query_usage=understated_usage,
        )


def test_publish_accepts_query_usage_covering_vector_retrieval_and_refinement(
    tmp_path: Path,
) -> None:
    vector_results = (
        RetrievalCaseResult(
            case_id="case-1",
            retriever="voyage_chroma",
            retrieved_source_ids=("github:google/gson:issue:1",),
            latency_seconds=0.4,
        ),
    )
    usage = _query_usage(
        query_embedding_request_count=2,
        query_embedding_token_count=20,
        includes_refinement_diagnostic=True,
    )
    target = _publish(tmp_path, retrieval_results=vector_results, query_usage=usage)
    loaded = load_evaluation_run(target, question_set=_question_set())
    assert loaded.query_usage.query_embedding_request_count == 2


def test_load_evaluation_run_rejects_query_usage_tampered_after_publish(
    tmp_path: Path,
) -> None:
    """Tampering with the persisted `query-usage.json` after publication
    must be caught on reload -- the recomputed report reflects the
    tampered usage and therefore no longer matches the persisted report."""
    target = _publish(tmp_path)
    _rewrite_json_file(
        target / "query-usage.json",
        lambda data: data.__setitem__("query_embedding_request_count", 99),
    )
    with pytest.raises(EvaluationRunCorrupted):
        load_evaluation_run(target, question_set=_question_set())


def test_publish_and_load_round_trip_persists_pricing_basis(tmp_path: Path) -> None:
    """A recorded price basis is its own field on the manifest and must
    reload exactly as published, with the report rendering it."""
    pricing_bases = (
        PricingBasis(
            provider="anthropic",
            model="claude-opus-5",
            input_price_per_million_usd=5.0,
            output_price_per_million_usd=25.0,
            verified_on=date(2026, 9, 7),
        ),
    )
    target = _publish(tmp_path, manifest=_manifest(pricing_bases=pricing_bases))

    loaded = load_evaluation_run(target, question_set=_question_set())
    assert loaded.manifest.pricing_bases == pricing_bases
    assert "## Pricing basis" in loaded.report_markdown
    assert "claude-opus-5" in loaded.report_markdown


def test_load_evaluation_run_rejects_pricing_basis_tampered_after_publish(
    tmp_path: Path,
) -> None:
    """Tampering with the persisted manifest's `pricing_bases` after
    publication must be caught on reload: the recomputed report reflects
    the tampered price basis and therefore no longer matches the
    persisted report."""
    pricing_bases = (
        PricingBasis(
            provider="anthropic",
            model="claude-opus-5",
            input_price_per_million_usd=5.0,
            output_price_per_million_usd=25.0,
            verified_on=date(2026, 9, 7),
        ),
    )
    target = _publish(tmp_path, manifest=_manifest(pricing_bases=pricing_bases))
    _rewrite_json_file(
        target / "run-manifest.json",
        lambda data: data["pricing_bases"][0].__setitem__(
            "output_price_per_million_usd", 999.0
        ),
    )
    with pytest.raises(EvaluationRunCorrupted):
        load_evaluation_run(target, question_set=_question_set())


# --- split-aware publication and loading -------------------------------------


def test_publish_and_load_evaluation_run_for_held_out_split(tmp_path: Path) -> None:
    """A held-out-split run publishes and reloads with its own manifest
    split and its own (not the whole question set's) case count."""
    question_set = _question_set(include_held_out_case=True)
    manifest = _manifest(split="held_out")
    retrieval_results: tuple[RetrievalCaseResult, ...] = ()
    answer_results = (
        AnswerCaseResult(
            case_id="case-2",
            outcome=InsufficientEvidenceOutcome(explanation="No documented rationale."),
            trace=_trace(),
        ),
    )

    target = publish_evaluation_run(
        root=tmp_path,
        run_id=manifest.run_id,
        manifest=manifest,
        indexing=_indexing(),
        query_usage=_query_usage(),
        question_set=question_set,
        retrieval_results=retrieval_results,
        answer_results=answer_results,
    )

    loaded = load_evaluation_run(target, question_set=question_set)
    assert loaded.manifest.split == "held_out"
    assert loaded.summary.split == "held_out"
    assert loaded.summary.case_count == 1
    assert "**Split:** held_out" in loaded.report_markdown


def test_publish_rejects_retrieval_result_from_other_split(tmp_path: Path) -> None:
    """A retrieval result for a case belonging to the other split must be
    rejected before publication, not silently accepted into a
    development-scoped run."""
    question_set = _question_set(include_held_out_case=True)
    cross_split_result = (
        RetrievalCaseResult(
            case_id="case-2",
            retriever="bm25",
            retrieved_source_ids=(),
            latency_seconds=0.0,
        ),
    )
    with pytest.raises(ValueError, match="belongs to split 'held_out'"):
        _publish(
            tmp_path,
            question_set=question_set,
            retrieval_results=_retrieval_results() + cross_split_result,
        )


def test_publish_rejects_answer_result_from_other_split(tmp_path: Path) -> None:
    question_set = _question_set(include_held_out_case=True)
    cross_split_answer = (
        AnswerCaseResult(
            case_id="case-2",
            outcome=InsufficientEvidenceOutcome(explanation="No documented rationale."),
            trace=_trace(),
        ),
    )
    with pytest.raises(ValueError, match="belongs to split 'held_out'"):
        _publish(
            tmp_path,
            question_set=question_set,
            answer_results=_answer_results() + cross_split_answer,
        )


def test_publish_rejects_incomplete_retriever_coverage_of_the_selected_split(
    tmp_path: Path,
) -> None:
    """A retrieval-only run must supply exactly one result per retriever
    for every answerable case in the selected split; a still-partial
    development run must never be publishable as complete."""
    question_set = EvaluationQuestionSet(
        question_set_schema_version=1,
        question_set_version=1,
        cases=(
            EvaluationCase(
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
            ),
            EvaluationCase(
                case_id="case-3",
                split="development",
                question="Why was Z changed?",
                category="direct_retrieval",
                expected_outcome="answered",
                expected_sources=(
                    ExpectedSource(
                        source_id="github:google/gson:issue:3",
                        source_url="https://github.com/google/gson/issues/3",
                    ),
                ),
                review_note="Confirmed against the linked thread.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="does not have exactly one result"):
        _publish(
            tmp_path,
            question_set=question_set,
            retrieval_results=_retrieval_results(),
            answer_results=(),
        )


def test_publish_rejects_incomplete_answer_coverage_of_the_selected_split(
    tmp_path: Path,
) -> None:
    """When answer results are present at all, they must cover every case
    in the selected split, including insufficient-evidence controls."""
    question_set = _question_set(include_held_out_case=False)
    question_set = question_set.model_copy(
        update={
            "cases": (
                *question_set.cases,
                EvaluationCase(
                    case_id="case-3",
                    split="development",
                    question="Was this ever discussed?",
                    category="unanswerable_control",
                    expected_outcome="insufficient_evidence",
                    review_note="No matching decision found.",
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="do not cover every case in split"):
        _publish(
            tmp_path,
            question_set=question_set,
            answer_results=_answer_results(),
        )


def test_publish_rejects_manifest_schema_version_1(tmp_path: Path) -> None:
    """`RUN_MANIFEST_SCHEMA_VERSION` was bumped to 2 for the required
    `split` field; the old value of 1 must be rejected outright rather
    than accepted as if `split` were optional."""
    bad_manifest = _manifest().model_copy(update={"run_manifest_schema_version": 1})
    with pytest.raises(ValueError, match="run_manifest_schema_version"):
        _publish(tmp_path, manifest=bad_manifest)


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
        ("missing_query_usage", EvaluationRunNotFound),
        ("bad_json_query_usage", EvaluationRunCorrupted),
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
        ("manifest_split_mismatch_with_summary", EvaluationRunCorrupted),
        ("legacy_v1_manifest_missing_split", EvaluationRunCorrupted),
    ],
)
def test_load_evaluation_run_rejects_corrupt_variants(
    corrupt: str, expected_error: type[Exception], tmp_path: Path
) -> None:
    target = _publish(tmp_path)

    if corrupt == "missing_manifest":
        (target / "run-manifest.json").unlink()
    elif corrupt == "missing_query_usage":
        (target / "query-usage.json").unlink()
    elif corrupt == "bad_json_query_usage":
        (target / "query-usage.json").write_text("not json", encoding="utf-8")
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
    elif corrupt == "manifest_split_mismatch_with_summary":
        _rewrite_json_file(
            target / "run-manifest.json",
            lambda data: data.__setitem__("split", "held_out"),
        )
    elif corrupt == "legacy_v1_manifest_missing_split":

        def _strip_split(data: dict[str, object]) -> None:
            data.pop("split", None)
            data["run_manifest_schema_version"] = 1

        _rewrite_json_file(target / "run-manifest.json", _strip_split)
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
