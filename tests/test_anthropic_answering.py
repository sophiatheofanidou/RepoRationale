"""Tests for the Anthropic answering adapter: request/response translation
with a fake `messages.create` client — the maintainer-supplied model ID,
the fixed tool schemas (`search_history`, `refine_search`, `provide_answer`,
`report_insufficient_evidence`), the forced `tool_choice` and disabled
`thinking` on every request, the tool-result evidence context sent back to
the model, structured action translation for a search request, a final
answer, and an abstention, and reported token usage. No network call is
made; every test substitutes a fake client satisfying
`AnthropicMessagesClient`.

This adapter previously asked the model to write its final answer and
refinement rationale as free-form JSON text, then (once that was replaced
by forced tool calls) as an optional `missing_information` field on a
single `search_history` tool. A live comparison across Haiku, Sonnet, and
Opus on 2026-09-06 found neither reliable: models returned a blank final
text block, omitted the required text block when refining, and later left
the optional `missing_information` field unset when refining. The
regression tests below reproduce the response shapes those designs could
not parse reliably; the current design forces every turn through exactly
one schema-validated tool call and splits the first search from a
refinement into two tools so a refinement's stated reason is a required
field, not a merely requested one.
"""

import json
from dataclasses import dataclass, field

import pytest

from reporationale.adapters.anthropic_answering import (
    _PROVIDE_ANSWER_TOOL,
    _REFINE_SEARCH_TOOL,
    _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    _SEARCH_HISTORY_TOOL,
    AnthropicAnswerError,
    AnthropicAnsweringAdapter,
)
from reporationale.domain.answering import (
    FinalAnswer,
    FinalInsufficientEvidence,
    SearchRequested,
)
from reporationale.domain.chunk import SourceChunk
from reporationale.domain.retrieval import RankedEvidence

_EVIDENCE_ID = "github:octo-org/example-repo:markdown:polling.md:chunk:0"


@dataclass
class _FakeToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, object]


@dataclass
class _FakeThinkingBlock:
    """A content block type this adapter never looks for — used to
    reproduce a response that carries no usable `tool_use` content at
    all, so the diagnostic annotation wrapping a parse failure can be
    tested without depending on any specific provider content type."""

    type: str
    thinking: str


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    content: list[object]
    usage: _FakeUsage
    stop_reason: str | None = "tool_use"


@dataclass
class _RecordedCall:
    model: str
    max_tokens: int
    system: str
    tools: list[dict[str, object]]
    tool_choice: object
    thinking: object
    messages: list[dict[str, object]]


class _FakeMessagesNamespace:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = iter(responses)
        self.calls: list[_RecordedCall] = []

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, object]],
        tool_choice: object,
        thinking: object,
        messages: list[dict[str, object]],
    ) -> _FakeResponse:
        self.calls.append(
            _RecordedCall(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=list(tools),
                tool_choice=tool_choice,
                thinking=thinking,
                messages=list(messages),
            )
        )
        return next(self._responses)


@dataclass
class _FakeAnthropicClient:
    messages: _FakeMessagesNamespace = field(
        default_factory=lambda: _FakeMessagesNamespace([])
    )


def _evidence() -> RankedEvidence:
    return RankedEvidence(
        evidence_id=_EVIDENCE_ID,
        rank=1,
        score=0.05,
        score_kind="cosine_distance",
        chunk=SourceChunk(
            chunk_id=_EVIDENCE_ID,
            source_id="github:octo-org/example-repo:markdown:polling.md",
            chunk_index=0,
            text="Polling was chosen because webhook delivery was unreliable.",
            platform="github",
            repository="octo-org/example-repo",
            source_type="markdown",
            source_url="https://github.com/octo-org/example-repo/blob/main/polling.md",
            title="Polling rationale",
        ),
    )


def _search_history_response(*, tool_use_id: str, query: str) -> _FakeResponse:
    return _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use",
                id=tool_use_id,
                name="search_history",
                input={"query": query},
            )
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )


def _refine_search_response(
    *, tool_use_id: str, query: str, missing_information: str | None = "still missing"
) -> _FakeResponse:
    tool_input: dict[str, object] = {"query": query}
    if missing_information is not None:
        tool_input["missing_information"] = missing_information
    return _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use", id=tool_use_id, name="refine_search", input=tool_input
            )
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )


