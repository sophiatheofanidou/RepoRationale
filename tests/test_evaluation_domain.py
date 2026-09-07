"""Tests for the offline evaluation-harness domain contracts: question-set
validation, ground-truth invariants, and measurement/result record shapes.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from reporationale.domain.answering import AnsweredOutcome, Citation
from reporationale.domain.evaluation import (
    AnswerCaseResult,
    EvaluationCase,
    EvaluationQuestionSet,
    ExpectedSource,
    IndexingMeasurement,
    MeasurementLimitation,
    PhaseTiming,
    RetrievalCaseResult,
    RetrievalQueryUsage,
    RunManifest,
)
from reporationale.domain.repository_identity import RepositoryIdentity

_IDENTITY = RepositoryIdentity(platform="github", owner="google", name="gson")
_COMMIT_SHA = "a" * 40


def _expected_source(number: int = 1) -> ExpectedSource:
    return ExpectedSource(
        source_id=f"github:google/gson:issue:{number}",
        source_url=f"https://github.com/google/gson/issues/{number}",
    )


def _answered_case(case_id: str = "case-1", **overrides: object) -> EvaluationCase:
    fields: dict[str, object] = {
        "case_id": case_id,
        "split": "development",
        "question": "Why was X changed?",
        "category": "direct_retrieval",
        "expected_outcome": "answered",
        "expected_sources": (_expected_source(),),
        "review_note": "Confirmed against the linked issue thread.",
    }
    fields.update(overrides)
    return EvaluationCase(**fields)


def _insufficient_case(case_id: str = "case-2", **overrides: object) -> EvaluationCase:
    fields: dict[str, object] = {
        "case_id": case_id,
        "split": "held_out",
        "question": "Why was Y changed?",
        "category": "unanswerable_control",
        "expected_outcome": "insufficient_evidence",
        "review_note": "No matching rationale found in the reviewed snapshot.",
    }
    fields.update(overrides)
    return EvaluationCase(**fields)


# --- EvaluationCase / EvaluationQuestionSet -------------------------------


def test_valid_question_set_round_trips_through_json() -> None:
    question_set = EvaluationQuestionSet(
        question_set_schema_version=1,
        question_set_version=1,
        cases=(_answered_case(), _insufficient_case()),
    )
    reloaded = EvaluationQuestionSet.model_validate_json(question_set.model_dump_json())
    assert reloaded == question_set


def test_answered_case_without_expected_sources_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _answered_case(expected_sources=())


def test_insufficient_evidence_case_with_expected_sources_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _insufficient_case(expected_sources=(_expected_source(),))


@pytest.mark.parametrize("field", ["case_id", "question", "category", "review_note"])
def test_blank_case_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        _answered_case(**{field: "   "})


def test_duplicate_expected_source_ids_are_rejected() -> None:
    duplicate = _expected_source(1)
    with pytest.raises(ValidationError):
        _answered_case(expected_sources=(duplicate, duplicate))


def test_duplicate_case_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EvaluationQuestionSet(
            question_set_schema_version=1,
            question_set_version=1,
            cases=(_answered_case("dup"), _insufficient_case("dup")),
        )


def test_question_set_requires_at_least_one_case() -> None:
    with pytest.raises(ValidationError):
        EvaluationQuestionSet(
            question_set_schema_version=1, question_set_version=1, cases=()
        )


def test_expected_source_rejects_invalid_source_id_pattern() -> None:
    with pytest.raises(ValidationError):
        ExpectedSource(
            source_id="not-namespaced",
            source_url="https://github.com/google/gson/issues/1",
        )


def test_question_set_rejects_unknown_top_level_field() -> None:
    with pytest.raises(ValidationError):
        EvaluationQuestionSet.model_validate(
            {
                "question_set_schema_version": 1,
                "question_set_version": 1,
                "cases": [],
                "unexpected": True,
            }
        )


# --- IndexingMeasurement ---------------------------------------------------


def test_indexing_measurement_round_trips() -> None:
    measurement = IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        phase_timings=(PhaseTiming(phase="collection", seconds=12.5),),
        github_request_count=400,
        source_count_by_type={"issue": 10, "pull_request": 5},
        chunk_count=42,
        embedding_request_count=42,
        embedding_token_count=1000,
        estimated_embedding_cost_usd=0.001,
        snapshot_size_bytes=2048,
    )
    reloaded = IndexingMeasurement.model_validate_json(measurement.model_dump_json())
    assert reloaded == measurement


def test_indexing_measurement_rejects_invalid_commit_sha() -> None:
    with pytest.raises(ValidationError):
        IndexingMeasurement(
            repository=_IDENTITY,
            resolved_commit_sha="not-a-sha",
            github_request_count=1,
            chunk_count=1,
        )


def test_indexing_measurement_rejects_bool_as_source_count() -> None:
    with pytest.raises(ValidationError):
        IndexingMeasurement(
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            github_request_count=1,
            source_count_by_type={"issue": True},
            chunk_count=1,
        )


def test_indexing_measurement_rejects_negative_github_request_count() -> None:
    with pytest.raises(ValidationError):
        IndexingMeasurement(
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            github_request_count=-1,
            chunk_count=1,
        )


def test_indexing_measurement_carries_no_credential_field() -> None:
    """The contract has no field that could ever hold a credential,
    header, or environment value; adding one is rejected outright."""
    with pytest.raises(ValidationError):
        IndexingMeasurement.model_validate(
            {
                "repository": _IDENTITY.model_dump(),
                "resolved_commit_sha": _COMMIT_SHA,
                "github_request_count": 1,
                "chunk_count": 1,
                "github_token": "should-not-exist",
            }
        )


def test_indexing_measurement_measurement_limitations_default_to_empty() -> None:
    """A run with no known measurement gap must not be required to state
    one; an old persisted record without this field must still load."""
    measurement = IndexingMeasurement.model_validate(
        {
            "repository": _IDENTITY.model_dump(),
            "resolved_commit_sha": _COMMIT_SHA,
            "github_request_count": 1,
            "chunk_count": 1,
        }
    )
    assert measurement.measurement_limitations == ()
    assert measurement.skipped_items == ()


def test_indexing_measurement_round_trips_with_measurement_limitations() -> None:
    measurement = IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        github_request_count=1,
        chunk_count=1,
        measurement_limitations=(
            MeasurementLimitation(
                description=(
                    "Blank-message commits were excluded as non-rationale "
                    "content during source ingestion, but their exact "
                    "count and identities cannot be reconstructed from "
                    "the completed normalized snapshot."
                )
            ),
        ),
    )
    reloaded = IndexingMeasurement.model_validate_json(measurement.model_dump_json())
    assert reloaded == measurement
    assert len(reloaded.measurement_limitations) == 1


def test_indexing_measurement_empty_skipped_items_and_limitations_are_distinguishable() -> (
    None
):
    """An empty `skipped_items` (no individually identified item was
    skipped) must remain distinguishable from a populated
    `measurement_limitations` (a known, unquantifiable measurement gap
    exists) -- the two are independent facts about the same run."""
    measurement = IndexingMeasurement(
        repository=_IDENTITY,
        resolved_commit_sha=_COMMIT_SHA,
        github_request_count=1,
        chunk_count=1,
        skipped_items=(),
        measurement_limitations=(MeasurementLimitation(description="Known gap."),),
    )
    assert measurement.skipped_items == ()
    assert len(measurement.measurement_limitations) == 1


def test_measurement_limitation_rejects_blank_description() -> None:
    with pytest.raises(ValidationError):
        MeasurementLimitation(description="   ")


# --- RetrievalQueryUsage ------------------------------------------------------


def test_retrieval_query_usage_round_trips() -> None:
    usage = RetrievalQueryUsage(
        query_embedding_request_count=4,
        query_embedding_token_count=49,
        estimated_query_embedding_cost_usd=0.00000294,
        includes_refinement_diagnostic=True,
    )
    reloaded = RetrievalQueryUsage.model_validate_json(usage.model_dump_json())
    assert reloaded == usage


def test_retrieval_query_usage_allows_zero_usage_for_a_non_paid_run() -> None:
    usage = RetrievalQueryUsage(
        query_embedding_request_count=0,
        query_embedding_token_count=0,
        estimated_query_embedding_cost_usd=None,
        includes_refinement_diagnostic=False,
    )
    assert usage.query_embedding_request_count == 0
    assert usage.estimated_query_embedding_cost_usd is None


def test_retrieval_query_usage_rejects_negative_request_count() -> None:
    with pytest.raises(ValidationError):
        RetrievalQueryUsage(
            query_embedding_request_count=-1,
            query_embedding_token_count=0,
            includes_refinement_diagnostic=False,
        )


def test_retrieval_query_usage_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        RetrievalQueryUsage(
            query_embedding_request_count=1,
            query_embedding_token_count=1,
            estimated_query_embedding_cost_usd=-0.01,
            includes_refinement_diagnostic=False,
        )


def test_retrieval_query_usage_requires_includes_refinement_diagnostic() -> None:
    with pytest.raises(ValidationError):
        RetrievalQueryUsage.model_validate(
            {"query_embedding_request_count": 0, "query_embedding_token_count": 0}
        )


# --- RetrievalCaseResult ----------------------------------------------------


def test_retrieval_case_result_round_trips() -> None:
    result = RetrievalCaseResult(
        case_id="case-1",
        retriever="vector_voyage_chroma",
        retrieved_source_ids=("github:google/gson:issue:1", "github:google/gson:pr:2"),
        latency_seconds=0.343,
    )
    reloaded = RetrievalCaseResult.model_validate_json(result.model_dump_json())
    assert reloaded == result


def test_retrieval_case_result_rejects_blank_retriever() -> None:
    with pytest.raises(ValidationError):
        RetrievalCaseResult(case_id="case-1", retriever="  ", latency_seconds=0.1)


def test_retrieval_case_result_rejects_negative_latency() -> None:
    with pytest.raises(ValidationError):
        RetrievalCaseResult(case_id="case-1", retriever="bm25", latency_seconds=-0.1)


# --- AnswerCaseResult --------------------------------------------------------


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


def test_answer_case_result_carries_no_credential_or_provider_response_field() -> None:
    with pytest.raises(ValidationError):
        AnswerCaseResult.model_validate(
            {
                "case_id": "case-1",
                "outcome": _answer_outcome().model_dump(mode="json"),
                "trace": {
                    "model": "claude-opus-5",
                    "agent_version": "test-agent/1",
                    "searches": [],
                    "model_call_latencies_seconds": [],
                    "total_search_count": 0,
                    "total_latency_seconds": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
                "anthropic_api_key": "should-not-exist",
            }
        )


def test_answer_case_result_rejects_blank_case_id() -> None:
    with pytest.raises(ValidationError):
        AnswerCaseResult.model_validate(
            {
                "case_id": "   ",
                "outcome": _answer_outcome().model_dump(mode="json"),
                "trace": {
                    "model": "claude-opus-5",
                    "agent_version": "test-agent/1",
                    "searches": [],
                    "model_call_latencies_seconds": [],
                    "total_search_count": 0,
                    "total_latency_seconds": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            }
        )


# --- RunManifest -------------------------------------------------------------


def test_run_manifest_round_trips() -> None:
    manifest = RunManifest(
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
        library_versions={"chromadb": "1.5.9"},
    )
    reloaded = RunManifest.model_validate_json(manifest.model_dump_json())
    assert reloaded == manifest


def test_run_manifest_rejects_timezone_naive_created_at() -> None:
    with pytest.raises(ValidationError):
        RunManifest(
            run_manifest_schema_version=2,
            run_id="run-1",
            created_at=datetime(2026, 9, 10),
            command="uv run ...",
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            source_schema_version=1,
            chunk_schema_version=1,
            chunker_algorithm_version=1,
            max_chars=2000,
            retrieval_result_limit=5,
            question_set_version=1,
            split="development",
        )


def test_run_manifest_rejects_invalid_commit_sha() -> None:
    with pytest.raises(ValidationError):
        RunManifest(
            run_manifest_schema_version=2,
            run_id="run-1",
            created_at=datetime(2026, 9, 10, tzinfo=UTC),
            command="uv run ...",
            repository=_IDENTITY,
            resolved_commit_sha="not-a-sha",
            source_schema_version=1,
            chunk_schema_version=1,
            chunker_algorithm_version=1,
            max_chars=2000,
            retrieval_result_limit=5,
            question_set_version=1,
            split="development",
        )


def test_run_manifest_rejects_blank_run_id() -> None:
    with pytest.raises(ValidationError):
        RunManifest(
            run_manifest_schema_version=2,
            run_id="   ",
            created_at=datetime(2026, 9, 10, tzinfo=UTC),
            command="uv run ...",
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            source_schema_version=1,
            chunk_schema_version=1,
            chunker_algorithm_version=1,
            max_chars=2000,
            retrieval_result_limit=5,
            question_set_version=1,
            split="development",
        )


def test_run_manifest_rejects_invalid_split() -> None:
    with pytest.raises(ValidationError):
        RunManifest(
            run_manifest_schema_version=2,
            run_id="run-1",
            created_at=datetime(2026, 9, 10, tzinfo=UTC),
            command="uv run ...",
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            source_schema_version=1,
            chunk_schema_version=1,
            chunker_algorithm_version=1,
            max_chars=2000,
            retrieval_result_limit=5,
            question_set_version=1,
            split="not-a-real-split",
        )


def test_run_manifest_rejects_blank_command() -> None:
    """The manifest's `command` field is the run's reproducibility
    record; a blank command can never describe how the run was actually
    produced."""
    with pytest.raises(ValidationError):
        RunManifest(
            run_manifest_schema_version=2,
            run_id="run-1",
            created_at=datetime(2026, 9, 10, tzinfo=UTC),
            command="   ",
            repository=_IDENTITY,
            resolved_commit_sha=_COMMIT_SHA,
            source_schema_version=1,
            chunk_schema_version=1,
            chunker_algorithm_version=1,
            max_chars=2000,
            retrieval_result_limit=5,
            question_set_version=1,
            split="development",
        )
