"""Tests for the Voyage embeddings adapter: document-versus-query input-type
selection with the fixed model and no-truncation guarantees, deterministic
batching and order preservation with accumulated token usage, and one
representative malformed-response failure. No network call is made; every
test substitutes a fake client satisfying `VoyageEmbeddingClient`.
"""

from dataclasses import dataclass, field

import pytest

from reporationale.adapters.voyage_embeddings import (
    VOYAGE_EMBEDDING_MODEL,
    VoyageEmbeddingAdapter,
    VoyageEmbeddingError,
)


@dataclass
class _FakeResponse:
    embeddings: list[list[float]] | list[list[int]]
    total_tokens: int


@dataclass
class _RecordedCall:
    texts: list[str]
    model: str | None
    input_type: str | None
    truncation: bool


@dataclass
class _RecordingVoyageClient:
    """Records every call it receives and returns one deterministic vector
    per input text, so batching, order, and per-call arguments can be
    checked without a network call."""

    calls: list[_RecordedCall] = field(default_factory=list)

    def embed(
        self,
        texts: list[str],
        model: str | None = None,
        input_type: str | None = None,
        truncation: bool = True,
    ) -> _FakeResponse:
        self.calls.append(
            _RecordedCall(
                texts=list(texts),
                model=model,
                input_type=input_type,
                truncation=truncation,
            )
        )
        vectors = [[float(len(text)), float(index)] for index, text in enumerate(texts)]
        return _FakeResponse(embeddings=vectors, total_tokens=len(texts))


def test_embed_documents_and_embed_query_use_expected_request_shape() -> None:
    client = _RecordingVoyageClient()
    adapter = VoyageEmbeddingAdapter(client)
    texts = [f"chunk-{i}" for i in range(130)]  # spans two batches of 128 + 2

    document_batch = adapter.embed_documents(texts)
    query_batch = adapter.embed_query("why was polling chosen?")

    assert len(client.calls) == 3
    document_calls = client.calls[:2]
    query_call = client.calls[2]

    assert [len(call.texts) for call in document_calls] == [128, 2]
    assert document_calls[0].texts + document_calls[1].texts == texts
    for call in document_calls:
        assert call.model == VOYAGE_EMBEDDING_MODEL == "voyage-4"
        assert call.input_type == "document"
        assert call.truncation is False

    assert query_call.texts == ["why was polling chosen?"]
    assert query_call.model == VOYAGE_EMBEDDING_MODEL
    assert query_call.input_type == "query"
    assert query_call.truncation is False

    assert len(document_batch.vectors) == len(texts)
    assert [vector[0] for vector in document_batch.vectors] == [
        float(len(text)) for text in texts
    ]
    assert document_batch.total_tokens == 128 + 2
    assert len(query_batch.vectors) == 1


def test_embed_documents_rejects_a_vector_count_mismatch() -> None:
    class _BadClient:
        def embed(
            self,
            texts: list[str],
            model: str | None = None,
            input_type: str | None = None,
            truncation: bool = True,
        ) -> _FakeResponse:
            # Two inputs requested, only one embedding returned.
            return _FakeResponse(embeddings=[[0.1, 0.2]], total_tokens=1)

    adapter = VoyageEmbeddingAdapter(_BadClient())

    with pytest.raises(VoyageEmbeddingError):
        adapter.embed_documents(["a", "b"])
