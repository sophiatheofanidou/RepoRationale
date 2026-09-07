"""Project-owned boundary around the Anthropic Messages API for the bounded
rationale-answering agent loop.

Isolates the `anthropic` SDK's client, request shape, and response content
blocks behind this module, the same way `adapters.voyage_embeddings`
isolates the Voyage SDK: nothing outside this module ever imports
`anthropic` or sees an Anthropic request or response object. Callers depend
only on `AnthropicAnsweringAdapter` (satisfying the application layer's
`AnsweringModel` Protocol), the provider-neutral `ModelTurn`/`ProviderAction`
domain shapes it returns, and `AnthropicAnswerError`.

The application performs the first repository-history search with the user's
exact question. `refine_search` is the model's only retrieval tool and is
available only after it has seen evidence, so its schema can require the
refinement's stated reason. It exposes no repository, source, author, date,
state, item-ID, backend, filter, or result-count parameter. The model's final
answer and abstention are
also expressed as forced tool calls (`provide_answer`,
`report_insufficient_evidence`) rather than free-form JSON text; see the
accepted decision this amends for why. Every model turn is requested with a
`tool_choice` that forces exactly one call from the tools
currently offered, so this adapter never needs to parse or tolerate
free-form response text at all. It still rejects a malformed, missing,
multiple, unknown, or contradictory tool call with a clear
`AnthropicAnswerError` rather than guessing what the provider meant.

`_BASE_SYSTEM_PROMPT` also carries two development-pilot corrections
(see `docs/evaluation.md`'s Gson development end-to-end answering pilot):
a source's own hedged or speculative language must not be promoted into a
stated fact or design intent; and a question whose
premise the evidence does not document, or actively contradicts, must
resolve to `report_insufficient_evidence` rather than `provide_answer`
even when the model can explain what the evidence actually shows. The former
first-query prompt mitigation was replaced by a deterministic application
search after held-out diagnostics showed that a near-paraphrase could still
discard useful wording.
"""

import json
from collections.abc import Sequence
from typing import Any, Protocol, cast

from anthropic import Anthropic as _AnthropicClient
from pydantic import ValidationError

from reporationale.domain.answering import (
    CitedReference,
    FinalAnswer,
    FinalInsufficientEvidence,
    ModelTurn,
    ProviderAction,
    SearchRequested,
)
from reporationale.domain.retrieval import RankedEvidence

_REFINE_SEARCH_TOOL: dict[str, object] = {
    "name": "refine_search",
    "strict": True,
    "description": (
        "Search again because the previous evidence was insufficient. "
        "Never used for the first search of the run."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": "A more focused natural-language search query.",
            },
            "missing_information": {
                "type": "string",
                "description": "What evidence was missing from the previous search.",
            },
        },
        "required": ["query", "missing_information"],
    },
}

_PROVIDE_ANSWER_TOOL: dict[str, object] = {
    "name": "provide_answer",
    "strict": True,
    "description": (
        "Give the final evidence-grounded answer once retrieved evidence is "
        "sufficient. Cite evidence only by the evidence_id values you were "
        "given; never invent one. Never call this with an empty citations "
        "list; call report_insufficient_evidence instead."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "answer": {
                "type": "string",
                "description": (
                    "The answer text, with [1], [2], ... markers matching "
                    "the order of citations."
                ),
            },
            "citations": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
            },
        },
        "required": ["answer", "citations"],
    },
}

_REPORT_INSUFFICIENT_EVIDENCE_TOOL: dict[str, object] = {
    "name": "report_insufficient_evidence",
    "strict": True,
    "description": (
        "Report that the retrieved evidence does not document an answer, "
        "instead of guessing from general knowledge."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "explanation": {
                "type": "string",
                "description": "A concise reason the evidence was insufficient.",
            }
        },
        "required": ["explanation"],
    },
}