def _answer_response(*, tool_use_id: str = "tool-answer") -> _FakeResponse:
    return _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use",
                id=tool_use_id,
                name="provide_answer",
                input={
                    "answer": "Polling was chosen because webhooks were unreliable. [1]",
                    "citations": [{"evidence_id": _EVIDENCE_ID}],
                },
            )
        ],
        usage=_FakeUsage(input_tokens=150, output_tokens=40),
    )


def test_adapter_translates_search_then_answer_with_expected_requests_and_usage() -> (
    None
):
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(
                    tool_use_id="tool-1", query="why was polling chosen?"
                ),
                _answer_response(),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    start_turn = adapter.start(question="Why was polling chosen?")

    assert isinstance(start_turn.action, SearchRequested)
    assert start_turn.action.query == "why was polling chosen?"
    assert start_turn.action.missing_information is None
    assert start_turn.input_tokens == 10
    assert start_turn.output_tokens == 5

    first_call = client.messages.calls[0]
    assert first_call.model == "claude-test-model"
    assert first_call.tools == [_SEARCH_HISTORY_TOOL]
    assert first_call.tool_choice == {
        "type": "tool",
        "name": "search_history",
        "disable_parallel_tool_use": True,
    }
    assert first_call.thinking == {"type": "disabled"}
    assert first_call.messages == [
        {"role": "user", "content": "Why was polling chosen?"}
    ]

    final_turn = adapter.submit_evidence(evidence=(_evidence(),), search_available=True)

    second_call = client.messages.calls[1]
    assert second_call.tools == [
        _REFINE_SEARCH_TOOL,
        _PROVIDE_ANSWER_TOOL,
        _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    ]
    assert second_call.tool_choice == {"type": "any", "disable_parallel_tool_use": True}
    assert second_call.thinking == {"type": "disabled"}
    assert second_call.messages[0] == {
        "role": "user",
        "content": "Why was polling chosen?",
    }
    assert second_call.messages[1]["role"] == "assistant"
    tool_result_message = second_call.messages[2]
    assert tool_result_message["role"] == "user"
    tool_result_block = tool_result_message["content"][0]  # type: ignore[index]
    assert tool_result_block["tool_use_id"] == "tool-1"
    context = json.loads(tool_result_block["content"])
    assert context[0]["evidence_id"] == _EVIDENCE_ID
    assert context[0]["source_url"] == (
        "https://github.com/octo-org/example-repo/blob/main/polling.md"
    )
    assert context[0]["title"] == "Polling rationale"
    assert (
        context[0]["text"]
        == "Polling was chosen because webhook delivery was unreliable."
    )

    assert isinstance(final_turn.action, FinalAnswer)
    assert final_turn.action.answer.startswith("Polling was chosen")
    assert [citation.evidence_id for citation in final_turn.action.citations] == [
        _EVIDENCE_ID
    ]
    assert final_turn.input_tokens == 150
    assert final_turn.output_tokens == 40


