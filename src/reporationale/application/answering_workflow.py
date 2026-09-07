"""The bounded rationale-answering workflow.

The user's exact question is the deterministic first repository-history
query. The answering model may request narrower refinements after seeing
those results. This workflow drives at most three executed searches, requires
an explicit `sufficient`/`insufficient` assessment after every search,
validates citations deterministically against the evidence actually
returned during this run, and returns exactly one structured `answered` or
`insufficient_evidence` outcome together with a compact in-memory run trace.

Never touches GitHub, Chroma, BM25, or any provider SDK directly: retrieval
happens only through the injected `SearchHistoryCapability.search_history`,
and the answering model is reached only through the narrow `AnsweringModel`
Protocol, whose real implementation lives in
`reporationale.adapters.anthropic_answering`. A provider or protocol
failure raised here is never converted into an `insufficient_evidence`
product outcome; it propagates so the caller can distinguish "retrieval
found nothing supportive" from "the provider or agent loop broke".
"""

import re
import time
from collections.abc import Callable
from typing import Protocol

from reporationale.domain.answering import (
    AnsweredOutcome,
    AnsweringRunResult,
    AnswerOutcome,
    Citation,
    CitedReference,
    FinalAnswer,
    FinalInsufficientEvidence,
    InsufficientEvidenceOutcome,
    ModelTurn,
    ProviderAction,
    RunTrace,
    SearchRecord,
    SearchRequested,
)
from reporationale.domain.retrieval import RankedEvidence

# Kept fixed in project-owned application code, not exposed as a caller or
# UI setting (see the accepted bounded tool-calling decision).
MAX_SEARCH_CALLS = 3

# Bumped to /3 when the exact user question replaced the model-generated first
# query. The total three-search budget and the two final outcome shapes remain
# unchanged.
AGENT_VERSION = "answering-workflow/3"

Clock = Callable[[], float]


class AnsweringWorkflowError(Exception):
    """Base for every typed failure this workflow itself raises, as opposed
    to a `SearchHistoryContractError` or an adapter failure, both of which
    propagate unchanged."""


class AnsweringProtocolError(AnsweringWorkflowError):
    """The answering model produced a malformed or contradictory action for
    the current turn: a search requested after the fixed `MAX_SEARCH_CALLS`
    budget is exhausted, an unrecognized action type, or a refinement missing
    its required `missing_information`."""


class UnknownCitationEvidenceError(AnsweringWorkflowError):
    """The model cited an evidence ID that this run never returned from
    `search_history`."""


class CitationMarkerMismatchError(AnsweringWorkflowError):
    """The answer text's numbered `[1]`, `[2]`, ... markers do not refer
    exactly to the resolved citation numbers `1..N`: a required marker is
    missing, or a marker refers to a number outside that range."""


_CITATION_MARKER_PATTERN = re.compile(r"\[(\d+)\]")


class SearchHistoryCapability(Protocol):
    """The narrow retrieval boundary this workflow depends on: exactly the
    `search_history` method of `reporationale.application.vector_retrieval.
    SearchHistoryService`, so tests can substitute a fake search callable or
    service without building a real persisted vector index."""

    def search_history(self, query: str) -> tuple[RankedEvidence, ...]: ...


class AnsweringModel(Protocol):
    """The narrow answering-model boundary this workflow depends on: one
    turn to start the run from the user's question, and one turn to submit
    retrieved evidence and get the next action. Satisfied structurally by
    `reporationale.adapters.anthropic_answering.AnthropicAnsweringAdapter`
    and by any fake test double."""

    @property
    def model_id(self) -> str: ...

    def start(
        self,
        *,
        question: str,
        evidence: tuple[RankedEvidence, ...],
        search_available: bool,
    ) -> ModelTurn: ...

    def submit_evidence(
        self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
    ) -> ModelTurn: ...


def _require_non_blank_question(question: str) -> None:
    if not question.strip():
        raise ValueError("question must not be blank")


def _resolve_citations(
    cited: tuple[CitedReference, ...],
    evidence_by_id: dict[str, RankedEvidence],
) -> tuple[Citation, ...]:
    for reference in cited:
        if reference.evidence_id not in evidence_by_id:
            raise UnknownCitationEvidenceError(
                f"citation refers to evidence id {reference.evidence_id!r}, "
                "which was not returned during this run"
            )
    # Deterministic numbering by first appearance; a repeated evidence_id in
    # the model's own citation list must not produce ambiguous duplicate
    # citation numbers.
    ordered_unique_ids: dict[str, None] = {}
    for reference in cited:
        ordered_unique_ids.setdefault(reference.evidence_id, None)
    resolved: list[Citation] = []
    for number, evidence_id in enumerate(ordered_unique_ids, start=1):
        chunk = evidence_by_id[evidence_id].chunk
        resolved.append(
            Citation(
                number=number,
                evidence_id=evidence_id,
                source_id=chunk.source_id,
                source_title=chunk.title,
                source_url=str(chunk.source_url),
                excerpt=chunk.text,
            )
        )
    return tuple(resolved)


