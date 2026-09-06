"""Provider-neutral embedding result.

`EmbeddingBatch` keeps every embedding-provider SDK type (Voyage's response
objects included) out of the application and domain layers: an adapter such
as `reporationale.adapters.voyage_embeddings` is the only place that ever
sees a raw provider response, and it returns this small immutable shape
instead. It says nothing about which provider, model, or input type produced
it — that context lives in the caller and, once persisted, in
`VectorIndexManifest` (see `reporationale.domain.snapshot`).
"""

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbeddingBatch(BaseModel):
    """One batch of embedding vectors and the total provider token usage
    that produced them, in input order.

    Frozen and independently validated: every vector must be non-empty,
    every vector in the batch must share the same dimension, and every
    component must be finite. An empty `vectors` tuple (no inputs embedded)
    is valid and trivially satisfies these checks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vectors: tuple[tuple[float, ...], ...]
    total_tokens: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def vectors_must_be_finite_and_equal_dimension(self) -> "EmbeddingBatch":
        if not self.vectors:
            return self
        dimension = len(self.vectors[0])
        for vector in self.vectors:
            if len(vector) != dimension:
                raise ValueError("every vector in a batch must share one dimension")
            if dimension == 0:
                raise ValueError("a vector must not be empty")
            if not all(math.isfinite(component) for component in vector):
                raise ValueError("every vector component must be a finite number")
        return self
