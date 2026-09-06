"""Shared ranked-evidence contract returned by any retrieval backend.

`RankedEvidence` is deliberately retriever-agnostic: the lexical BM25
baseline in `reporationale.application.lexical_retrieval` returns it today,
and a later vector retriever is expected to return the same shape so the
rest of the system (and its tests) can consume ranked evidence without
caring which backend produced it. It does not define a closed set of
retriever types or score kinds, and it does not claim that `score` values
from different retrievers share a scale or are directly comparable.
"""

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reporationale.domain.chunk import SourceChunk


class RankedEvidence(BaseModel):
    """One ranked retrieval result: a chunk plus its rank and raw score
    within one search call.

    `evidence_id` is fixed to equal `chunk.chunk_id` and is independently
    re-checked against `chunk` on both direct construction and
    deserialization, so a result's identity can never disagree with the
    chunk it actually carries. Provenance is nested, not flattened: the
    complete originating `SourceChunk` is embedded rather than duplicating
    its fields onto this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    rank: int = Field(strict=True, gt=0)
    score: float
    score_kind: str = Field(min_length=1)
    chunk: SourceChunk

    @field_validator("score")
    @classmethod
    def score_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("score must be a finite number")
        return value

    @field_validator("score_kind")
    @classmethod
    def score_kind_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("score_kind must not be blank")
        return value

    @model_validator(mode="after")
    def evidence_id_must_match_chunk(self) -> "RankedEvidence":
        if self.evidence_id != self.chunk.chunk_id:
            raise ValueError("evidence_id must equal chunk.chunk_id")
        return self