def _validate_citation_markers(answer: str, citation_count: int) -> None:
    """Require the answer's numeric `[n]` markers to refer exactly to the
    resolved citation numbers `1..citation_count` -- every number in that
    range must appear at least once, and no marker may name a number
    outside it. A marker may repeat; only which numbers appear is checked."""
    found = {int(match) for match in _CITATION_MARKER_PATTERN.findall(answer)}
    expected = set(range(1, citation_count + 1))
    if found != expected:
        missing = sorted(expected - found)
        unexpected = sorted(found - expected)
        raise CitationMarkerMismatchError(
            "answer citation markers do not match the resolved citation "
            f"numbers 1..{citation_count}: missing {missing}, unexpected {unexpected}"
        )


def answer_question(
    question: str,
    *,
    search_history: SearchHistoryCapability,
    model: AnsweringModel,
    clock: Clock = time.monotonic,
) -> AnsweringRunResult:
    """Answer one rationale question through the bounded agent loop.

    Rejects a blank `question`. Executes the first search deterministically
    with the user's exact question, then lets the model answer, abstain, or
    request a narrower refinement. Executes at most `MAX_SEARCH_CALLS` searches, and
    requires an explicit sufficiency assessment after every one before
    either final outcome is reachable. Stops as soon as evidence is
    assessed sufficient; after the third search, `search_available=False`
    withholds the search tool from the final turn, and a further search
    request at that point is a typed `AnsweringProtocolError` rather than a
    fourth executed search.
    """
    _require_non_blank_question(question)

    run_started_at = clock()
    model_call_latencies: list[float] = []
    search_records: list[SearchRecord] = []
    evidence_by_id: dict[str, RankedEvidence] = {}
    searches_executed = 0
    total_input_tokens = 0
    total_output_tokens = 0

    def _call_model(turn: Callable[[], ModelTurn]) -> ProviderAction:
        nonlocal total_input_tokens, total_output_tokens
        started_at = clock()
        response = turn()
        model_call_latencies.append(clock() - started_at)
        total_input_tokens += response.input_tokens
        total_output_tokens += response.output_tokens
        return response.action

    query = question
    first_search = True
    outcome: AnswerOutcome | None = None
    while outcome is None:
        search_started_at = clock()
        evidence = search_history.search_history(query)
        retrieval_latency = clock() - search_started_at
        searches_executed += 1
        for result in evidence:
            evidence_by_id.setdefault(result.evidence_id, result)
        evidence_ids = tuple(result.evidence_id for result in evidence)

        search_available = searches_executed < MAX_SEARCH_CALLS
        if first_search:
            next_action = _call_model(
                lambda: model.start(
                    question=question,
                    evidence=evidence,
                    search_available=search_available,
                )
            )
            first_search = False
        else:
            next_action = _call_model(
                lambda: model.submit_evidence(
                    evidence=evidence, search_available=search_available
                )
            )

        if isinstance(next_action, SearchRequested):
            if not search_available:
                raise AnsweringProtocolError(
                    "the answering model requested another search after the "
                    f"fixed {MAX_SEARCH_CALLS}-call budget was exhausted"
                )
            if next_action.missing_information is None:
                raise AnsweringProtocolError(
                    "a refined search must record what evidence is missing"
                )
            search_records.append(
                SearchRecord(
                    query=query,
                    evidence_ids=evidence_ids,
                    latency_seconds=retrieval_latency,
                    assessment="insufficient",
                    missing_information=next_action.missing_information,
                    next_query=next_action.query,
                )
            )
            query = next_action.query
            continue

        if isinstance(next_action, FinalAnswer):
            search_records.append(
                SearchRecord(
                    query=query,
                    evidence_ids=evidence_ids,
                    latency_seconds=retrieval_latency,
                    assessment="sufficient",
                )
            )
            citations = _resolve_citations(next_action.citations, evidence_by_id)
            _validate_citation_markers(next_action.answer, len(citations))
            outcome = AnsweredOutcome(answer=next_action.answer, citations=citations)
            continue

        if isinstance(next_action, FinalInsufficientEvidence):
            search_records.append(
                SearchRecord(
                    query=query,
                    evidence_ids=evidence_ids,
                    latency_seconds=retrieval_latency,
                    assessment="insufficient",
                    missing_information=next_action.explanation,
                )
            )
            outcome = InsufficientEvidenceOutcome(explanation=next_action.explanation)
            continue

        raise AnsweringProtocolError(f"unrecognized provider action: {next_action!r}")

    trace = RunTrace(
        model=model.model_id,
        agent_version=AGENT_VERSION,
        searches=tuple(search_records),
        model_call_latencies_seconds=tuple(model_call_latencies),
        total_search_count=searches_executed,
        total_latency_seconds=clock() - run_started_at,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
    )
    return AnsweringRunResult(outcome=outcome, trace=trace)
