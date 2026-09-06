"""Tests for the Anthropic answering adapter: request/response translation
with a fake `messages.create` client — the maintainer-supplied model ID,
the exact query-only `search_history` tool schema, the tool-result evidence
context sent back to the model, structured action translation for both a
search request and a final answer, and reported token usage. No network
call is made; every test substitutes a fake client satisfying
`AnthropicMessagesClient`.
"""

import json
from dataclasses import dataclass, field

from reporationale.adapters.anthropic_answering import (
    _SEARCH_HISTORY_TOOL,
    AnthropicAnsweringAdapter,
)
from reporationale.domain.answering import FinalAnswer, SearchRequested
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
class _FakeTextBlock:
    type: str
    text: str


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    content: list[object]
    usage: _FakeUsage


@dataclass
class _RecordedCall:
    model: str
    max_tokens: int
    system: str
    tools: list[dict[str, object]]
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
        messages: list[dict[str, object]],
    ) -> _FakeResponse:
        self.calls.append(
            _RecordedCall(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=list(tools),
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


def test_adapter_translates_search_then_answer_with_expected_requests_and_usage() -> (
    None
):
    first_response = _FakeResponse(
        content=[
            _FakeToolUseBlock(
                type="tool_use",
                id="tool-1",
                name="search_history",
                input={"query": "why was polling chosen?"},
            )
        ],
        usage=_FakeUsage(input_tokens=100, output_tokens=20),
    )
    final_payload = json.dumps(
        {
            "status": "answered",
            "answer": "Polling was chosen because webhooks were unreliable. [1]",
            "citations": [{"evidence_id": _EVIDENCE_ID}],
        }
    )
    second_response = _FakeResponse(
        content=[_FakeTextBlock(type="text", text=final_payload)],
        usage=_FakeUsage(input_tokens=150, output_tokens=40),
    )
    client = _FakeAnthropicClient(
        messages=_FakeMessagesNamespace([first_response, second_response])
    )
    adapter = AnthropicAnsweringAdapter(client=client, model_id="claude-test-model")

    start_turn = adapter.start(question="Why was polling chosen?")

    assert isinstance(start_turn.action, SearchRequested)
    assert start_turn.action.query == "why was polling chosen?"
    assert start_turn.action.missing_information is None
    assert start_turn.input_tokens == 100
    assert start_turn.output_tokens == 20

    first_call = client.messages.calls[0]
    assert first_call.model == "claude-test-model"
    assert first_call.tools == [_SEARCH_HISTORY_TOOL]
    assert first_call.messages == [
        {"role": "user", "content": "Why was polling chosen?"}
    ]

    final_turn = adapter.submit_evidence(evidence=(_evidence(),), search_available=True)

    second_call = client.messages.calls[1]
    assert second_call.tools == [_SEARCH_HISTORY_TOOL]
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
