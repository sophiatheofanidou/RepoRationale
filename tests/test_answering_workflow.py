"""Tests for the bounded rationale-answering workflow: an answerable happy
path with grounded citations and a completed trace, a query-refinement path
that records the first insufficient assessment before a second search
recovers useful evidence, an insufficient-evidence path that stops within
the fixed search limit, rejection of a citation ID unseen during the
current run, rejection of an answer whose numbered markers do not match the
resolved citations, and hard enforcement of the fixed three-search maximum
against a fake model that keeps requesting more.

Every test uses small fake `AnsweringModel` and `SearchHistoryCapability`
doubles and a deterministic fake clock; no network or paid API call is made.
"""

import itertools
from collections.abc import Callable, Iterator

import pytest

from reporationale.application.answering_workflow import (
    AGENT_VERSION,
    MAX_SEARCH_CALLS,
    AnsweringProtocolError,
    CitationMarkerMismatchError,
    UnknownCitationEvidenceError,
    answer_question,
)
from reporationale.domain.answering import (
    AnsweredOutcome,
    CitedReference,
    FinalAnswer,
    FinalInsufficientEvidence,
    InsufficientEvidenceOutcome,
    ModelTurn,
    SearchRequested,
)
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.retrieval import RankedEvidence

_MODEL_ID = "fake-model/1"


def _chunk(evidence_id: str, *, text: str, title: str | None = None) -> SourceChunk:
    source_id, _, chunk_index_text = evidence_id.rpartition(":chunk:")
    return SourceChunk(
        chunk_id=evidence_id,
        source_id=source_id,
        chunk_index=int(chunk_index_text),
        text=text,
        platform="github",
        repository="octo-org/example-repo",
        source_type="markdown",
        source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
        title=title,
    )


def _evidence(
    evidence_id: str,
    *,
    rank: int = 1,
    score: float = 0.1,
    text: str,
    title: str | None = None,
) -> RankedEvidence:
    return RankedEvidence(
        evidence_id=evidence_id,
        rank=rank,
        score=score,
        score_kind="cosine_distance",
        chunk=_chunk(evidence_id, text=text, title=title),
    )


class _FakeSearchHistory:
    """Maps each expected query to a fixed evidence tuple and records every
    query it actually received, in order."""

    def __init__(self, results_by_query: dict[str, tuple[RankedEvidence, ...]]) -> None:
        self._results_by_query = results_by_query
        self.calls: list[str] = []

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]:
        self.calls.append(query)
        return self._results_by_query[query]


class _FakeModel:
    """Returns one pre-scripted `ModelTurn` per call, in order, regardless
    of what it is given -- enough to drive every workflow branch without a
    real Anthropic client."""

    model_id = _MODEL_ID

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns: Iterator[ModelTurn] = iter(turns)
        self.initial_evidence: tuple[RankedEvidence, ...] | None = None

    def start(
        self,
        *,
        question: str,
        evidence: tuple[RankedEvidence, ...],
        search_available: bool,
    ) -> ModelTurn:
        self.initial_evidence = evidence
        return next(self._turns)

    def submit_evidence(
        self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
    ) -> ModelTurn:
        return next(self._turns)


def _make_clock() -> Callable[[], float]:
    """A deterministic monotonic clock stand-in: each call returns the next
    whole second, so per-call and total latencies are reproducible."""
    values: Iterator[float] = iter(itertools.count(0.0, 1.0))

    def _clock() -> float:
        return next(values)

    return _clock


