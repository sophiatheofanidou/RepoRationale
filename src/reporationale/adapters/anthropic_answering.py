"""Project-owned boundary around the Anthropic Messages API for the bounded
rationale-answering agent loop.

Isolates the `anthropic` SDK's client, request shape, and response content
blocks behind this module, the same way `adapters.voyage_embeddings`
isolates the Voyage SDK: nothing outside this module ever imports
`anthropic` or sees an Anthropic request or response object. Callers depend
only on `AnthropicAnsweringAdapter` (satisfying the application layer's
`AnsweringModel` Protocol), the provider-neutral `ModelTurn`/`ProviderAction`
domain shapes it returns, and `AnthropicAnswerError`.

`search_history` is exposed as the model's only tool, using exactly the
fixed query-only schema the project has settled on; there is no
repository, source, author, date, state, item-ID, backend, filter, or
result-count parameter. This adapter translates the normal Anthropic
`tool_use`/`tool_result` conversation flow to and from the project's own
typed actions and rejects malformed, multiple, unknown, or contradictory
tool/action responses with a clear `AnthropicAnswerError` rather than
guessing what the provider meant.
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

# The exact, project-owned tool contract exposed to the answering model.
# No repository, source, author, date, state, item-ID, backend, filter, or
# result-count parameter is permitted here.
_SEARCH_HISTORY_TOOL: dict[str, object] = {
    "name": "search_history",
    "description": (
        "Search the active repository-history index for evidence relevant "
        "to the user's rationale question."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "A focused natural-language search query.",
            }
        },
        "required": ["query"],
    },
}

_SYSTEM_PROMPT = (
    "You help recover documented rationale from a repository's indexed "
    "history. search_history is your only source of evidence; never answer "
    "from general knowledge instead of retrieved evidence.\n\n"
    "Your first response must call search_history with a focused query, and "
    "nothing else.\n\n"
    "After you receive search results, respond in exactly one of these ways:\n"
    "1. To request another search because the evidence is insufficient, call "
    "search_history again and, in the same turn, include a text block "
    'containing only this JSON object: {"missing_information": "<what is '
    'still missing>"}.\n'
    "2. To give a final answer, respond with a text block containing only "
    'this JSON object: {"status": "answered", "answer": "<answer text with '
    '[1], [2], ... citation markers>", "citations": [{"evidence_id": "<id>"}, '
    "...]}. Cite evidence only by the evidence_id values you were given; "
    "never invent one, and never restate its source metadata or excerpt "
    "yourself.\n"
    "3. To abstain because the retrieved evidence does not document an "
    "answer, respond with a text block containing only this JSON object: "
    '{"status": "insufficient_evidence", "explanation": "<concise reason>"}.\n\n'
    "Never emit any text outside that single JSON object when giving a "
    "final answer or abstaining, and never call any tool other than "
    "search_history."
)

_MAX_TOKENS = 2048


class AnthropicAnswerError(Exception):
    """The Anthropic API returned a response this adapter could not
    translate into one well-formed `ProviderAction`: more than one tool
    call in a turn, an unknown tool name, a missing or malformed
    `search_history` input, a missing or non-JSON final text block, or a
    final JSON object missing its required fields."""


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


class _MessagesNamespace(Protocol):
    """`tools` and `messages` are typed `Any` only because the real SDK's
    parameter types are precise `TypedDict` unions that a generic
    `dict[str, object]` cannot structurally satisfy; every dict this
    adapter actually builds and passes is concretely typed at its own call
    site (see `_SEARCH_HISTORY_TOOL` and the message-history assembly
    below)."""

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: Any,
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


class _TextBlock(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def text(self) -> str: ...


def _tool_use_blocks(content: Sequence[object]) -> list[_ToolUseBlock]:
    return [
        cast(_ToolUseBlock, block)
        for block in content
        if getattr(block, "type", None) == "tool_use"
    ]


def _text_blocks(content: Sequence[object]) -> list[_TextBlock]:
    return [
        cast(_TextBlock, block)
        for block in content
        if getattr(block, "type", None) == "text"
    ]


def _require_query_input(raw_input: object) -> str:
    if not isinstance(raw_input, dict):
        raise AnthropicAnswerError("search_history tool call input must be an object")
    query = raw_input.get("query")
    if not isinstance(query, str) or not query.strip():
        raise AnthropicAnswerError(
            "search_history tool call must include a non-blank string 'query'"
        )
    return query


def _require_single_json_block(text_blocks: list[_TextBlock]) -> dict[str, object]:
    if len(text_blocks) != 1:
        raise AnthropicAnswerError(
            f"expected exactly one text block, got {len(text_blocks)}"
        )
    try:
        payload = json.loads(text_blocks[0].text)
    except json.JSONDecodeError as error:
        raise AnthropicAnswerError(
            f"response text was not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise AnthropicAnswerError("response JSON must be an object")
    return payload


def _require_missing_information(text_blocks: list[_TextBlock]) -> str:
    payload = _require_single_json_block(text_blocks)
    missing = payload.get("missing_information")
    if not isinstance(missing, str) or not missing.strip():
        raise AnthropicAnswerError(
            "a refinement search must include text with a non-blank "
            "'missing_information'"
        )
    return missing


def _require_evidence_id(entry: object) -> str:
    if not isinstance(entry, dict) or not isinstance(entry.get("evidence_id"), str):
        raise AnthropicAnswerError(
            f"malformed citation entry, expected {{'evidence_id': str}}: {entry!r}"
        )
    return cast(str, entry["evidence_id"])


def _parse_final_answer(payload: dict[str, object]) -> FinalAnswer:
    answer = payload.get("answer")
    citations_raw = payload.get("citations")
    if not isinstance(answer, str) or not answer.strip():
        raise AnthropicAnswerError("an answered response needs a non-blank 'answer'")
    if not isinstance(citations_raw, list) or not citations_raw:
        raise AnthropicAnswerError("an answered response needs at least one citation")
    try:
        citations = tuple(
            CitedReference(evidence_id=_require_evidence_id(entry))
            for entry in citations_raw
        )
    except ValidationError as error:
        raise AnthropicAnswerError(f"malformed citation entry: {error}") from error
    return FinalAnswer(answer=answer, citations=citations)


def _parse_final_insufficient(payload: dict[str, object]) -> FinalInsufficientEvidence:
    explanation = payload.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        raise AnthropicAnswerError(
            "an insufficient_evidence response needs a non-blank 'explanation'"
        )
    return FinalInsufficientEvidence(explanation=explanation)


def _parse_action(
    content: Sequence[object], *, expect_first_action: bool
) -> tuple[ProviderAction, str | None]:
    tool_use_blocks = _tool_use_blocks(content)
    text_blocks = _text_blocks(content)

    if len(tool_use_blocks) > 1:
        raise AnthropicAnswerError(
            "the model produced more than one tool call in a single turn"
        )

    if tool_use_blocks:
        tool_use = tool_use_blocks[0]
        if tool_use.name != _SEARCH_HISTORY_TOOL["name"]:
            raise AnthropicAnswerError(
                f"the model called unknown tool {tool_use.name!r}"
            )
        query = _require_query_input(tool_use.input)
        missing_information: str | None = None
        if expect_first_action:
            if text_blocks:
                raise AnthropicAnswerError(
                    "the first action must call search_history with no other content"
                )
        else:
            missing_information = _require_missing_information(text_blocks)
        return (
            SearchRequested(query=query, missing_information=missing_information),
            tool_use.id,
        )

    if expect_first_action:
        raise AnthropicAnswerError("the model's first action must call search_history")

    payload = _require_single_json_block(text_blocks)
    status = payload.get("status")
    if status == "answered":
        return _parse_final_answer(payload), None
    if status == "insufficient_evidence":
        return _parse_final_insufficient(payload), None
    raise AnthropicAnswerError(f"unrecognized final response status {status!r}")


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
    instance: exposes the fixed `search_history` tool and system
    instruction, drives the Anthropic `tool_use`/`tool_result` conversation
    flow for exactly one bounded answering run, and translates every
    response into the provider-neutral `ModelTurn`/`ProviderAction` shapes
    the application workflow depends on.

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

    def start(self, *, question: str) -> ModelTurn:
        self._messages = [{"role": "user", "content": question}]
        return self._request(tools=[_SEARCH_HISTORY_TOOL], expect_first_action=True)

    def submit_evidence(
        self, *, evidence: tuple[RankedEvidence, ...], search_available: bool
    ) -> ModelTurn:
        if self._pending_tool_use_id is None:
            raise AnthropicAnswerError(
                "submit_evidence called with no pending search_history call "
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
        tools = [_SEARCH_HISTORY_TOOL] if search_available else []
        return self._request(tools=tools, expect_first_action=False)

    def _request(
        self, *, tools: list[dict[str, object]], expect_first_action: bool
    ) -> ModelTurn:
        response = self._client.messages.create(
            model=self._model_id,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=tools,
            messages=self._messages,
        )
        action, tool_use_id = _parse_action(
            response.content, expect_first_action=expect_first_action
        )
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
        messages: Any,
    ) -> _MessagesResponse:
        return self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
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
