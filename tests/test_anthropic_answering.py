"""Offline contract tests for the Anthropic answering adapter."""

import json
from dataclasses import dataclass, field

import pytest

from reporationale.adapters.anthropic_answering import (
    _PROVIDE_ANSWER_TOOL,
    _REFINE_SEARCH_TOOL,
    _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    AnthropicAnswerError,
    AnthropicAnsweringAdapter,
    _conversation_context_block,
)
from reporationale.domain.answering import (
    ConversationContext,
    ConversationTurn,
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


def _tool_response(name: str, tool_input: dict[str, object]) -> _FakeResponse:
    return _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use", id=f"tool-{name}", name=name, input=tool_input
            )
        ],
        usage=_FakeUsage(input_tokens=10, output_tokens=5),
    )


def _answer_response() -> _FakeResponse:
    return _tool_response(
        "provide_answer",
        {
            "answer": "Polling was chosen because webhooks were unreliable. [1]",
            "citations": [{"evidence_id": _EVIDENCE_ID}],
        },
    )


def _start(  # type: ignore[no-untyped-def]
    adapter: AnthropicAnsweringAdapter,
    *,
    search_available: bool = True,
    context: ConversationContext | None = None,
):
    return adapter.start(
        question="Why was polling chosen?",
        evidence=(_evidence(),),
        search_available=search_available,
        context=context,
    )


def test_start_uses_initial_evidence_and_offers_only_post_search_actions() -> None:
    client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    turn = _start(adapter)

    assert isinstance(turn.action, FinalAnswer)
    assert turn.input_tokens == 10
    assert turn.output_tokens == 5
    call = client.messages.calls[0]
    assert call.model == "claude-test-model"
    assert call.tools == [
        _REFINE_SEARCH_TOOL,
        _PROVIDE_ANSWER_TOOL,
        _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    ]
    assert call.tool_choice == {"type": "any", "disable_parallel_tool_use": True}
    assert call.thinking == {"type": "disabled"}
    assert len(call.messages) == 1
    content = call.messages[0]["content"]
    assert isinstance(content, str)
    assert content.startswith("Question:\nWhy was polling chosen?")
    context = json.loads(content.split("Initial search results (JSON):\n", 1)[1])
    assert context[0]["evidence_id"] == _EVIDENCE_ID
    assert context[0]["title"] == "Polling rationale"


def test_refinement_then_answer_sends_tool_result_evidence() -> None:
    client = _FakeAnthropicClient(
        _FakeMessagesNamespace(
            [
                _tool_response(
                    "refine_search",
                    {
                        "query": "polling webhook reliability rationale",
                        "missing_information": "the rejected alternative",
                    },
                ),
                _answer_response(),
            ]
        )
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    first = _start(adapter)
    assert isinstance(first.action, SearchRequested)
    assert first.action.query == "polling webhook reliability rationale"
    assert first.action.missing_information == "the rejected alternative"

    final = adapter.submit_evidence(evidence=(_evidence(),), search_available=False)
    assert isinstance(final.action, FinalAnswer)
    second_call = client.messages.calls[1]
    tool_result = second_call.messages[-1]["content"][0]  # type: ignore[index]
    assert tool_result["tool_use_id"] == "tool-refine_search"
    assert json.loads(tool_result["content"])[0]["evidence_id"] == _EVIDENCE_ID


def test_no_search_available_offers_only_terminal_tools() -> None:
    response = _tool_response(
        "report_insufficient_evidence", {"explanation": "Not documented."}
    )
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    turn = _start(adapter, search_available=False)

    assert isinstance(turn.action, FinalInsufficientEvidence)
    assert client.messages.calls[0].tools == [
        _PROVIDE_ANSWER_TOOL,
        _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            _tool_response("refine_search", {"query": "x"}),
            "non-blank string 'missing_information'",
        ),
        (
            _tool_response(
                "refine_search", {"query": " ", "missing_information": "missing"}
            ),
            "non-blank string 'query'",
        ),
        (
            _tool_response(
                "provide_answer",
                {"answer": " ", "citations": [{"evidence_id": _EVIDENCE_ID}]},
            ),
            "non-blank 'answer'",
        ),
        (
            _tool_response(
                "provide_answer", {"answer": "Unsupported.", "citations": []}
            ),
            "at least one citation",
        ),
        (
            _tool_response("report_insufficient_evidence", {"explanation": " "}),
            "non-blank 'explanation'",
        ),
    ],
)
def test_malformed_action_fields_are_rejected(
    response: _FakeResponse, message: str
) -> None:
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    with pytest.raises(AnthropicAnswerError, match=message):
        _start(adapter)


def test_a_tool_not_offered_this_turn_is_rejected() -> None:
    response = _tool_response(
        "refine_search", {"query": "x", "missing_information": "missing"}
    )
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    with pytest.raises(AnthropicAnswerError, match="not offered this turn"):
        _start(adapter, search_available=False)


def test_more_than_one_tool_call_is_rejected() -> None:
    response = _FakeResponse(
        content=[
            _FakeToolUseBlock("tool_use", "one", "provide_answer", {}),
            _FakeToolUseBlock("tool_use", "two", "provide_answer", {}),
        ],
        usage=_FakeUsage(10, 5),
    )
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    with pytest.raises(AnthropicAnswerError, match="expected exactly one tool call"):
        _start(adapter)


def test_unknown_tool_name_is_rejected() -> None:
    response = _tool_response("delete_repository", {})
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    with pytest.raises(AnthropicAnswerError, match="not offered this turn"):
        _start(adapter)