def test_answerable_happy_path_returns_grounded_citations_and_completed_trace() -> None:
    evidence_id = "github:octo-org/example-repo:markdown:polling.md:chunk:0"
    search = _FakeSearchHistory(
        {
            "Why was polling chosen?": (
                _evidence(
                    evidence_id,
                    text="Polling was chosen because webhooks were unreliable.",
                    title="Polling rationale",
                ),
            )
        }
    )
    model = _FakeModel(
        [
            ModelTurn(
                action=FinalAnswer(
                    answer="Polling was chosen because webhooks were unreliable. [1]",
                    citations=(CitedReference(evidence_id=evidence_id),),
                ),
                input_tokens=20,
                output_tokens=15,
            ),
        ]
    )

    result = answer_question(
        "Why was polling chosen?",
        search_history=search,
        model=model,
        clock=_make_clock(),
    )

    assert isinstance(result.outcome, AnsweredOutcome)
    assert result.outcome.answer.startswith("Polling was chosen")
    assert len(result.outcome.citations) == 1
    citation = result.outcome.citations[0]
    assert citation.number == 1
    assert citation.evidence_id == evidence_id
    assert citation.source_title == "Polling rationale"
    assert citation.excerpt == "Polling was chosen because webhooks were unreliable."

    trace = result.trace
    assert trace.model == _MODEL_ID
    assert trace.agent_version == AGENT_VERSION
    assert trace.total_search_count == 1
    assert len(trace.searches) == 1
    assert trace.searches[0].assessment == "sufficient"
    assert trace.searches[0].evidence_ids == (evidence_id,)
    assert trace.searches[0].missing_information is None
    assert len(trace.model_call_latencies_seconds) == 1
    assert trace.input_tokens == 20
    assert trace.output_tokens == 15
    assert trace.total_latency_seconds > 0
    assert search.calls == ["Why was polling chosen?"]
    assert model.initial_evidence is not None
    assert model.initial_evidence[0].evidence_id == evidence_id


def test_refinement_recovers_evidence_absent_from_first_search() -> None:
    first_id = "github:octo-org/example-repo:markdown:general.md:chunk:0"
    second_id = "github:octo-org/example-repo:markdown:specific.md:chunk:0"
    search = _FakeSearchHistory(
        {
            "Why polling?": (
                _evidence(first_id, text="Polling exists in the sync layer."),
            ),
            "why polling instead of webhooks?": (
                _evidence(
                    second_id,
                    text="Webhooks were unreliable across customer networks.",
                ),
            ),
        }
    )
    model = _FakeModel(
        [
            ModelTurn(
                action=SearchRequested(
                    query="why polling instead of webhooks?",
                    missing_information="why webhooks specifically were rejected",
                ),
                input_tokens=1,
                output_tokens=1,
            ),
            ModelTurn(
                action=FinalAnswer(
                    answer="Webhooks were unreliable, so polling was used. [1][2]",
                    citations=(
                        CitedReference(evidence_id=second_id),
                        CitedReference(evidence_id=first_id),
                    ),
                ),
                input_tokens=1,
                output_tokens=1,
            ),
        ]
    )

    result = answer_question(
        "Why polling?",
        search_history=search,
        model=model,
        clock=_make_clock(),
    )

    assert isinstance(result.outcome, AnsweredOutcome)
    assert [citation.evidence_id for citation in result.outcome.citations] == [
        second_id,
        first_id,
    ]
    assert [citation.number for citation in result.outcome.citations] == [1, 2]

    trace = result.trace
    assert trace.total_search_count == 2
    first_record, second_record = trace.searches
    assert first_record.assessment == "insufficient"
    assert first_record.missing_information == "why webhooks specifically were rejected"
    assert first_record.next_query == "why polling instead of webhooks?"
    assert second_record.assessment == "sufficient"
    assert second_record.evidence_ids == (second_id,)
    assert search.calls == ["Why polling?", "why polling instead of webhooks?"]


def test_insufficient_evidence_stops_within_limit_with_no_citations() -> None:
    evidence_id = "github:octo-org/example-repo:markdown:unrelated.md:chunk:0"
    search = _FakeSearchHistory(
        {
            "What was the undocumented rationale?": (
                _evidence(evidence_id, text="Unrelated content."),
            )
        }
    )
    model = _FakeModel(
        [
            ModelTurn(
                action=FinalInsufficientEvidence(
                    explanation="The repository does not document this rationale."
                ),
                input_tokens=1,
                output_tokens=1,
            ),
        ]
    )

    result = answer_question(
        "What was the undocumented rationale?",
        search_history=search,
        model=model,
        clock=_make_clock(),
    )

    assert isinstance(result.outcome, InsufficientEvidenceOutcome)
    assert (
        result.outcome.explanation == "The repository does not document this rationale."
    )
    assert result.trace.total_search_count == 1
    assert result.trace.searches[0].assessment == "insufficient"
    assert (
        result.trace.searches[0].missing_information
        == "The repository does not document this rationale."
    )
    assert result.trace.searches[0].next_query is None
    assert search.calls == ["What was the undocumented rationale?"]


