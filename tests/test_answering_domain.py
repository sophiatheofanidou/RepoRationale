"""Tests for the session-scoped `ConversationContext`/`ConversationTurn`
contract in `reporationale.domain.answering`: strict validation of blank and
unknown fields, the fixed three-turn maximum, both `answered` and
`insufficient_evidence` outcome shapes, and the `ConversationTurn.from_outcome`
convenience constructor a future caller will use to build one from a
completed `AnsweringRunResult`.
"""

import pytest
from pydantic import ValidationError

from reporationale.domain.answering import (
    AnsweredOutcome,
    Citation,
    ConversationContext,
    ConversationTurn,
    InsufficientEvidenceOutcome,
)

_ANSWERED_TURN_KWARGS = {
    "question": "Why was polling chosen instead of webhooks?",
    "outcome": "answered",
    "response": "The history documents unreliable webhook delivery.",
}


def test_empty_context_is_the_default() -> None:
    assert ConversationContext().turns == ()


def test_context_accepts_up_to_three_turns() -> None:
    turns = tuple(
        ConversationTurn(
            question=f"Question {i}?", outcome="answered", response=f"Answer {i}."
        )
        for i in range(3)
    )
    context = ConversationContext(turns=turns)
    assert context.turns == turns


def test_context_rejects_a_fourth_turn() -> None:
    turns = tuple(
        ConversationTurn(
            question=f"Question {i}?", outcome="answered", response=f"Answer {i}."
        )
        for i in range(4)
    )
    with pytest.raises(ValidationError):
        ConversationContext(turns=turns)


def test_context_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ConversationContext.model_validate({"turns": (), "extra": "nope"})


@pytest.mark.parametrize(
    "kwargs",
    [
        {**_ANSWERED_TURN_KWARGS, "question": "   "},
        {**_ANSWERED_TURN_KWARGS, "response": ""},
    ],
)
def test_turn_rejects_blank_text_fields(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        ConversationTurn(**kwargs)


def test_turn_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ConversationTurn.model_validate({**_ANSWERED_TURN_KWARGS, "citations": []})


def test_turn_rejects_unknown_outcome_literal() -> None:
    with pytest.raises(ValidationError):
        ConversationTurn.model_validate({**_ANSWERED_TURN_KWARGS, "outcome": "partial"})


def test_turn_represents_answered_outcome() -> None:
    turn = ConversationTurn(**_ANSWERED_TURN_KWARGS)
    assert turn.outcome == "answered"
    assert turn.response == "The history documents unreliable webhook delivery."


def test_turn_represents_insufficient_evidence_outcome() -> None:
    turn = ConversationTurn(
        question="Was polling reconsidered?",
        outcome="insufficient_evidence",
        response="The repository does not document a reconsideration.",
    )
    assert turn.outcome == "insufficient_evidence"


def test_from_outcome_builds_answered_turn_from_answered_result() -> None:
    outcome = AnsweredOutcome(
        answer="Polling was chosen because webhooks were unreliable. [1]",
        citations=(
            Citation(
                number=1,
                evidence_id="github:octo-org/example-repo:markdown:a.md:chunk:0",
                source_id="github:octo-org/example-repo:markdown:a.md",
                source_title="Polling rationale",
                source_url="https://github.com/octo-org/example-repo/blob/main/a.md",
                excerpt="Polling was chosen because webhooks were unreliable.",
            ),
        ),
    )

    turn = ConversationTurn.from_outcome(
        question="Why was polling chosen instead of webhooks?", outcome=outcome
    )

    assert turn.outcome == "answered"
    assert turn.response == outcome.answer
    # Citation excerpts, IDs, and URLs must not leak into the conversation
    # context -- only the resolved answer text does.
    assert "evidence_id" not in turn.model_dump()
    assert "citations" not in turn.model_dump()


def test_from_outcome_builds_insufficient_evidence_turn() -> None:
    outcome = InsufficientEvidenceOutcome(
        explanation="The repository does not document this rationale."
    )

    turn = ConversationTurn.from_outcome(
        question="Was this ever tried?", outcome=outcome
    )

    assert turn.outcome == "insufficient_evidence"
    assert turn.response == outcome.explanation
