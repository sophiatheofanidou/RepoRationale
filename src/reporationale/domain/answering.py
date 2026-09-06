"""Provider-neutral contracts for one bounded rationale-answering run.

This module owns every shape the answering workflow
(`reporationale.application.answering_workflow`) exchanges with an answering
model: the typed action a model turn may produce (`ProviderAction` and its
members), the token-usage envelope around one such turn (`ModelTurn`), the
final structured `answered`/`insufficient_evidence` outcome
(`AnswerOutcome`), and the compact in-memory execution trace (`RunTrace`).

Nothing here depends on the Anthropic SDK or any other provider: the sole
boundary that translates real Anthropic Messages API content into these
types is `reporationale.adapters.anthropic_answering`, which never leaks an
SDK request or response object past itself.

A model may cite evidence only by `evidence_id` (`CitedReference`); it is
never trusted to invent or restate citation metadata or excerpts. The
workflow resolves every `Citation`'s remaining fields deterministically from
the `RankedEvidence` actually accumulated during the current run.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class CitedReference(BaseModel):
    """A model-selected citation, addressed only by the evidence ID it was
    given during this run. Carries nothing else the model could invent or
    rewrite; the workflow resolves the remaining citation metadata and
    excerpt from the accumulated `RankedEvidence` this ID identifies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)

    @field_validator("evidence_id")
    @classmethod
    def evidence_id_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class SearchRequested(BaseModel):
    """The model's action for one turn: request a `search_history` call.

    `missing_information` is `None` for the very first search of a run
    (nothing has been assessed insufficient yet); it is required and
    non-blank whenever this action refines a search after a prior retrieval
    was assessed insufficient, so the workflow can record what evidence the
    model believed was missing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["search"] = "search"
    query: str = Field(min_length=1)
    missing_information: str | None = None

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)

    @field_validator("missing_information")
    @classmethod
    def missing_information_must_not_be_blank_when_present(
        cls, value: str | None
    ) -> str | None:
        if value is not None:
            _require_non_blank(value)
        return value


class FinalAnswer(BaseModel):
    """The model's action for one turn: a sufficient-evidence assessment
    together with its answer and the evidence IDs it cites. Citation
    metadata is resolved later by the workflow, never trusted from here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["answered"] = "answered"
    answer: str = Field(min_length=1)
    citations: tuple[CitedReference, ...] = Field(min_length=1)

    @field_validator("answer")
    @classmethod
    def answer_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class FinalInsufficientEvidence(BaseModel):
    """The model's action for one turn: an insufficient-evidence assessment
    that ends the run, with a concise explanation and no citations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["insufficient_evidence"] = "insufficient_evidence"
    explanation: str = Field(min_length=1)

    @field_validator("explanation")
    @classmethod
    def explanation_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


ProviderAction = SearchRequested | FinalAnswer | FinalInsufficientEvidence


class ModelTurn(BaseModel):
    """One completed answering-model call: the translated `ProviderAction`
    plus whatever input/output token usage the provider reported for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ProviderAction
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class Citation(BaseModel):
    """One fully resolved citation in an `answered` outcome: deterministic
    numbering, the cited evidence ID, and the source provenance and exact
    excerpt resolved from the accumulated `RankedEvidence` for that ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(strict=True, gt=0)
    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_title: str | None
    source_url: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)


class AnsweredOutcome(BaseModel):
    """An evidence-grounded answer: non-blank answer text and at least one
    numbered citation, each traceable to evidence returned during this run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["answered"] = "answered"
    answer: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)

    @field_validator("answer")
    @classmethod
    def answer_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


class InsufficientEvidenceOutcome(BaseModel):
    """An explicit abstention: retrieval ran successfully but did not
    support an answer. Carries no citations and no unsupported claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["insufficient_evidence"] = "insufficient_evidence"
    explanation: str = Field(min_length=1)

    @field_validator("explanation")
    @classmethod
    def explanation_must_not_be_blank(cls, value: str) -> str:
        return _require_non_blank(value)


AnswerOutcome = AnsweredOutcome | InsufficientEvidenceOutcome


class SearchRecord(BaseModel):
    """One executed `search_history` call and the assessment made against
    the original question immediately afterward.

    `missing_information` is populated whenever this search's assessment was
    `insufficient`: for a refined search it is the model's stated reason for
    refining, and for a final insufficient-evidence outcome it is that
    outcome's own explanation, so the reason evidence was judged
    insufficient survives even when the run does not refine again. It stays
    `None` only for a `sufficient` assessment. `next_query` is populated
    only when this search's insufficient assessment led to a refined
    search; it stays `None` for a `sufficient` assessment and for a final
    `insufficient` assessment that ends the run instead of refining it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    latency_seconds: float = Field(ge=0)
    assessment: Literal["sufficient", "insufficient"]
    missing_information: str | None = None
    next_query: str | None = None


class RunTrace(BaseModel):
    """The compact in-memory execution trace for one answering run: every
    search this run performed, per-call answering-model latency, aggregate
    counts, total run latency, and available provider token usage. Never
    persisted; returned in memory alongside the run's `AnswerOutcome`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    searches: tuple[SearchRecord, ...]
    model_call_latencies_seconds: tuple[float, ...]
    total_search_count: int = Field(strict=True, ge=0)
    total_latency_seconds: float = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def total_search_count_must_match_recorded_searches(self) -> "RunTrace":
        if self.total_search_count != len(self.searches):
            raise ValueError(
                "total_search_count must equal the number of recorded searches"
            )
        return self


class AnsweringRunResult(BaseModel):
    """The complete result of one bounded answering run: the structured
    outcome plus its execution trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: AnswerOutcome
    trace: RunTrace