def test_refine_search_carries_query_and_missing_information() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="first query"),
                _refine_search_response(
                    tool_use_id="tool-2",
                    query="refined query",
                    missing_information="the decision comment was not retrieved",
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    refined_turn = adapter.submit_evidence(
        evidence=(_evidence(),), search_available=True
    )

    assert isinstance(refined_turn.action, SearchRequested)
    assert refined_turn.action.query == "refined query"
    assert (
        refined_turn.action.missing_information
        == "the decision comment was not retrieved"
    )


def test_submit_evidence_without_search_available_offers_only_terminal_tools() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _FakeResponse(
                    content=[
                        _FakeToolUseBlock(
                            type="tool_use",
                            id="tool-2",
                            name="report_insufficient_evidence",
                            input={"explanation": "Not documented."},
                        )
                    ],
                    usage=_FakeUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    final_turn = adapter.submit_evidence(
        evidence=(_evidence(),), search_available=False
    )

    final_call = client.messages.calls[1]
    assert final_call.tools == [
        _PROVIDE_ANSWER_TOOL,
        _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    ]
    assert final_call.tool_choice == {"type": "any", "disable_parallel_tool_use": True}
    assert final_call.thinking == {"type": "disabled"}
    assert isinstance(final_turn.action, FinalInsufficientEvidence)
    assert final_turn.action.explanation == "Not documented."


def test_refine_search_without_missing_information_is_rejected() -> None:
    """Reproduces the exact live failure this tool split was introduced to
    close structurally: a refinement made with no stated reason. Unlike
    the earlier single-tool design, `missing_information` is now a
    required field on `refine_search`'s own schema rather than an
    application-level check on an optional one."""
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="first query"),
                _refine_search_response(
                    tool_use_id="tool-2",
                    query="refined query",
                    missing_information=None,
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(
        AnthropicAnswerError, match="non-blank string 'missing_information'"
    ):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=True)


def test_a_turn_rejects_a_tool_call_not_offered_this_turn() -> None:
    """Reproduces the exact live failure: after the search budget was
    exhausted (`search_available=False`, so only `provide_answer` and
    `report_insufficient_evidence` were offered), the model still named
    `refine_search` in its tool call — a tool this turn never declared.
    Previously the parser accepted any of the four globally known tool
    names regardless of what was actually offered, so this surfaced only
    later, confusingly, as the workflow's generic "budget exhausted"
    protocol error rather than a clear adapter-level rejection."""
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _refine_search_response(tool_use_id="tool-2", query="y"),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(AnthropicAnswerError, match="not offered this turn"):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=False)