_BASE_SYSTEM_PROMPT = (
    "You help recover documented rationale from a repository's indexed "
    "history; never answer from general knowledge instead of retrieved "
    "evidence.\n\n"
    "The application has already searched the repository-history index with "
    "the user's exact question and supplied those results. Assess that evidence "
    "before deciding whether to answer, report insufficient evidence, or request "
    "a narrower refinement.\n\n"
    "Preserve the evidence's own certainty. If a source uses a hedge such "
    'as "maybe", "might", "could", or "probably", or phrases '
    "something as a question, keep that same uncertainty in your answer "
    "instead of stating it as settled fact. A source author's own "
    "hypothesis or guess must never be presented as a documented "
    "historical fact or design intent. Distinguish clearly between what "
    "the history explicitly documents as fact, what a source author only "
    "speculated, and anything you are synthesizing yourself. Citing a "
    "source is not enough on its own: the claim you make must carry the "
    "same epistemic strength the source itself expresses, not more.\n\n"
    "If the question assumes that a specific change happened or that a "
    "specific rationale existed, but the retrieved evidence does not "
    "document that premise, or shows the opposite, call "
    "report_insufficient_evidence rather than provide_answer, even if you "
    "can explain what the evidence actually shows. Your explanation may "
    "say that the evidence contradicts the question's premise or does not "
    "confirm it; this is still an insufficient-evidence outcome, not an "
    "answered rationale question, because the premise itself is what was "
    "asked about.\n\n"
    "Cite evidence only by the evidence_id values you were given; never "
    "invent one.\n\n"
    "The answer's [n] markers must correspond exactly and only to the "
    "distinct evidence_ids in the citations list, numbered in "
    "first-appearance order. Use every resulting citation number at least "
    "once and never use a number outside that range.\n\n"
    "Never call provide_answer with an empty citations list. If the "
    "available evidence does not support an answer, call "
    "report_insufficient_evidence instead."
)

# Appended to the base prompt on every model turn, so the model's actual,
# current ability to search again is stated explicitly
# rather than left for it to infer from the absence of a tool. Haiku,
# Sonnet, and Opus have all been observed live calling `refine_search`
# after the search budget was exhausted and it was no longer offered;
# this closes the gap on the request side, uniformly for every model,
# without changing the three-search limit itself or which tools are
# offered (see `_tools_for`, unchanged).
_SEARCH_AVAILABLE_GUIDANCE = (
    "After this search, decide whether the evidence is sufficient to "
    "answer the user's question. You may still search again: if it is "
    "not sufficient, call refine_search with a more focused query and an "
    "explanation of what is still missing. If it is sufficient, call "
    "provide_answer. If you judge it insufficient and do not want to "
    "search again, call report_insufficient_evidence."
)

_SEARCH_EXHAUSTED_GUIDANCE = (
    "All 3 permitted searches have already been used. You cannot search "
    "or refine again. You must call either provide_answer or "
    "report_insufficient_evidence."
)


def _system_prompt_for(*, search_available: bool) -> str:
    """The system prompt for one turn: the fixed base instructions, plus —
    on every turn after the mandatory first search — an explicit statement
    of whether `refine_search` is actually still available this turn.
    Never states the exhausted-budget guidance when a search is still
    available, and never omits it once the budget is spent."""
    guidance = (
        _SEARCH_AVAILABLE_GUIDANCE if search_available else _SEARCH_EXHAUSTED_GUIDANCE
    )
    return f"{_BASE_SYSTEM_PROMPT}\n\n{guidance}"


_MAX_TOKENS = 2048

# Explicitly turned off on every request. Left unset, some current model
# generations default to "adaptive" extended thinking and may return a
# `thinking` content block instead of — or mixed in with — the tool call
# this adapter's fixed protocol depends on for every turn. Disabling it
# keeps every model's response shape consistent with that protocol.
_THINKING_CONFIG: dict[str, object] = {"type": "disabled"}


