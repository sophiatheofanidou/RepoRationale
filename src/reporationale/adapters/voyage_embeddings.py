"""Project-owned boundary around the Voyage AI embeddings API.

Isolates the `voyageai` SDK's client, request shape, and response objects
behind this module, the same way `adapters.github` isolates GitHub's REST
API: nothing outside this module ever imports `voyageai` or sees a Voyage
response object. Callers depend only on `EmbeddingProvider` (a small
structural `Protocol`), the provider-neutral `EmbeddingBatch` domain model,
and `VoyageEmbeddingError`.
"""

from collections.abc import Sequence
from typing import Literal, Protocol

from voyageai.client import Client as _VoyageClient

from reporationale.domain.embedding import EmbeddingBatch

# The accepted fixed Voyage embedding model, used at its default embedding
# dimension. Not user-configurable.
VOYAGE_EMBEDDING_MODEL = "voyage-4"

# One conservative internal batch size for document-embedding requests.
# Preserves input order across batches; not a tuning knob exposed to callers.
_DOCUMENT_BATCH_SIZE = 128

_InputType = Literal["document", "query"]


class VoyageEmbeddingError(Exception):
    """The Voyage API returned a response this adapter could not trust: a
    vector count that does not match the request, an empty or
    inconsistent vector dimension, or a non-finite component."""


class _EmbeddingResponse(Protocol):
    """The exact `voyageai.Client.embed` response surface this adapter
    depends on (`voyageai.object.embeddings.EmbeddingsObject` satisfies
    this structurally); nothing else of the SDK's response shape leaks
    past `VoyageEmbeddingAdapter`."""

    embeddings: list[list[float]] | list[list[int]]
    total_tokens: int


class VoyageEmbeddingClient(Protocol):
    """The exact `voyageai.Client` surface this adapter depends on, so
    tests can substitute a fake without a network call or the real SDK."""

    def embed(
        self,
        texts: list[str],
        model: str | None = None,
        input_type: str | None = None,
        truncation: bool = True,
    ) -> _EmbeddingResponse: ...


class EmbeddingProvider(Protocol):
    """The narrow embedding boundary the application layer depends on:
    embed a batch of chunk texts as documents, or embed one query. Satisfied
    structurally by `VoyageEmbeddingAdapter` and by any fake test double —
    this is the smallest boundary needed for application tests, not a
    general provider registry or factory."""

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch: ...

    def embed_query(self, text: str) -> EmbeddingBatch: ...


def _validate_embedding_response(
    response: _EmbeddingResponse, *, expected_count: int
) -> list[list[float]] | list[list[int]]:
    vectors = response.embeddings
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise VoyageEmbeddingError(
            "Voyage returned a different number of embeddings than requested."
        )
    dimension: int | None = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise VoyageEmbeddingError("Voyage returned an empty embedding vector.")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise VoyageEmbeddingError(
                "Voyage returned embedding vectors of inconsistent dimension."
            )
        for component in vector:
            if not isinstance(component, int | float) or not _is_finite(component):
                raise VoyageEmbeddingError(
                    "Voyage returned a non-finite embedding component."
                )
    return vectors


def _is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


class VoyageEmbeddingAdapter:
    """Embeds chunk texts and search queries through the Voyage API,
    returning only the provider-neutral `EmbeddingBatch` shape.

    Every embedding call fixes `model=voyage-4` and `truncation=False`
    (content is never silently truncated), and uses `input_type="document"`
    for `embed_documents` and `input_type="query"` for `embed_query`.
    Document requests are split into batches of at most
    `_DOCUMENT_BATCH_SIZE` texts, preserving input order across batches, and
    the returned `total_tokens` accumulates across every batch actually
    sent. No network call is made unless `embed_documents`/`embed_query` is
    actually invoked.
    """

    def __init__(self, client: VoyageEmbeddingClient) -> None:
        self._client = client

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        return self._embed(texts, input_type="document")

    def embed_query(self, text: str) -> EmbeddingBatch:
        return self._embed([text], input_type="query")

    def _embed(self, texts: Sequence[str], *, input_type: _InputType) -> EmbeddingBatch:
        texts_list = list(texts)
        vectors: list[tuple[float, ...]] = []
        total_tokens = 0
        for start in range(0, len(texts_list), _DOCUMENT_BATCH_SIZE):
            batch = texts_list[start : start + _DOCUMENT_BATCH_SIZE]
            response = self._client.embed(
                batch,
                model=VOYAGE_EMBEDDING_MODEL,
                input_type=input_type,
                truncation=False,
            )
            batch_vectors = _validate_embedding_response(
                response, expected_count=len(batch)
            )
            vectors.extend(tuple(vector) for vector in batch_vectors)
            total_tokens += response.total_tokens
        return EmbeddingBatch(vectors=tuple(vectors), total_tokens=total_tokens)


def build_voyage_embedding_adapter(*, api_key: str) -> VoyageEmbeddingAdapter:
    """Construct a `VoyageEmbeddingAdapter` backed by the real Voyage SDK.
    The only place `voyageai.Client` is constructed; `api_key` is passed
    straight through to the SDK and never stored or logged by this module."""
    return VoyageEmbeddingAdapter(_VoyageClient(api_key=api_key))
