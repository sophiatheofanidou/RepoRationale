"""Tests for the Voyage embeddings adapter: document-versus-query input-type
selection with the fixed model and no-truncation guarantees, deterministic
batching and order preservation with accumulated token usage, and one
representative malformed-response failure. No network call is made; every
test substitutes a fake client satisfying `VoyageEmbeddingClient`.
"""

from dataclasses import dataclass, field
from threading import Lock
from time import sleep

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
    _lock: Lock = field(default_factory=Lock, repr=False)

    def embed(
        self,
        texts: list[str],
        model: str | None = None,
        input_type: str | None = None,
        truncation: bool = True,
    ) -> _FakeResponse:
        with self._lock:
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
    document_calls = sorted(
        (call for call in client.calls if call.input_type == "document"),
        key=lambda call: texts.index(call.texts[0]),
    )
    query_call = next(call for call in client.calls if call.input_type == "query")

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


def test_document_batches_run_with_bounded_concurrency_and_keep_input_order() -> None:
    class _ConcurrentClient:
        def __init__(self) -> None:
            self._lock = Lock()
            self.active = 0
            self.maximum_active = 0
            self.started_batches: list[int] = []

        def embed(
            self,
            texts: list[str],
            model: str | None = None,
            input_type: str | None = None,
            truncation: bool = True,
        ) -> _FakeResponse:
            batch_number = int(texts[0].removeprefix("chunk-")) // 128
            with self._lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                self.started_batches.append(batch_number)
            try:
                # Later batches finish first, proving result order is independent
                # of request completion order.
                sleep(0.01 * (4 - batch_number))
                vectors = [[float(int(text.removeprefix("chunk-")))] for text in texts]
                return _FakeResponse(
                    embeddings=vectors,
                    total_tokens=(batch_number + 1) * len(texts),
                )
            finally:
                with self._lock:
                    self.active -= 1

    client = _ConcurrentClient()
    texts = [f"chunk-{index}" for index in range(640)]

    result = VoyageEmbeddingAdapter(client).embed_documents(texts)

    assert client.maximum_active == 4
    assert sorted(client.started_batches) == [0, 1, 2, 3, 4]
    assert [vector[0] for vector in result.vectors] == [float(i) for i in range(640)]
    assert result.total_tokens == 128 * sum(range(1, 6))


def test_document_batch_failure_is_raised_in_input_order_and_stops_submission() -> None:
    class _FailingClient:
        def __init__(self) -> None:
            self._lock = Lock()
            self.started_batches: list[int] = []

        def embed(
            self,
            texts: list[str],
            model: str | None = None,
            input_type: str | None = None,
            truncation: bool = True,
        ) -> _FakeResponse:
            batch_number = int(texts[0].removeprefix("chunk-")) // 128
            with self._lock:
                self.started_batches.append(batch_number)
            if batch_number == 0:
                sleep(0.03)
                raise RuntimeError("first input batch failed")
            if batch_number == 1:
                raise RuntimeError("later input batch failed sooner")
            sleep(0.01)
            return _FakeResponse(
                embeddings=[[float(batch_number)] for _text in texts],
                total_tokens=len(texts),
            )

    client = _FailingClient()
    texts = [f"chunk-{index}" for index in range(640)]

    with pytest.raises(RuntimeError, match="first input batch failed"):
        VoyageEmbeddingAdapter(client).embed_documents(texts)

    # Only the fixed initial window was submitted before the earliest input
    # batch failed; the fifth batch was never sent.
    assert sorted(client.started_batches) == [0, 1, 2, 3]