def _tools_for(*, search_available: bool) -> list[dict[str, object]]:
    """Offer both terminal actions and, while budget remains, refinement.

    The application has already executed the exact-question first search,
    so the model never receives a separate first-search tool.
    """
    if search_available:
        return [
            _REFINE_SEARCH_TOOL,
            _PROVIDE_ANSWER_TOOL,
            _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
        ]
    return [_PROVIDE_ANSWER_TOOL, _REPORT_INSUFFICIENT_EVIDENCE_TOOL]


def _tool_choice_for() -> dict[str, object]:
    """The explicit `tool_choice` for one request, so the API itself
    guarantees exactly one call from the tools offered — never a parallel
    second call, and never any accompanying free text."""
    return {"type": "any", "disable_parallel_tool_use": True}


class AnthropicAnswerError(Exception):
    """The Anthropic API returned a response this adapter could not
    translate into one well-formed `ProviderAction`: not exactly one tool
    call in a turn, an unknown tool name, or a malformed or missing tool
    input field."""


class _Usage(Protocol):
    """Read-only `@property` members throughout this file's response
    Protocols (here and below), rather than plain annotations, so a
    concrete type only needs a covariantly compatible attribute or property
    to satisfy them — a plain settable annotation would instead require
    exact invariant type equality and reject the real SDK's response
    types."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...


class _MessagesResponse(Protocol):
    @property
    def content(self) -> Sequence[object]: ...

    @property
    def usage(self) -> _Usage: ...

    @property
    def stop_reason(self) -> str | None: ...


class _MessagesNamespace(Protocol):
    """`tools`, `tool_choice`, `thinking`, and `messages` are typed `Any`
    only because the real SDK's parameter types are precise `TypedDict`
    unions that a generic `dict[str, object]` cannot structurally satisfy;
    every dict this adapter actually builds and passes is concretely typed
    at its own call site (see the tool constants and `_tool_choice_for`
    above, and the message-history assembly below)."""

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: Any,
        tool_choice: Any,
        thinking: Any,
        messages: Any,
    ) -> _MessagesResponse: ...


class AnthropicMessagesClient(Protocol):
    """The exact `anthropic.Anthropic` surface this adapter depends on, so
    tests can substitute a fake without a network call or the real SDK.

    `messages` is declared read-only (a `@property`, not a plain
    annotation) because the real `anthropic.Anthropic.messages` is itself a
    read-only attribute; a plain annotation would require it to be
    settable too and reject the real client structurally."""

    @property
    def messages(self) -> _MessagesNamespace: ...


class _ToolUseBlock(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def input(self) -> object: ...


def _tool_use_blocks(content: Sequence[object]) -> list[_ToolUseBlock]:
    return [
        cast(_ToolUseBlock, block)
        for block in content
        if getattr(block, "type", None) == "tool_use"
    ]


def _require_evidence_id(entry: object) -> str:
    if not isinstance(entry, dict) or not isinstance(entry.get("evidence_id"), str):
        raise AnthropicAnswerError(
            f"malformed citation entry, expected {{'evidence_id': str}}: {entry!r}"
        )
    return cast(str, entry["evidence_id"])


def _require_query(raw_input: object, *, tool_name: str) -> str:
    if not isinstance(raw_input, dict):
        raise AnthropicAnswerError(f"{tool_name} tool call input must be an object")
    query = raw_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise AnthropicAnswerError(
            f"{tool_name} tool call must include a non-blank string 'query'"
        )
    return query


def _parse_refine_search(raw_input: object) -> SearchRequested:
    query = _require_query(raw_input, tool_name="refine_search")
    missing_information = cast(dict[str, object], raw_input).get("missing_information")
    if not isinstance(missing_information, str) or not missing_information.strip():
        raise AnthropicAnswerError(
            "refine_search tool call must include a non-blank string "
            "'missing_information'"
        )
    return SearchRequested(query=query, missing_information=missing_information)


def _parse_provide_answer(raw_input: object) -> FinalAnswer:
    if not isinstance(raw_input, dict):
        raise AnthropicAnswerError("provide_answer tool call input must be an object")
    answer = raw_input.get("answer")
    citations_raw = raw_input.get("citations")
    if not isinstance(answer, str) or not answer.strip():
        raise AnthropicAnswerError("provide_answer needs a non-blank 'answer'")
    if not isinstance(citations_raw, list) or not citations_raw:
        raise AnthropicAnswerError("provide_answer needs at least one citation")
    try:
        citations = tuple(
            CitedReference(evidence_id=_require_evidence_id(entry))
            for entry in citations_raw
        )
    except ValidationError as error:
        raise AnthropicAnswerError(f"malformed citation entry: {error}") from error
    return FinalAnswer(answer=answer, citations=citations)


def _parse_report_insufficient_evidence(raw_input: object) -> FinalInsufficientEvidence:
    if not isinstance(raw_input, dict):
        raise AnthropicAnswerError(
            "report_insufficient_evidence tool call input must be an object"
        )
    explanation = raw_input.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise AnthropicAnswerError(
            "report_insufficient_evidence needs a non-blank 'explanation'"
        )
    return FinalInsufficientEvidence(explanation=explanation)


def _parse_action(
    content: Sequence[object], *, offered_tool_names: frozenset[str]
) -> tuple[ProviderAction, str]:
    """Translate one turn's content into exactly one `ProviderAction`.

    Every turn's `tool_choice` forces the model to make exactly one call
    from the tools it was offered, so a well-formed response always
    carries exactly one `tool_use` block naming one of `offered_tool_names`;
    anything else is a contract violation this raises `AnthropicAnswerError`
    for rather than guessing. The offered-tool check is defense in depth
    independent of the API's own `tool_choice` enforcement: this adapter
    must not treat a tool name the model referenced from earlier
    conversation history, but that this specific turn did not offer (for
    example, a search tool named again after the search budget was
    exhausted and withheld), as if it were a valid action for this turn.
    """
    tool_use_blocks = _tool_use_blocks(content)
    if len(tool_use_blocks) != 1:
        raise AnthropicAnswerError(
            "expected exactly one tool call in the response, got "
            f"{len(tool_use_blocks)}"
        )
    tool_use = tool_use_blocks[0]
    if tool_use.name not in offered_tool_names:
        raise AnthropicAnswerError(
            f"the model called tool {tool_use.name!r}, which was not offered this turn"
        )
    if tool_use.name == _REFINE_SEARCH_TOOL["name"]:
        return _parse_refine_search(tool_use.input), tool_use.id
    if tool_use.name == _PROVIDE_ANSWER_TOOL["name"]:
        return _parse_provide_answer(tool_use.input), tool_use.id
    if tool_use.name == _REPORT_INSUFFICIENT_EVIDENCE_TOOL["name"]:
        return _parse_report_insufficient_evidence(tool_use.input), tool_use.id
    raise AnthropicAnswerError(f"the model called unknown tool {tool_use.name!r}")


def _evidence_context(evidence: RankedEvidence) -> dict[str, object]:
    chunk = evidence.chunk
    return {
        "evidence_id": evidence.evidence_id,
        "rank": evidence.rank,
        "score": evidence.score,
        "score_kind": evidence.score_kind,
        "text": chunk.text,
        "source_id": chunk.source_id,
        "source_type": chunk.source_type,
        "source_url": str(chunk.source_url),
        "title": chunk.title,
    }


class AnthropicAnsweringAdapter:
    """One project-owned Anthropic Messages API answering turn per
    instance: exposes the fixed tool set and system instruction, drives
    the Anthropic `tool_use`/`tool_result` conversation flow for exactly
    one bounded answering run, and translates every response into the
    provider-neutral `ModelTurn`/`ProviderAction` shapes the application
    workflow depends on.

    A fresh instance is used per run: it owns the growing Anthropic message
    history for that one question and must not be reused across questions.
    Nothing outside this module ever sees an Anthropic SDK request or
    response object.
    """

    def __init__(self, *, client: AnthropicMessagesClient, model_id: str) -> None:
        self._client = client
        self._model_id = model_id
        self._messages: list[dict[str, object]] = []
        self._pending_tool_use_id: str | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    def start(
        self,
        *,
        question: str,
        evidence: tuple[RankedEvidence, ...],
        search_available: bool,
    ) -> ModelTurn:
        self._messages = [
            {
                "role": "user",
                "content": (
                    f"Question:\n{question}\n\n"
                    "Initial search results (JSON):\n"
                    + json.dumps([_evidence_context(result) for result in evidence])
                ),
            }
        ]
        return self._request(search_available=search_available)

    def submit_evidence(
        self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
    ) -> ModelTurn:
        if self._pending_tool_use_id is None:
            raise AnthropicAnswerError(
                "submit_evidence called with no pending refine_search call "
                "to respond to"
            )
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": self._pending_tool_use_id,
                        "content": json.dumps(
                            [_evidence_context(result) for result in evidence]
                        ),
                    }
                ],
            }
        )
        return self._request(search_available=search_available)

    def _request(self, *, search_available: bool) -> ModelTurn:
        tools = _tools_for(search_available=search_available)
        response = self._client.messages.create(
            model=self._model_id,
            max_tokens=_MAX_TOKENS,
            system=_system_prompt_for(search_available=search_available),
            tools=tools,
            tool_choice=_tool_choice_for(),
            thinking=_THINKING_CONFIG,
            messages=self._messages,
        )
        offered_tool_names = frozenset(cast(str, tool["name"]) for tool in tools)
        try:
            action, tool_use_id = _parse_action(
                response.content, offered_tool_names=offered_tool_names
            )
        except AnthropicAnswerError as error:
            # Append diagnostic context the strict parser itself cannot see:
            # the response's stop reason and the type of every content
            # block it received. Without this, a response the parser
            # cannot use (for example, a `thinking` block returned in
            # place of the required tool call) is indistinguishable from a
            # well-formed but contract-violating one; both raised only the
            # parser's own generic message.
            block_types = [
                getattr(block, "type", type(block).__name__)
                for block in response.content
            ]
            raise AnthropicAnswerError(
                f"{error} (stop_reason={response.stop_reason!r}, "
                f"content_block_types={block_types!r})"
            ) from error
        self._messages.append({"role": "assistant", "content": list(response.content)})
        self._pending_tool_use_id = tool_use_id
        return ModelTurn(
            action=action,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class _RealMessagesNamespace:
    """Adapts the real SDK's overloaded `Messages.create` (its streaming and
    non-streaming variants) to the single, concrete, non-overloaded
    signature `_MessagesNamespace` declares. Structural Protocol matching
    against an overloaded method is unreliable in mypy; a plain wrapper
    method sidesteps that entirely, since this call always resolves to the
    SDK's non-streaming overload."""

    def __init__(self, client: _AnthropicClient) -> None:
        self._client = client

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: Any,
        tool_choice: Any,
        thinking: Any,
        messages: Any,
    ) -> _MessagesResponse:
        return self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            thinking=thinking,
            messages=messages,
        )


class _RealAnthropicClient:
    """The only place `anthropic.Anthropic` is constructed; satisfies
    `AnthropicMessagesClient` through `_RealMessagesNamespace` above."""

    def __init__(self, *, api_key: str) -> None:
        self._client = _AnthropicClient(api_key=api_key)
        self.messages = _RealMessagesNamespace(self._client)


def build_anthropic_answering_adapter(
    *, api_key: str, model: str
) -> AnthropicAnsweringAdapter:
    """Construct an `AnthropicAnsweringAdapter` backed by the real Anthropic
    SDK. `model` is the maintainer-supplied Claude model identifier — this
    adapter never selects or hard-codes one."""
    return AnthropicAnsweringAdapter(
        client=_RealAnthropicClient(api_key=api_key), model_id=model
    )