def test_a_turn_rejects_more_than_one_tool_call() -> None:
    """Reproduces the exact live failure: the model made two `tool_use`
    calls (parallel tool use) in a single turn, even though every turn's
    `tool_choice` requests `disable_parallel_tool_use`."""
    response = _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use",
                id="tool-1",
                name="search_history",
                input={"query": "x"},
            ),
            _FakeToolUseBlock(
                type="tool_use",
                id="tool-2",
                name="search_history",
                input={"query": "y"},
            ),
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )
    client = _FakeAnthropicClient(messages=_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    with pytest.raises(AnthropicAnswerError, match="expected exactly one tool call"):
        adapter.start(question="Why?")


def test_unknown_tool_name_is_rejected() -> None:
    """A name that is not one of the four known tools at all — distinct
    from `test_a_turn_rejects_a_tool_call_not_offered_this_turn`, which
    covers a known tool simply not offered this particular turn. Both are
    caught by the same offered-tool-names check; only the resulting
    message differs, as a genuinely unknown name never reaches the
    per-tool dispatch below it."""
    response = _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use", id="tool-1", name="delete_repository", input={}
            )
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )
    client = _FakeAnthropicClient(messages=_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    with pytest.raises(
        AnthropicAnswerError,
        match="tool 'delete_repository', which was not offered this turn",
    ):
        adapter.start(question="Why?")


def test_provide_answer_without_citations_is_rejected() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _FakeResponse(
                    content=[
                        _FakeToolUseBlock(
                            type="tool_use",
                            id="tool-2",
                            name="provide_answer",
                            input={
                                "answer": "An answer with no evidence.",
                                "citations": [],
                            },
                        )
                    ],
                    usage=_FakeUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(AnthropicAnswerError, match="at least one citation"):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=True)


def test_parse_failure_is_annotated_with_stop_reason_and_content_block_types() -> None:
    """Reproduces the exact live failure shape: a turn whose content held
    no `tool_use` block at all — for example, only a `thinking` block —
    so the strict parser's own message alone gave no way to tell that
    apart from a genuine contract violation. The wrapped message must
    surface the response's `stop_reason` and every content block type it
    received."""
    unusable_response = _FakeResponse(
        content=[_FakeThinkingBlock(type="thinking", thinking="...")],
        usage=_FakeUsage(input_tokens=10, output_tokens=2048),
        stop_reason="max_tokens",
    )
    client = _FakeAnthropicClient(messages=_FakeMessagesNamespace([unusable_response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    with pytest.raises(AnthropicAnswerError, match="got 0") as exc_info:
        adapter.start(question="Why?")

    message = str(exc_info.value)
    assert "stop_reason='max_tokens'" in message
    assert "'thinking'" in message


# --- Strict tool use ---------------------------------------------------
#
# Anthropic's `strict: true` on a tool guarantees schema validation of
# tool names and inputs. Real strict-schema support does not honor every
# JSON Schema keyword; `minLength` in particular is not a supported
# strict-schema keyword, so every remaining string-blankness check is the
# application-side `.strip()` validation already exercised throughout
# this file above, not the JSON schema. These tests confirm both halves
# of that contract: every tool declares `strict: true`, none of their
# schemas still declares `minLength`, and the application-side checks
# still catch a blank value a stricter schema alone no longer would.

_ALL_TOOLS = (
    _SEARCH_HISTORY_TOOL,
    _REFINE_SEARCH_TOOL,
    _PROVIDE_ANSWER_TOOL,
    _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_every_tool_declares_strict_true() -> None:
    for tool in _ALL_TOOLS:
        assert tool["strict"] is True, tool["name"]


def test_no_tool_schema_declares_min_length() -> None:
    for tool in _ALL_TOOLS:
        assert not _contains_key(tool["input_schema"], "minLength"), tool["name"]


def test_blank_query_is_rejected_by_application_check_not_schema() -> None:
    response = _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use",
                id="tool-1",
                name="search_history",
                input={"query": "   "},
            )
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )
    client = _FakeAnthropicClient(messages=_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    with pytest.raises(AnthropicAnswerError, match="non-blank string 'query'"):
        adapter.start(question="Why?")


def test_blank_missing_information_string_is_rejected_by_application_check() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="first query"),
                _refine_search_response(
                    tool_use_id="tool-2", query="refined", missing_information="   "
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(
        AnthropicAnswerError, match="non-blank string 'missing_information'"
    ):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=True)


def test_blank_answer_is_rejected_by_application_check() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _FakeResponse(
                    content=[
                        _FakeToolUseBlock(
                            type="tool_use",
                            id="tool-2",
                            name="provide_answer",
                            input={
                                "answer": "   ",
                                "citations": [{"evidence_id": _EVIDENCE_ID}],
                            },
                        )
                    ],
                    usage=_FakeUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(AnthropicAnswerError, match="non-blank 'answer'"):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=True)


def test_blank_explanation_is_rejected_by_application_check() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _FakeResponse(
                    content=[
                        _FakeToolUseBlock(
                            type="tool_use",
                            id="tool-2",
                            name="report_insufficient_evidence",
                            input={"explanation": "   "},
                        )
                    ],
                    usage=_FakeUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    with pytest.raises(AnthropicAnswerError, match="non-blank 'explanation'"):
        adapter.submit_evidence(evidence=(_evidence(),), search_available=True)


# --- State-aware search-budget guidance ---------------------------------


def test_search_exhausted_guidance_is_stated_and_refine_search_is_withheld() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _FakeResponse(
                    content=[
                        _FakeToolUseBlock(
                            type="tool_use",
                            id="tool-2",
                            name="report_insufficient_evidence",
                            input={"explanation": "Not documented."},
                        )
                    ],
                    usage=_FakeUsage(input_tokens=10, output_tokens=5),
                ),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    adapter.submit_evidence(evidence=(_evidence(),), search_available=False)

    final_call = client.messages.calls[1]
    assert "All 3 permitted searches have already been used" in final_call.system
    assert "cannot search or refine again" in final_call.system
    assert _REFINE_SEARCH_TOOL not in final_call.tools
    assert all(tool["name"] != "refine_search" for tool in final_call.tools)


def test_search_available_guidance_is_stated_without_false_exhaustion_claim() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [
                _search_history_response(tool_use_id="tool-1", query="x"),
                _answer_response(),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")
    adapter.submit_evidence(evidence=(_evidence(),), search_available=True)

    second_call = client.messages.calls[1]
    assert "You may still search again" in second_call.system
    assert "All 3 permitted searches have already been used" not in second_call.system
    assert _REFINE_SEARCH_TOOL in second_call.tools


def test_citation_marker_discipline_instruction_is_in_the_common_prompt() -> None:
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace(
            [_search_history_response(tool_use_id="tool-1", query="x")]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    adapter.start(question="Why?")

    first_call = client.messages.calls[0]
    assert (
        "must correspond exactly and only to the distinct evidence_ids"
        in first_call.system
    )
    assert "never use a number outside that range" in first_call.system
    assert "Never call provide_answer with an empty citations list" in first_call.system