def test_citation_to_unseen_evidence_id_is_rejected() -> None:
    evidence_id = "github:octo-org/example-repo:markdown:seen.md:chunk:0"
    search = _FakeSearchHistory(
        {"A question?": (_evidence(evidence_id, text="Some documented text."),)}
    )
    model = _FakeModel(
        [
            ModelTurn(
                action=FinalAnswer(
                    answer="Answer citing evidence never returned. [1]",
                    citations=(
                        CitedReference(evidence_id="github:other:markdown:x:chunk:0"),
                    ),
                ),
                input_tokens=1,
                output_tokens=1,
            ),
        ]
    )

    with pytest.raises(UnknownCitationEvidenceError):
        answer_question(
            "A question?",
            search_history=search,
            model=model,
            clock=_make_clock(),
        )


def test_answer_with_mismatched_citation_markers_is_rejected() -> None:
    first_id = "github:octo-org/example-repo:markdown:one.md:chunk:0"
    second_id = "github:octo-org/example-repo:markdown:two.md:chunk:0"
    search = _FakeSearchHistory(
        {
            "A question?": (
                _evidence(first_id, text="First documented reason."),
                _evidence(second_id, text="Second documented reason."),
            )
        }
    )
    model = _FakeModel(
        [
            ModelTurn(
                # Two citations resolve (numbers 1 and 2), but the answer
                # text omits marker [2] -- a mismatch the model must not be
                # trusted to get right.
                action=FinalAnswer(
                    answer="Only the first reason is mentioned. [1]",
                    citations=(
                        CitedReference(evidence_id=first_id),
                        CitedReference(evidence_id=second_id),
                    ),
                ),
                input_tokens=1,
                output_tokens=1,
            ),
        ]
    )

    with pytest.raises(CitationMarkerMismatchError):
        answer_question(
            "A question?",
            search_history=search,
            model=model,
            clock=_make_clock(),
        )


def test_hard_maximum_enforces_exactly_three_searches() -> None:
    search = _FakeSearchHistory(
        {
            f"query-{i}": (
                _evidence(
                    f"github:octo-org/example-repo:markdown:doc{i}.md:chunk:0",
                    text=f"Content {i}.",
                ),
            )
            for i in range(2, 5)
        }
    )
    initial_question = "A question that never gets resolved?"
    search._results_by_query[initial_question] = (
        _evidence(
            "github:octo-org/example-repo:markdown:doc1.md:chunk:0",
            text="Content 1.",
        ),
    )

    class _StubbornModel:
        model_id = _MODEL_ID

        def __init__(self) -> None:
            self._next_query_index = 2

        def start(
            self,
            *,
            question: str,
            evidence: tuple[RankedEvidence, ...],
            search_available: bool,
        ) -> ModelTurn:
            action = SearchRequested(
                query=f"query-{self._next_query_index}",
                missing_information="still need more evidence",
            )
            self._next_query_index += 1
            return ModelTurn(action=action, input_tokens=1, output_tokens=1)

        def submit_evidence(
            self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
        ) -> ModelTurn:
            action = SearchRequested(
                query=f"query-{self._next_query_index}",
                missing_information="still need more evidence",
            )
            self._next_query_index += 1
            return ModelTurn(action=action, input_tokens=1, output_tokens=1)

    with pytest.raises(AnsweringProtocolError):
        answer_question(
            initial_question,
            search_history=search,
            model=_StubbornModel(),
            clock=_make_clock(),
        )

    assert search.calls == [initial_question, "query-2", "query-3"]
    assert MAX_SEARCH_CALLS == 3