def test_parse_failure_includes_provider_diagnostics() -> None:
    response = _FakeResponse(
        content=[_FakeThinkingBlock(type="thinking", thinking="...")],
        usage=_FakeUsage(10, 2048),
        stop_reason="max_tokens",
    )
    client = _FakeAnthropicClient(_FakeMessagesNamespace([response]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    with pytest.raises(AnthropicAnswerError, match="got 0") as exc_info:
        _start(adapter)
    assert "stop_reason='max_tokens'" in str(exc_info.value)
    assert "'thinking'" in str(exc_info.value)


_ALL_TOOLS = (
    _REFINE_SEARCH_TOOL,
    _PROVIDE_ANSWER_TOOL,
    _REPORT_INSUFFICIENT_EVIDENCE_TOOL,
)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_all_three_tools_are_strict_without_unsupported_min_length() -> None:
    assert [tool["name"] for tool in _ALL_TOOLS] == [
        "refine_search",
        "provide_answer",
        "report_insufficient_evidence",
    ]
    for tool in _ALL_TOOLS:
        assert tool["strict"] is True
        assert not _contains_key(tool["input_schema"], "minLength")


def test_search_budget_guidance_matches_available_tools() -> None:
    available_client = _FakeAnthropicClient(
        _FakeMessagesNamespace([_answer_response()])
    )
    available = AnthropicAnsweringAdapter(
        client=available_client, model_id="claude-test-model"
    )
    _start(available, search_available=True)
    assert "You may still search again" in available_client.messages.calls[0].system
    assert _REFINE_SEARCH_TOOL in available_client.messages.calls[0].tools

    exhausted_client = _FakeAnthropicClient(
        _FakeMessagesNamespace([_answer_response()])
    )
    exhausted = AnthropicAnsweringAdapter(
        client=exhausted_client, model_id="claude-test-model"
    )
    _start(exhausted, search_available=False)
    call = exhausted_client.messages.calls[0]
    assert "All 3 permitted searches have already been used" in call.system
    assert _REFINE_SEARCH_TOOL not in call.tools


def test_common_prompt_describes_deterministic_first_search_and_grounding_rules() -> (
    None
):
    client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    _start(adapter)

    system = client.messages.calls[0].system
    assert "already searched" in system
    assert "user's exact question" in system
    assert "must never be presented as a documented" in system
    assert "same epistemic strength the source itself expresses" in system
    assert "call report_insufficient_evidence rather than provide_answer" in system
    assert "even if you can explain what the evidence actually shows" in system
    assert "must correspond exactly and only to the distinct evidence_ids" in system
    assert "never use a number outside that range" in system


def test_system_prompt_marks_prior_conversation_as_non_evidentiary() -> None:
    client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    _start(adapter)

    system = client.messages.calls[0].system
    assert "not repository evidence" in system
    assert "never cite them" in system
    assert "never restate a prior answer's claim" in system
    assert "You may cite only evidence returned by a search executed" in system
    assert "resolve references, ellipsis, or the topic" in system


def _context_with_one_prior_turn() -> ConversationContext:
    return ConversationContext(
        turns=(
            ConversationTurn(
                question="Why was polling chosen instead of webhooks?",
                outcome="answered",
                response="The history documents unreliable webhook delivery.",
            ),
        )
    )


def test_start_serializes_prior_context_question_and_evidence_distinctly() -> None:
    client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")
    context = _context_with_one_prior_turn()

    _start(adapter, context=context)

    content = client.messages.calls[0].messages[0]["content"]
    assert isinstance(content, str)
    expected_context_block = _conversation_context_block(context)
    assert expected_context_block
    # Three distinct, ordered sections in one message: prior context first,
    # then the current question, then this run's initial evidence.
    assert content.startswith(expected_context_block)
    rest = content[len(expected_context_block) :]
    assert rest.startswith("Question:\nWhy was polling chosen?")
    parsed = json.loads(expected_context_block.split(":\n", 1)[1].rsplit("\n\n", 1)[0])
    assert parsed == [
        {
            "question": "Why was polling chosen instead of webhooks?",
            "outcome": "answered",
            "response": "The history documents unreliable webhook delivery.",
        }
    ]


def test_context_serialization_excludes_citations_evidence_and_traces() -> None:
    client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    _start(adapter, context=_context_with_one_prior_turn())

    content = client.messages.calls[0].messages[0]["content"]
    assert isinstance(content, str)
    context_section = content.split("Question:\n", 1)[0]
    for excluded in (
        "evidence_id",
        "excerpt",
        "citations",
        "source_url",
        "latency",
        "input_tokens",
        "output_tokens",
        "model_id",
    ):
        assert excluded not in context_section


def test_omitted_and_empty_context_produce_identical_first_message() -> None:
    omitted_client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    omitted_adapter = AnthropicAnsweringAdapter(
        client=omitted_client, model_id="claude-test-model"
    )
    _start(omitted_adapter)

    empty_client = _FakeAnthropicClient(_FakeMessagesNamespace([_answer_response()]))
    empty_adapter = AnthropicAnsweringAdapter(
        client=empty_client, model_id="claude-test-model"
    )
    _start(empty_adapter, context=ConversationContext())

    omitted_content = omitted_client.messages.calls[0].messages[0]["content"]
    empty_content = empty_client.messages.calls[0].messages[0]["content"]
    assert omitted_content == empty_content
    assert isinstance(omitted_content, str)
    assert "Prior conversation context" not in omitted_content
    assert omitted_content.startswith("Question:\nWhy was polling chosen?")
